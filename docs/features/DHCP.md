# DHCP Feature Specification

> **Implementation status (2026-04-15):** Kea driver, agent runtime, container image, backend API, and frontend UI landed. ISC DHCP driver, HA hook library coordination, DDNS pipeline, and reconciliation report are deferred. NTP (DHCP option 42) is supported as a first-class option.

## Overview

SpatiumDDI manages DHCP servers as authoritative configuration sources. The IPAM database is the **source of truth** — DHCP server configs are pushed from IPAM, not read from the servers. DHCP servers are configured to report lease events back to SpatiumDDI for real-time IP status tracking and DDNS updates.

Supported backends: **ISC Kea DHCP** (preferred), **ISC DHCP v4** (legacy).

---

## 1. DHCP Server Model

```
DHCPServer
  id, name, description
  driver: enum(kea, isc_dhcp)
  host, port
  credentials (encrypted Fernet)
  roles: [enum(dhcp4, dhcp6)]     -- server can run both
  server_group_id (nullable)      -- logical grouping for HA pairs
  status: enum(online, offline, degraded, syncing)
  last_sync_at, last_health_check_at
  config_cache_path: str          -- local path where agent caches last-good config
```

### DHCP Server Groups

For HA deployments, two DHCP servers can be placed in the same **server group** (Kea uses `HA hook library`; ISC DHCP uses `failover peer`). The SpatiumDDI driver is aware of the HA relationship and pushes config to both.

```
DHCPServerGroup
  id, name
  mode: enum(load-balancing, hot-standby)
  servers: [DHCPServer]          -- exactly 2 for failover/HA
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
  └── DHCPScope (one per subnet per DHCP server)
        ├── DHCPPool (one or more dynamic ranges)
        └── DHCPStaticAssignment (zero or many)
```

### DHCPScope Model

```
DHCPScope
  id, server_id, subnet_id
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
  class_restriction: str (nullable)   -- Kea client class or ISC DHCP "allow" class
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
  id, server_id
  name: str             -- e.g., "VoIPPhones"
  match_expression: str -- Kea expression or ISC DHCP conditional
                        -- e.g., "option[60].hex == 'Cisco7960'"
  description: str
```

Classes are referenced by pool `class_restriction` field. The DHCP driver translates these to server-native syntax.

---

## 5. DHCP Lease Tracking

Leases are **read-only** in SpatiumDDI — they are pulled from the DHCP server, not managed directly.

```
DHCPLease (not persisted long-term — cached in Redis, written to DB for history)
  ip_address, mac_address, hostname
  scope_id, server_id
  starts_at, ends_at, expires_at
  state: enum(active, expired, released, abandoned)
  client_id, user_class
  last_seen_at
```

### Lease Sync Strategy

- **Real-time (preferred)**: Kea `lease_cmds` hook + webhook to SpatiumDDI on lease events
- **Polling fallback**: Celery task pulls lease dump every N minutes via DHCP driver API
- Leases are used to update `IPAddress.status`, `IPAddress.last_seen`, and trigger DDNS

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

The DHCP daemon config file (Kea JSON or ISC DHCP `dhcpd.conf`) is generated from this cache. On startup, the agent always checks if the cache is newer than the running daemon config and applies if so.

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
- Formats: JSON (native), ISC DHCP config snippet, Kea config JSON

### Import (file → IPAM)
- Import from ISC DHCP `dhcpd.conf` (parse scopes, ranges, hosts)
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

## 14. DHCP Pool Coordination Between Multiple Containers

When multiple DHCP server containers serve the same pool (for HA), they must not assign the same IP to different MAC addresses. SpatiumDDI handles this via:

### Kea HA (preferred)

Kea's built-in `HA hook library` handles pool coordination natively:

- **Load-balancing mode**: Each server handles 50% of the pool range (split by hash of client MAC). If one server fails, the other takes the full range.
- **Hot-standby mode**: Primary handles all requests; secondary takes over on primary failure.
- The two Kea nodes communicate directly via the Kea Control Agent API — no SpatiumDDI coordination needed.

Configuration pushed by SpatiumDDI to both nodes in a server group includes the HA section automatically.

### ISC DHCP Failover

ISC DHCP uses the native failover protocol (RFC 3074). SpatiumDDI pushes matching `failover peer` config to both servers.

### SpatiumDDI-level coordination (future)

For non-HA architectures serving different subnets from multiple containers:
- Each container serves non-overlapping subnets (managed by SpatiumDDI scope assignments)
- Scope assignments are never duplicated across servers without an explicit HA group
