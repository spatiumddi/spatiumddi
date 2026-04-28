# DHCP Feature Specification

> **Implementation status (2026-04-28):** Kea driver, agent runtime, container image, backend API, and frontend UI shipped in the `2026.04.16-1` release. Pool overlap validation, existing-IP warning, resize, static ↔ IPAM sync (including DNS forward/reverse), lease → IPAM mirror (with auto-cleanup on expiry), DHCP Pool membership column in the IPAM subnet view, per-scope DHCP defaults prefilled from Settings. **Windows DHCP driver shipped** — Path A (agentless, WinRM + PowerShell, read-only lease monitoring + per-object scope / pool / reservation CRUD). **DDNS pipeline shipped for both paths** — agentless lease pull (2026-04-19) and agent-side Kea lease events (2026-04-21). **Group-centric Kea HA shipped** (`2026.04.21-2`) — load-balanced or hot-standby pairs with self-healing peer-IP drift, supervised daemons, and live `status-get` reporting. **Scope authoring helpers (`2026.04.28-2`):** 95-entry RFC 2132 + IANA option-code library with autocomplete on the custom-options row, plus named group-scoped option templates (e.g. "VoIP phones", "PXE BIOS clients") with a one-click "Apply template…" picker on the scope create / edit modal. **Still deferred:** PXE / iPXE first-class fields, Option 82 (relay agent info) class matching, DHCP fingerprinting, lease histogram by hour, reconciliation report, lease import. NTP (DHCP option 42) is a first-class option.

## Overview

SpatiumDDI manages DHCP servers as authoritative configuration sources. The IPAM database is the **source of truth** — DHCP server configs are pushed from IPAM, not read from the servers. DHCP servers are configured to report lease events back to SpatiumDDI for real-time IP status tracking and DDNS updates.

Supported backends: **ISC Kea DHCP** (preferred) and **Windows Server DHCP** (agentless). ISC DHCP v4 is intentionally **not** supported — upstream declared end-of-life in 2022; Kea is the successor.

---

## 1. DHCP Server Model

```
DHCPServer
  id, name, description
  driver: enum(kea, windows_dhcp)
  host, port
  credentials (encrypted Fernet)
  roles: [enum(dhcp4, dhcp6)]     -- server can run both
  server_group_id (nullable)      -- logical grouping for HA pairs
  status: enum(online, offline, degraded, syncing)
  last_sync_at, last_health_check_at
  config_cache_path: str          -- local path where agent caches last-good config
```

### DHCP Server Groups

**DHCPServerGroup is the primary configuration container.** Scopes, pools, statics, and client classes all belong to a group — every member server renders the same Kea config. A group with one member is a standalone DHCP service; a group with two Kea members is implicitly an HA pair, driving the `libdhcp_ha.so` hook.

```
DHCPServerGroup
  id, name, description
  mode: enum(standalone, hot-standby, load-balancing)
  heartbeat_delay_ms, max_response_delay_ms, max_ack_delay_ms, max_unacked_clients
  auto_failover: bool
  servers: [DHCPServer]          -- 2 Kea members = HA pair
  scopes: [DHCPScope]            -- rendered on every member
  client_classes: [DHCPClientClass]
```

---

## 2. DHCP Scopes and Pools

This is the most granular part of the DHCP model. Within a **subnet**, you can define:
- Multiple **pools** with different behaviors (dynamic, reserved, excluded)
- **Static assignments** (fixed IPs by MAC address)
- **Pool-level DHCP options** that override subnet defaults

### Hierarchy

```
Subnet
  └── DHCPScope (one per subnet per DHCP server group)
        ├── DHCPPool (one or more dynamic ranges)
        └── DHCPStaticAssignment (zero or many)
```

### DHCPScope Model

```
DHCPScope
  id, group_id, subnet_id
  is_active: bool
  lease_time: int (seconds, default 86400)
  max_lease_time: int
  options: JSONB {               -- scope-level options (override server defaults)
    "routers": ["10.1.2.1"],
    "domain-name-servers": ["10.0.0.53", "10.0.0.54"],
    "domain-name": "internal.example.com",
    "domain-search": ["internal.example.com", "example.com"],
    "tftp-server-name": "10.0.0.10",   -- for PXE
    "bootfile-name": "pxelinux.0",
    "vendor-class-identifier": "...",
    custom_options: { "176": "..." }   -- vendor-specific options by code
  }
  ddns_enabled: bool
  ddns_hostname_policy: enum(client_provided, client_or_generated, always_generate, disabled)
  last_pushed_at: timestamp
```

