---
layout: default
title: DHCP Drivers
---

# DHCP Driver Specification

DHCP drivers are the backend-specific layer that turns SpatiumDDI's internal DHCP model into operations on real DHCP servers. The service layer only ever speaks to [`DHCPDriver`](../../backend/app/drivers/dhcp/base.py) (CLAUDE.md non-negotiable #10) — no Kea / PowerShell specifics leak above this line.

The driver registry ([`registry.py`](../../backend/app/drivers/dhcp/registry.py)) classifies drivers along two axes:

| Axis | Values | What it means |
|---|---|---|
| `AGENTLESS_DRIVERS` | `windows_dhcp` | Driver runs from the control plane. No co-located agent, no `ConfigBundle` long-poll. |
| `READ_ONLY_DRIVERS` | `windows_dhcp` | Driver implements lease reads only. Config push / reload / restart raise `NotImplementedError`. |

All other drivers (currently just `kea`) are agented: the control plane renders a `ConfigBundle`, hash-keyed by SHA-256 ETag, and the co-located `spatium-dhcp-agent` long-polls `/config` to pick up changes.

---

## 1. Driver shapes

Today's drivers split into two shapes:

```
┌──────────────────────────────┐     ┌──────────────────────────────┐
│  Agented + write             │     │  Agentless + read-only       │
│  (Kea)                       │     │  (Windows DHCP — Path A)     │
│                              │     │                              │
│  Control plane:              │     │  Control plane:              │
│    render_config() → bundle  │     │    get_leases() via WinRM    │
│    ETag long-poll            │     │    get_scopes() via WinRM    │
│                              │     │                              │
│  Agent (sidecar):            │     │  No agent.                   │
│    fetch bundle              │     │  Writes raise                │
│    apply_config()            │     │    NotImplementedError.      │
│    reload / restart          │     │                              │
└──────────────────────────────┘     └──────────────────────────────┘
```

The abstract base (`DHCPDriver`) has methods for both halves. Agentless drivers implement only the read methods + a stub `apply_config` / `reload` / `restart` / `validate_config` that raises; the API layer consults `READ_ONLY_DRIVERS` before offering write endpoints.

---

## 2. Abstract base class

Key methods on [`DHCPDriver`](../../backend/app/drivers/dhcp/base.py):

```python
class DHCPDriver(ABC):
    name: ClassVar[str]

    # Rendering (agented drivers).
    @abstractmethod
    def render_config(self, server: Any, bundle: ConfigBundle) -> str: ...

    # Applying on the server host (agent-side).
    @abstractmethod
    async def apply_config(self, server: Any, config: str) -> None: ...
    @abstractmethod
    async def reload(self, server: Any) -> None: ...
    @abstractmethod
    async def restart(self, server: Any) -> None: ...
    @abstractmethod
    async def validate_config(self, config: str) -> tuple[bool, str]: ...

    # Reads (all drivers).
    @abstractmethod
    async def get_leases(self, server: Any) -> list[LeaseData]: ...
    @abstractmethod
    async def get_scopes(self, server: Any) -> list[ScopeData]: ...

    # Write-through (Path B / optional per-object CRUD).
    async def apply_scope(self, server: Any, scope: ScopeDef) -> None: ...
    async def remove_scope(self, server: Any, subnet_cidr: str) -> None: ...
    async def apply_reservation(self, server: Any, subnet_cidr: str,
                                 reservation: StaticAssignmentDef) -> None: ...
    async def remove_reservation(self, server: Any, subnet_cidr: str,
                                  mac_address: str) -> None: ...
    async def apply_exclusion(self, server: Any, subnet_cidr: str,
                               pool: PoolDef) -> None: ...
    async def remove_exclusion(self, server: Any, subnet_cidr: str,
                                start_ip: str, end_ip: str) -> None: ...
```

Neutral data classes (`ScopeDef`, `PoolDef`, `StaticAssignmentDef`, `ClientClassDef`, `ConfigBundle`) are frozen dataclasses — hashing them gives the ETag that drives long-poll.

---

## 3. Kea driver (agented + write)

Located at [`app/drivers/dhcp/kea.py`](../../backend/app/drivers/dhcp/kea.py). Agent image: [`agent/dhcp/`](../../agent/dhcp/).

### Update strategy

| Operation | Mechanism | Notes |
|---|---|---|
| Full scope/pool/reservation push | `render_config()` → `ConfigBundle` → agent fetches via long-poll → agent writes `/etc/kea/kea-dhcp4.conf` → `kea-dhcp4 --test` → `config-reload` via Kea Control Agent | Incremental config — no daemon restart. |
| Validate | `kea-dhcp4 --test -c <rendered.conf>` | Driver method returns `(ok, message)`. |
| Read leases | Kea `lease_cmds` hook → HTTP POST to Kea Control Agent `/` with `command: lease4-get-all` | Real-time; falls back to polling if CA is unreachable. |
| Read scopes | Kea Control Agent `config-get` | Used by the /scopes read endpoints. |

