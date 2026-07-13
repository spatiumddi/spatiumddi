---
layout: default
title: DHCP Drivers
---

# DHCP Driver Specification

DHCP drivers are the backend-specific layer that turns SpatiumDDI's internal DHCP model into operations on real DHCP servers. The service layer only ever speaks to [`DHCPDriver`](../../backend/app/drivers/dhcp/base.py) (CLAUDE.md non-negotiable #10) — no Kea / PowerShell specifics leak above this line.

The driver registry ([`registry.py`](../../backend/app/drivers/dhcp/registry.py)) classifies drivers along two axes:

| Axis | Values | What it means |
|---|---|---|
| `AGENTLESS_DRIVERS` | `windows_dhcp`, `fortigate` | Driver runs from the control plane. No co-located agent, no `ConfigBundle` long-poll. |
| `READ_ONLY_DRIVERS` | `windows_dhcp` | Driver implements lease reads only. Config push / reload / restart raise `NotImplementedError`. |
| `CLOUD_DHCP_DRIVERS` | `fortigate` | Agentless **and** write-capable: pushes whole-scope config to a provider REST API. `is_cloud()` gates the write-through + port defaults. |

All other drivers (currently just `kea`) are agented: the control plane renders a `ConfigBundle`, hash-keyed by SHA-256 ETag, and the co-located `spatium-dhcp-agent` long-polls `/config` to pick up changes.

---

## 1. Driver shapes

Today's drivers split into three shapes:

```
┌────────────────────────┐  ┌────────────────────────┐  ┌────────────────────────┐
│  Agented + write       │  │  Agentless + read-only │  │  Agentless + write     │
│  (Kea)                 │  │  (Windows DHCP—Path A) │  │  (FortiGate — cloud)   │
│                        │  │                        │  │                        │
│  Control plane:        │  │  Control plane:        │  │  Control plane:        │
│    render_config()     │  │    get_leases() WinRM  │  │    apply_scope_full()  │
│    ETag long-poll      │  │    get_scopes() WinRM  │  │      → REST PUT/POST   │
│                        │  │                        │  │    get_leases() REST   │
│  Agent (sidecar):      │  │  No agent.             │  │  No agent.             │
│    fetch bundle        │  │  Writes raise          │  │  Whole-scope push,     │
│    apply_config()      │  │    NotImplementedError.│  │    synchronous,        │
│    reload / restart    │  │                        │  │    before-commit.      │
└────────────────────────┘  └────────────────────────┘  └────────────────────────┘
```

The abstract base (`DHCPDriver`) has methods for both halves. Read-only agentless drivers (Windows) implement only the read methods + a stub `apply_config` / `reload` / `restart` / `validate_config` that raises; the API layer consults `READ_ONLY_DRIVERS` before offering write endpoints. Cloud agentless drivers (FortiGate) are write-capable but push the **whole scope object** synchronously over a REST API instead of rendering a daemon config bundle — see §5.

---

## 2. Abstract base class

Key methods on [`DHCPDriver`](../../backend/app/drivers/dhcp/base.py):