### DHCPPool Model (Dynamic Ranges)

Each scope can have **multiple pools**, each with its own range and optional class restrictions.

```
DHCPPool
  id, scope_id
  name: str (optional label, e.g., "Workstations", "Printers", "VoIP")
  start_ip: inet
  end_ip: inet
  pool_type: enum(dynamic, excluded, reserved)
    -- dynamic:   IPs handed out to any eligible client
    -- excluded:  range exists in subnet but DHCP will NOT offer these IPs
    -- reserved:  range held for static assignments only (not auto-assigned)
  class_restriction: str (nullable)   -- Kea client class
  lease_time_override: int (nullable) -- overrides scope lease_time for this pool
  options_override: JSONB (nullable)  -- additional options for this pool only
  utilization_percent: float (computed)
  current_lease_count: int (computed, pulled from server)
```

### Example: Multiple Pools in One Subnet (10.1.2.0/24)

| Pool Name | Range | Type | Notes |
|---|---|---|---|
| Infrastructure | 10.1.2.1–10.1.2.20 | excluded | Gateways, servers — never DHCP |
| Static Only | 10.1.2.21–10.1.2.50 | reserved | Printers with static assignments |
| VoIP Phones | 10.1.2.51–10.1.2.100 | dynamic | Class: VoIP, short lease, option 150 |
| Workstations | 10.1.2.101–10.1.2.200 | dynamic | Standard lease, all defaults |
| Guest WiFi | 10.1.2.201–10.1.2.240 | dynamic | Class: Guest, 2h lease, no internal DNS |
| Management | 10.1.2.241–10.1.2.254 | excluded | Network equipment |

---

## 3. Static DHCP Assignments

Static assignments bind a MAC address (or client identifier) to a specific IP, hostname, and optional per-host options.

```
DHCPStaticAssignment
  id, scope_id
  ip_address: inet              -- must be within subnet
  mac_address: macaddr          -- primary identifier
  client_id: str (nullable)     -- DHCP client identifier (alternative to MAC)
  hostname: str
  description: str
  options_override: JSONB (nullable)  -- host-specific options
  ip_address_id (FK → IPAddress)      -- linked IPAM record
  created_by_user_id, created_at
```

### Static Assignment UI Workflow

1. Navigate to Subnet → DHCP tab → Static Assignments
2. Add: enter MAC, pick IP from the "available" list (respects reserved pool), set hostname
3. Or: click an existing IPAddress row → "Convert to Static DHCP Assignment"
4. On save: IPAM updates IPAddress.status = `static_dhcp`, pushes to DHCP server via driver

### Conflict Detection

Before saving a static assignment:
- Verify IP is not already assigned to another static entry on the same or different scope
- Verify MAC is not already in another static assignment across all scopes
- Verify IP falls within a `reserved` or `dynamic` pool (not `excluded`)

---

## 4. DHCP Client Classes

Client classes let you define rules for how clients are categorized and which pool or options they receive.

```
DHCPClientClass
  id, group_id          -- all members of the group get the same classes
  name: str             -- e.g., "VoIPPhones"
  match_expression: str -- Kea expression
                        -- e.g., "option[60].hex == 'Cisco7960'"
  description: str
```

Classes are referenced by pool `class_restriction` field. The DHCP driver translates these to server-native syntax.

---

## 4a. DHCP MAC Blocklist

A group-level deny list. Any MAC address listed here is dropped on every scope served by every member of the group — no leases, no response packets at all.

```
DHCPMACBlock
  id, group_id             -- every member blocks every listed MAC
  mac_address: MACADDR     -- unique per (group_id, mac_address)
  reason: str              -- rogue | lost_stolen | quarantine | policy | other
  description: str
  enabled: bool            -- soft-disable toggle
  expires_at: timestamptz  -- nullable; expired rows stay in DB, stripped from rendered config
  created_at, created_by_user_id
  updated_at, updated_by_user_id
  last_match_at, match_count  -- telemetry (wiring deferred)
```

**How each driver enforces the block**

| Driver | Enforcement | Update mechanism |
|---|---|---|
| Kea | Packets matching the reserved `DROP` client class are silently dropped before allocation. The agent renders active blocks as `hexstring(pkt4.mac, ':') == '…'` OR-clauses inside `DROP.test`. | ConfigBundle — blocklist changes shift the bundle ETag; agent long-poll picks them up and re-renders. |
| Windows DHCP | Server-level deny filter list (`Add-DhcpServerv4Filter -List Deny`). Deny filter is server-global — every scope on the server enforces it. | 60 s Celery beat task diffs desired-set against `Get-DhcpServerv4Filter -List Deny` and ships one batched PS script per WinRM round trip. |