Kea runs an HTTP Control Agent on `localhost:8000` (inside the agent pod/container). The agent drives Kea by:

1. Rendering the config bundle into Kea JSON (`Dhcp4` for IPv4, `Dhcp6` for IPv6 — address-family split on `DHCPScope.address_family`).
2. POSTing to `config-test` before `config-set` to catch validation errors early.
3. Calling `config-reload` which re-reads the file without dropping in-flight leases.

The IPv6 path renders a `Dhcp6` tree in parallel to `Dhcp4`. Dhcp6 option-name translation is marked TODO in the driver — today it passes option codes through unchanged.

### HA coordination

Kea's built-in `libdhcp_ha.so` hook handles pool coordination between paired servers. SpatiumDDI pairs two Kea servers in a **`DHCPFailoverChannel`** row — see `backend/app/models/dhcp.py` for the model, [`docs/features/DHCP.md` §14](../features/DHCP.md) for the feature-level spec.

- `_resolve_failover` in `backend/app/services/dhcp/config_bundle.py` emits a `FailoverConfig` on the `ConfigBundle` when the server belongs to a channel.
- The agent's `render_kea.py:_ha_hook()` injects `libdhcp_ha.so` alongside the always-loaded `libdhcp_lease_cmds.so` (the HA hook depends on it).
- Each peer's `this-server-name` is derived from its `DHCPServer.name`; the `peers` array carries both entries with roles `primary` + (`standby` in hot-standby / `secondary` in load-balancing) and the shared heartbeat / max-response / max-ack / max-unacked tuning from the channel.
- Agent-side `HAStatusPoller` (`agent/dhcp/spatium_dhcp_agent/ha_status.py`) calls `ha-status-get` every ~15 s and POSTs the state to the control plane — drives the live HA pill in the UI.
- The driver does **not** replicate leases itself — Kea's hook talks directly to the peer's `kea-ctrl-agent`. SpatiumDDI just renders the config.

### Agent bootstrap

Identical pattern to the DNS agent ([`DNS_AGENT.md`](../deployment/DNS_AGENT.md)):

1. Agent starts with `DHCP_AGENT_KEY` (PSK) in its environment.
2. Calls `POST /api/v1/dhcp/agents/bootstrap` with PSK → receives a per-server rotating JWT.
3. Long-polls `GET /api/v1/dhcp/agents/config?etag=<last>` with JWT.
4. On 401 or 404, re-bootstraps from the PSK.
5. Caches the last-good bundle under `/var/lib/spatium-dhcp-agent/`.

---

## 4. Windows DHCP driver (agentless + read-only, Path A)

Located at [`app/drivers/dhcp/windows.py`](../../backend/app/drivers/dhcp/windows.py). Class: `WindowsDHCPReadOnlyDriver`.

### Capabilities

| Operation | Status | How |
|---|---|---|
| `get_leases` | ✅ | `Get-DhcpServerv4Scope` → `Get-DhcpServerv4Lease` per scope via WinRM. |
| `get_scopes` | ✅ | `Get-DhcpServerv4Scope` + options + exclusions + reservations in one PowerShell call, JSON-serialised back. |
| `apply_scope` / `remove_scope` | ✅ | `Add-DhcpServerv4Scope` / `Remove-DhcpServerv4Scope`. Called per-object from the API — not via a bundle push. |
| `apply_reservation` / `remove_reservation` | ✅ | `Add-DhcpServerv4Reservation` / `Remove-DhcpServerv4Reservation`. |
| `apply_exclusion` / `remove_exclusion` | ✅ | `Add-DhcpServerv4ExclusionRange` / `Remove-DhcpServerv4ExclusionRange`. |
| `render_config` | ❌ | Raises `NotImplementedError`. Windows DHCP is cmdlet-driven, not config-file-driven. |
| `apply_config` / `reload` / `restart` / `validate_config` | ❌ | Raise `NotImplementedError`. |

The `/sync` endpoint (bundle push) rejects read-only drivers; the `/sync-leases-now` endpoint and per-object CRUD drive all writes instead.

### Credentials

Stored on `DHCPServer.credentials_encrypted` as a Fernet-encrypted JSON dict:

```json
{
  "username": "CORP\\spatium-dhcp",
  "password": "…",
  "winrm_port": 5985,
  "transport": "ntlm",
  "use_tls": false,
  "verify_tls": false
}
```

The service account needs to be in the Windows `DHCP Users` local group (read-only) or `DHCP Administrators` (for per-object writes). See [WINDOWS.md](../deployment/WINDOWS.md) for the account setup and WinRM configuration.

### PowerShell calls

The driver shells out to pre-built PowerShell strings with `$ErrorActionPreference = 'Stop'` and `ConvertTo-Json -Compress -Depth 3` for machine-readable output. An example — the lease pull:

```powershell
$scopes = Get-DhcpServerv4Scope | Where-Object { $_.State -eq 'Active' }
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
```

WinRM transport is `pywinrm` (`winrm.Session`), wrapped in `asyncio.to_thread` because `pywinrm` is synchronous. Transport, port, and TLS options come from the credential dict.

