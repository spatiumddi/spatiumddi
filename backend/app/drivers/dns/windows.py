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
import json
from typing import Any

import structlog

from app.core.crypto import decrypt_dict
from app.drivers.dns.base import (
    ConfigBundle,
    DNSDriver,
    EffectiveBlocklistData,
    RecordChange,
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

        if getattr(server, "credentials_encrypted", None):
            await self._apply_record_change_winrm(server, change)
            return

        await self._apply_record_change_rfc2136(server, change)

    async def _apply_record_change_rfc2136(
        self, server: Any, change: RecordChange
    ) -> None:
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

    async def _apply_record_change_winrm(
        self, server: Any, change: RecordChange
    ) -> None:
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

    async def _pull_zone_records_winrm(
        self, server: Any, zone_name: str
    ) -> list[RecordData]:
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
    elif rtype == "TLSA":
        # TLSA doesn't have a dedicated cmdlet in older DnsServer modules;
        # surface this cleanly rather than silently dropping the write.
        raise ValueError("windows_dns: TLSA writes over WinRM are not implemented")
    else:
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