Group-global is deliberate. Kea supports per-subnet via class/pool pinning; Windows doesn't support per-scope deny at all. A single "this device is bad, nowhere gets to serve it" rule is the usage pattern — per-scope precision is deferred until a concrete need surfaces.

**MAC input shapes** — the API accepts the common operator formats (`aa:bb:cc:dd:ee:ff`, `aa-bb-cc-dd-ee-ff`, `aabb.ccdd.eeff`, or bare `aabbccddeeff`, any case) and canonicalizes to colon-separated lowercase server-side. Agents see canonical form only.

**Expiry** — null = permanent; setting `expires_at` in the past is idempotent with `enabled=False` (both filter the row out of the rendered config). The beat task scanning for Windows DHCP means expiry transitions propagate within 60 s even without a config push.

**UI** — DHCP server → "MAC Blocks" tab. Filter-as-you-type over MAC / vendor / IP / hostname; vendor column sourced from `oui_vendor` (opt-in feature, null when OUI lookup is disabled); IPAM cross-ref shows any `IPAddress` rows currently tied to the blocked MAC, with IP + subnet + hostname. Per-row edit toggles `enabled`, `expires_at`, `reason`, and `description` — the MAC itself is immutable so the audit trail for each MAC stays linear (rename = delete + re-add).

**Permission** — `dhcp_mac_block`. The built-in "DHCP Editor" role gets it automatically.

---

## 5. DHCP Lease Tracking

Leases are **read-only** in SpatiumDDI — they are pulled from the DHCP server, not managed directly.

```
DHCPLease (not persisted long-term — cached in Redis, written to DB for history)
  ip_address, mac_address, hostname
  scope_id, server_id   -- per-server (each Kea owns its own memfile)
  starts_at, ends_at, expires_at
  state: enum(active, expired, released, abandoned)
  client_id, user_class
  last_seen_at
```

### Lease Sync Strategy

- **Real-time (preferred)**: Kea `lease_cmds` hook + webhook to SpatiumDDI on lease events
- **Polling fallback**: Celery task pulls lease dump every N minutes via DHCP driver API
- Leases are used to update `IPAddress.status`, `IPAddress.last_seen`, and trigger DDNS

### Lease History (forensic trail)

Landed in `2026.04.26-1` via migration
`f4e1d2a09b75_lease_history_and_nat`. The `dhcp_lease_history`
table records every lease that ever expired, was reassigned to a
different MAC, or got swept on absence-delete — gives operators a
"who had this IP last week" audit trail when the live
`dhcp_lease` row is gone.

- Written from three sites: the `dhcp_lease_cleanup` expiry sweep,
  the agent lease-event ingest path on MAC change, and
  `pull_leases` on absence-delete.
- Surfaced on the DHCP server detail as a new **Lease History**
  tab with filtering by MAC / IP / time window.
- Daily prune task (`app.tasks.dhcp_lease_history_prune`) honours
  `PlatformSettings.dhcp_lease_history_retention_days` (default
  90; set to 0 to keep forever).

---

## 6. Local Config Caching on DHCP Agents

**Critical resilience requirement**: DHCP servers must continue operating even when SpatiumDDI control plane is unreachable.

### Caching Architecture

Each DHCP server is managed by an **SpatiumDDI Agent** — a lightweight sidecar process running on or near the DHCP server.

```
┌─────────────────────────────────────────┐
│   DHCP Server Host / Container           │
│                                         │
│   ┌─────────────┐    ┌───────────────┐  │
│   │  DHCP daemon │←── │  SpatiumDDI     │  │
│   │  (Kea/ISC)  │    │  Agent        │  │
│   └─────────────┘    │               │  │
│                       │  config cache │  │
│                       │  (local disk) │  │
│                       └──────┬────────┘  │
│                              │ pull/push │
└──────────────────────────────┼───────────┘
                               │
                      ┌────────▼────────┐
                      │  SpatiumDDI API   │
                      │  (control plane)│
                      └─────────────────┘
```

### Agent Behavior

**When control plane is reachable:**
1. Agent polls SpatiumDDI API for config changes every N seconds (configurable, default 30s)
2. On config change: agent validates new config, writes to local cache, applies to DHCP daemon
3. Agent reports lease events back to SpatiumDDI API in real time
4. Agent writes a "last successful sync" timestamp to local disk