### Lease → IPAM mirror

Leases drive a scheduled Celery beat task ([`app.tasks.dhcp_pull_leases.auto_pull_dhcp_leases`](../../backend/app/tasks/dhcp_pull_leases.py)). Beat fires every 60s; the task gates on `PlatformSettings.dhcp_pull_leases_enabled` / `_interval_minutes`, so the UI can change cadence without restarting beat.

Per poll cycle:

1. Enumerate agentless DHCP servers.
2. For each, call `driver.get_leases(server)`.
3. Upsert into `DHCPLease` by `(server_id, ip_address)`.
4. If the lease's IP falls inside a known subnet, mirror it into `IPAddress` with `status="dhcp"` and `auto_from_lease=True`.
5. **Absence-delete** — any active `DHCPLease` row for this server whose IP didn't come back in the wire response is deleted along with its `auto_from_lease=True` IPAM mirror. The driver's `get_leases()` returns only currently-active leases, so absence means the server deleted it (admin purged / client released / etc.). `PullLeasesResult` gains `removed` + `ipam_revoked` counters; both flow into the scheduled-task audit row and the manual sync response. See [features/DHCP.md §15.3](../features/DHCP.md) for the rationale.
6. The time-based `dhcp_lease_cleanup` sweep still handles leases that drift past `expires_at` between polls. The two mechanisms overlap harmlessly.

### 4.5 Batched WinRM writes

Per-object writes against Windows DHCP used to round-trip WinRM **per reservation / exclusion** — a bulk delete of 200 reservations took minutes. The driver now groups writes into a single PowerShell script per `(server, scope)` chunk.

**Driver surface.** New plural methods on the ABC:

```python
class DHCPDriver(ABC):
    async def apply_reservations(
        self, scope_id: str, reservations: Sequence[Reservation]
    ) -> Sequence[OpResult]: ...

    async def remove_reservations(
        self, scope_id: str, reservations: Sequence[Reservation]
    ) -> Sequence[OpResult]: ...

    async def apply_exclusions(
        self, scope_id: str, exclusions: Sequence[Exclusion]
    ) -> Sequence[OpResult]: ...
```

Default ABC impls call the singular method in a loop — Kea inherits the plural interface without changes.

**Windows batch size — 30 ops per chunk.** `pywinrm.run_ps` ships the script as a single CMD.EXE command line (8191-char cap, see [DNS_DRIVERS.md §3.7](DNS_DRIVERS.md#37-batched-winrm-dispatch) for the full math). DHCP payloads are leaner than DNS — each reservation / exclusion op is ~60 raw chars of JSON vs. DNS's ~160 — so the cmdline limit is farther away, but capped at 30 to stay comfortably inside it. Documented in `_WINRM_BATCH_SIZE` in [`drivers/dhcp/windows.py`](../../backend/app/drivers/dhcp/windows.py).

**Dispatcher.** `push_statics_bulk_delete` groups by `(server, scope)` so the IPAM purge-orphans path went from N×M WinRM calls to one per server. Same state-aware commit pattern as DNS — only state=`applied` ops delete the DB row.

---

## 5. Error handling

All driver methods:

- Raise `DriverConnectionError` for network / auth failures.
- Raise `DriverOperationError` for a successful connection but failed operation (e.g. PowerShell cmdlet failed, Kea validation rejected the bundle).
- Never swallow errors. Log full details at `ERROR` before raising.
- Are safe to retry — service layer handles retry via Celery task retries, drivers are not responsible for retry.

For WinRM drivers, `pywinrm` errors get caught and re-raised as `DriverConnectionError` with the PowerShell `std_err` in the message — the API surfaces this verbatim in the 502 response so the UI "Test Connection" button shows the real Windows error.

---

## 7. Adding a new driver

1. Subclass `DHCPDriver`. Implement all abstract methods. If read-only, raise `NotImplementedError` on writes.
2. Register in `app/drivers/dhcp/registry.py`:
   ```python
   _DRIVERS["my_driver"] = MyDriverClass
   ```
3. If agentless, add to `AGENTLESS_DRIVERS`. If read-only, add to `READ_ONLY_DRIVERS`.
4. Add the driver name to the enum in `DHCPServer.driver` (Alembic migration).
5. Update the UI's server create modal to render the right credential fields (see how `windows_dhcp` conditionally shows WinRM fields in `frontend/src/pages/dhcp/CreateServerModal.tsx`).
6. Add a "Test Connection" PowerShell / API probe at `POST /dhcp/test-credentials` so operators can validate before saving.

---

## Related docs

- [DHCP Features](../features/DHCP.md) — user-facing: scopes, pools, leases, HA modes.
- [Getting Started](../GETTING_STARTED.md) — where DHCP fits in the setup order.
- [Windows Setup](../deployment/WINDOWS.md) — WinRM prerequisites, service accounts.
- [DNS Drivers](DNS_DRIVERS.md) — the parallel structure on the DNS side.
