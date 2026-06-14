"""Windows DHCP driver (agentless, WinRM + PowerShell).

Covers both Path A (read-only: lease monitoring, scope import) and
the core of Path B (write-through: per-object CRUD on scopes, pools,
reservations, and scope options). Drives a Windows DHCP server over
WinRM by invoking the ``DhcpServer`` PowerShell module.

**What's implemented:**

  * Reads — ``get_leases`` (``Get-DhcpServerv4Lease``) and
    ``get_scopes`` (``Get-DhcpServerv4Scope`` + option values +
    exclusions + reservations rolled into one PowerShell call).
  * Writes (per-object, idempotent) — ``apply_scope`` /
    ``remove_scope`` / ``apply_reservation`` /
    ``remove_reservation`` / ``apply_exclusion`` /
    ``remove_exclusion``. Called from the SpatiumDDI scope / pool /
    static API endpoints via ``services.dhcp.windows_writethrough``.

**What's NOT implemented** — the whole-bundle push contract
(``render_config`` / ``apply_config`` / ``reload`` / ``restart``) is
not applicable to Windows DHCP and raises ``NotImplementedError``.
Windows reconfigures cmdlet-by-cmdlet, not by re-reading a config
file. The ``/sync`` endpoint rejects this driver (see
``READ_ONLY_DRIVERS`` in the registry); the ``/sync-leases`` and per-
object CRUD endpoints drive all writes.

Credentials live on ``DHCPServer.credentials_encrypted`` as a
Fernet-encrypted JSON dict:

    {
      "username": "SPATIUM\\dhcpreader",
      "password": "…",
      "winrm_port": 5985,
      "transport": "ntlm",    # ntlm | kerberos | basic | credssp
      "use_tls": false,
      "verify_tls": false
    }

A service account in the Windows ``DHCP Users`` (read) or ``DHCP
Administrators`` (read+write) group is sufficient for the PowerShell
calls we invoke.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.crypto import decrypt_dict
from app.drivers._winrm import (
    MAX_ENCODED_COMMAND,
    WinRMCommandTooLong,
    encoded_command_len,
)
from app.drivers._winrm import (
    run_ps as _winrm_run_ps,
)
from app.drivers.dhcp.base import (
    ConfigBundle,
    DHCPDriver,
    ExclusionItem,
    ExclusionResult,
    MACBlockDef,
    RemoveReservationItem,
    ReservationItem,
    ReservationResult,
)

logger = structlog.get_logger(__name__)


# PowerShell snippet: enumerate all IPv4 scopes, pull every lease, emit
# one JSON array where each element has the fields we care about.
# ``ConvertTo-Json -Compress -Depth 3`` keeps the payload small and the
# single-object edge case is handled client-side.
_PS_LIST_LEASES = r"""
$ErrorActionPreference = 'Stop'
# #426: enumerate ALL scopes, not just State -eq 'Active'. A
# deactivated-but-existing scope still holds valid leases on the server;
# pre-filtering them out made them disappear from the wire and the
# absence-delete reconcile then purged the DHCPLease rows + their IPAM
# mirrors. get_scopes() also enumerates unfiltered, so the two reads now
# agree. Per-lease liveness is decided by _ACTIVE_STATES below.
$scopes = Get-DhcpServerv4Scope
$all = @()
foreach ($s in $scopes) {
    $leases = Get-DhcpServerv4Lease -ScopeId $s.ScopeId -AllLeases
    foreach ($l in $leases) {
        $all += [PSCustomObject]@{
            ScopeId       = $s.ScopeId.ToString()
            IPAddress     = $l.IPAddress.ToString()
            ClientId      = $l.ClientId
            HostName      = $l.HostName
            AddressState  = $l.AddressState
            LeaseExpiryTime = if ($l.LeaseExpiryTime) { $l.LeaseExpiryTime.ToString('o') } else { $null }
        }
    }
}
$all | ConvertTo-Json -Compress -Depth 3
"""


# Windows AddressState values we treat as "live" leases worth mirroring.
# ``InactiveReservation`` and ``Declined`` intentionally omitted.
_ACTIVE_STATES: frozenset[str] = frozenset(
    {
        "Active",
        "ActiveReservation",
    }
)


# Emits one JSON array, one element per v4 scope, with pools (start/end),
# exclusions, reservations, and option values nested inline. Errors inside
# the loop are swallowed per-scope so one broken scope doesn't poison the
# whole run. Subnet mask is emitted as dotted-quad; the service layer
# converts to CIDR prefix-length via Python's ipaddress module (cleaner
# than doing the bitcount in PowerShell).
_PS_LIST_TOPOLOGY = r"""
$ErrorActionPreference = 'Stop'
$scopes = Get-DhcpServerv4Scope
$result = @()
foreach ($s in $scopes) {
    $options = @{}
    try {
        foreach ($o in (Get-DhcpServerv4OptionValue -ScopeId $s.ScopeId -ErrorAction SilentlyContinue)) {
            $options["" + $o.OptionId] = [PSCustomObject]@{
                name  = $o.Name
                value = @($o.Value)
            }
        }
    } catch { }

    $exclusions = @()
    try {
        foreach ($e in (Get-DhcpServerv4ExclusionRange -ScopeId $s.ScopeId -ErrorAction SilentlyContinue)) {
            $exclusions += [PSCustomObject]@{
                start_ip = $e.StartRange.ToString()
                end_ip   = $e.EndRange.ToString()
            }
        }
    } catch { }

    $reservations = @()
    try {
        foreach ($r in (Get-DhcpServerv4Reservation -ScopeId $s.ScopeId -ErrorAction SilentlyContinue)) {
            $reservations += [PSCustomObject]@{
                ip_address = $r.IPAddress.ToString()
                client_id  = $r.ClientId
                name       = $r.Name
                description= $r.Description
            }
        }
    } catch { }

    $result += [PSCustomObject]@{
        scope_id               = $s.ScopeId.ToString()
        name                   = $s.Name
        description            = $s.Description
        subnet_mask            = $s.SubnetMask.ToString()
        start_range            = $s.StartRange.ToString()
        end_range              = $s.EndRange.ToString()
        lease_duration_seconds = [int]$s.LeaseDuration.TotalSeconds
        state                  = $s.State.ToString()
        options                = $options
        exclusions             = $exclusions
        reservations           = $reservations
    }
}
$result | ConvertTo-Json -Compress -Depth 5
"""


# Hard upper bound on ops per WinRM round-trip in the batch dispatchers.
# This is a sanity cap only — the REAL gate is the encoded-command-line
# budget (CMD.EXE ~8191 chars; see app/drivers/_winrm.py). #426 reworked
# the dispatcher to ship DATA-ONLY payloads (a few short fields per op)
# with ONE shared cmdlet body, then pack each chunk under
# ``MAX_ENCODED_COMMAND`` via ``encoded_command_len``. The previous design
# embedded a full ~600-char PowerShell snippet per op and Invoke-Expression'd
# each, so a fixed count of 30 blew the cmdline cap at 3-4 reservations.
_WINRM_MAX_BATCH_OPS = 100


# Windows option IDs → canonical SpatiumDDI option names (matches
# STANDARD_OPTION_NAMES in drivers/dhcp/base.py and the Kea rendering).
# Unmapped option IDs are kept as ``opt-<id>`` so they survive a round-
# trip without being lost; the UI can still show them as "unknown
# option".
_OPTION_ID_TO_NAME: dict[int, str] = {
    2: "time-offset",
    3: "routers",
    6: "dns-servers",
    15: "domain-name",
    26: "mtu",
    28: "broadcast-address",
    42: "ntp-servers",
    66: "tftp-server-name",
    67: "bootfile-name",
    119: "domain-search",
    150: "tftp-server-address",
}


class WindowsDHCPReadOnlyDriver(DHCPDriver):
    """WinRM-driven Windows DHCP driver.

    Name kept for backwards-compat with the initial Path A landing even
    though the driver is no longer strictly read-only — see module
    docstring for the current capability set.
    """

    name = "windows_dhcp"

    # ── reads ──────────────────────────────────────────────────────────

    async def get_leases(self, server: Any) -> list[dict[str, Any]]:
        """Return active leases as neutral dicts.

        Each dict: ``{"ip_address", "mac_address", "hostname", "client_id",
        "state", "expires_at"}`` — same shape the Kea driver produces so
        the upsert service doesn't branch.
        """
        creds = _load_credentials(server)
        raw = await asyncio.to_thread(_run_ps, server, creds, _PS_LIST_LEASES)
        return _parse_leases(raw)

    async def get_scopes(self, server: Any) -> list[dict[str, Any]]:
        """Return a read-only snapshot of every IPv4 scope on the server.

        Each scope dict:

            {
              "scope_id":            "192.168.30.0",
              "subnet_cidr":         "192.168.30.0/24",
              "name":                "...",
              "description":         "...",
              "lease_time":          86400,
              "is_active":           True,
              "options":             {"routers": [...], "dns-servers": [...], ...},
              "pools":               [
                  {"start_ip", "end_ip", "pool_type": "dynamic"},
                  {"start_ip", "end_ip", "pool_type": "excluded"},  # from exclusions
              ],
              "statics":             [
                  {"ip_address", "mac_address", "hostname", "client_id", "description"},
              ],
            }

        All writes are external — the service layer upserts these into
        ``DHCPScope`` / ``DHCPPool`` / ``DHCPStaticAssignment`` only for
        scopes whose CIDR matches an existing IPAM ``Subnet``. No auto-
        subnet creation.
        """
        creds = _load_credentials(server)
        raw = await asyncio.to_thread(_run_ps, server, creds, _PS_LIST_TOPOLOGY)
        return _parse_scopes(raw)

    # ── writes (per-object, surgical) ──────────────────────────────────
    #
    # Path B, trimmed. Instead of the full "apply config bundle" contract
    # that Kea satisfies (render_config + apply_config + reload), we
    # implement per-object cmdlets called directly from the SpatiumDDI
    # scope / pool / static API endpoints. Each method is idempotent:
    # upserts where Windows already has the object, creates otherwise.
    #
    # All inputs are escaped via the ``_ps_literal`` / base64 pattern —
    # opaque user values (option values, hostnames, descriptions) are
    # shipped as a base64-encoded JSON blob, never interpolated raw.

    async def apply_scope(
        self,
        server: Any,
        *,
        scope_id: str,
        subnet_mask: str,
        start_range: str,
        end_range: str,
        name: str,
        description: str,
        lease_seconds: int,
        is_active: bool,
        options: dict[str, Any],
    ) -> None:
        """Create or update a scope on Windows DHCP.

        ``options`` is keyed by our canonical names (``routers``,
        ``dns-servers``, …). We translate back to option IDs here and
        ship the whole desired-option-set; existing options not in the
        desired set are removed so Windows matches our DB exactly.
        """
        id_options = _options_by_id(options)
        payload_b64 = base64.b64encode(json.dumps({"options": id_options}).encode("utf-8")).decode(
            "ascii"
        )
        state = "Active" if is_active else "InActive"
        script = f"""