**When control plane is unreachable:**
1. Agent detects connectivity failure after 3 consecutive failed polls
2. Agent logs: `"Control plane unreachable — operating from cached config"`
3. Agent continues serving from cached config — **DHCP service is NOT interrupted**
4. Agent retries connectivity every 60 seconds
5. On reconnect: agent reports "gap period" lease events in bulk

### Cache Format

Cached config is stored in a structured JSON file:
```json
{
  "version": "1.4.2",
  "generated_at": "2024-01-15T14:30:00Z",
  "checksum": "sha256:...",
  "scopes": [...],
  "pools": [...],
  "static_assignments": [...],
  "client_classes": [...]
}
```

The Kea daemon config file (JSON) is generated from this cache. On startup, the agent always checks if the cache is newer than the running daemon config and applies if so.

### Cache Invalidation

- Cache is **never automatically deleted**
- A manual "force resync" is available from the admin UI (`POST /api/v1/dhcp/servers/{id}/sync`)
- Cache version is tracked; SpatiumDDI will reject applying a cache version older than the current DB version

---

## 7. DHCP ↔ IPAM Synchronization

### Push (IPAM → DHCP Server)
Triggered by:
- Scope/pool/static assignment create/update/delete
- Manual "Force Sync" from UI
- Scheduled full sync (default: every 5 minutes)

The push is **diff-based** — only changed objects are sent, not a full config replacement. This prevents unnecessary DHCP server disruption.

### Pull (DHCP Server → IPAM)
Triggered by:
- Lease events (real-time via webhook or Kea hook)
- Scheduled lease dump pull (fallback)
- Manual "Import Leases" from UI

### Reconciliation Report
Available from the admin UI: compares IPAM DB state vs. live DHCP server state and flags:
- IPs in DHCP scope but not in IPAM
- Static assignments in DHCP not in IPAM
- Scopes in DHCP not known to IPAM

---

## 8. Import / Export

### Export (IPAM → file)
- Export all DHCP scopes + pools + static assignments for a server or subnet
- Formats: JSON (native), Kea config JSON

### Import (file → IPAM)
- Import from Kea JSON config
- Import from CSV (static assignments: IP, MAC, hostname columns)
- Dry-run mode: shows what would be created before committing
- Conflict resolution: skip / overwrite / error on duplicates

---

## 9. DHCP Permissions

| Role | Capability |
|---|---|
| **superadmin** | Full server, scope, pool, static assignment management |
| **admin** (subnet scope) | Manage scopes and static assignments within their subnets |
| **operator** (subnet scope) | Add/modify/delete static assignments; cannot change pool ranges |
| **viewer** | View scopes and leases; no modifications |

---

## 10. Environment Variables for DHCP

```bash
DHCP_SYNC_INTERVAL_SECONDS=30       # How often agents poll for config
DHCP_LEASE_SYNC_INTERVAL_MINUTES=5  # Fallback polling for leases
DHCP_CONFIG_CACHE_PATH=/var/cache/spatiumddi/dhcp-config.json
DHCP_AGENT_RECONNECT_INTERVAL=60    # Seconds between reconnect attempts
DHCP_AGENT_MAX_CACHE_AGE_HOURS=72   # Alert if cache older than this
```

---

## 11. DHCP Options Reference

The following standard DHCP options can be configured at the scope, pool, or host level. All options are configurable in the UI and via API. Parent scope options are inherited by child pools unless overridden.

| Option Code | Name | Description |
|---|---|---|
| 1 | Subnet Mask | Auto-computed from subnet prefix |
| 3 | Router | Default gateway IP(s) |
| 6 | Domain Name Server | DNS server IPs (up to 3) |
| 12 | Host Name | Override hostname sent to client |
| 15 | Domain Name | DNS search domain (e.g., corp.example.com) |
| 28 | Broadcast Address | Auto-computed |
| 43 | Vendor Specific | Raw hex or vendor-specific encapsulated options |
| 51 | IP Address Lease Time | Seconds; default lease time |
| 58 | Renewal Time | T1 (default: 50% of lease time) |
| 59 | Rebinding Time | T2 (default: 87.5% of lease time) |
| 66 | TFTP Server Name | Boot server hostname (PXE) |
| 67 | Bootfile Name | PXE bootfile path |
| 119 | Domain Search | Multiple search domains |
| 150 | TFTP Server Address | Cisco VoIP boot server IP |

**Min/Max lease times**: Configured as `min_lease_time` / `max_lease_time` on the DHCPScope. Clients requesting shorter/longer leases are clamped to this range.

---

## 12. Parent/Child Setting Inheritance

DHCP options follow the same inheritance model as IPAM:

```
IPSpace → IPBlock → Subnet → DHCPScope → DHCPPool → DHCPStaticAssignment
```