```python
class DHCPDriver(ABC):
    name: str = "abstract"

    # Rendering (agented drivers).
    @abstractmethod
    def render_config(self, bundle: ConfigBundle) -> str: ...

    # Applying on the server host (agent-side).
    @abstractmethod
    async def apply_config(self, server: Any, bundle: ConfigBundle) -> None: ...
    @abstractmethod
    async def reload(self, server: Any) -> None: ...
    @abstractmethod
    async def restart(self, server: Any) -> None: ...
    @abstractmethod
    def validate_config(self, bundle: ConfigBundle) -> tuple[bool, list[str]]: ...

    # Reads.
    @abstractmethod
    async def get_leases(self, server: Any) -> list[dict[str, Any]]: ...
    @abstractmethod
    async def health_check(self, server: Any) -> tuple[bool, str]: ...
    @abstractmethod
    def capabilities(self) -> dict[str, Any]: ...

    # Optional per-object write APIs (Windows DHCP today). The default
    # batch impls below loop over the singular methods, which only the
    # Windows driver overrides; non-Windows drivers raise NotImplementedError.
    async def apply_reservations(self, server: Any, *,
                                 items: Sequence[ReservationItem]
                                 ) -> list[ReservationResult]: ...
    async def remove_reservations(self, server: Any, *,
                                  items: Sequence[RemoveReservationItem]
                                  ) -> list[ReservationResult]: ...
    async def apply_exclusions(self, server: Any, *,
                               items: Sequence[ExclusionItem]
                               ) -> list[ExclusionResult]: ...
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

Kea runs an HTTP Control Agent on `localhost:8544` (inside the agent pod/container — `:8000` is reserved for the HA hook's peer-to-peer listener). The agent drives Kea by:

1. Rendering the config bundle into Kea JSON (`Dhcp4` for IPv4, `Dhcp6` for IPv6 — address-family split on `DHCPScope.address_family`).
2. POSTing to `config-test` before `config-set` to catch validation errors early.
3. Calling `config-reload` which re-reads the file without dropping in-flight leases.

The IPv6 path renders a `Dhcp6` tree in parallel to `Dhcp4`. Dhcp6 option-name translation lands via `_KEA_OPTION_NAMES_V6` in both `backend/app/drivers/dhcp/kea.py` and the agent's `render_kea._options_from_mapping_v6`; v4-only options (`routers`, `broadcast-address`, `mtu`, `time-offset`, tftp-*) are dropped from v6 scopes with a warning.

### Wire shape from control plane → renderer

The HTTP envelope returned by `GET /api/v1/dhcp/agents/config` is:

```json
{
  "server_id": "<uuid>",
  "etag": "sha256:…",
  "bundle": {
    "server_name": "…",
    "driver": "kea",
    "roles": [...],
    "server": { "interfaces": ["*"], "dhcp_socket_type": "raw" },
    "scopes": [
      {
        "subnet_cidr": "10.0.0.0/24",
        "lease_time": 3600,
        "options": {…},
        "pools": [{"start_ip": "...", "end_ip": "...", "pool_type": "dynamic"}, …],
        "statics": [{"ip_address": "...", "mac_address": "...", "hostname": "..."}, …],
        "ddns_enabled": false
      }, …
    ],
    "client_classes": [{"name": "...", "match_expression": "...", "options": {…}}, …],
    "failover": { ... } | null
  },
  "pending_ops": [...]
}
```

Shipped 2026.04.21-2: `render_kea.py:_scope_to_subnet` maps each wire scope to a Kea `subnet4` entry — `subnet_cidr` → `subnet`, `pools[start_ip,end_ip,pool_type]` → `{"pool": "start - end"}` (only `dynamic` pools are emitted; `excluded` / `reserved` are IPAM bookkeeping and must **not** become Kea lease pools), `statics[ip_address,mac_address,hostname]` → `reservations[ip-address,hw-address,hostname]`. Client-class `match_expression` → Kea `test`. Subnet `id` is derived deterministically from the CIDR via truncated SHA-256, so a config reload never orphans active leases by renumbering subnets.

**Socket type (issue #365).** The `server.dhcp_socket_type` field drives `Dhcp4.interfaces-config.dhcp-socket-type`. It is derived from the server group's `dhcp_socket_mode` in `services/dhcp/config_bundle.py` (`direct` → `raw`, `relay` → `udp`) and is part of `ConfigBundle.compute_etag()`, so flipping the mode shifts the ETag and the agent re-renders on its next long-poll. The default is `raw` (AF_PACKET) so Kea hears broadcast `DHCPDISCOVER`s from directly-attached clients; the agent's `render_kea.py` also falls back to `raw` when an older control plane omits the `server` block. `raw` needs `CAP_NET_RAW` (appliance DaemonSet + shipped compose Kea services grant it). DHCPv6 has no socket-type concept — the field applies to `Dhcp4` only.

### HA coordination

Kea's built-in `libdhcp_ha.so` hook handles pool coordination between paired servers. Under the group-centric data model (shipped 2026.04.21-2), SpatiumDDI treats a `DHCPServerGroup` with exactly two Kea members as an implicit HA pair — HA tuning lives on the group, per-peer URLs live on each `DHCPServer.ha_peer_url`. There is no separate "failover channel" object any more.

- `_resolve_failover` in `backend/app/services/dhcp/config_bundle.py` walks the server's group. If the group has ≥ 2 Kea members and each has a non-empty `ha_peer_url`, it emits a `FailoverConfig` carrying the group's mode / heartbeat / max-response / max-ack / max-unacked tuning and both peers' URLs. Members are sorted by `id` so both peers render an identical `peers` array.
- The agent's `render_kea.py:_ha_hook()` injects `libdhcp_ha.so` alongside the always-loaded `libdhcp_lease_cmds.so` (the HA hook depends on it).
- **Peer URL hostname resolution** — Kea's HA hook parses peer URLs with Boost asio directly and only accepts IP literals. Hostnames like `http://dhcp-kea-2:8000/` fail with `bad url ...: Failed to convert string to address`. `render_kea._resolve_peer_url` resolves the hostname agent-side (via the container's resolver — Docker DNS on compose, k8s DNS on Kubernetes) before emitting the URL into Kea config. IPv4/v6 literals pass through unchanged.
- **Peer IP drift self-healing** — `PeerResolveWatcher` (`agent/dhcp/spatium_dhcp_agent/peer_resolve.py`) runs as a 30 s background thread, re-resolves every peer hostname from the last-applied bundle's failover block, and if any resolution has changed, triggers the renderer + reload via the agent's existing apply pipeline. Resolution failures are treated as transient (cached IP kept, retried next tick) so a brief DNS outage doesn't thrash reloads. Matters most on k8s where pod IPs churn; on compose bridges IPs are mostly stable across `restart`, but `docker compose --force-recreate` WILL reshuffle bridge allocations.
- **Port topology** — Kea 2.6's HA hook spins up its own `CmdHttpListener` bound to the `this-server` peer URL to receive peer-to-peer traffic. That **must not** collide with `kea-ctrl-agent`. SpatiumDDI's Kea image dedicates:
  - `:8000` — HA hook peer-to-peer HTTP (the URL advertised to the partner).
  - `:8544` — `kea-ctrl-agent` operator-facing REST (deliberately off 8000).
