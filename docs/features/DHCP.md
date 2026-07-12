# DHCP Feature Specification

> **Implementation status (2026-04-28):** Kea driver, agent runtime, container image, backend API, and frontend UI shipped in the `2026.04.16-1` release. Pool overlap validation, existing-IP warning, resize, static ↔ IPAM sync (including DNS forward/reverse), lease → IPAM mirror (with auto-cleanup on expiry), DHCP Pool membership column in the IPAM subnet view, per-scope DHCP defaults prefilled from Settings. **Windows DHCP driver shipped** — Path A (agentless, WinRM + PowerShell, read-only lease monitoring + per-object scope / pool / reservation CRUD). **DDNS pipeline shipped for both paths** — agentless lease pull (2026-04-19) and agent-side Kea lease events (2026-04-21). **Group-centric Kea HA shipped** (`2026.04.21-2`) — load-balanced or hot-standby pairs with self-healing peer-IP drift, supervised daemons, and live `status-get` reporting. **Scope authoring helpers (`2026.04.28-2`):** 95-entry RFC 2132 + IANA option-code library with autocomplete on the custom-options row, plus named group-scoped option templates (e.g. "VoIP phones", "PXE BIOS clients") with a one-click "Apply template…" picker on the scope create / edit modal. **PXE / iPXE provisioning profiles** (issue #51) and **passive DHCP fingerprinting** (Phase 2 device profiling) have since shipped — see §17 and §18. **Still deferred:** Option 82 (relay agent info) class matching, lease histogram by hour, reconciliation report, lease import. NTP (DHCP option 42) is a first-class option.

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
  dhcp_socket_mode: enum(direct, relay)   -- Kea dhcp-socket-type selector
  heartbeat_delay_ms, max_response_delay_ms, max_ack_delay_ms, max_unacked_clients
  auto_failover: bool
  servers: [DHCPServer]          -- 2 Kea members = HA pair
  scopes: [DHCPScope]            -- rendered on every member
  client_classes: [DHCPClientClass]
```

**Client reachability (`dhcp_socket_mode`, issue #365).** Kea's
`dhcp-socket-type` is a *per-daemon* setting — it can't vary per subnet —
so it lives on the group and applies to every member Kea. Two values,
surfaced in the create/edit modal as **Client reachability**:

| Mode | Renders | Use when |
|---|---|---|
| `direct` *(default)* | `dhcp-socket-type: raw` | The server is on the same L2 as its clients. Raw (AF_PACKET) sockets receive broadcast `DHCPDISCOVER`s from clients that have no IP yet **and** relayed traffic — the superset, and Kea's own default. Needs `CAP_NET_RAW` (granted on the appliance DaemonSet + the shipped compose Kea services). |
| `relay` | `dhcp-socket-type: udp` | Kea sits exclusively behind a DHCP relay (`ip helper-address` / `dhcrelay`), or the runtime can't grant raw-socket capability. UDP datagram sockets cannot receive direct L2 broadcasts. |

The value flows to every deploy path (k8s / compose / appliance) through
the ConfigBundle long-poll and is folded into the bundle ETag, so a
change re-renders the running Kea within a heartbeat. It is distinct from
`network_mode` (host vs bridged), which controls the *container's* network
namespace on supervisor-managed appliances — a directly-attached server
typically wants `network_mode: host` **and** `dhcp_socket_mode: direct`.
See [TROUBLESHOOTING.md](../TROUBLESHOOTING.md#dhcp-server-kea-doesnt-respond-to-discover)
for the "no DHCPOFFER" diagnosis flow.

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
    "dns-servers": ["10.0.0.53", "10.0.0.54"],
    "domain-name": "internal.example.com",
    "domain-search": ["internal.example.com", "example.com"],
    "tftp-server-name": "10.0.0.10",   -- for PXE
    "bootfile-name": "pxelinux.0",
    "vendor-class-identifier": "...",
    custom_options: { "176": "..." }   -- vendor-specific options by code
  }
  ddns_enabled: bool
  ddns_hostname_policy: enum(client_provided, client_or_generated, always_generate, disabled)
  address_family: enum(ipv4, ipv6)   -- inferred from the bound subnet's CIDR
  v6_address_mode: enum(stateful, stateless, slaac)  -- v6 only (issue #52)
  ra_managed_flag: bool              -- intended RA M-flag (router-side intent)
  ra_other_flag: bool                -- intended RA O-flag (router-side intent)
  last_pushed_at: timestamp
```

#### DHCPv6 operating mode (issue #52)

For IPv6 scopes, `v6_address_mode` chooses how clients on the subnet get
their address, and drives what the Kea driver renders into the `subnet6`:

| Mode | Kea `subnet6` render | RA flags (set on the router) | Client behaviour |
|---|---|---|---|
| `stateful` | address `pools` + `option-data` + reservations | M=1, O=1 | DHCPv6 hands out the address (IA_NA) and options |
| `stateless` | **no pools**, `option-data` only + reservations | M=0, O=1 | Client SLAACs its address from the RA prefix, asks DHCPv6 (Information-Request) for DNS / domain-search |
| `slaac` | bare subnet — **no pools, no options, no reservations** | M=0, O=0 | The router's RA does everything; DHCPv6 is not involved |

The `ra_managed_flag` (M) / `ra_other_flag` (O) columns record the
intended Router-Advertisement flags, auto-suggested from the chosen mode
(and freely overridable). Historically these were pure "set this on your
router" intent — **as of issue #524 SpatiumDDI can also emit RAs itself**
by running radvd on the DHCP agent from control-plane-rendered config
(opt-in per scope via `ra_enabled`; see §19). The mode picker only appears on IPv6
scopes; v4 scopes ignore these columns and always serve addresses +
options. Changing the mode shifts the agent ConfigBundle ETag, so the
Kea agent re-pulls and re-renders.

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
  id, scope_id                  -- NOT NULL / ON DELETE CASCADE; part of the reservation's identity
  ip_address: inet              -- must fall inside the scope's subnet (422 if not — issue #619)
  mac_address: macaddr          -- primary identifier
  client_id: str (nullable)     -- DHCP client identifier (alternative to MAC)
  hostname: str
  description: str
  options_override: JSONB (nullable)  -- host-specific options
  ip_address_id (FK → IPAddress)      -- linked IPAM record
  deleted_at, deleted_by_user_id, deletion_batch_id  -- soft-delete, as a cascade child of the scope (#617)
  created_by_user_id, created_at
```

`scope_id` cannot be re-pointed. Kea renders a reservation *nested inside* its
scope's `subnet4` stanza and Windows binds it to the scope's network address, so
there is no renderable form of a relocated reservation — a body `scope_id` on
create/update is rejected with a `422` (issue #619). To move one, delete it and
re-create it under the target scope.

### Static Assignment UI Workflow

Static assignments are **created from the IPAM side**, at IP-allocation time. A
static is a child of a DHCP **scope** (it carries `scope_id`), and Kea renders
reservations per scope — so **a scope for the subnet must exist first**, or there
is nothing for the reservation to attach to and Kea's `reservations` array stays
empty.

**Prerequisite — create a scope for the subnet.** A scope lives on a DHCP
**server group** that has at least one Kea member (DHCP is group-centric —
scopes / pools / statics / classes all belong to the group). Create it from
**IPAM → the subnet → "DHCP Pools" tab → Create Scope**, or from
**DHCP → the server group → Scopes tab → New Scope**. A dynamic pool is optional;
the scope alone is enough to emit the `subnet4` block that reservations hang off.

**Create the reservation — two equivalent paths:**

*From the DHCP page:* **DHCP → the server group → "Static Assignments" tab →
New static assignment**. Pick the scope (a picker appears when the group has
more than one), then enter the MAC + IP + hostname and save. Edit and Delete
live on the per-row right-click menu. Creating / editing / deleting a
reservation requires a **superadmin**.

*From IPAM (at allocation time):*

1. **IPAM → select the subnet → "Allocate IP"** (the primary header button; also
   reachable from a free-range gap row or the subnet context menu).
2. In the **Allocate IP Address** modal set **Type / Status = `static_dhcp`**.
   This reveals the **DHCP Scope** picker.
3. Pick the **DHCP Scope**, enter the **MAC address** (required — see caveat
   below), and set the hostname. If no scope exists for the subnet yet, the
   picker offers a **Create a scope** button that opens the scope modal inline.
4. Click **Allocate**. IPAM creates the address row with
   `IPAddress.status = static_dhcp` and mirrors it into the scope as a
   `DHCPStaticAssignment` (via `POST /api/v1/dhcp/scopes/{scope_id}/statics`).

Either way, the Kea agent picks up the new `ConfigBundle` (a group wake fires +
the ETag shifts) and renders the host reservation within seconds. The **Static
Assignments** tab lists every reservation across the group's scopes.

**Caveats / common "nothing happened" traps:**

- **A MAC is mandatory — a blank MAC is rejected, not silent.** Allocating a
  `static_dhcp` address without a `mac_address` returns **422**
  (`mac_address is required when status is 'static_dhcp'`) from both the
  `create` and `next-address` endpoints, so **nothing is created** — neither the
  IPAM row nor the reservation.
- **`static_dhcp` with a MAC but no scope creates the IPAM row and silently skips
  the reservation.** If no DHCP scope is selected (e.g. none exists for the
  subnet yet), the IPAM address is created but the mirror to Kea is **not
  attempted** — this is the real "I set it static but nothing happened" trap.
  Create a scope first (see the prerequisite above).
- **Editing an existing IPAM row to `static_dhcp` does not create a reservation.**
  Flipping an *existing* IP to `static_dhcp` via **Edit** (or bulk-edit) updates
  the IPAM row but does **not** mirror it to Kea. Add the reservation from the
  DHCP **Static Assignments** tab, or delete and re-allocate via the IPAM flow.
- **Creating a static currently requires a superadmin.** A non-superadmin who
  allocates a `static_dhcp` IP gets the IPAM row but the mirror call returns 403.

### Troubleshooting — reservations render empty in Kea

Reservations are emitted **per subnet**, nested inside each `subnet4` / `subnet6`
object — there is no top-level reservations list, so check the `reservations`
array *inside* the relevant `subnet4`. If a reservation you created never shows
up, walk this checklist — each item is a real, mostly-silent drop point:

- **The static was never actually created.** Confirm it exists via
  `GET /api/v1/dhcp/scopes/{scope_id}/statics` or the group's Static Assignments
  tab. If it's absent, distinguish two cases: the allocation was
  **rejected** (a blank MAC 422s the whole allocation — nothing was created), or
  it **succeeded without mirroring** (a MAC was given but no scope was selected,
  or a non-superadmin hit 403 on the mirror call). See the caveats above.
- **The scope is inactive.** Only `is_active = true` scopes (and the statics
  under them) are assembled into the config bundle — a disabled scope silently
  drops every reservation it holds.
- **The scope's subnet was deleted.** If the `Subnet` backing a scope is gone but
  the scope survived, the scope and all its statics are silently skipped during
  bundle assembly.
- **The scope's group has no Kea member.** A server not attached to a group (or a
  group with zero Kea members) renders an empty bundle — nothing polls for the
  reservation.
- **The reservation IP is outside the scope's subnet.** Rejected with a `422`
  since issue #619 — but a row created *before* that landed (or written by an
  importer) can still be out of range, and Kea rejects the *entire* config at
  load when a host reservation falls outside its subnet, so one out-of-range IP
  makes **all** statics on that server silently fail while the agent keeps
  serving its last-good config. Keep the reserved IP inside the subnet CIDR.
- **Pure-SLAAC IPv6 scopes emit no reservations by design** — a v6 scope in
  SLAAC-only mode has no stateful addressing role, so its `reservations` is
  always empty.

### Conflict Detection

`create_static` / `update_static` reject (never silently) on:
- **Duplicate MAC within the server group** — a MAC can be reserved only once
  across every scope in the same group (409).
- **IP inside a `dynamic` pool on the scope** — the IP must be excluded from any
  dynamic pool first (409). (Reserved/excluded pools do not block a static.)
- **IP outside the scope's subnet** — 422 (issue #619). See §16 for the
  rationale.
- **A body `scope_id`** — 422 (issue #619). The scope comes from the path on
  create and cannot change on update.
- **Malformed IP** — 422.
- **Malformed hostname** — 422. The reservation hostname is operator-entered,
  so it is validated against the RFC 1123 host rule rather than sanitized (see
  `DNS.md` §18).

---

## 3a. Scope deletion, cascade, and restore

Deleting a DHCP scope is a **soft delete** by default: the row is stamped
(`deleted_at` / `deleted_by_user_id` / `deletion_batch_id`), disappears from
every read surface, and is recoverable from **Administration → Trash** for the
retention window (`PlatformSettings.soft_delete_purge_days`, default **30
days**) before the nightly purge sweep hard-deletes it. `DELETE
/api/v1/dhcp/scopes/{id}?permanent=true` skips the trash — but the UI never
sends that flag, so the soft path is what an operator actually exercises.

Four things happen on that path. Each was a shipped bug until `2026.07.11-1`
(issues #616–#619, migration `b3e7d21c9f04`).

### Pools and reservations cascade with the scope (#617)

`DHCPPool` and `DHCPStaticAssignment` carry `SoftDeleteMixin` and ride the
scope's `deletion_batch_id`
(`backend/app/services/soft_delete.py::_collect_descendants`), so one **Restore**
brings the scope back **whole** — its ranges and its reservations with it. They
are cascade-only children: never soft-deleted on their own, never listed in the
trash individually (`SOFT_DELETE_RESOURCE_TYPES` deliberately omits them), but
present in `TYPE_TO_MODEL` so `restore_batch` sweeps them.

Before this, a scope was treated as a cascade **leaf** — its pools and
reservations stayed live and un-stamped under a hidden parent:

- still answering `GET /api/v1/dhcp/scopes/{id}/statics`,
- still enforcing the group-wide MAC conflict check, which `409`'d naming a
  scope UUID the operator could no longer see,
- still visible to the `find_dhcp_statics` MCP tool,
- and the approval-workflow preview reported a **zero blast radius** for a scope
  holding hundreds of reservations.

Restore is conflict-checked per row: while the scope sat in the trash a live
scope in the same group may have claimed one of its MACs, so
`default_conflict_check` refuses the restore rather than resurrecting a
group-wide duplicate the create path would have refused. The two reservation
uniqueness rules are **partial** unique indexes (`WHERE deleted_at IS NULL`), so
a trashed reservation never holds the `(scope, mac)` / `(scope, ip)` slot against
a live one.

### Agentless write-through fires on the soft path (#616)

Soft-delete means *stop serving*, and that has to hold on every backend. Kea
members converge on their own — a stamped scope drops straight out of the
rendered `ConfigBundle` and the ETag shifts. **Agentless** members (Windows DHCP
today) only converge on an explicit push, and `push_scope_delete` previously ran
**only** on the `permanent=true` branch. Since the UI never sends that flag, a
UI-deleted scope vanished from SpatiumDDI and from Kea's rendered config while
the Windows DHCP server kept serving it — **and its reservations** — forever.
Nothing removed it later either: the trash permanent-delete skips the
write-through hooks, and the purge sweep is a Core `DELETE` that runs no Python.

The push is now driven off the **soft-delete batch**
(`backend/app/services/ai/operations_risky.py::_push_agentless_scope_deletes`)
rather than a hand-rolled per-handler query, so **every** delete whose cascade
can reach a scope — scope, subnet, block, space — is covered, and a new ancestor
type added to `_collect_descendants` cannot silently skip it. Order is
load-bearing: the push runs *before* `apply_soft_delete` stamps the batch, or the
global filter would hide the subnet the push needs to resolve the scope's CIDR.

**Restore pushes the inverse** — `push_scope_restore` re-creates the scope, its
non-dynamic pools, and its reservations on every Windows member. It is
best-effort by design (failures are logged, not raised): a `502` from an
unreachable Windows box would roll the DB restore back and make the row
*unrestorable*, which is the opposite of what a recovery action should do.

### The IPAM mirror is released on wholesale deletes (#618)

A reservation owns an `ip_address` row at `status="static_dhcp"`, back-linked via
`IPAddress.static_assignment_id`. `_detach_ipam_for_static` used to live in the
router, so it was only reachable from the per-reservation CRUD handlers — every
path that destroys reservations *in bulk* (FK `CASCADE`, or a Core `DELETE`: no
Python runs) stranded the mirror at `status="static_dhcp"` pointing at a
reservation Postgres had already dropped. Not allocated, not free, not
reclaimable by any sweeper.

It is now `backend/app/services/dhcp/static_ipam.py` —
`detach_ipam_for_static` + `detach_ipam_for_scope_statics` — and wired into every
wholesale path:

| Path | Where |
|---|---|
| Scope permanent-delete | `services/ai/operations_risky.py::_apply_delete_scope` |
| Trash permanent-delete | `api/v1/admin/trash.py::permanent_delete_from_trash` |
| Nightly purge sweep | `tasks/trash_purge.py::_release_ipam_mirrors` |
| DHCP server-group delete | `services/ai/operations_risky.py::_apply_delete_group` |
| DHCP importer `overwrite` mode | `services/dhcp_import/commit.py` |

The row is released to `available` (not `allocated`): a leftover `allocated` row
is skipped by the agent's lease-mirror refresh, so it would shadow a future
dynamic lease at that IP *and* never be reaped (#478). Migration `b3e7d21c9f04`
repairs the rows already stranded by pre-existing hard-deletes.

### Known gap — Windows scope sync (issue #620)

`services/dhcp/pull_leases.py::_upsert_scope` still Core-`DELETE`s a Windows
scope's reservations and re-inserts them from the wire without going through the
release path, so a **UI-created reservation's IPAM mirror can be stranded on the
next Windows scope sync**. It is deliberately *not* patched with a plain detach:
that reconciler runs on a schedule, and a detach would tear down and recreate the
forward A record on every pass for reservations that never changed. It needs a
re-point-by-IP reconcile instead.

The `IPAddress.static_assignment_id` `varchar` → `uuid` FK retype is deferred
alongside it — it breaks the #296 rolling-upgrade contract and needs a
two-release expand/contract.

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

### Migrating from an existing DHCP server (issue #129)

The **DHCP configuration importer** is the one-shot path for loading a
real DHCP estate into SpatiumDDI without retyping every scope, pool,
reservation, and option-set. It lives under **DHCP Import** in the
sidebar (gated by the `dhcp.import` feature module, default-on) and
covers three sources:

- **Kea** — upload a `kea-dhcp4.conf` / `kea-dhcp6.conf` from a
  non-managed daemon.
- **Windows DHCP** — live-pull every IPv4 scope from a registered
  `windows_dhcp` server over WinRM (reuses the Path A read driver).
- **ISC DHCP** — upload a `dhcpd.conf` (the importer ships its own
  tokeniser; ISC is supported as an *import source* only — SpatiumDDI
  does not run ISC daemons).

The flow is preview → commit: the preview parses the source and shows
the would-create scopes (with pool / reservation counts, conflicts,
IPAM linkage, and a "didn't import" panel) before any DB write; commit
writes each scope in its own savepoint. Every imported row is stamped
with `import_source` + `imported_at` provenance. Each scope either
links to an existing IPAM subnet on its CIDR or auto-creates one under
an operator-chosen IP space + block. **Live leases are never imported**
— they repopulate from the running daemon once a Kea server is attached
to the target group.

Full reference: [Migration](MIGRATION.md). Parser internals:
[DHCP Drivers § Importing existing daemon configs](../drivers/DHCP_DRIVERS.md).

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
| 6 | Domain Name Server | DNS server IPs. Canonical option name is `dns-servers`; the IANA name `domain-name-servers` is accepted as a legacy alias on write and still maps to code 6 on read |
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

`sync-leases` is an agentless-only operation — agent-based Kea streams lease events continuously and converges scope/config via the ConfigBundle long-poll, so there is nothing to pull. As of `2026.06.25-1` (#453) calling it against an agent-based server is **no longer a 400**: the endpoint returns a no-op `SyncLeasesResponse` with an explanatory `note` (and nudges the agent to re-poll its config so the scope definition converges immediately), rendered as an info line in both the subnet sync modal and the server-detail banner.

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
- **Static IP inside a dynamic pool is allowed (#631).** Pinning a
  reservation whose IP falls inside a `dynamic` pool is the standard
  idiom on every driver we ship — Kea honours it (default
  `reservations-out-of-pool: false` won't hand the reserved address to
  another client), FortiGate renders `reserved-address` independently
  of `ip-range`, and Windows *requires* the reservation to fall inside
  the scope's range. No conflict check refuses it. Pool occupancy
  counts in-pool reservations as assigned so exhaustion isn't
  under-reported even while the reserved device is offline.
- **Reservation IP outside the scope's subnet.** `422` at
  `backend/app/api/v1/dhcp/statics.py:177` (issue #619). Kea renders a
  reservation **nested inside** its subnet's `subnet4` stanza and Windows
  binds it to the scope's network address, so an out-of-CIDR reservation
  ships structurally invalid config to the agent — Kea refuses the whole
  config at load, taking every *other* reservation on that server down with
  it. Caught here as a legible 422 instead of a downstream agent failure.
- **A body `scope_id` on create / update.** `422` at
  `backend/app/api/v1/dhcp/statics.py:41` (issue #619). A reservation belongs
  to its scope (uniqueness is keyed on it, and there is no renderable form of
  a relocated row), so on create the scope comes from the path and on update
  it cannot change at all. It used to be **silently dropped with a `200`**
  (Pydantic's default `extra="ignore"`), so a caller re-pointing a reservation
  got no error and no effect. Rejected as a *declared* field rather than via a
  blanket `extra="forbid"` — that would also `422` an ordinary
  GET → edit → PUT round-trip, since `StaticResponse` carries the server-owned
  `id` / `created_at` / `modified_at`.
- **Malformed IP.** Non-parseable IP strings return `422` at
  `backend/app/api/v1/dhcp/statics.py:162`.
- **Malformed hostname.** The reservation hostname is operator-entered, so a
  bad value is rejected (`422`), not sanitized — RFC 1123 host rule, via
  `app.core.dns_names.validate_hostname` at
  `backend/app/api/v1/dhcp/statics.py:30`. (A *client-supplied* hostname
  arriving off the DHCP lease wire is sanitized instead — see `DNS.md` §18.)
- **Malformed `domain-name` / `domain-search` scope option.** `422` — validated
  as an FQDN at `backend/app/api/v1/dhcp/scopes.py:90`.

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

## 17. Passive DHCP Fingerprinting (Phase 2 device profiling)

Auto-populate "what is this thing?" for every DHCP-leasing client by
sniffing each DISCOVER / REQUEST and looking the resulting
option-55 / option-60 signature up against fingerbank's device
taxonomy. Pairs with the active layer (auto-nmap on lease) — both
write into the same `IPAddress.device_type` / `device_class` /
`device_manufacturer` columns, so the IP detail modal shows one
consolidated answer regardless of which layer enriched the row.

### How it flows

1. The DHCP agent's `DhcpFingerprintShipper` thread runs scapy's
   `AsyncSniffer` against the BPF filter `udp and (port 67 or port 68)`
   and extracts option-55 (parameter request list), option-60
   (vendor class), option-77 (user class) and option-61 (client id)
   from each DISCOVER / REQUEST.
2. Observations are buffered + deduped at the agent layer (one
   batch per MAC per minute) and POSTed to
   `/api/v1/dhcp/agents/dhcp-fingerprints` every 10 s in batches of
   up to 50.
3. The control plane upserts the fingerprint into the
   `dhcp_fingerprint` table (keyed by MAC) and enqueues a Celery
   task per fresh / changed signature.
4. The task hits fingerbank's `/api/v2/combinations/interrogate`
   endpoint, caches the result on the row for 7 days, and stamps
   matching `IPAddress` rows (joined on MAC) with the resolved
   `device_type` / `device_class` / `device_manufacturer`. Rows
   with `user_modified_at` set are left alone — the operator's
   edits win.

### Enabling it

The feature is **default off** for two reasons: it requires
`CAP_NET_RAW` to bind a BPF socket, and sniffing every DHCP
transaction is privacy-sensitive on guest / BYOD subnets.

**On the agent** — add the env var **and** the Linux capability to
your compose override:

```yaml
services:
  dhcp-kea:
    cap_add:
      - NET_RAW
    environment:
      DHCP_FINGERPRINT_ENABLED: "1"
      # Optional — defaults to "any" which works for host-networked
      # containers. Bridge-networked containers should pick a real
      # interface name (eth0, etc).
      DHCP_FINGERPRINT_IFACE: "any"
```

The shipped `docker-compose.yml` does **not** add `NET_RAW`
unconditionally — operators who don't need fingerprinting shouldn't
have to grant the capability. Same reasoning for not enabling it via
default env.

**On the control plane** — set the fingerbank API key in
**Settings → IPAM → Device Profiling**. The form is a password-style
input with a "Configured ✓ Replace… Clear" view once a key is on file
(the encrypted value is never echoed back). Without a key, the agent
still ships raw signatures (visible as the option-55 / option-60
strings under the "Raw signature" disclosure on the IP detail
modal), but no enrichment runs. Get a key at https://fingerbank.org
(free tier: 30 lookups / hour / account is plenty for small-to-medium
fleets).

### Privacy + data shape

The fingerprint table is **MAC-keyed, not lease-keyed**. We don't
record per-transaction history; one row per device, refreshed
in-place on every observation. That means:

- A device that comes and goes still produces a single row with
  `last_seen_at` bumped on every observation.
- The raw option-55 / option-60 strings persist in the DB for
  operator triage. There's no separate retention sweep — the table
  is bounded by the number of unique MACs the agent has ever seen,
  which scales with the size of your physical fleet.
- We don't capture client IPs in fingerprints — the linkage to a
  specific IP comes from `IPAddress.mac_address` and is updated on
  every lease event (the existing path).

### Operator-facing UI

The IP detail modal surfaces both the enriched device line and (in
a "Raw signature" disclosure) the option-55 / option-60 strings.
The same surface lets operators force a re-lookup if they think the
fingerbank result is wrong (the dispatched task ignores the cache
window for that one MAC).

## 18. PXE / iPXE provisioning profiles (issue #51)

Operator-curated `pxe_profile` rows wrap the per-architecture TFTP
boot story so the same scope can serve legacy PXE BIOS, EFI x86_64,
EFI ARM64, and EFI x86 clients without hand-written client classes.

**Data model.** New `pxe_profile` table — name + description +
`next_server` (TFTP host) + four nullable `boot_filename_*` columns
keyed on architecture (`bios_x86` / `efi_x86_64` / `efi_arm64` /
`efi_x86`) + an optional `ipxe_script` body. Profiles live at the
DHCP server-group level so the same profile serves every Kea
server in the group.

**Per-scope binding.** `DHCPScope.pxe_profile_id` is a SET-NULL FK
to `pxe_profile`. The scope create / edit modal surfaces a "PXE
profile" picker; null = no PXE on this scope.

**Kea render.** When a scope has a PXE profile attached, the agent
renders one `client-class` per arch-match (matching DHCP option 93
via `option dhcp.client-arch`) plus one `iPXE` class guarded by a
substring match on `option dhcp.user-class` so legacy PXE clients
see the BIOS bootfile and iPXE clients (which loop back with the
`iPXE` user-class set) jump straight to the chained iPXE script.

**Admin UI.** New `/dhcp/groups/{id}/pxe` page lists profiles for a
group with create / edit / delete + an in-line preview of the
rendered Kea client-class block.

## 19. IPv6 Router Advertisements + rogue-RA detection (issue #524)

SpatiumDDI ships DHCPv6 via Kea, but Kea does not emit ICMPv6 Router
Advertisements. This feature lets the DHCP agent run **radvd** from
config rendered by the control plane, and passively watches the segment
for **rogue RAs**. Both live behind the default-enabled
`ipv6.router_advertisements` feature module (Settings → Features).

### 19.1 RA management (radvd)

RA config is per-IPv6-scope, opt-in via `DHCPScope.ra_enabled`. When on,
the control plane renders a full `radvd.conf` stanza for the subnet and
ships it in the **DHCP ConfigBundle** (`radvd_conf`, folded into the
bundle ETag so a change wakes the agent long-poll — same path as the Kea
config). The agent writes it to `RADVD_CONFIG_PATH` and reloads radvd
(SIGHUP via pidfile); the last-known-good config rides the on-disk bundle
cache, so radvd keeps advertising if the control plane is unreachable
(non-negotiable #5).

Per-scope RA columns (v6 scopes only; edited in the scope modal's IPv6
section):

| Column | Meaning |
|---|---|
| `ra_enabled` | Opt-in — emit RAs for this subnet |
| `ra_mo_override` | Use `ra_managed_flag`/`ra_other_flag` verbatim instead of deriving M/O from the mode |
| `ra_router_lifetime` | `AdvDefaultLifetime` (s); 0 = not a default route |
| `ra_max_interval` | `AdvMaxInterval` (s) between unsolicited RAs |
| `ra_prefix_valid_lifetime` / `ra_prefix_preferred_lifetime` | Advertised prefix lifetimes (s) |
| `ra_prefix_on_link` / `ra_prefix_autonomous` | Per-prefix `AdvOnLink` / `AdvAutonomous` (SLAAC) |
| `ra_interface` | Host NIC radvd advertises on (blank = agent `RADVD_DEFAULT_IFACE`) |

**M/O derivation.** By default the advertised M (Managed) / O (Other)
flags derive from the scope's `v6_address_mode`: `stateful` → (1,1),
`stateless` → (0,1), `slaac` → (0,0). Set `ra_mo_override` to advertise
the literal `ra_managed_flag`/`ra_other_flag` instead.

**RDNSS / DNSSL.** The RA advertises RDNSS (RFC 8106 IPv6 resolvers) from
the scope's `dns-servers` option (IPv6 only), falling back to the
subnet's `dns_servers`; DNSSL search domains come from `domain-search` /
`domain-name` / the subnet's `domain_name`.

**Running radvd.** radvd is baked into the Kea agent image but started
only when `RADVD_MANAGED=1` (needs `CAP_NET_RAW` + `CAP_NET_ADMIN` and
host `net.ipv6.conf.all.forwarding=1`). The entrypoint waits for the
agent to render a config before launching radvd. The `/dhcp/groups/{id}`
→ **Router Adverts** tab previews the rendered `radvd.conf` + resolved
per-subnet M/O.

### 19.2 Rogue-RA detection

The IPv6 twin of the rogue-DHCP probe. An opt-in passive sniffer on the
DHCP agent (`DHCP_RA_SNIFFER_ENABLED=1`, same `CAP_NET_RAW` posture as
the fingerprint sniffer, default OFF) uses a scapy `AsyncSniffer` for
ICMPv6 type-134 RAs and ships each observed router (source IP + MAC,
advertised prefixes, M/O flags, router lifetime) to
`POST /dhcp/agents/ra-observations`.

The control plane classifies each source against the group's
**expected-router allowlist** (`ra_router_allowlist`, matched on source
IP or MAC): on the list → `expected`, otherwise → `rogue`, upserting a
`ra_observed_router` row per (group, source IP). The **`rogue_ra` alert
rule** (seeded disabled, enable once the sniffer is on) fires on rows
classified `rogue` within its recency window and rides the standard
AlertEvent fan-out (syslog / webhook / SMTP / chat). Acknowledging a
router from the Router Adverts tab allowlists it and reclassifies it so
the alert auto-resolves.

### 19.3 API + MCP

REST (gated by the module): `GET /dhcp/ra/groups/{id}/ra-config`
(rendered preview), `GET|POST /dhcp/ra/groups/{id}/observed-routers`
(+`/{id}/acknowledge`), and `GET|POST|DELETE
/dhcp/ra/groups/{id}/ra-allowlist`. Operator-Copilot tools:
`find_ra_subnets`, `find_observed_ra_routers`, `count_rogue_ra_routers`
(reads, default on) + `propose_allowlist_ra_router` (write proposal).