At each level, options can be:
- **Inherited** (not set → use parent's value)
- **Overridden** (set → use this level's value, ignoring parent)
- **Extended** (for list-type options like domain-search: append to parent list)

Example:
```
IPSpace: Corporate
  domain-name: corp.example.com

  Subnet: 10.1.2.0/24 (HR VLAN)
    domain-name: hr.corp.example.com   ← overrides parent

    DHCPPool: Guest
      domain-name: guest.corp.example.com  ← overrides subnet
      lease-time: 7200                      ← 2-hour lease for guest
```

---

## 13. Hostname → IPAM Sync (Configurable)

When a DHCP client receives a lease, the hostname provided by the client can be automatically written back into the IPAM module (setting `IPAddress.hostname`).

```
DHCPScope.hostname_to_ipam_sync: enum(disabled, on_lease, on_static_only)
```

| Mode | Behavior |
|---|---|
| `disabled` | No hostname sync — IPAM hostname is managed manually |
| `on_lease` | Hostname from every new DHCP lease is written to IPAM |
| `on_static_only` | Only static DHCP assignments sync hostname to IPAM |

**Recommendation**: Set to `disabled` or `on_static_only` for large dynamic subnets (e.g., WiFi /16) where lease hostname data is noisy. Set to `on_lease` for server subnets where every IP should have a known hostname.

---

## 14. DHCP Pool Coordination — Kea HA on a Server Group

When two DHCP server containers serve the same pool, they must not hand the same IP to different MACs. SpatiumDDI solves this by treating a **`DHCPServerGroup` with two Kea members as an implicit HA pair** — HA tuning lives on the group, per-peer URL lives on each server, and Kea's `libdhcp_ha.so` hook is rendered on every member's config. There is no separate "failover channel" row any more (that was removed in 2026.04.22-1 when scopes moved to the group).

### Data model

- HA config fields live on `DHCPServerGroup`: `mode`, `heartbeat_delay_ms`, `max_response_delay_ms`, `max_ack_delay_ms`, `max_unacked_clients`, `auto_failover`.
- Each `DHCPServer` has its own `ha_peer_url` — the listener endpoint the partner calls for heartbeats + lease updates. Empty string for standalone servers.
- A group with **one Kea member** is standalone; HA fields are ignored. A group with **two Kea members + non-empty `ha_peer_url` on both** renders HA into their configs. Three-or-more Kea members is nonsensical for `libdhcp_ha.so` (it only speaks pairs) and should be validated at the CRUD layer.
- Mixed groups (Kea + Windows DHCP read-only) are allowed — only the Kea members participate in HA.

### Modes

- **`hot-standby`** — one active peer + one passive standby. The primary serves all clients; the standby takes over on `partner-down`. Secondary peer's role is rendered as `standby` in the HA hook.
- **`load-balancing`** — both peers active; Kea splits traffic by hash of client identifier. Secondary peer's role is rendered as `secondary`.

### What the agent ships

On each `ConfigBundle` long-poll, the control plane emits a `failover` block alongside scopes / client-classes when the server's group is an HA pair. The agent's `render_kea.py` injects two hook entries in `Dhcp4.hooks-libraries`:

```json
{ "library": "/usr/lib/kea/hooks/libdhcp_lease_cmds.so" }
{ "library": "/usr/lib/kea/hooks/libdhcp_ha.so",
  "parameters": {
    "high-availability": [{
      "this-server-name": "<local server name>",
      "mode": "hot-standby|load-balancing",
      "heartbeat-delay": 10000,
      "max-response-delay": 60000,
      "max-ack-delay": 10000,
      "max-unacked-clients": 5,
      "peers": [
        {"name": "dhcp1", "url": "http://10.0.0.5:8000/", "role": "primary",   "auto-failover": true},
        {"name": "dhcp2", "url": "http://10.0.0.6:8000/", "role": "standby",   "auto-failover": true}
      ]
    }]
  }
}
```

The `libdhcp_lease_cmds.so` hook is a hard prerequisite for HA and is loaded unconditionally — leaving it out will cause the HA hook to refuse to load.

### Live state reporting

A fourth thread in the agent (`HAStatusPoller`, `agent/dhcp/spatium_dhcp_agent/ha_status.py`) calls `status-get` against the local Kea control socket every ~15 s with small jitter and POSTs the result to `POST /api/v1/dhcp/agents/ha-status`. Kea 2.6 folded HA state into the generic `status-get` response under `arguments.high-availability[0].ha-servers.local.state`; the extractor also accepts pre-2.6 `ha-status-get` shapes for forward-compat. The control plane stores the state on `DHCPServer.ha_state` + `ha_last_heartbeat_at`. The poller self-disables when the most recent bundle carried no `failover` block, so standalone servers don't spam Kea with commands that return an error.

Kea state names pass through verbatim (`normal` / `hot-standby` / `load-balancing` / `ready` / `waiting` / `syncing` / `communications-interrupted` / `partner-down` / `backup` / `passive-backup` / `terminated`). The DHCP server detail header renders a colored `HA: <state>` pill. The dashboard's DHCP column lists one row per HA-paired group with a state dot per peer. The group detail view shows the same pill inline per-server so you can see HA state without drilling into each server page; use the Refresh button there after changing HA mode to repaint without waiting for the 30 s React Query poll.

### Peer IP drift self-healing

Kea's HA hook parses peer URLs with Boost asio, which only accepts IP literals — hostnames aren't looked up by Kea itself. The agent resolves hostnames at render time via `_resolve_peer_url`, but the IP can change afterwards (compose `--force-recreate`, k8s pod restart, bridge-IP reshuffle), leaving Kea pointing at a stale peer. A fifth thread (`PeerResolveWatcher`, `agent/dhcp/spatium_dhcp_agent/peer_resolve.py`) re-resolves peer hostnames every 30 s and, if any have drifted, triggers a render + config-reload with the fresh URL. Resolution failures are treated as transient (keep the cached IP, try again next tick), so a brief DNS outage doesn't thrash reloads.

### Not yet shipped (follow-up)

- **State-transition actions** — `ha-maintenance-start`, `ha-continue`, force-sync. The state machine is observable today but operators can't drive it from the UI; `kubectl exec` + manual `kea-shell` is the workaround.
- **Peer compatibility validation** — the control plane doesn't yet refuse groups with ≥ 3 Kea members (Kea HA only supports pairs).
- **Per-pool HA scope tuning** — the Kea HA hook supports per-subnet scope overrides; we render the relationship globally only.
- **Kea version skew guard** — `status-get` HA shape shifted between Kea 2.4 and 2.6. The extractor handles both, but the control plane still accepts pairing peers on mismatched Kea versions.
- **DDNS double-write under HA** — agent-side `apply_ddns_for_lease` doesn't gate on HA state. If the standby ever serves a lease (pre-sync window, partner-down), both peers could try to write the same RR.
- **HA DHCP e2e test** — the kind-based workflow stands up a single DNS agent; an HA DHCP variant would have caught all the bootstrap / port-split / `status-get` / wire-shape regressions shaken out in 2026.04.21-2.

### Managing HA

HA is configured on the server group, not a separate page. Edit the group under the DHCP tab, pick mode (`hot-standby` / `load-balancing`), tune the heartbeat / max-response / max-ack / max-unacked fields if the defaults don't fit your network, and make sure each Kea member has its **HA Peer URL** filled in (the server-level field) — typically `http://<host>:8000/` on the SpatiumDDI-shipped image. The HA hook renders automatically once the group has two Kea peers with non-empty URLs. Removing a server from the group or clearing its URL drops the hook on the next config push.

---

## 15. Windows DHCP — Path A (read-only)

SpatiumDDI supports Windows Server DHCP as an **agentless** backend. Today's implementation (Path A) is WinRM-driven and focused on **lease mirroring** — SpatiumDDI polls the Windows server for its active leases and reflects them into IPAM, but does not push config bundles. Path B (full scope/reservation CRUD via WinRM) is on the roadmap.

### 15.1 What's implemented

| Capability | Status | Mechanism |
|---|---|---|
| Read leases | ✅ | `Get-DhcpServerv4Scope` + `Get-DhcpServerv4Lease` per scope, JSON-serialised back. |
| Read scopes | ✅ | `Get-DhcpServerv4Scope` + options + exclusions + reservations in one PowerShell call. |
| Per-object scope CRUD | ✅ | `Add-DhcpServerv4Scope` / `Remove-DhcpServerv4Scope`. |
| Per-object reservation CRUD | ✅ | `Add-DhcpServerv4Reservation` / `Remove-DhcpServerv4Reservation`. |
| Per-object exclusion CRUD | ✅ | `Add-DhcpServerv4ExclusionRange` / `Remove-DhcpServerv4ExclusionRange`. |
| Bundle push (`/sync`) | ❌ | `READ_ONLY_DRIVERS` — rejected by the API. Windows DHCP is cmdlet-driven, not config-file-driven. |
| `reload` / `restart` / `validate_config` | ❌ | Not applicable to Windows; raise `NotImplementedError`. |

The driver lives at [`app/drivers/dhcp/windows.py`](../../backend/app/drivers/dhcp/windows.py) (class `WindowsDHCPReadOnlyDriver`). See [DHCP_DRIVERS.md](../drivers/DHCP_DRIVERS.md#4-windows-dhcp-driver-agentless--read-only-path-a) for internals.

### 15.2 Credentials

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

Service account requirements:
- **Read-only lease mirroring**: member of the Windows `DHCP Users` local group.
- **Per-object scope/reservation/exclusion CRUD**: member of `DHCP Administrators`.

See [WINDOWS.md](../deployment/WINDOWS.md) for the WinRM + account setup.

### 15.3 Scheduled lease pull

Scheduled Celery beat task: [`app.tasks.dhcp_pull_leases.auto_pull_dhcp_leases`](../../backend/app/tasks/dhcp_pull_leases.py). Beat fires every **10 seconds**; the task gates on platform settings so the UI can change cadence without restarting beat. A 10-second beat tick means operators can configure near-real-time IPAM population from Windows DHCP — the interval is the only knob that limits poll frequency now.

| Setting | Default | Description |
|---|---|---|
| `dhcp_pull_leases_enabled` | off | Master toggle. |
| `dhcp_pull_leases_interval_seconds` | 15 | How often to poll each agentless server, in seconds. Floor is 10 (matching the beat tick). Operators who don't need sub-minute freshness can raise it to 60 / 300 / etc to reduce WinRM load. |
| `dhcp_pull_leases_last_run_at` | — | Populated after each pass; visible in Settings. |

> **Windows DHCP has no streaming primitive.** The lease audit log (`DhcpSrvLog-<Day>.log`) and `Get-DhcpServerv4Lease` are the only real windows into the running service, and both are pull-based. WinRM itself is request/response — `Get-Content -Wait` does exist in PowerShell but it holds the HTTPS connection open indefinitely and can't flush partial output back through `pywinrm.run_ps`, so true push-style streaming would require an agent process on the DC that POSTs events back to SpatiumDDI. Short-interval polling is the practical upper bound without adding that complexity.

Per poll:

1. Enumerate agentless DHCP servers (`DHCPServer.driver in AGENTLESS_DRIVERS`).
2. For each, call `driver.get_leases(server)` over WinRM.
3. Upsert the lease into `DHCPLease` by `(server_id, ip_address)`.
4. If the lease's IP falls inside a known subnet, mirror it into `IPAddress` with `status="dhcp"` and `auto_from_lease=True`.
5. **Absence-delete** — any active `DHCPLease` row for this server whose IP didn't appear in the wire response is deleted, along with its mirrored `auto_from_lease=True` IPAM row. The Windows DHCP driver only returns *currently-active* leases, so absence from the response is the server's way of saying "that lease is gone" (admin purged it, client released it, etc.). Before this fix, deleted-on-server leases persisted in our DB indefinitely because `pull_leases` was upsert-only and the time-based cleanup sweep only looked at `expires_at`. The response adds two counters — `removed` (lease rows dropped) and `ipam_revoked` (IPAM mirrors cleaned up alongside) — both surface in the scheduled-task audit row and the manual sync modal.
6. The existing time-based `dhcp_lease_cleanup` sweep still handles leases that drift past `expires_at` between polls (e.g. when lease pull is disabled). The two mechanisms overlap harmlessly.

**Scope absence** — not yet deleted. If an operator removes a scope on the Windows server, the `DHCPScope` row stays in SpatiumDDI's DB until deleted via the UI. Scope-absence cleanup is tracked separately.

### 15.4 Manual "Sync Leases" button

Agentless DHCP servers have a **Sync Leases** button on the server detail header that runs the same lease pull immediately without waiting for the scheduled task. Useful after adding a new server, or when debugging a new lease. From the **subnet detail**, a `[Sync ▾]` dropdown with a **DHCP** entry fans out `POST /dhcp/servers/{id}/sync-leases` across every unique server backing a scope in that subnet and opens a result modal with per-server counters (active / refreshed / new / removed / IPAM revoked + any errors).

### 15.5 Scope auto-import

The first lease pull against a Windows DHCP server also imports its scopes — scopes found on the server but not in SpatiumDDI are created with their options, exclusions, and reservations. This mirrors the auto-import pattern from the DNS "Sync with Servers" flow.

### 15.6 Not yet (Path B full CRUD)

Full config-push to Windows DHCP (analogous to Windows DNS Path B) would unlock:

- Scope options pushed from SpatiumDDI instead of being managed in the Windows DHCP MMC.
- Client class / policy rendering.
- DHCP failover pair configuration from SpatiumDDI.

The per-object CRUD methods are already in place (`apply_scope`, `apply_reservation`, `apply_exclusion`) — what's missing is the API-side wiring that routes write events from the scope / pool / static endpoints into those methods for agentless drivers.

## 16. Rules & constraints

Server-side validations that reject requests with a human-readable
error. Clients should render the `detail` string into their UI — every
rule here has been surfaced to an operator, not just silently logged.

### Scopes

- **Scope already exists for this group + subnet.** A subnet may host
  at most one scope per server group. `409` at
  `backend/app/api/v1/dhcp/scopes.py`.
- **`group_id` required when multiple groups are registered.** If
  more than one DHCP server group is defined, scope-create must name
  one explicitly; single-group deployments can omit the field.
  `422` at `backend/app/api/v1/dhcp/scopes.py`.
- **Hostname sync mode must be one of the configured values.** Mode
  picker is validated against `VALID_SYNC_MODES`; reserved / internal
  modes are not selectable from the API. `422`.
- **DDNS hostname policy enum.** `ddns_hostname_policy` must match one
  of the documented values (see §13). Pydantic validator.

### Pools

- **Pools in the same scope cannot overlap.** Start/end ranges are
  checked against every other pool on the scope before insert. `409`
  at `backend/app/api/v1/dhcp/pools.py:143`.
- **Pool type enum.** `pool_type` must be in `VALID_POOL_TYPES`
  (`dynamic`, `reserved`, `excluded`). Validator at
  `backend/app/api/v1/dhcp/pools.py:40`.

### Static reservations

- **Duplicate MAC in the same group.** A MAC can only be reserved
  once per server group, regardless of which scope it's attached to
  — under the group-centric model every peer in the group serves
  the same reservation so duplicates across scopes in the same
  group are rejected. `409` at `backend/app/api/v1/dhcp/statics.py`.
- **Static IP inside a dynamic pool.** A static reservation whose IP
  falls inside a `dynamic` pool is refused; delete or shrink the pool,
  or convert the pool to `reserved` / `excluded` first. `409` at
  `backend/app/api/v1/dhcp/statics.py:164`.
- **Malformed IP.** Non-parseable IP strings return `422` at
  `backend/app/api/v1/dhcp/statics.py:155`.

### Servers & server groups

- **Duplicate server name.** Each `DHCPServer.name` is globally unique.
  `409` at `backend/app/api/v1/dhcp/servers.py:228`.
- **Driver enum.** `driver` must be one of `kea`, `windows_dhcp`.
  `422` at `backend/app/api/v1/dhcp/servers.py:73`.
- **Read-only drivers refuse config push.** Attempting to push a
  config bundle to an agentless read-only driver (e.g.
  `windows_dhcp`) returns `400` with a message directing the operator
  to `/sync-leases` instead. `backend/app/api/v1/dhcp/servers.py:378`.
- **Windows credentials must be complete on first set.** Setting
  credentials on a `windows_dhcp` server requires both `username` and
  `password` — you can't partial-update an empty credential blob.
  Later edits may include either field alone. `400` at
  `backend/app/api/v1/dhcp/servers.py:310`.
- **Duplicate server-group name.** `409` at
  `backend/app/api/v1/dhcp/server_groups.py:73`.
- **Server-group mode enum.** `VALID_MODES` only; enforced at
  `backend/app/api/v1/dhcp/server_groups.py:35`.

### Kea HA (on a server group)

- **At most 2 Kea members in a group.** `libdhcp_ha.so` only supports
  pairs; adding a third Kea member makes the config ambiguous.
  Validation is a deferred follow-up (see `CLAUDE.md`), not enforced
  at the CRUD layer today.
- **Group mode enum.** `mode` must be `standalone`, `hot-standby`, or
  `load-balancing`. Enforced at `backend/app/api/v1/dhcp/server_groups.py`.
- **HA rendering requires both peers' URLs.** If either Kea member in
  a 2-member group has an empty `ha_peer_url`, the config bundle
  drops the `failover` block and neither peer loads the HA hook —
  silent fall-through to "not-yet-configured" state.
- **Mixed driver groups are OK.** A group can contain Windows DHCP
  servers alongside Kea; only Kea members participate in the HA
  rendering path.

### Client classes

- **Duplicate client-class name per group.** Class names are scoped
  to the server group — every member renders the same classes. `409` at
  `backend/app/api/v1/dhcp/client_classes.py`.