- Each peer's `this-server-name` is derived from its `DHCPServer.name`; the `peers` array carries both entries with roles `primary` + (`standby` in hot-standby / `secondary` in load-balancing).
- Agent-side `HAStatusPoller` (`agent/dhcp/spatium_dhcp_agent/ha_status.py`) calls `status-get` on the Kea unix socket every ~15 s and POSTs the state to the control plane — drives the live HA pill in the UI. Kea 2.6 moved HA state into the generic `status-get` response under `arguments.high-availability[]`; the poller also accepts pre-2.6 `ha-status-get` shapes for forward-compat.
- **Bootstrap reload** — on agent startup the entrypoint launches `kea-dhcp4` in the background with the Dockerfile-baked config, then the agent re-renders the cached bundle and issues `config-reload` with up to 15 s of retry so Kea picks up HA even on cold starts where the control socket isn't live yet.
- **Daemon supervision** — both `kea-dhcp4` and `kea-ctrl-agent` run under per-daemon supervise loops in the container entrypoint. Each loop scrubs stale `/run/kea/*.pid` files before every launch (Kea only removes its own PID on GRACEFUL shutdown, so SIGKILL / hard crash / mid-init SIGTERM leaves the file behind and `createPIDFile` refuses to start with `DHCP4_ALREADY_RUNNING`), restarts the daemon on transient crashes (e.g. the Docker bridge-attach race that intermittently fails HA-hook bind with "Address not available" right after a container restart), and trips a 5-in-30s crash-loop guard so a truly broken config surfaces instead of looping forever. SIGTERM handlers inside each loop forward to the live daemon AND flip a stopping flag so we don't restart during container shutdown.
- The driver does **not** replicate leases itself — Kea's hook talks directly to the peer's HA listener. SpatiumDDI just renders the config.
- **Scope mirroring** is automatic under the group-centric model: scopes / pools / statics / client classes live on the group, and every member of the group renders the same Kea config. Operators configure scopes once, both peers serve them.

### Agent bootstrap

Identical pattern to the DNS agent ([`DNS_AGENT.md`](../deployment/DNS_AGENT.md)):

1. Agent starts with `SPATIUM_AGENT_KEY` (PSK) in its environment; the control plane validates it against its own `DHCP_AGENT_KEY`.
2. Calls `POST /api/v1/dhcp/agents/register` with the PSK in the `X-DHCP-Agent-Key` header → receives a per-server rotating JWT (`agent_token`).
3. Long-polls `GET /api/v1/dhcp/agents/config` with the JWT and an `If-None-Match` ETag.
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

The `/sync` endpoint (bundle push) rejects read-only drivers; the `/{server_id}/sync-leases` endpoint and per-object CRUD drive all writes instead.

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

Leases drive a scheduled Celery beat task ([`app.tasks.dhcp_pull_leases.auto_pull_dhcp_leases`](../../backend/app/tasks/dhcp_pull_leases.py)). Beat fires every 60s; the task gates on `PlatformSettings.dhcp_pull_leases_enabled` / `dhcp_pull_leases_interval_seconds`, so the UI can change cadence without restarting beat.