$ErrorActionPreference = 'Stop'
$scopeId    = {_ps_literal(scope_id)}
$mask       = {_ps_literal(subnet_mask)}
$startRange = {_ps_literal(start_range)}
$endRange   = {_ps_literal(end_range)}
$name       = {_ps_literal(name)}
$desc       = {_ps_literal(description)}
$lease      = New-TimeSpan -Seconds {int(lease_seconds)}
$state      = {_ps_literal(state)}
$payload    = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{payload_b64}'))
$data       = $payload | ConvertFrom-Json

$existing = Get-DhcpServerv4Scope -ScopeId $scopeId -ErrorAction SilentlyContinue
if ($existing) {{
    Set-DhcpServerv4Scope -ScopeId $scopeId -Name $name -Description $desc `
        -LeaseDuration $lease -State $state `
        -StartRange $startRange -EndRange $endRange
}} else {{
    Add-DhcpServerv4Scope -Name $name -Description $desc `
        -StartRange $startRange -EndRange $endRange `
        -SubnetMask $mask -LeaseDuration $lease -State $state
}}

# Reconcile option values — #426: set-then-prune (NOT wipe-then-set), so a
# failure mid-reconcile under $ErrorActionPreference='Stop' can never leave
# the scope with its options wiped and not re-applied. Apply every desired
# option first (upsert), then remove only the options Windows still has that
# we no longer want.
$desiredIds = @()
if ($data.options) {{
    $data.options.PSObject.Properties | ForEach-Object {{
        $optId = [int]$_.Name
        $desiredIds += $optId
        $value = $_.Value
        if ($value -isnot [array]) {{ $value = @($value) }}
        # Set-DhcpServerv4OptionValue is upsert semantics.
        Set-DhcpServerv4OptionValue -ScopeId $scopeId -OptionId $optId -Value $value
    }}
}}
Get-DhcpServerv4OptionValue -ScopeId $scopeId -ErrorAction SilentlyContinue | ForEach-Object {{
    if ($desiredIds -notcontains [int]$_.OptionId) {{
        Remove-DhcpServerv4OptionValue -ScopeId $scopeId -OptionId $_.OptionId -ErrorAction SilentlyContinue
    }}
}}
"OK"
"""
        creds = _load_credentials(server)
        await asyncio.to_thread(_run_ps, server, creds, script)

    async def remove_scope(self, server: Any, scope_id: str) -> None:
        """Delete a scope on Windows DHCP. ``Remove-DhcpServerv4Scope -Force``."""
        script = f"""
