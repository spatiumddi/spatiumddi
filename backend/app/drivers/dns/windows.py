"""Windows DNS driver (RFC 2136 + optional WinRM / PowerShell).

Two coexisting capability tiers, on the same driver class. Which one
applies at runtime depends on whether the server has credentials set:

**Path A — agentless, RFC 2136 (always available).**
Record CRUD via ``dnspython``; AXFR for record pulls. Used when no WinRM
credentials are configured. The Windows DNS admin is expected to create
zones in DNS Manager, enable "Nonsecure and secure" dynamic updates (or
front with a TSIG-aware forwarder), and allow zone transfers to the
SpatiumDDI host.

**Path B — agentless, WinRM + PowerShell (credentials required).**
Zone CRUD and server-level reads via ``pypsrp``/``pywinrm`` against the
``DnsServer`` PowerShell module. Enabled per-server by setting
``credentials_encrypted`` (the same Fernet-encoded dict shape used by
Windows DHCP). This unlocks ``pull_zones_from_server`` so zone topology
reconciles both directions — record CRUD still rides RFC 2136 so we
don't pay the PowerShell-per-record cost for hot writes.

Not yet (follow-up work):
  * GSS-TSIG (Kerberos-signed updates) — Windows' default for AD-integrated
    zones' "Secure only" setting. Today Path B is the mitigation: create
    / manage zones via WinRM while records use plain RFC 2136.
  * SIG(0) authentication.
  * Zone writes via WinRM (``Add-DnsServerPrimaryZone`` / ``Remove-Dns...``).
    Scaffolding is here; wire-up lives behind a future endpoint.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Sequence
from typing import Any

import structlog

from app.core.crypto import decrypt_dict
from app.drivers.dns.base import (
    ConfigBundle,
    DNSDriver,
    EffectiveBlocklistData,
    RecordChange,
    RecordChangeResult,
    RecordData,
    ServerOptions,
    ZoneData,
)

logger = structlog.get_logger(__name__)


# Record types this driver knows how to format for an RFC 2136 update.
_SUPPORTED_RECORD_TYPES = (
    "A",
    "AAAA",
    "CNAME",
    "MX",
    "TXT",
    "PTR",
    "SRV",
    "NS",
    "TLSA",
)

# Record types older ``DnsServer`` PowerShell modules don't expose a
# cmdlet for. These types always go over RFC 2136 even when WinRM
# credentials are configured — dispatching to Path B for them would
# raise ``ValueError`` at write time.
_WINRM_UNSUPPORTED_RECORD_TYPES = frozenset({"TLSA"})


# Upper bound on ops bundled into a single WinRM round-trip in the batch
# dispatcher (``apply_record_changes``). WinRM HTTP ``MaxEnvelopeSize``
# defaults to 500KB (500 000 bytes, half a megabyte-ish). At ~1KB per
# WinRM constraint: ``pywinrm.run_ps`` dispatches via ``powershell
# -encodedcommand <base64>`` as a single CMD.EXE command line, capped
# at 8191 chars by Windows. The minified wrapper below measures ~2000
# raw chars (all eight record types + op dispatch), which costs ~5350
# chars of cmdline budget before any ops. Each op adds ~160 raw chars
# (~430 cmdline chars) once JSON-escaped + UTF-16-LE + base64'd.
# Empirically **6 ops fit with realistic data**; 7 trips the CMD limit
# ("The command line is too long"). Measured via
# ``_ps_apply_record_batch(...)`` in the dev container.
#
# For 40 records that's 7 round trips — 6× faster than singular
# dispatch with minimal added complexity. **To go higher**, switch
# pywinrm for pypsrp: PSRP uses the WSMan Runspace protocol instead
# of CMD.EXE and removes the 8K ceiling entirely. Tracked as a
# follow-up (would yield ~100 ops/batch on the same envelope settings).
_WINRM_BATCH_SIZE = 6


def _format_rdata(r: RecordData) -> str:
    """Render ``RecordData`` into the wire-format string dnspython expects."""
    rtype = r.record_type.upper()
    if rtype == "MX":
        return f"{r.priority or 10} {r.value}"
    if rtype == "SRV":
        return f"{r.priority or 0} {r.weight or 0} {r.port or 0} {r.value}"
    if rtype == "TXT":
        s = r.value
        if s.startswith('"') and s.endswith('"') and len(s) >= 2:
            s = s[1:-1]
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        chunks = [s[i : i + 255] for i in range(0, len(s), 255)] or [""]
        return " ".join(f'"{c}"' for c in chunks)
    return r.value


class WindowsDNSDriver(DNSDriver):
    """Agentless RFC 2136 driver for Windows Server DNS.

    The rendering methods return empty strings: SpatiumDDI does not write
    ``named.conf``-style config for Windows DNS. Only ``apply_record_change``
    does real work. ``reload_*`` are no-ops — AD replication handles zone
    propagation across DCs.
    """

    name: str = "windows_dns"

    # ── Rendering (all no-ops; Windows manages its own zones) ─────────────

    def render_server_config(
        self, server: Any, options: ServerOptions, *, bundle: ConfigBundle | None = None
    ) -> str:
        return ""

    def render_zone_config(self, zone: ZoneData) -> str:
        return ""

    def render_zone_file(self, zone: ZoneData, records: list[RecordData]) -> str:
        return ""

    def render_rpz_zone(self, blocklist: EffectiveBlocklistData) -> str:
        return ""

    # ── Runtime — this is the only method that talks over the wire ──────

    async def apply_record_change(self, server: Any, change: RecordChange) -> None:
        """Push a record change to the Windows DC.

        Dispatches by credential state — same rule as ``pull_zone_records``:

        * **Credentials set (Path B)** — ``Add-DnsServerResourceRecord`` /
          ``Remove-DnsServerResourceRecord`` over WinRM. Works for
          Secure-only AD-integrated zones (which silently drop RFC 2136
          traffic) and sidesteps the "Nonsecure and secure" toggle
          entirely — the service account's DnsAdmins membership covers
          writes on any zone in the forest.
        * **No credentials (Path A)** — RFC 2136 dynamic update with
          optional TSIG signing. Requires the zone to accept nonsecure
          DDNS (or sign via a TSIG key shared with the server).
        """
        rtype = change.record.record_type.upper()
        if rtype not in _SUPPORTED_RECORD_TYPES:
            raise ValueError(
                f"WindowsDNSDriver does not support record type {rtype!r}; "
                f"supported: {_SUPPORTED_RECORD_TYPES}"
            )

        # Dispatch: WinRM (Path B) when credentials are configured AND the
        # record type has a DnsServer cmdlet. Otherwise RFC 2136 (Path A).
        use_winrm = (
            getattr(server, "credentials_encrypted", None)
            and rtype not in _WINRM_UNSUPPORTED_RECORD_TYPES
        )
        if use_winrm:
            await self._apply_record_change_winrm(server, change)
            return

        await self._apply_record_change_rfc2136(server, change)

    async def _apply_record_change_rfc2136(self, server: Any, change: RecordChange) -> None:
        import dns.message  # noqa: PLC0415
        import dns.name  # noqa: PLC0415
        import dns.query  # noqa: PLC0415
        import dns.rdatatype  # noqa: PLC0415
        import dns.tsigkeyring  # noqa: PLC0415
        import dns.update  # noqa: PLC0415

        host = getattr(server, "host", None)
        if not host:
            raise RuntimeError("WindowsDNSDriver: server.host is required")
        port = getattr(server, "api_port", None) or 53
        rtype = change.record.record_type.upper()

        tsig_name = change.tsig_key_name or getattr(server, "tsig_key_name", None)
        tsig_secret = getattr(server, "tsig_key_secret", None)
        tsig_algorithm = getattr(server, "tsig_key_algorithm", "hmac-sha256")
        if tsig_name and tsig_secret:
            keyring = dns.tsigkeyring.from_text({tsig_name: tsig_secret})
            update = dns.update.Update(
                change.zone_name,
                keyring=keyring,
                keyalgorithm=dns.name.from_text(tsig_algorithm),
            )
        else:
            keyring = None
            update = dns.update.Update(change.zone_name)

        rr = change.record
        rel_name = "@" if rr.name in ("", "@") else rr.name
        rdtype = dns.rdatatype.from_text(rtype)

        if change.op == "delete":
            update.delete(rel_name, rdtype)
        else:  # create | update
            if change.op == "update":
                update.delete(rel_name, rdtype)
            update.add(rel_name, rr.ttl or 3600, rtype, _format_rdata(rr))

        logger.info(
            "windows_dns.apply_record_change",
            path="rfc2136",
            server=str(getattr(server, "id", "")),
            host=host,
            port=port,
            zone=change.zone_name,
            op=change.op,
            name=rr.name,
            type=rtype,
            signed=keyring is not None,
        )
        await asyncio.to_thread(dns.query.tcp, update, host, port=port, timeout=10)

    async def _apply_record_change_winrm(self, server: Any, change: RecordChange) -> None:
        """Record create/update/delete via ``*-DnsServerResourceRecord``."""
        creds = _load_credentials(server)
        script = _ps_apply_record(change)
        await asyncio.to_thread(_run_ps, server, creds, script)
        logger.info(
            "windows_dns.apply_record_change",
            path="winrm",
            server=str(getattr(server, "id", "")),
            zone=change.zone_name,
            op=change.op,
            name=change.record.name,
            type=change.record.record_type,
        )

    async def apply_record_changes(
        self, server: Any, changes: Sequence[RecordChange]
    ) -> list[RecordChangeResult]:
        """Batched WinRM dispatch for many record changes against one server.

        The singular ``apply_record_change`` path opens a fresh WinRM
        session, ships one PowerShell cmdlet, and tears the session down
        — roughly 1.5s of overhead per op. An 81-record "Sync with
        servers" reconcile racks up to ~3 minutes that way. Batched
        dispatch groups up to ``_WINRM_BATCH_SIZE`` ops into one
        PowerShell script with ``$ErrorActionPreference = 'Continue'``
        plus per-op ``try / catch``, sends it in a single round trip,
        and parses per-op ``{ok, error}`` back out. Same 81 ops → one
        round trip, ~5–10s.

        Routing matches the singular path:

        * WinRM-eligible ops (server has credentials AND record type is
          not in ``_WINRM_UNSUPPORTED_RECORD_TYPES``) are partitioned
          into chunks of ``_WINRM_BATCH_SIZE`` and dispatched as a
          single script per chunk.
        * Everything else (TLSA on any server, or any record on a
          credentials-less server) falls through to the RFC 2136 path.
          RFC 2136 ops are small and independent — we run them
          concurrently with ``asyncio.gather`` instead of trying to
          batch the wire protocol.

        Per-op failures surface as ``RecordChangeResult(ok=False)`` —
        the caller decides whether to treat that as a hard error. Only
        whole-batch failures (WinRM auth, connection refused,
        PowerShell parse error in our generated script) raise.
        """
        if not changes:
            return []

        winrm_eligible: list[tuple[int, RecordChange]] = []
        rfc2136_ops: list[tuple[int, RecordChange]] = []

        has_creds = bool(getattr(server, "credentials_encrypted", None))
        for idx, change in enumerate(changes):
            rtype = change.record.record_type.upper()
            if rtype not in _SUPPORTED_RECORD_TYPES:
                # Preserve the singular-path error semantics for the
                # unsupported case: a hard failure on the whole op, not a
                # silent skip. Surface it as a per-op failed result so the
                # rest of the batch still runs.
                rfc2136_ops.append((idx, change))
                continue
            if has_creds and rtype not in _WINRM_UNSUPPORTED_RECORD_TYPES:
                winrm_eligible.append((idx, change))
            else:
                rfc2136_ops.append((idx, change))

        # Pre-size the result list so we can splat batch results back by
        # original index without caring about dispatch order.
        results: list[RecordChangeResult | None] = [None] * len(changes)

        # RFC 2136 dispatch — run in parallel. Each op is its own socket
        # to port 53 and a small, independent nsupdate; there's nothing
        # to batch at the wire level.
        async def _one_rfc2136(idx: int, change: RecordChange) -> None:
            try:
                await self._apply_record_change_rfc2136(server, change)
                results[idx] = RecordChangeResult(ok=True, change=change)
            except Exception as exc:  # noqa: BLE001 — per-op isolation
                results[idx] = RecordChangeResult(ok=False, change=change, error=str(exc))

        if rfc2136_ops:
            await asyncio.gather(*(_one_rfc2136(i, c) for i, c in rfc2136_ops))

        # WinRM dispatch — chunked into single-script round trips.
        if winrm_eligible:
            creds = _load_credentials(server)
            chunks = [
                winrm_eligible[i : i + _WINRM_BATCH_SIZE]
                for i in range(0, len(winrm_eligible), _WINRM_BATCH_SIZE)
            ]
            total_chunks = len(chunks)
            for chunk_index, chunk in enumerate(chunks):
                batch_changes = [c for _, c in chunk]
                script = _ps_apply_record_batch(batch_changes)
                raw = await asyncio.to_thread(_run_ps, server, creds, script)
                batch_results = _parse_record_batch_results(raw, batch_changes)
                ok_count = sum(1 for r in batch_results if r.ok)
                failed_count = len(batch_results) - ok_count
                logger.info(
                    "dns_apply_record_changes_batch",
                    server=str(getattr(server, "id", "")),
                    count=len(batch_results),
                    ok_count=ok_count,
                    failed_count=failed_count,
                    chunk_index=chunk_index,
                    chunks=total_chunks,
                )
                for (orig_idx, _), res in zip(chunk, batch_results, strict=True):
                    results[orig_idx] = res

        # Any ``None`` at this point is a bug — defensively coerce to a
        # failure result so we never return a list with holes.
        final: list[RecordChangeResult] = []
        for idx, entry in enumerate(results):
            if entry is None:
                final.append(
                    RecordChangeResult(
                        ok=False,
                        change=changes[idx],
                        error="internal: result slot not populated",
                    )
                )
            else:
                final.append(entry)
        return final

    async def apply_zone_change(self, server: Any, zone: Any, op: str) -> None:
        """Create / delete a zone on the Windows DC over WinRM.

        Only meaningful when the server has stored credentials — without
        them there's no admin channel to Windows. Called by the zone CRUD
        service helper on servers with ``windows_dns`` + creds.

        ``op`` is one of ``create`` / ``delete``. Name / type changes are
        not supported as a single op (Windows doesn't have a rename) —
        the caller sends delete+create instead.
        """
        if op not in {"create", "delete"}:
            raise ValueError(f"windows_dns.apply_zone_change: unsupported op {op!r}")
        if not getattr(server, "credentials_encrypted", None):
            raise RuntimeError(
                "windows_dns.apply_zone_change requires WinRM credentials on the server"
            )
        creds = _load_credentials(server)
        script = _ps_apply_zone(zone, op)
        await asyncio.to_thread(_run_ps, server, creds, script)
        logger.info(
            "windows_dns.apply_zone_change",
            server=str(getattr(server, "id", "")),
            zone=getattr(zone, "name", ""),
            op=op,
            kind=getattr(zone, "kind", None),
        )

    async def reload_config(self, server: Any) -> None:
        # Windows handles its own config lifecycle; nothing to do remotely.
        return

    async def reload_zone(self, server: Any, zone_name: str) -> None:
        return

    async def pull_zone_records(self, server: Any, zone_name: str) -> list[RecordData]:
        """Return the records for ``zone_name`` from the Windows DC.

        Dispatches by credential state:

        * **Credentials set (Path B)** — reads records via WinRM +
          ``Get-DnsServerResourceRecord``. Works for AD-integrated zones
          without having to open AXFR on the server — the default
          ``Name Servers tab only`` zone-transfer policy rejects AXFR
          from the control plane, so Path A fails with REFUSED on a
          stock DC. The PowerShell path only needs the account to have
          read rights on the zone (DnsAdmins or a custom role).
        * **No credentials (Path A)** — falls back to standard RFC 2136
          AXFR. Requires the zone to allow transfers to the SpatiumDDI
          host.
        """
        if getattr(server, "credentials_encrypted", None):
            return await self._pull_zone_records_winrm(server, zone_name)

        from app.drivers.dns._axfr import axfr_zone_records  # noqa: PLC0415

        host = getattr(server, "host", None)
        if not host:
            raise RuntimeError("WindowsDNSDriver.pull_zone_records: server.host is required")
        port = getattr(server, "api_port", None) or 53
        return await axfr_zone_records(
            host=host,
            port=port,
            zone_name=zone_name,
            log_driver="windows_dns",
            server_id=str(getattr(server, "id", "")),
        )

    async def _pull_zone_records_winrm(self, server: Any, zone_name: str) -> list[RecordData]:
        """Pull records via ``Get-DnsServerResourceRecord`` over WinRM."""
        creds = _load_credentials(server)
        script = _ps_list_records(zone_name)
        raw = await asyncio.to_thread(_run_ps, server, creds, script)
        records = _parse_records(raw)
        logger.info(
            "windows_dns.pull_zone_records_winrm",
            server=str(getattr(server, "id", "")),
            zone=zone_name,
            count=len(records),
        )
        return records

    # ── Validation / capabilities ────────────────────────────────────────

    def validate_config(self, bundle: ConfigBundle) -> tuple[bool, list[str]]:
        # We don't render config for Windows; the bundle is informational
        # only. Accept anything so upstream validators don't block.
        return (True, [])

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": "windows_dns",
            "agentless": True,
            "manages_zones": False,
            "views": False,
            "rpz": False,
            "dnssec_inline_signing": False,
            "incremental_updates": "rfc2136",
            "tsig": "optional",
            "zone_types": ["primary (external)", "secondary (external)"],
            "record_types": list(_SUPPORTED_RECORD_TYPES),
            "notes": (
                "Agentless Windows DNS driver. Path A: record CRUD via RFC "
                "2136 (always). Path B: zone topology reads via WinRM + "
                "PowerShell when credentials are configured on the server."
            ),
        }

    # ── Path B — WinRM / PowerShell (credentials required) ──────────────

    async def pull_zones_from_server(self, server: Any) -> list[dict[str, Any]]:
        """List the zones hosted on the Windows DNS server over WinRM.

        Returns a list of neutral dicts::

            [
              {"name": "example.com", "zone_type": "Primary",
               "is_ad_integrated": True, "is_reverse_lookup": False,
               "dynamic_update": "Secure"},
              …
            ]

        Caller reconciles these against ``DNSZone`` rows in the DB —
        today that reconciliation is manual (import / pick), the Celery
        auto-sync for zones is future work.

        Requires ``server.credentials_encrypted`` to be set. Raises
        ``ValueError`` with a helpful message otherwise.
        """
        creds = _load_credentials(server)
        raw = await asyncio.to_thread(_run_ps, server, creds, _PS_LIST_ZONES)
        return _parse_zones(raw)

    # ── Logs — Windows Event Log reads ────────────────────────────────

    def available_log_names(self) -> list[tuple[str, str]]:
        """Event logs this driver surfaces in the Logs UI.

        ``DNS Server`` is the classic log — always present on a Windows
        DNS role and captures zone loads, transfer successes/failures,
        SOA updates. ``Microsoft-Windows-DNSServer/Audit`` is the
        modern audit log (config changes like Add-DnsServerZone). The
        Analytical log is deliberately omitted — it's per-query and
        noisy, and the operator usually wants Windows DNS Manager for
        that view rather than a slow WinRM pull.
        """
        return [
            ("DNS Server", "DNS Server — Classic Log"),
            ("Microsoft-Windows-DNSServer/Audit", "DNS Server — Audit"),
        ]

    async def get_events(
        self,
        server: Any,
        *,
        log_name: str,
        max_events: int = 100,
        level: int | None = None,
        since: Any = None,
        event_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query a Windows Event Log over WinRM. Requires credentials."""
        from app.drivers.windows_events import fetch_events  # noqa: PLC0415

        creds = _load_credentials(server)
        return await fetch_events(
            server,
            creds,
            run_ps=_run_ps,
            log_name=log_name,
            max_events=max_events,
            level=level,
            since=since,
            event_id=event_id,
        )


# ── WinRM helpers (Path B) ────────────────────────────────────────────


_PS_LIST_ZONES = (
    "Get-DnsServerZone "
    "| Where-Object { -not $_.IsAutoCreated } "
    "| Select-Object ZoneName, ZoneType, IsDsIntegrated, IsReverseLookupZone, "
    "DynamicUpdate "
    "| ConvertTo-Json -Compress"
)


def _ps_escape_single_quoted(value: str) -> str:
    """Escape ``value`` for use inside a PowerShell single-quoted literal.

    PS single-quoted strings have exactly one escape rule — double the
    inner single quote. No backticks, no variable expansion, nothing to
    worry about. Keeps zone names with apostrophes (unlikely but legal)
    from breaking the script.
    """
    return (value or "").replace("'", "''")


def _ps_list_records(zone_name: str) -> str:
    """PowerShell script to list records in ``zone_name`` as compact JSON.

    Skips SOA (Windows owns it) and apex NS (zone-level control surface).
    Every record emits ``{HostName, Type, TTL, Value, Priority?, Weight?,
    Port?}`` so the Python-side parser can round-trip into ``RecordData``
    without caring about the source format.
    """
    zone = _ps_escape_single_quoted(zone_name.rstrip("."))
    # Inline a small walker. Use ForEach-Object so each RecordData variant
    # is unpacked into a flat object — ConvertTo-Json otherwise descends
    # into nested RecordData and blows up the output with noise.
    return f"""
$records = Get-DnsServerResourceRecord -ZoneName '{zone}' -ErrorAction Stop
$out = @()
foreach ($r in $records) {{
    $t = $r.RecordType
    if ($t -eq 'SOA') {{ continue }}
    if ($t -eq 'NS' -and $r.HostName -eq '@') {{ continue }}
    $ttl = [int]$r.TimeToLive.TotalSeconds
    $data = $r.RecordData
    $o = [ordered]@{{
        HostName = $r.HostName
        Type     = "$t"
        TTL      = $ttl
    }}
    switch ("$t") {{
        'A'     {{ $o.Value = $data.IPv4Address.IPAddressToString }}
        'AAAA'  {{ $o.Value = $data.IPv6Address.IPAddressToString }}
        'CNAME' {{ $o.Value = "$($data.HostNameAlias)" }}
        'NS'    {{ $o.Value = "$($data.NameServer)" }}
        'PTR'   {{ $o.Value = "$($data.PtrDomainName)" }}
        'MX'    {{
            $o.Value = "$($data.MailExchange)"
            $o.Priority = [int]$data.Preference
        }}
        'SRV'   {{
            $o.Value = "$($data.DomainName)"
            $o.Priority = [int]$data.Priority
            $o.Weight = [int]$data.Weight
            $o.Port = [int]$data.Port
        }}
        'TXT'   {{
            $o.Value = ($data.DescriptiveText -join '')
        }}
        default {{ $o.Value = "$($data)" }}
    }}
    $out += (New-Object PSObject -Property $o)
}}
$out | ConvertTo-Json -Compress -Depth 3
""".strip()


# Record types Path B can map into ``RecordData``. Anything outside this
# set falls through the default branch in the PowerShell script with its
# raw ``.ToString()`` representation; we skip those on the Python side
# rather than guess at the structure, so the pull importer doesn't
# generate garbage rows for exotic types (WINS, WINS-R, AFSDB, …).
_PULL_RECORD_TYPES: frozenset[str] = frozenset(
    {"A", "AAAA", "CNAME", "NS", "PTR", "MX", "SRV", "TXT"}
)


def _parse_records(raw: str) -> list[RecordData]:
    """Parse ``_ps_list_records`` JSON output into neutral ``RecordData``.

    Normalises the same three Windows JSON shapes as ``_parse_zones`` —
    empty string for zero records, single object for one, array for 2+.
    """
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("windows_dns_record_parse_failed", raw=text[:400], error=str(exc))
        return []
    items: list[dict[str, Any]] = parsed if isinstance(parsed, list) else [parsed]
    out: list[RecordData] = []
    for item in items:
        rtype = str(item.get("Type") or "").upper()
        if rtype not in _PULL_RECORD_TYPES:
            continue
        name = str(item.get("HostName") or "@")
        if not name:
            name = "@"
        value = item.get("Value")
        if value is None:
            continue
        ttl_raw = item.get("TTL")
        try:
            ttl = int(ttl_raw) if ttl_raw is not None else None
        except (TypeError, ValueError):
            ttl = None
        priority = item.get("Priority")
        weight = item.get("Weight")
        port = item.get("Port")
        out.append(
            RecordData(
                name=name,
                record_type=rtype,
                value=str(value),
                ttl=ttl,
                priority=int(priority) if priority is not None else None,
                weight=int(weight) if weight is not None else None,
                port=int(port) if port is not None else None,
            )
        )
    return out


def _load_credentials(server: Any) -> dict[str, Any]:
    """Decrypt and return the WinRM cred dict from ``server.credentials_encrypted``.

    Raises ``ValueError`` if the server has no credentials set — the caller
    surfaces that to the UI so the operator can distinguish "Path A only"
    from "WinRM misconfigured".
    """
    blob = getattr(server, "credentials_encrypted", None)
    if not blob:
        raise ValueError(
            f"DNS server {getattr(server, 'name', '<unknown>')!r} has no Windows "
            "credentials set; configure username/password on the server to enable "
            "WinRM-based zone management."
        )
    return decrypt_dict(blob)


def _run_ps(server: Any, creds: dict[str, Any], script: str) -> str:
    """Run a PowerShell script on the Windows DNS server over WinRM.

    Mirrors the DHCP-side helper: same credential dict shape, same
    transport / TLS / port defaults. Blocking — call via
    ``asyncio.to_thread``. Returns stdout as text; raises ``RuntimeError``
    on non-zero exit.
    """
    # Deferred import: keeps celery-worker startup light and means hosts
    # that only run agent-based drivers don't need the pywinrm wheel.
    import winrm  # noqa: PLC0415

    transport = creds.get("transport") or "ntlm"
    use_tls = bool(creds.get("use_tls", False))
    verify_tls = bool(creds.get("verify_tls", False))
    port = int(creds.get("winrm_port") or (5986 if use_tls else 5985))
    scheme = "https" if use_tls else "http"
    host = getattr(server, "host", "")
    endpoint = f"{scheme}://{host}:{port}/wsman"

    session = winrm.Session(
        endpoint,
        auth=(creds.get("username", ""), creds.get("password", "")),
        transport=transport,
        server_cert_validation="validate" if verify_tls else "ignore",
    )
    result = session.run_ps(script)
    stdout = (result.std_out or b"").decode("utf-8", errors="replace")
    stderr = (result.std_err or b"").decode("utf-8", errors="replace")
    if result.status_code != 0:
        raise RuntimeError(
            f"winrm exit={result.status_code}: {stderr.strip() or stdout.strip() or '<no output>'}"
        )
    return stdout


def _parse_zones(raw: str) -> list[dict[str, Any]]:
    """Parse ``ConvertTo-Json -Compress`` output from ``_PS_LIST_ZONES``.

    Windows emits an empty string for zero zones, a single object for one
    zone, and an array for 2+. Normalise all three to a list and shape
    into neutral dicts so callers don't depend on PowerShell key casing.
    """
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("windows_dns_zone_parse_failed", raw=text[:400], error=str(exc))
        return []
    items: list[dict[str, Any]] = parsed if isinstance(parsed, list) else [parsed]
    out: list[dict[str, Any]] = []
    for item in items:
        name = item.get("ZoneName")
        if not name:
            continue
        out.append(
            {
                "name": str(name).rstrip("."),
                "zone_type": item.get("ZoneType"),
                "is_ad_integrated": bool(item.get("IsDsIntegrated")),
                "is_reverse_lookup": bool(item.get("IsReverseLookupZone")),
                "dynamic_update": item.get("DynamicUpdate"),
            }
        )
    return out


def _ps_apply_zone(zone: Any, op: str) -> str:
    """PowerShell script for ``apply_zone_change(op=create|delete)``.

    Create uses ``Add-DnsServerPrimaryZone -ReplicationScope Domain`` —
    AD-integrated is the sane default for a DC. If the Windows host isn't
    a DC the operator will see the PS error (``Active Directory not
    available``) and can flip to a file-backed zone from DNS Manager.

    Delete uses ``Remove-DnsServerZone -Force`` so we don't stall on the
    interactive confirmation prompt.
    """
    name = _ps_escape_single_quoted((getattr(zone, "name", "") or "").rstrip("."))
    if not name:
        raise ValueError("windows_dns._ps_apply_zone: zone name is required")

    if op == "create":
        # Existing zone → no-op with a reassuring log line rather than a
        # hard error. The common case is: operator seeded the zone in
        # DNS Manager, imported it via sync, then edited it in SpatiumDDI
        # — we don't want to "re-create" it.
        return f"""
if (Get-DnsServerZone -Name '{name}' -ErrorAction SilentlyContinue) {{
    Write-Output "zone '{name}' already exists on server"
}} else {{
    Add-DnsServerPrimaryZone -Name '{name}' -ReplicationScope Domain -DynamicUpdate Secure -ErrorAction Stop
    Write-Output "zone '{name}' created"
}}
""".strip()
    if op == "delete":
        # ``-Force`` skips the prompt. SilentlyContinue on the probe so
        # we turn "already gone" into a clean success — idempotent
        # delete matches the DHCP-side semantics.
        return f"""
if (Get-DnsServerZone -Name '{name}' -ErrorAction SilentlyContinue) {{
    Remove-DnsServerZone -Name '{name}' -Force -ErrorAction Stop
    Write-Output "zone '{name}' deleted"
}} else {{
    Write-Output "zone '{name}' was not present on server"
}}
""".strip()

    raise ValueError(f"windows_dns._ps_apply_zone: bad op {op!r}")


def _ps_apply_record(change: RecordChange) -> str:
    """PowerShell script for ``apply_record_change`` create/update/delete.

    Zone + record names, rdata, and TTL all flow through
    ``_ps_escape_single_quoted`` so any PS metacharacter is neutered.
    Update maps to delete+create on the target ``{name, type}`` since
    Windows' PS cmdlets don't offer an atomic ``Set-…`` for all types.
    That matches the ``_apply_record_change_rfc2136`` logic too.
    """
    zone = _ps_escape_single_quoted((change.zone_name or "").rstrip("."))
    rtype = change.record.record_type.upper()
    rel_name = change.record.name if change.record.name not in ("", "@") else "@"
    name = _ps_escape_single_quoted(rel_name)
    ttl = int(change.record.ttl or 3600)
    value = _ps_escape_single_quoted(change.record.value)

    # Per-type create script. ``-AllowUpdateAny`` isn't a real switch — we
    # use ``-ErrorAction Stop`` so any duplicate surfaces a clean failure
    # rather than silently lying. The update op strips the old RR first.
    priority = int(change.record.priority or 0)
    weight = int(change.record.weight or 0)
    port = int(change.record.port or 0)

    if rtype == "A":
        create = (
            f"Add-DnsServerResourceRecordA -ZoneName '{zone}' -Name '{name}' "
            f"-IPv4Address '{value}' -TimeToLive ([TimeSpan]::FromSeconds({ttl})) "
            "-ErrorAction Stop"
        )
    elif rtype == "AAAA":
        create = (
            f"Add-DnsServerResourceRecordAAAA -ZoneName '{zone}' -Name '{name}' "
            f"-IPv6Address '{value}' -TimeToLive ([TimeSpan]::FromSeconds({ttl})) "
            "-ErrorAction Stop"
        )
    elif rtype == "CNAME":
        create = (
            f"Add-DnsServerResourceRecordCName -ZoneName '{zone}' -Name '{name}' "
            f"-HostNameAlias '{value}' -TimeToLive ([TimeSpan]::FromSeconds({ttl})) "
            "-ErrorAction Stop"
        )
    elif rtype == "PTR":
        create = (
            f"Add-DnsServerResourceRecordPtr -ZoneName '{zone}' -Name '{name}' "
            f"-PtrDomainName '{value}' -TimeToLive ([TimeSpan]::FromSeconds({ttl})) "
            "-ErrorAction Stop"
        )
    elif rtype == "MX":
        create = (
            f"Add-DnsServerResourceRecordMX -ZoneName '{zone}' -Name '{name}' "
            f"-MailExchange '{value}' -Preference {priority} "
            f"-TimeToLive ([TimeSpan]::FromSeconds({ttl})) -ErrorAction Stop"
        )
    elif rtype == "SRV":
        create = (
            f"Add-DnsServerResourceRecord -ZoneName '{zone}' -Name '{name}' -Srv "
            f"-DomainName '{value}' -Priority {priority} -Weight {weight} -Port {port} "
            f"-TimeToLive ([TimeSpan]::FromSeconds({ttl})) -ErrorAction Stop"
        )
    elif rtype == "NS":
        create = (
            f"Add-DnsServerResourceRecord -ZoneName '{zone}' -Name '{name}' -NS "
            f"-NameServer '{value}' -TimeToLive ([TimeSpan]::FromSeconds({ttl})) "
            "-ErrorAction Stop"
        )
    elif rtype == "TXT":
        create = (
            f"Add-DnsServerResourceRecord -ZoneName '{zone}' -Name '{name}' -Txt "
            f"-DescriptiveText '{value}' -TimeToLive ([TimeSpan]::FromSeconds({ttl})) "
            "-ErrorAction Stop"
        )
    else:
        # TLSA + other WinRM-unsupported types are routed to RFC 2136 in
        # ``apply_record_change`` before reaching this method; anything
        # that still lands here is a bug in dispatch.
        raise ValueError(f"windows_dns._ps_apply_record: unsupported type {rtype!r}")

    # Delete is idempotent: no-op if the zone or the record is already
    # absent. Without these guards, Get-DnsServerResourceRecord's
    # zone-not-found error leaks out through the WinRM exit code and
    # surfaces as a "failed" record op — confusing for the operator,
    # since the desired end state (record gone) is already met.
    #
    # Reverse-zone drift is the common trigger: IPAM creates the PTR
    # zone in SpatiumDDI but the operator never ran Sync with Servers,
    # so deleting an IP tries to delete a PTR from a zone Windows
    # doesn't know about.
    delete = f"""
if (-not (Get-DnsServerZone -Name '{zone}' -ErrorAction SilentlyContinue)) {{
    Write-Output "zone '{zone}' not present on server; delete is a no-op"
}} else {{
    $rrs = Get-DnsServerResourceRecord -ZoneName '{zone}' -Name '{name}' -RRType {rtype} -ErrorAction SilentlyContinue
    if ($rrs) {{
        $rrs | Remove-DnsServerResourceRecord -ZoneName '{zone}' -Force -ErrorAction Stop
        Write-Output "record '{name}' {rtype} deleted from '{zone}'"
    }} else {{
        Write-Output "record '{name}' {rtype} not present in '{zone}'; delete is a no-op"
    }}
}}
""".strip()

    # For create/update, fail loudly with a clear message when the zone
    # doesn't exist on the server — the caller already has write-through
    # at zone create time, so this only fires on pre-existing drift; the
    # fix is one click on Sync with Servers.
    create_with_zone_guard = f"""
if (-not (Get-DnsServerZone -Name '{zone}' -ErrorAction SilentlyContinue)) {{
    throw "Zone '{zone}' not found on Windows server. Click 'Sync with Servers' on the server group to push SpatiumDDI zones first."
}}
{create}
""".strip()

    if change.op == "create":
        return create_with_zone_guard
    if change.op == "delete":
        return delete
    if change.op == "update":
        # Replace: wipe the existing RR(s) at (name, type) — idempotent —
        # then add the new rdata guarded on zone existence. Matches what
        # the RFC 2136 path does with update.delete(...) + update.add(...).
        return f"{delete}\n{create_with_zone_guard}"
    raise ValueError(f"windows_dns._ps_apply_record: bad op {change.op!r}")


def _ps_apply_record_batch(changes: Sequence[RecordChange]) -> str:
    """Render one PowerShell script that applies every change in ``changes``.

    The earlier version of this function embedded a full per-op PS
    snippet inside the JSON payload. That bloated each op to ~700 chars
    and, once JSON-escaped + base64'd + UTF-16-LE-encoded + base64'd
    again for ``powershell.exe -EncodedCommand``, a 30-op batch blew
    through the Windows command-line length cap ("The filename or
    extension is too long" / ``wsmanfault_code: 2147942606``).

    This version keeps the payload to data only (~120 chars per op):
    short-key JSON with ``{i, op, z, n, t, v, ttl, pr, w, p}``. The
    wrapper holds **one** copy of the per-type cmdlet dispatch and calls
    it in a loop. That brings a 30-op batch to ~4KB raw / ~12KB encoded
    — well under any Windows cmdline limit — and makes 100+ op batches
    feasible on a stock host.

    ``$ErrorActionPreference = 'Continue'`` at the top ensures a per-op
    throw from one cmdlet doesn't abort the enclosing script: the
    try/catch records the error into the result array and the loop
    moves on. Chunk-wide script errors (syntax, base64 decode) still
    raise from ``_run_ps`` and propagate.
    """
    ops: list[dict[str, Any]] = []
    for i, change in enumerate(changes):
        rtype = change.record.record_type.upper()
        name = change.record.name if change.record.name not in ("", "@") else "@"
        ops.append(
            {
                "i": i,
                "op": change.op,
                "z": (change.zone_name or "").rstrip("."),
                "n": name,
                "t": rtype,
                "v": change.record.value or "",
                "ttl": int(change.record.ttl or 3600),
                "pr": int(change.record.priority or 0),
                "w": int(change.record.weight or 0),
                "p": int(change.record.port or 0),
            }
        )
    blob = json.dumps(ops, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.b64encode(blob).decode("ascii")

    # Script aggressively minified. ``pywinrm.run_ps`` base64-encodes
    # with UTF-16-LE then sends via ``powershell -encodedcommand <b64>``
    # as a single CMD.EXE command line — hard-capped at 8191 chars by
    # Windows. Base64 costs ×1.33, UTF-16-LE costs ×2, so each raw
    # script char eats ~2.67 chars of command-line budget. Budget works
    # out to ~3050 raw chars total. The wrapper below is ~1000 chars;
    # with ~130 chars per op in JSON that leaves room for ~15 ops per
    # batch on stock Windows. Set ``_WINRM_BATCH_SIZE`` accordingly.
    return (
        "$E='Continue'\n"
        "$p=[Text.Encoding]::UTF8.GetString("
        f"[Convert]::FromBase64String('{payload_b64}'))\n"
        "$r=@()\n"
        "($p|ConvertFrom-Json)|%{\n"
        ' $e=[ordered]@{change_index=[int]$_.i;name="$($_.n)";'
        'type="$($_.t)";op="$($_.op)";ok=$false;error=$null}\n'
        " try{\n"
        "  $z=$_.z;$n=$_.n;$t=$_.t;$v=$_.v;"
        "$tl=[TimeSpan]::FromSeconds([int]$_.ttl)\n"
        "  $ze=[bool](Get-DnsServerZone -Name $z -EA SilentlyContinue)\n"
        "  if($_.op -eq 'delete'){\n"
        "   if($ze){$x=Get-DnsServerResourceRecord -ZoneName $z -Name $n"
        " -RRType $t -EA SilentlyContinue;"
        "if($x){$x|Remove-DnsServerResourceRecord -ZoneName $z -Force -EA Stop}}\n"
        "  }elseif($_.op -eq 'create' -or $_.op -eq 'update'){\n"
        '   if(!$ze){throw "Zone $z not found"}\n'
        "   if($_.op -eq 'update'){$x=Get-DnsServerResourceRecord -ZoneName $z"
        " -Name $n -RRType $t -EA SilentlyContinue;"
        "if($x){$x|Remove-DnsServerResourceRecord -ZoneName $z -Force -EA Stop}}\n"
        "   switch($t){\n"
        "    'A'{Add-DnsServerResourceRecordA -ZoneName $z -Name $n"
        " -IPv4Address $v -TimeToLive $tl -EA Stop}\n"
        "    'AAAA'{Add-DnsServerResourceRecordAAAA -ZoneName $z -Name $n"
        " -IPv6Address $v -TimeToLive $tl -EA Stop}\n"
        "    'CNAME'{Add-DnsServerResourceRecordCName -ZoneName $z -Name $n"
        " -HostNameAlias $v -TimeToLive $tl -EA Stop}\n"
        "    'PTR'{Add-DnsServerResourceRecordPtr -ZoneName $z -Name $n"
        " -PtrDomainName $v -TimeToLive $tl -EA Stop}\n"
        "    'MX'{Add-DnsServerResourceRecordMX -ZoneName $z -Name $n"
        " -MailExchange $v -Preference([int]$_.pr) -TimeToLive $tl -EA Stop}\n"
        "    'SRV'{Add-DnsServerResourceRecord -ZoneName $z -Name $n -Srv"
        " -DomainName $v -Priority([int]$_.pr) -Weight([int]$_.w)"
        " -Port([int]$_.p) -TimeToLive $tl -EA Stop}\n"
        "    'NS'{Add-DnsServerResourceRecord -ZoneName $z -Name $n -NS"
        " -NameServer $v -TimeToLive $tl -EA Stop}\n"
        "    'TXT'{Add-DnsServerResourceRecord -ZoneName $z -Name $n -Txt"
        " -DescriptiveText $v -TimeToLive $tl -EA Stop}\n"
        '    default{throw "Unsupported type $t"}\n'
        "   }\n"
        '  }else{throw "Unsupported op"}\n'
        "  $e.ok=$true\n"
        ' }catch{$e.error="$($_.Exception.Message)"}\n'
        " $r+=(New-Object PSObject -Property $e)\n"
        "}\n"
        "$r|ConvertTo-Json -Compress -Depth 3"
    )


def _parse_record_batch_results(
    raw: str, batch_changes: Sequence[RecordChange]
) -> list[RecordChangeResult]:
    """Zip PS per-op output back onto ``batch_changes`` by ``change_index``.

    Windows' ``ConvertTo-Json -Compress`` emits:

    * empty string when the results array is empty (no ops ran),
    * a single JSON object when exactly one op ran,
    * a JSON array otherwise.

    Normalise to a list keyed by ``change_index`` so duplicates in the
    input batch (same name/type/op appearing twice — possible if the
    upstream dispatcher doesn't dedup) match by position and not by
    ambiguous identity. Missing indices become failed results with a
    synthetic error so the output is always the same length as the
    input batch.
    """
    text = (raw or "").strip()
    if not text:
        return [
            RecordChangeResult(ok=False, change=c, error="batch returned no output")
            for c in batch_changes
        ]

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("windows_dns_record_batch_parse_failed", raw=text[:400], error=str(exc))
        return [
            RecordChangeResult(ok=False, change=c, error=f"batch result parse failed: {exc}")
            for c in batch_changes
        ]

    items: list[dict[str, Any]] = parsed if isinstance(parsed, list) else [parsed]

    by_index: dict[int, RecordChangeResult] = {}
    for item in items:
        raw_idx = item.get("change_index")
        if raw_idx is None:
            continue
        try:
            idx = int(raw_idx)
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(batch_changes)):
            continue
        ok = bool(item.get("ok"))
        error_raw = item.get("error")
        by_index[idx] = RecordChangeResult(
            ok=ok,
            change=batch_changes[idx],
            error=None if ok or error_raw in (None, "") else str(error_raw),
        )

    return [
        by_index.get(
            i,
            RecordChangeResult(
                ok=False,
                change=change,
                error="internal: no result returned for op",
            ),
        )
        for i, change in enumerate(batch_changes)
    ]


async def test_winrm_credentials(host: str, credentials: dict[str, Any]) -> tuple[bool, str]:
    """Dry-run probe: can we reach ``host`` over WinRM with ``credentials``
    and run ``Get-DnsServerSetting``? Used by the create/edit modal's
    "Test Connection" button so credential issues surface before save.

    Returns ``(ok, message)`` — message is either the DNS server version
    string on success, or the WinRM/PowerShell error on failure.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    fake = SimpleNamespace(name="<test>", host=host)
    try:
        raw = await asyncio.to_thread(
            _run_ps,
            fake,
            credentials,
            # Lightest possible probe: return the computer name + DNS
            # version. ``Get-DnsServerSetting`` requires the ``DnsServer``
            # module which is what we actually need anyway — confirms both
            # reachability and that the host is a DNS server.
            "(Get-DnsServerSetting -All | Select-Object -ExpandProperty BuildNumber).ToString()",
        )
        return True, f"windows_dns reachable (DNS build {raw.strip()})"
    except Exception as exc:  # noqa: BLE001 — surface any transport/PS error verbatim
        return False, str(exc)


__all__ = ["WindowsDNSDriver", "test_winrm_credentials"]