Per poll cycle:

1. Enumerate agentless DHCP servers.
2. For each, call `driver.get_leases(server)`.
3. Upsert into `DHCPLease` by `(server_id, ip_address)`.
4. If the lease's IP falls inside a known subnet, mirror it into `IPAddress` with `status="dhcp"` and `auto_from_lease=True`.
5. **Absence-delete** — any active `DHCPLease` row for this server whose IP didn't come back in the wire response is deleted along with its `auto_from_lease=True` IPAM mirror. The driver's `get_leases()` returns only currently-active leases, so absence means the server deleted it (admin purged / client released / etc.). `PullLeasesResult` gains `removed` + `ipam_revoked` counters; both flow into the scheduled-task audit row and the manual sync response. See [features/DHCP.md §15.3](../features/DHCP.md) for the rationale.
6. The time-based `dhcp_lease_cleanup` sweep still handles leases that drift past `expires_at` between polls. The two mechanisms overlap harmlessly.

### Scope reconcile (topology)

The same beat task runs a **topology** phase ahead of the lease phase, for any driver that implements `get_scopes` — which today means Windows DHCP alone. For each wire scope whose CIDR exactly matches an IPAM `Subnet.network` (no auto-create), the scope row is upserted and its pools + reservations are **diff-merged** against the wire. Reservations are keyed on MAC, pools on `(start, end)`; a MAC / range that is already tracked is updated in place, so **a reservation keeps its row id across polls**.