$ErrorActionPreference = 'Stop'
Remove-DhcpServerv4Scope -ScopeId {_ps_literal(scope_id)} -Force
"OK"
"""
        creds = _load_credentials(server)
        await asyncio.to_thread(_run_ps, server, creds, script)

    async def apply_reservation(
        self,
        server: Any,
        *,
        scope_id: str,
        ip_address: str,
        mac_address: str,
        hostname: str = "",
        description: str = "",
    ) -> None:
        """Upsert a DHCP reservation. Windows keys reservations by ClientId
        (MAC with dashes); if one exists for that MAC we Set-, else Add-.
        """
        client_id = mac_address.lower().replace(":", "-")
        script = f"""
$ErrorActionPreference = 'Stop'
$scopeId  = {_ps_literal(scope_id)}
$ip       = {_ps_literal(ip_address)}
$clientId = {_ps_literal(client_id)}
$name     = {_ps_literal(hostname)}
$desc     = {_ps_literal(description)}

$existing = Get-DhcpServerv4Reservation -ScopeId $scopeId -ErrorAction SilentlyContinue |
    Where-Object {{ $_.ClientId -eq $clientId }}
if ($existing) {{
    Set-DhcpServerv4Reservation -ClientId $clientId -ScopeId $scopeId `
        -IPAddress $ip -Name $name -Description $desc
}} else {{
    Add-DhcpServerv4Reservation -ScopeId $scopeId -IPAddress $ip `
        -ClientId $clientId -Name $name -Description $desc
}}
"OK"
"""
        creds = _load_credentials(server)
        await asyncio.to_thread(_run_ps, server, creds, script)

    async def remove_reservation(self, server: Any, *, scope_id: str, mac_address: str) -> None:
        """Delete a reservation by MAC (ClientId)."""
        client_id = mac_address.lower().replace(":", "-")
        script = f"""
$ErrorActionPreference = 'Stop'
Remove-DhcpServerv4Reservation -ScopeId {_ps_literal(scope_id)} `
    -ClientId {_ps_literal(client_id)} -ErrorAction SilentlyContinue
"OK"
"""
        creds = _load_credentials(server)
        await asyncio.to_thread(_run_ps, server, creds, script)

    async def apply_exclusion(
        self, server: Any, *, scope_id: str, start_ip: str, end_ip: str
    ) -> None:
        """Add an exclusion range, idempotently.

        #426: check-then-act on the (start, end) pair instead of matching
        the English substring 'already' in the error — the real Windows
        overlap message is locale-dependent ("…overlaps with an existing
        exclusion range") and has no 'already', so the old guard re-threw
        on every re-apply on a non-English host.
        """
        script = f"""
$ErrorActionPreference = 'Stop'
$scopeId = {_ps_literal(scope_id)}
$start   = {_ps_literal(start_ip)}
$end     = {_ps_literal(end_ip)}
$exists = Get-DhcpServerv4ExclusionRange -ScopeId $scopeId -ErrorAction SilentlyContinue |
    Where-Object {{ [string]$_.StartRange -eq $start -and [string]$_.EndRange -eq $end }}
if (-not $exists) {{
    Add-DhcpServerv4ExclusionRange -ScopeId $scopeId -StartRange $start -EndRange $end
}}
"OK"
"""
        creds = _load_credentials(server)
        await asyncio.to_thread(_run_ps, server, creds, script)

    async def remove_exclusion(
        self, server: Any, *, scope_id: str, start_ip: str, end_ip: str
    ) -> None:
        """Remove an exclusion range by its (start, end) pair."""
        script = f"""
$ErrorActionPreference = 'Stop'
Remove-DhcpServerv4ExclusionRange -ScopeId {_ps_literal(scope_id)} `
    -StartRange {_ps_literal(start_ip)} -EndRange {_ps_literal(end_ip)} -ErrorAction SilentlyContinue
"OK"
"""
        creds = _load_credentials(server)
        await asyncio.to_thread(_run_ps, server, creds, script)

    # ── batch writes ───────────────────────────────────────────────────
    #
    # The singular ``apply_reservation`` / ``remove_reservation`` /
    # ``apply_exclusion`` methods open a fresh WinRM session and ship
    # one cmdlet per call — perfect for one-off UI edits but punishing
    # for many-at-once paths (initial scope import, bulk static
    # conversion, pool rewrites). These batch methods pack ops into one
    # data-only PowerShell script per WinRM round trip — sized under the
    # encoded-command budget (#426) — and parse per-op ``{ok, error}`` back.
    #
    # Per-op failures surface as ``ReservationResult(ok=False)`` — the
    # caller decides whether to 500 or partial-success. Whole-batch
    # errors (auth, connection refused, PS parse error) raise.
    #
    # Not offering ``apply_scopes`` (plural): scope edits arrive from
    # the UI one at a time and the singular ``apply_scope`` already
    # resets all options for that scope in one round-trip. The payoff
    # for a plural would be marginal and the single-scope-per-UI-edit
    # pattern makes per-scope error attribution cleaner.

    async def apply_reservations(
        self, server: Any, *, items: Sequence[ReservationItem]
    ) -> list[ReservationResult]:
        if not items:
            return []
        creds = _load_credentials(server)
        return await _dispatch_dhcp_batch(
            server=server,
            creds=creds,
            items=items,
            op_name="apply_reservations",
            data_fn=_data_apply_reservation,
            op_body=_PS_BODY_APPLY_RESERVATION,
            result_ctor=lambda ok, item, error: ReservationResult(ok=ok, item=item, error=error),
            chunk_log="dhcp_apply_reservations_batch",
        )

    async def remove_reservations(
        self, server: Any, *, items: Sequence[RemoveReservationItem]
    ) -> list[ReservationResult]:
        if not items:
            return []
        creds = _load_credentials(server)
        return await _dispatch_dhcp_batch(
            server=server,
            creds=creds,
            items=items,
            op_name="remove_reservations",
            data_fn=_data_remove_reservation,
            op_body=_PS_BODY_REMOVE_RESERVATION,
            result_ctor=lambda ok, item, error: ReservationResult(ok=ok, item=item, error=error),
            chunk_log="dhcp_remove_reservations_batch",
        )

    async def apply_exclusions(
        self, server: Any, *, items: Sequence[ExclusionItem]
    ) -> list[ExclusionResult]:
        if not items:
            return []
        creds = _load_credentials(server)
        return await _dispatch_dhcp_batch(
            server=server,
            creds=creds,
            items=items,
            op_name="apply_exclusions",
            data_fn=_data_apply_exclusion,
            op_body=_PS_BODY_APPLY_EXCLUSION,
            result_ctor=lambda ok, item, error: ExclusionResult(ok=ok, item=item, error=error),
            chunk_log="dhcp_apply_exclusions_batch",
        )

    # ── MAC blocklist (deny filter) ───────────────────────────────────

    async def sync_mac_blocks(
        self, server: Any, *, desired: Sequence[MACBlockDef]
    ) -> tuple[int, int]:
        """Reconcile this server's Windows DHCP deny filter list.

        Pulls the current deny-list via ``Get-DhcpServerv4Filter -List
        Deny``, diffs against ``desired``, and issues one batched PS
        script that adds missing entries and removes extras. Filters are
        server-global on Windows — this one PS script covers every scope.

        Returns ``(added, removed)``. Whole-batch errors raise so the
        caller can surface the failure in audit + health rather than
        silently drift.
        """
        creds = _load_credentials(server)

        # Windows wants dash-separated MACs (``AA-BB-CC-DD-EE-FF``) on
        # both write and read. We canonicalize inputs to that form and
        # keep a mapping back to the original (colon-separated) so the
        # audit log references the shape operators typed.
        dash_by_colon: dict[str, str] = {}
        desired_set: set[str] = set()
        desc_by_dash: dict[str, str] = {}
        for mb in desired:
            colon = mb.mac_address.lower()
            dash = colon.replace(":", "-").upper()
            desired_set.add(dash)
            dash_by_colon[colon] = dash
            desc_by_dash[dash] = mb.description or mb.reason or ""

        existing_raw = await asyncio.to_thread(
            _run_ps,
            server,
            creds,
            "Get-DhcpServerv4Filter -List Deny | "
            "Select-Object @{N='mac';E={$_.MacAddress}}, Description | "
            "ConvertTo-Json -Compress",
        )
        existing_set: set[str] = set()
        text = (existing_raw or "").strip()
        if text:
            parsed = json.loads(text)
            rows = parsed if isinstance(parsed, list) else [parsed]
            for row in rows:
                mac = str(row.get("mac", "")).upper()
                if mac:
                    existing_set.add(mac)

        to_add = sorted(desired_set - existing_set)
        to_remove = sorted(existing_set - desired_set)
        if not to_add and not to_remove:
            return (0, 0)

        # One round trip for both adds and removes. ``-ErrorAction
        # SilentlyContinue`` on each swallows the "already exists" /
        # "not found" races that naturally occur when multiple agents
        # converge on the same state.
        lines: list[str] = ["$ErrorActionPreference = 'Continue'"]
        for mac in to_add:
            desc = desc_by_dash.get(mac, "")
            lines.append(
                f"Add-DhcpServerv4Filter -List Deny -MacAddress {_ps_literal(mac)} "
                f"-Description {_ps_literal(desc)} -ErrorAction SilentlyContinue"
            )
        for mac in to_remove:
            lines.append(
                f"Remove-DhcpServerv4Filter -List Deny -MacAddress {_ps_literal(mac)} "
                "-ErrorAction SilentlyContinue"
            )
        lines.append('"OK"')
        await asyncio.to_thread(_run_ps, server, creds, "\n".join(lines))

        logger.info(
            "dhcp_mac_blocks_sync",
            server=str(getattr(server, "id", "")),
            added=len(to_add),
            removed=len(to_remove),
        )
        return (len(to_add), len(to_remove))

    # ── reads ──────────────────────────────────────────────────────────
    # (health_check continues below.)

    async def health_check(self, server: Any) -> tuple[bool, str]:
        """WinRM round-trip probe using ``Get-DhcpServerVersion``."""
        try:
            creds = _load_credentials(server)
            out = await asyncio.to_thread(
                _run_ps, server, creds, "(Get-DhcpServerVersion).ToString()"
            )
            return True, f"windows_dhcp reachable ({out.strip()})"
        except Exception as exc:  # noqa: BLE001 — surface any WinRM error
            return False, f"windows_dhcp health-check failed: {exc}"

    # ── Logs — Windows Event Log reads ────────────────────────────────

    def available_log_names(self) -> list[tuple[str, str]]:
        """Surface the event logs this driver knows about.

        Returns ``[(log_name, display)]``. The ``log_name`` is what
        ``Get-WinEvent -LogName`` takes; ``display`` is what the UI
        shows in the source picker. Both logs are cheap to expose:
        any missing log returns ``[]`` via the shared helper's
        try/catch around ``EventLogException``.
        """
        return [
            ("Microsoft-Windows-Dhcp-Server/Operational", "DHCP Server — Operational"),
            (
                "Microsoft-Windows-Dhcp-Server/FilterNotifications",
                "DHCP Server — Filter Notifications",
            ),
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
        """Query a Windows Event Log over WinRM.

        Thin wrapper around :func:`app.drivers.windows_events.fetch_events`
        — exists so the Logs API can call the driver abstraction instead
        of importing the helper directly (CLAUDE.md non-negotiable #10).
        """
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

    # ── DHCP audit log (per-lease events) ──────────────────────────────

    async def get_dhcp_audit_events(
        self,
        server: Any,
        *,
        day: str | None = None,
        max_events: int = 500,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Read the Windows DHCP audit log for ``day``.

        The audit log (``C:\\Windows\\System32\\dhcp\\DhcpSrvLog-<Day>.log``)
        is the per-lease event trail — grants, renewals, releases,
        conflict detections, DNS update results. Different schema
        from the Windows Event Log (which only covers service-level
        events), so this is exposed as a separate endpoint. Returns
        ``(rows, truncated)``.
        """
        from app.drivers.windows_dhcp_audit import fetch_dhcp_audit_events  # noqa: PLC0415

        creds = _load_credentials(server)
        return await fetch_dhcp_audit_events(
            server,
            creds,
            run_ps=_run_ps,
            day=day,
            max_events=max_events,
        )

    def capabilities(self) -> dict[str, Any]:
        return {
            # No full-bundle config push (render_config / apply_config /
            # reload / restart stay unimplemented — those aren't how
            # Windows DHCP reconfigures). But individual scope / pool /
            # reservation writes ARE supported via direct WinRM cmdlets.
            "read_only": False,
            "bundle_config_push": False,
            "lease_monitoring": True,
            "scope_import": True,
            "scope_management": True,
            "reservation_management": True,
            "address_families": ["ipv4"],
            "transport": "winrm",
        }

    # ── writes — intentionally unimplemented (Path B) ─────────────────

    def render_config(self, bundle: ConfigBundle) -> str:
        raise NotImplementedError("windows_dhcp is read-only (Path A); config management is Path B")

    async def apply_config(self, server: Any, bundle: ConfigBundle) -> None:
        raise NotImplementedError("windows_dhcp is read-only (Path A); apply_config is Path B")

    async def reload(self, server: Any) -> None:
        raise NotImplementedError("windows_dhcp is read-only (Path A); reload is Path B")

    async def restart(self, server: Any) -> None:
        raise NotImplementedError("windows_dhcp is read-only (Path A); restart is Path B")

    def validate_config(self, bundle: ConfigBundle) -> tuple[bool, list[str]]:
        return False, ["windows_dhcp is read-only (Path A); validate_config is Path B"]


# ── helpers ───────────────────────────────────────────────────────────


def _load_credentials(server: Any) -> dict[str, Any]:
    """Decrypt and return the WinRM cred dict from ``server.credentials_encrypted``.

    Raises ``ValueError`` if the server has no credentials set — with a
    message the scheduled task can include in its error summary.
    """
    blob = getattr(server, "credentials_encrypted", None)
    if not blob:
        raise ValueError(
            f"DHCP server {server.name!r} has no Windows credentials set; "
            "configure username/password before enabling lease sync."
        )
    return decrypt_dict(blob)


def _run_ps(server: Any, creds: dict[str, Any], script: str) -> str:
    """Run a PowerShell script on the Windows DHCP server over WinRM.

    Thin wrapper over the shared :func:`app.drivers._winrm.run_ps`
    chokepoint (transport/TLS/timeout/size-budget). Blocking — call via
    ``asyncio.to_thread``.
    """
    return _winrm_run_ps(getattr(server, "host", ""), creds, script, op_label="dhcp")


def _parse_leases(raw: str) -> list[dict[str, Any]]:
    """Parse the ``ConvertTo-Json -Compress`` output from ``_PS_LIST_LEASES``.

    Windows returns an empty string for zero leases, a single object (not
    wrapped in an array) for exactly one lease, and an array for 2+. We
    normalise all three to a list, then filter and shape into neutral
    dicts.
    """
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        # #426: a garbled / truncated / warning-prefixed (but exit-0)
        # payload must NOT read as "zero leases" — that's indistinguishable
        # from an empty server and drives pull_leases' absence-delete to
        # purge every active lease + IPAM mirror (stamping the audit row
        # success). Re-raise so the reconcile aborts and leaves data intact.
        logger.warning("windows_dhcp_lease_parse_failed", raw=text[:400], error=str(exc))
        raise RuntimeError(f"Windows DHCP lease response was not valid JSON: {exc}") from exc
    items: list[dict[str, Any]] = parsed if isinstance(parsed, list) else [parsed]

    out: list[dict[str, Any]] = []
    for item in items:
        state = item.get("AddressState") or ""
        if state not in _ACTIVE_STATES:
            continue
        ip = item.get("IPAddress")
        client_id = item.get("ClientId") or ""
        if not ip or not client_id:
            continue
        mac = _normalise_mac(client_id)
        if mac is None:
            continue
        out.append(
            {
                "ip_address": str(ip),
                "mac_address": mac,
                "hostname": item.get("HostName") or None,
                "client_id": client_id,
                "state": "active",
                "expires_at": _parse_iso(item.get("LeaseExpiryTime")),
            }
        )
    return out


def _parse_scopes(raw: str) -> list[dict[str, Any]]:
    """Shape PowerShell's scope + options + exclusions + reservations JSON
    into the neutral dicts the service layer expects.

    Computes the CIDR prefix length from the dotted-quad subnet mask here
    rather than in PowerShell (Python's ``ipaddress`` is much cleaner).
    Maps Windows option IDs to canonical SpatiumDDI option names via
    ``_OPTION_ID_TO_NAME``; unmapped IDs become ``opt-<id>`` so they're
    not silently dropped.
    """
    import ipaddress  # noqa: PLC0415

    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("windows_dhcp_scope_parse_failed", raw=text[:400], error=str(exc))
        return []

    items: list[dict[str, Any]] = parsed if isinstance(parsed, list) else [parsed]

    out: list[dict[str, Any]] = []
    for item in items:
        scope_id = item.get("scope_id")
        mask = item.get("subnet_mask")
        if not scope_id or not mask:
            continue
        try:
            prefix = ipaddress.IPv4Network(f"{scope_id}/{mask}", strict=False).prefixlen
            cidr = f"{scope_id}/{prefix}"
        except (ValueError, TypeError):
            continue

        options = _translate_options(item.get("options") or {})

        pools: list[dict[str, Any]] = []
        start = item.get("start_range")
        end = item.get("end_range")
        if start and end:
            pools.append({"start_ip": start, "end_ip": end, "pool_type": "dynamic"})
        for ex in item.get("exclusions") or []:
            if ex.get("start_ip") and ex.get("end_ip"):
                pools.append(
                    {
                        "start_ip": ex["start_ip"],
                        "end_ip": ex["end_ip"],
                        "pool_type": "excluded",
                    }
                )

        statics: list[dict[str, Any]] = []
        for r in item.get("reservations") or []:
            mac = _normalise_mac(r.get("client_id") or "")
            if not r.get("ip_address") or mac is None:
                continue
            statics.append(
                {
                    "ip_address": r["ip_address"],
                    "mac_address": mac,
                    "hostname": r.get("name") or "",
                    "client_id": r.get("client_id"),
                    "description": r.get("description") or "",
                }
            )

        out.append(
            {
                "scope_id": scope_id,
                "subnet_cidr": cidr,
                "name": item.get("name") or "",
                "description": item.get("description") or "",
                "lease_time": int(item.get("lease_duration_seconds") or 86400),
                "is_active": (item.get("state") or "").lower() == "active",
                "options": options,
                "pools": pools,
                "statics": statics,
            }
        )
    return out


def _translate_options(raw_options: dict[str, Any]) -> dict[str, Any]:
    """Windows option-id keyed dict → SpatiumDDI option-name keyed dict.

    Single-value options unwrap from the PS-always-emits-array form.
    Unmapped option IDs fall through as ``opt-<id>`` so they survive the
    round-trip; the UI can still show them as raw.
    """
    result: dict[str, Any] = {}
    for key, payload in raw_options.items():
        try:
            opt_id = int(key)
        except (ValueError, TypeError):
            continue
        name = _OPTION_ID_TO_NAME.get(opt_id, f"opt-{opt_id}")
        value = payload.get("value") if isinstance(payload, dict) else payload
        # PS `@($o.Value)` always wraps to array. Unwrap when the value is
        # fundamentally scalar (domain-name, tftp-server-name, …).
        if isinstance(value, list):
            if name in {"domain-name", "tftp-server-name", "bootfile-name"} and len(value) == 1:
                value = value[0]
        result[name] = value
    return result


# Windows DHCP exposes ClientId in the format "aa-bb-cc-dd-ee-ff" (and
# occasionally prefixed with a "01-" hardware-type byte for some clients).
# Postgres MACADDR wants colon- or dash-separated 6-byte form.
_MAC_HEX_RE = re.compile(r"^([0-9a-f]{2})([-:]?([0-9a-f]{2})){5}$", re.IGNORECASE)


def _normalise_mac(client_id: str) -> str | None:
    raw = client_id.strip().lower()
    # Strip DHCP hardware-type prefix ("01-" = Ethernet) if we got 7 octets.
    parts = re.split(r"[-:]", raw)
    if len(parts) == 7 and parts[0] == "01":
        parts = parts[1:]
    if len(parts) != 6:
        return None
    if not all(re.fullmatch(r"[0-9a-f]{2}", p) for p in parts):
        return None
    return ":".join(parts)


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        # PowerShell "o" format: 2026-04-17T21:12:00.0000000+00:00
        s = str(value)
        # Python's fromisoformat up to 3.12 handles "+00:00" but not the
        # 7-digit fractional; truncate to 6.
        if "." in s:
            head, dot, tail = s.partition(".")
            frac_and_tz = tail
            # Split tz off (may be +HH:MM, -HH:MM, or Z).
            tz_match = re.search(r"[+\-Z]", frac_and_tz)
            if tz_match:
                idx = tz_match.start()
                frac, tz = frac_and_tz[:idx], frac_and_tz[idx:]
            else:
                frac, tz = frac_and_tz, ""
            frac = frac[:6]
            s = f"{head}.{frac}{tz}"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _ps_literal(value: str) -> str:
    """Return ``value`` quoted as a PowerShell single-quoted literal.

    Single quotes inside the string are doubled — PS's own escape rule
    for single-quoted literals. No backtick-based escaping, no variable
    expansion, no injection surface: whatever we pass survives intact.
    Use this for every user-controlled string that goes into a script.
    """
    return "'" + (value or "").replace("'", "''") + "'"


# Option-name → Windows option ID. Inverse of _OPTION_ID_TO_NAME above.
_OPTION_NAME_TO_ID: dict[str, int] = {name: oid for oid, name in _OPTION_ID_TO_NAME.items()}


def _options_by_id(options: dict[str, Any]) -> dict[int, Any]:
    """Translate SpatiumDDI option-name dict to Windows option-ID dict.

    Names that don't map to a known ID are silently dropped — Windows
    DHCP only accepts registered option IDs, and ``opt-<id>`` keys from
    the import path carry the id in the suffix so they round-trip fine.
    """
    out: dict[int, Any] = {}
    for name, value in (options or {}).items():
        if name in _OPTION_NAME_TO_ID:
            out[_OPTION_NAME_TO_ID[name]] = value
            continue
        # Handle ``opt-<id>`` passthrough emitted by the importer for
        # options we don't canonicalise.
        if name.startswith("opt-"):
            try:
                out[int(name[4:])] = value
            except ValueError:
                pass
    return out


async def test_winrm_credentials(host: str, credentials: dict[str, Any]) -> tuple[bool, str]:
    """Dry-run probe: can we reach ``host`` over WinRM with ``credentials``
    and run ``Get-DhcpServerVersion``? Used by the create/edit modal's
    "Test Connection" button so credential issues surface before save.

    Returns ``(ok, message)`` — message is either the DHCP server version
    on success, or the WinRM/PowerShell error on failure.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    fake = SimpleNamespace(name="<test>", host=host)
    try:
        raw = await asyncio.to_thread(
            _run_ps, fake, credentials, "(Get-DhcpServerVersion).ToString()"
        )
        return True, f"windows_dhcp reachable ({raw.strip()})"
    except Exception as exc:  # noqa: BLE001 — surface any transport/PS error verbatim
        return False, str(exc)


# ── batch dispatch helpers ───────────────────────────────────────────
#
# Shared between apply_reservations / remove_reservations /
# apply_exclusions. #426: each call site passes a ``data_fn`` (item →
# small JSON-safe dict of FIELDS) and an ``op_body`` (a FIXED PowerShell
# snippet that operates on ``$op.<field>``). The dispatcher ships a
# data-only payload + ONE embedded copy of ``op_body`` per chunk and
# data-binds operator values through ConvertFrom-Json, so (a) the
# payload stays tiny and (b) nothing operator-controlled ever touches
# the PS parser. Chunks are packed under the encoded-command-line budget
# (see app/drivers/_winrm.py), not a fixed op count.

# apply_reservation — upsert keyed on ClientId (MAC with dashes).
_PS_BODY_APPLY_RESERVATION = """
$existing = Get-DhcpServerv4Reservation -ScopeId $op.scopeId -ErrorAction SilentlyContinue |
    Where-Object { $_.ClientId -eq $op.clientId }
if ($existing) {
    Set-DhcpServerv4Reservation -ClientId $op.clientId -ScopeId $op.scopeId `
        -IPAddress $op.ip -Name $op.name -Description $op.desc -ErrorAction Stop
} else {
    Add-DhcpServerv4Reservation -ScopeId $op.scopeId -IPAddress $op.ip `
        -ClientId $op.clientId -Name $op.name -Description $op.desc -ErrorAction Stop
}
""".strip()

_PS_BODY_REMOVE_RESERVATION = """
Remove-DhcpServerv4Reservation -ScopeId $op.scopeId -ClientId $op.clientId `
    -ErrorAction SilentlyContinue
""".strip()

# apply_exclusion — #426: check-then-act on the (start, end) pair so a
# re-apply is idempotent on ANY locale (the old code matched the English
# substring 'already', which the localized overlap message lacks).
_PS_BODY_APPLY_EXCLUSION = """
$exists = Get-DhcpServerv4ExclusionRange -ScopeId $op.scopeId -ErrorAction SilentlyContinue |
    Where-Object { [string]$_.StartRange -eq $op.startRange -and [string]$_.EndRange -eq $op.endRange }
if (-not $exists) {
    Add-DhcpServerv4ExclusionRange -ScopeId $op.scopeId `
        -StartRange $op.startRange -EndRange $op.endRange -ErrorAction Stop
}
""".strip()


def _data_apply_reservation(item: ReservationItem) -> dict[str, Any]:
    return {
        "scopeId": item.scope_id,
        "ip": item.ip_address,
        "clientId": item.mac_address.lower().replace(":", "-"),
        "name": item.hostname or "",
        "desc": item.description or "",
    }


def _data_remove_reservation(item: RemoveReservationItem) -> dict[str, Any]:
    return {
        "scopeId": item.scope_id,
        "clientId": item.mac_address.lower().replace(":", "-"),
    }


def _data_apply_exclusion(item: ExclusionItem) -> dict[str, Any]:
    return {
        "scopeId": item.scope_id,
        "startRange": item.start_ip,
        "endRange": item.end_ip,
    }


def _build_dhcp_batch_script(op_body: str, payload: list[dict[str, Any]]) -> str:
    """Wrap a data-only ``payload`` + a fixed ``op_body`` into one batch
    script. ``op_body`` reads ``$op.<field>`` (data-bound, never parsed)."""
    payload_b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return f"""