That id stability is load-bearing (#620). A reservation owns an `ip_address` mirror that back-links to it by id, so anything that re-creates the row (the pre-#620 reconciler Core-DELETEd and re-inserted every reservation, every poll) leaves the mirror pointing at a row Postgres has dropped — an address that is neither allocated nor free, and that deleting the reservation in the UI never frees, because the lookup keys on the *current* id. Merging also means an unchanged reservation is not written at all, which is what keeps the reconciler from re-publishing its DNS records on every tick.

Reservations found on the server are mirrored into IPAM exactly like UI-created ones (`status="static_dhcp"`), via the same `upsert_ipam_for_static` helper — a reservation is an allocation whoever made it, and an IPAM that doesn't show it is an IPAM that will hand the address out twice. The mirror upsert (and therefore the DNS re-sync it performs) fires only when the reservation's address or hostname actually moved, or when its mirror is missing or stale.

Three things the merge deliberately refuses to do:

* **Absence-delete against a wire it can't trust.** Absence normally means "deleted on the server" — the reservation is dropped, its mirror deleted and its DNS torn down. But `_PS_LIST_TOPOLOGY` enumerates options / exclusions / reservations inside per-list `try/catch` blocks, so a failure hands back an *empty array*, not an error. The driver therefore reports `pools_ok` / `statics_ok` alongside each list, and the reconciler also declines to delete when a wholly-empty list arrives while it tracks rows (the same call the lease path's zero-wire floor guard makes, #482). Both cases record a soft error on the sync result. The trade is deliberate: a stale reservation an operator can delete beats a reservation and its A record torn down on a blip.
* **Act on rows newer than its own information.** The wire is a snapshot taken before a multi-second WinRM round-trip. A reservation created or edited *after* that snapshot is judged against stale data, so it is skipped — otherwise a reservation created in the UI mid-poll is "absent from the wire" and gets deleted out from under the operator.
* **Write an address change row-by-row.** Renumbering reservations on the server (`A` takes `B`'s address) produces a wire whose end state is legal but whose intermediate states violate `uq_dhcp_static_scope_ip` — which would abort the poll, and keep aborting it, since the wire keeps reporting the same state. Movers are lifted out of that partial index (it is `WHERE deleted_at IS NULL`) inside the transaction, re-addressed, and put back.

`PullLeasesResult` reports `scopes_imported` / `scopes_refreshed` / `scopes_skipped_no_subnet` plus `pools_synced` / `statics_synced` (created **or changed**) and `pools_removed` / `statics_removed`. Because the merge is a diff, a steady-state poll reports zeros.

### Batched WinRM writes

Per-object writes against Windows DHCP used to round-trip WinRM **per reservation / exclusion** — a bulk delete of 200 reservations took minutes. The driver now groups writes into a single PowerShell script per `(server, scope)` chunk.

**Driver surface.** New plural methods on the ABC:

```python
class DHCPDriver(ABC):
    async def apply_reservations(
        self, server: Any, *, items: Sequence[ReservationItem]
    ) -> list[ReservationResult]: ...

    async def remove_reservations(
        self, server: Any, *, items: Sequence[RemoveReservationItem]
    ) -> list[ReservationResult]: ...

    async def apply_exclusions(
        self, server: Any, *, items: Sequence[ExclusionItem]
    ) -> list[ExclusionResult]: ...
```

Default ABC impls call the singular method in a loop — Kea inherits the plural interface without changes.

**Windows batch size — 30 ops per chunk.** `pywinrm.run_ps` ships the script as a single CMD.EXE command line (8191-char cap, see [DNS_DRIVERS.md §3.7](DNS_DRIVERS.md#37-batched-winrm-dispatch) for the full math). DHCP payloads are leaner than DNS — each reservation / exclusion op is ~60 raw chars of JSON vs. DNS's ~160 — so the cmdline limit is farther away, but capped at 30 to stay comfortably inside it. Documented in `_WINRM_BATCH_SIZE` in [`drivers/dhcp/windows.py`](../../backend/app/drivers/dhcp/windows.py).

**Dispatcher.** `push_statics_bulk_delete` groups by `(server, scope)` so the IPAM purge-orphans path went from N×M WinRM calls to one per server. Same state-aware commit pattern as DNS — only state=`applied` ops delete the DB row.

---

## 5. FortiGate driver (agentless + cloud push)

[`fortigate.py`](../../backend/app/drivers/dhcp/fortigate.py) is the first **cloud** agentless DHCP driver: the control plane drives a FortiGate's per-interface DHCP server directly over the FortiOS REST API with an API-admin **Bearer token**, VDOM-scoped, no co-located agent. It subclasses [`AgentlessDHCPDriverBase`](../../backend/app/drivers/dhcp/_cloud_base.py) (the shared cloud base) rather than rendering a daemon config.

### Model mapping

One SpatiumDDI `DHCPServer(driver="fortigate")` = one FortiGate device + VDOM. A `DHCPScope` (one subnet) maps to the `system.dhcp.server` object on the **interface whose primary IP+netmask CIDR equals the scope CIDR** — SpatiumDDI never creates interfaces or changes interface IPs; no match / multiple matches raise a clear error. Dynamic pools → `ip-range`; excluded/reserved pools → `exclude-range` (clipped to the dynamic range — FortiGate rejects an exclude outside the ip-range); statics → `reserved-address`; scope options → first-class fields (`default-gateway` / `dns-server1..4` / `domain` / `ntp-server1..3` / `filename` / `lease-time`) or the generic `options` subtable. Numeric options (`mtu`, `time-offset`) are emitted as `type: "hex"` big-endian — a `string` MTU would reach the client as ASCII, not a 16-bit integer.

### Write unit

The whole DHCP-server object per scope: any scope / pool / static / option edit rebuilds the full desired object from the DB and PUTs it (create-if-absent). The cloud write-through ([`services/dhcp/cloud_writethrough.py`](../../backend/app/services/dhcp/cloud_writethrough.py)) runs **synchronously, before commit**, so a REST failure raises `CloudPushError` (502) and rolls the transaction back — keeping the DB and the FortiGate in sync. `push_cloud_scope_upsert` fans a scope out to every cloud member of its group; cascade / group / restore paths reach it through the shared `windows_writethrough` seam.

### Ownership + adopt-guard (#630)

So a push never silently overwrites or deletes a DHCP server an operator hand-managed on the FortiGate, the control plane records the FortiOS `mkey` of the object *SpatiumDDI created* on the scope (`DHCPScope.provider_refs`, keyed per cloud server) and passes it back as `provider_ref`:

* recorded mkey present → PUT that object;
* no mkey + interface empty → POST, then record the new mkey;
* no mkey + an object already exists → raise `CloudDHCPAdoptionError` → **409**, unless the operator opts in with `adopt_existing` (query param on `POST /{id}/sync`).

`_remove_scope` only deletes an object whose mkey we recorded. The `GET /{id}/fortigate-interfaces` preflight surfaces any pre-existing DHCP server (ip-range / reservation / option counts + a `managed` flag) so the clobber risk is visible before an adopt-and-sync.

### Credentials + TLS

Fernet-encrypted on `DHCPServer.credentials_encrypted`: `{"api_token", "vdom", "verify_tls", "ca_bundle_pem"}`. `verify_tls` **defaults to `True`** (the admin Bearer token is sensitive) with an optional `ca_bundle_pem` to pin a private-CA FortiGate; disabling verification logs a WARNING. The API echoes the non-secret `vdom` + `verify_tls` on the server response so the edit modal seeds the checkbox from the stored value instead of silently re-disabling it. API-created cloud servers default to **port 443** when the caller omits `port`.

### Endpoints

`POST /dhcp/servers/test-fortigate-credentials` (dry-run probe), `GET /dhcp/servers/{id}/fortigate-interfaces` (preflight), `POST /dhcp/servers/{id}/sync?adopt_existing=` (synchronous full reconcile). All three call `assert_safe_target` (advisory SSRF guard) before dialing the host.

---

## 6. Error handling

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
6. Add a "Test Connection" PowerShell / API probe (e.g. `POST /dhcp/servers/test-windows-credentials`) so operators can validate before saving.

---

## 8. Importing existing daemon configs (issue #129)

The **DHCP configuration importer** is separate from the driver
abstraction: drivers *render + push* config to managed servers; the
importer *reads* a foreign daemon's config one-shot and writes the
canonical SpatiumDDI rows. It never touches a live daemon's config
file. Code lives in `backend/app/services/dhcp_import/`.

**Canonical IR** (`canonical.py`) — every source parses into the same
neutral shapes: `ImportedScope` (CIDR, address family, lease times,
options, DDNS), `ImportedPool`, `ImportedReservation`,
`ImportedClientClass`, rolled into an `ImportPreview`. The shared
`commit.py` is the only module that touches the DB, so all three
sources share IPAM linkage, conflict handling, audit logging, and the
per-scope savepoint pattern.

**Parsers:**

- `kea_parser.py` — strips Kea's JSON-with-comments (`//`, `#`,
  `/* */`) string-aware, then walks `Dhcp4.subnet4` / `Dhcp6.subnet6`.
  Inverts the Kea driver's option-name map (`options.py` builds
  the inverse of `_KEA_OPTION_NAMES` / `_KEA_OPTION_NAMES_V6`). Pools
  parse both `"a - b"` and CIDR forms; `code:NN` option-data round-trips
  the same way the driver renders it. DUID-only v6 reservations are
  skipped (our reservations are MAC-keyed). Top-level `client-classes`
  import verbatim — Kea `test` expressions are SpatiumDDI's native class
  shape. `hooks-libraries` / `control-socket` / `lease-database` /
  `loggers` are listed unsupported.
- `windows_dhcp_pull.py` — reuses `WindowsDHCPReadOnlyDriver.get_scopes()`
  (the same Path A read path the Logs surface uses), reshaping its
  neutral dicts. IPv4 only; option names already arrive canonical.
- `isc_dhcp_parser.py` — a self-contained tokeniser (comment- +
  string-aware) → recursive-descent statement tree → walker.
  `subnet` / `subnet6` → scopes; `range` / `range6` / `pool {}` →
  pools (with `allow members of "class"` → `class_restriction`);
  `host` → reservations (global hosts attach to the subnet containing
  their `fixed-address`); `option` → canonical options;
  `shared-network` / `group` are flattened. ISC's runtime-expression
  classifier DSL doesn't map to our model, so `class` declarations are
  emitted `supported=false` (manual review, never auto-created);
  `failover` / `key` / `zone` / `include` are listed unsupported.

**IPAM linkage** — `DHCPScope.subnet_id` is mandatory, so each commit
resolves a `Subnet`: link to an existing one whose CIDR matches, or
auto-create under the operator-chosen IP space + block (containment +
non-overlap validated). Link-only mode (no space/block) reports an
actionable per-scope error for unmatched CIDRs rather than failing the
whole batch.

**Provenance** — `import_source` (`kea` / `windows_dhcp` / `isc_dhcp`)
+ `imported_at` are stamped on every `dhcp_scope` / `dhcp_pool` /
`dhcp_static_assignment` / `dhcp_client_class` row the importer creates
(migration `c7f1a3e58b94`). Endpoints under
`/api/v1/dhcp/import/{kea,windows,isc}/…` are gated by the `dhcp.import`
feature module + superadmin RBAC.

See [Migration](../features/MIGRATION.md) for the operator-facing flow
and the DNS-importer sibling.

---

## Related docs

- [DHCP Features](../features/DHCP.md) — user-facing: scopes, pools, leases, HA modes.
- [Getting Started](../GETTING_STARTED.md) — where DHCP fits in the setup order.
- [Windows Setup](../deployment/WINDOWS.md) — WinRM prerequisites, service accounts.
- [DNS Drivers](DNS_DRIVERS.md) — the parallel structure on the DNS side.