$ErrorActionPreference = 'Continue'
$payload = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String('{payload_b64}'))
$ops = $payload | ConvertFrom-Json
$results = @()
foreach ($op in $ops) {{
    $entry = [ordered]@{{ index = [int]$op.index; ok = $false; error = $null }}
    try {{
{op_body}
        $entry.ok = $true
    }} catch {{
        $entry.error = "$($_.Exception.Message)"
    }}
    $results += (New-Object PSObject -Property $entry)
}}
$results | ConvertTo-Json -Compress -Depth 3
""".strip()


def _pack_dhcp_chunks(
    items: Sequence[Any], data_fn: Any, op_body: str
) -> list[list[tuple[int, Any]]]:
    """Greedily pack ``items`` into chunks whose built script stays under
    the encoded-command budget (and the ``_WINRM_MAX_BATCH_OPS`` sanity
    cap). Each chunk is a list of ``(global_index, item)``. A single op
    that can't fit even alone still ships as a one-op chunk — _run_ps
    raises WinRMCommandTooLong for it, which the dispatcher turns into a
    failed result for that one op without aborting the rest."""
    chunks: list[list[tuple[int, Any]]] = []
    current: list[tuple[int, Any]] = []

    def fits(candidate: list[tuple[int, Any]]) -> bool:
        payload = [{"index": i, **data_fn(it)} for i, (_, it) in enumerate(candidate)]
        return (
            encoded_command_len(_build_dhcp_batch_script(op_body, payload)) <= MAX_ENCODED_COMMAND
        )

    for gidx, item in enumerate(items):
        trial = current + [(gidx, item)]
        if current and (len(trial) > _WINRM_MAX_BATCH_OPS or not fits(trial)):
            chunks.append(current)
            current = [(gidx, item)]
        else:
            current = trial
    if current:
        chunks.append(current)
    return chunks


async def _dispatch_dhcp_batch(
    *,
    server: Any,
    creds: dict[str, Any],
    items: Sequence[Any],
    op_name: str,
    data_fn: Any,
    op_body: str,
    result_ctor: Any,
    chunk_log: str,
) -> list[Any]:
    """Pack ``items`` into encoded-budget-sized chunks, dispatch one
    data-only PowerShell script per chunk, and zip per-op results back.

    Each op gets a JSON record ``{index, ok, error}``; we parse those
    back and call ``result_ctor(ok, item, error)`` to build the
    driver-typed per-op result. An over-budget single op (e.g. an absurd
    hostname/description) fails just that op; any item missing from the
    PS output becomes a failed result, so the returned list length always
    matches the input length.
    """
    chunks = _pack_dhcp_chunks(items, data_fn, op_body)
    total_chunks = len(chunks)
    all_results: list[Any] = []

    for chunk_index, chunk in enumerate(chunks):
        chunk_items = [it for _, it in chunk]
        payload = [{"index": idx, **data_fn(it)} for idx, (_, it) in enumerate(chunk)]
        script = _build_dhcp_batch_script(op_body, payload)

        try:
            raw = await asyncio.to_thread(_run_ps, server, creds, script)
            chunk_results = _parse_dhcp_batch_results(raw, chunk_items, result_ctor)
        except WinRMCommandTooLong as exc:
            # Single over-budget op (a 1-item chunk that still won't fit) —
            # fail just it; never abort the rest of the batch.
            chunk_results = [result_ctor(False, it, str(exc)) for it in chunk_items]

        ok_count = sum(1 for r in chunk_results if r.ok)
        failed_count = len(chunk_results) - ok_count
        logger.info(
            chunk_log,
            server=str(getattr(server, "id", "")),
            count=len(chunk_results),
            ok_count=ok_count,
            failed_count=failed_count,
            chunk_index=chunk_index,
            chunks=total_chunks,
            op=op_name,
        )
        all_results.extend(chunk_results)

    return all_results


def _parse_dhcp_batch_results(raw: str, chunk: Sequence[Any], result_ctor: Any) -> list[Any]:
    """Parse the ``ConvertTo-Json -Compress`` output from a batch script.

    Same normalisation as the DNS side (empty string → single object →
    array) and same index-keyed matching so duplicate items in the
    input list don't collide.
    """
    text = (raw or "").strip()
    if not text:
        return [result_ctor(False, item, "batch returned no output") for item in chunk]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("windows_dhcp_batch_parse_failed", raw=text[:400], error=str(exc))
        return [result_ctor(False, item, f"batch result parse failed: {exc}") for item in chunk]
    items_out: list[dict[str, Any]] = parsed if isinstance(parsed, list) else [parsed]

    by_index: dict[int, Any] = {}
    for out in items_out:
        raw_idx = out.get("index")
        if raw_idx is None:
            continue
        try:
            idx = int(raw_idx)
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(chunk)):
            continue
        ok = bool(out.get("ok"))
        error_raw = out.get("error")
        by_index[idx] = result_ctor(
            ok,
            chunk[idx],
            None if ok or error_raw in (None, "") else str(error_raw),
        )

    return [
        by_index.get(i, result_ctor(False, chunk[i], "internal: no result returned for op"))
        for i in range(len(chunk))
    ]


__all__ = ["WindowsDHCPReadOnlyDriver", "test_winrm_credentials"]
