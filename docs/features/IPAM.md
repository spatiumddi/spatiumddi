# IPAM Feature Specification

> **Implementation status (post-`2026.04.19-1`):** Full hierarchical CRUD (spaces, blocks with nesting, subnets, addresses); next-available allocation that skips dynamic DHCP pool ranges; orphan soft-delete + bulk orphan purge modal; block utilization rollup via recursive CTE; block/subnet overlap validation via PostgreSQL `cidr &&` operator; **grow-only subnet + block resize** with blast-radius preview, cross-subtree overlap scan, typed-CIDR confirmation gate, and pg advisory lock during commit; DNS assignment inheritance (space → block → subnet) with dual-listbox picker for additional zones and shared `ZoneOptions` primary/additional separator across Create / Edit / Bulk-edit flows; DNS sync check (subnet / block / space scope) reconciling missing, mismatched, and stale records, with a `[Sync ▾]` dropdown (DNS / DHCP / All) on the subnet header plus result modals for each; **scheduled IPAM ↔ DNS auto-sync** (opt-in Celery beat task gated on `PlatformSettings.dns_auto_sync_enabled`); reverse-zone auto-create + backfill; IP aliases (CNAME/A tied to the IP, auto-cleaned on purge) with single-step delete confirmation + query-invalidation fix for the subnet Aliases tab; VLAN association (router + VLAN columns); DHCP scope/pool/static linkage with per-IP pool-membership badge, **pool boundary rows in the IP listing**, and **dynamic-pool allocation gates** (422 on manual allocation, skipped by `/next-ip`); static DHCP creation flow integrated into Allocate IP; drag-drop reparenting; **bulk-edit IPs with per-field opt-in toggles** (status, description, tags-merge-or-replace, custom-fields merge, DNS zone); **IP assignment collision warnings** (FQDN + MAC collisions across any subnet; 409 with `force`-flag reconfirm); import/export (CSV/JSON/XLSX) with UTC-timestamped filenames + **subnet-scoped IP address importer**; custom fields per resource type with inherited-value placeholders on Edit Subnet / Edit Block modals; global search (Cmd+K); **mobile-responsive layout** (sidebar drawer, horizontally scrollable tables, modals cap at `95vw`); every modal is draggable (`bg-black/20` backdrop, Esc close). **Partial IPv6:** storage, UI, subnet create, AAAA/PTR sync, `/blocks/{id}/available-subnets` up to `/128`, and per-block "Find by size" with family-aware prefix options all land — remaining TODOs are EUI-64 / hash-based `/128` allocation for `/next-address` (returns 409 on v6 today).

## Overview

The IPAM module is the core of SpatiumDDI. It manages the hierarchy of IP space from broad routing domains down to individual IP addresses. All other modules (DHCP, DNS) reference IPAM resources.

---

## 1. IP Hierarchy

```
IPSpace  (VRF / routing domain)
  └── IPBlock  (aggregate/supernet, e.g., 10.0.0.0/8)
        └── IPBlock  (nested, e.g., 10.1.0.0/16)
              └── Subnet  (routable network, e.g., 10.1.2.0/24)  ← primary managed unit
                    ├── IPAddress  (individual host IP)
                    └── DHCPScope → DHCPPool(s)
```

### IP Space
- Represents a **VRF** or isolated routing domain (e.g., "Corporate", "Internet", "OOB")
- IPs in different spaces may overlap without conflict
- One space can be marked `is_default` for simplified deployments

### IP Block
- Aggregate/supernet range (e.g., `10.0.0.0/8`, `172.16.0.0/12`)
- Used for **organizational grouping** — you cannot directly assign IPs to a block
- Blocks can be **nested** (a /16 block inside a /8 block)
- Sub-blocks and subnets are children in the tree

### Subnet
- The primary unit of management — a routable network with a gateway
- IPs are allocated, tracked, and assigned at the subnet level
- Subnets cannot overlap within the same IP Space

---

## 2. IP Block & Subnet Tree UI

Both IP blocks and subnets are displayed in a **tree view** in the UI, mirroring the network hierarchy.

```
Corporate (IPSpace)
├── 10.0.0.0/8  [IPBlock — 42% used]
│   ├── 10.0.0.0/16  [IPBlock — HQ]
│   │   ├── 10.0.1.0/24  [Subnet — Servers VLAN 10]
│   │   ├── 10.0.2.0/24  [Subnet — Workstations VLAN 20]
│   │   └── 10.0.3.0/24  [Subnet — VoIP VLAN 30]
│   └── 10.1.0.0/16  [IPBlock — Branch Office]
│       └── 10.1.1.0/24  [Subnet — Branch LAN VLAN 100]
└── 10.128.0.0/9  [IPBlock — DMZ]
    └── 10.128.0.0/24  [Subnet — Public Services]
```

### Tree Features
- Expand/collapse nodes
- Utilization color-coded bars (green < 70%, amber 70–90%, red > 90%)
- Right-click context menu: Create child block, Create subnet, Edit, Delete
- Drag nodes to reorganize (with validation — child must fit inside parent CIDR)
- Free space visualization: show unallocated ranges within a block

---

## 3. Subnet Model (Extended)

```
Subnet
  id, space_id, block_id (nullable)
  network: cidr              -- e.g., 10.1.2.0/24
  name, description
  
  -- Layer 2 / Switching
  vlan_id: int (nullable)    -- 802.1Q VLAN tag (1–4094)
  vxlan_id: int (nullable)   -- VXLAN VNI (1–16777215)
  
  -- Routing Context
  gateway: inet (nullable)
  router_zone_id (FK → RouterZone, nullable)  -- see section 4
  
  -- DNS Integration
  forward_zone_id (FK → DNSZone, nullable)    -- primary A records zone
  reverse_zone_id (FK → DNSZone, nullable)    -- PTR records zone
  dns_servers: inet[] (nullable)              -- override IPs to push to DHCP clients
  domain_name: str (nullable)                 -- primary domain suffix (DHCP option 15)
  -- Multiple domains: see SubnetDomain junction table (section 11)
  
  -- DHCP
  dhcp_scope_id (FK → DHCPScope, nullable)
  dhcp_server_group_id (FK → DHCPServerGroup, nullable)
  
  -- Status / Metadata
  status: enum(active, deprecated, reserved, quarantine)
  utilization_percent: float (computed)
  total_ips: int (computed)
  allocated_ips: int (computed)
  
  -- Custom Fields (see section 6)
  custom_fields: JSONB
  
  tags: JSONB                 -- free-form key/value tags
  created_at, modified_at
```

---

## 4. VLAN / VXLAN and Router Zones

Subnets can be associated with Layer 2 and routing context.

### VLAN / VXLAN Assignment

- `vlan_id`: 802.1Q VLAN tag. Informational in IPAM (SpatiumDDI does not configure switches directly).
- `vxlan_id`: VXLAN VNI. Used in overlay network environments.
- Both can be set simultaneously (e.g., VLAN 100 mapped to VNI 10100).
- A lookup table `VLANMapping` tracks VLAN → VXLAN mappings for reference.

### Router Zone / Domain

A **RouterZone** groups subnets that are locally significant to a routing domain or site:

```
RouterZone
  id, name, description
  type: enum(site, vrf_lite, mpls_domain, data_center, custom)
  parent_zone_id (nullable)   -- zones can be nested (campus → building → floor)
  contact_info: str           -- who manages routing in this zone
  notes: str
```

Subnets assigned to the same RouterZone imply they share routing context (e.g., all subnets on a campus router). This is informational in Phase 1 but can drive automation in later phases (pushing route configs to routers via API).

---

## 5. IP Address Model (Extended)

```
IPAddress
  id, subnet_id
  address: inet
  status: enum(
    available,      -- not assigned, not seen
    allocated,      -- manually assigned in IPAM
    reserved,       -- held but not assigned (e.g., future use, gateway placeholder)
    dhcp,           -- currently active DHCP lease
    static_dhcp,    -- has a static DHCP assignment (requires mac_address)
    discovered,     -- seen on network but not in IPAM
    orphan,         -- was assigned, device no longer seen
    deprecated      -- was assigned, now decommissioned
  )
  hostname: str (nullable)
  fqdn: str (nullable, computed from hostname + domain)
  domain_id (FK → DNSZone, nullable)   -- which domain this hostname belongs to
  mac_address: macaddr (nullable)      -- required when status = static_dhcp
  description: str
  
  -- Ownership
  owner_user_id (FK → User, nullable)
  owner_group_id (FK → Group, nullable)
  managed_by: str (nullable)   -- free text, or pulled from custom fields
  
  -- Linked Records
  dns_record_id (FK → DNSRecord, nullable)
  dhcp_lease_id (FK → DHCPLease, nullable)
  static_assignment_id (FK → DHCPStaticAssignment, nullable)
  
  -- Discovery
  last_seen_at: timestamp (nullable)
  last_seen_method: enum(ping, arp, dhcp, manual, snmp)
  
  -- Custom Fields
  custom_fields: JSONB
  
  tags: JSONB
  created_at, modified_at, created_by_user_id
```

### Next Available IP Allocation

`POST /api/v1/ipam/subnets/{id}/next`

```json
{
  "strategy": "sequential",    // or "random"
  "skip_gateway": true,
  "hostname": "app-server-01",
  "description": "New app server",
  "custom_fields": { "ticket": "INC-12345" }
}
```

Returns the allocated IPAddress. Allocation is atomic (uses DB-level `SELECT ... FOR UPDATE` to prevent race conditions).

---

## 6. Custom Fields

Both **Subnets** and **IPAddresses** (and **IPBlocks** and **DNSZones**) support administrator-defined custom fields. This allows organizations to store domain-specific metadata without schema changes.

### Custom Field Definition

```
CustomFieldDefinition
  id
  resource_type: enum(subnet, ip_address, ip_block, dns_zone, dhcp_scope)
  name: str               -- snake_case key (e.g., "ticket_number")
  label: str              -- human-readable (e.g., "Change Ticket")
  field_type: enum(text, number, boolean, date, email, url, select, multi_select)
  options: JSONB (nullable)  -- for select/multi_select: list of allowed values
  is_required: bool
  is_searchable: bool        -- if true, indexed for search
  default_value: str (nullable)
  display_order: int
  description: str
```

### Example Custom Fields for Subnets

| Field Name | Type | Example Value |
|---|---|---|
| `managed_by` | text | "Network Team - Alice Smith" |
| `contact_email` | email | "netops@example.com" |
| `ticket_number` | text | "CHG-45678" |
| `environment` | select | "production" / "staging" / "dev" |
| `cost_center` | text | "CC-1234" |
| `decom_date` | date | "2025-12-31" |
| `notes` | text | Free-form notes |

### Custom Fields in the API

Custom fields are exposed under the `custom_fields` JSONB key on all resource objects. They are validated against their `CustomFieldDefinition` on write.

---

## 7. Import / Export

### IP Range Import

Supported input formats:
- **CSV**: columns: `network`, `name`, `gateway`, `vlan_id`, `description` + any custom field columns
- **JSON**: array of subnet objects (matches API schema)
- **IPAM vendor exports**: generic CSV (network, name, gateway, vlan_id), SolarWinds IPAM CSV, Netbox JSON

Import behavior:
- **Dry run first**: always show a preview table of what will be created/updated before committing
- **Conflict handling**: `skip` / `update` / `error` on existing networks (selectable per import)
- **Parent detection**: automatically places imported subnets under the correct block in the hierarchy
- **Custom fields**: mapped by column name / JSON key

### IP Range Export

- Export a block, subnet, or entire space
- Formats: CSV, JSON, Excel (`.xlsx`)
- Includes: all subnet metadata + utilization + custom fields + IP address list (optional)

### DNS Zone Import / Export

- **Import**: RFC 1035 zone file format → creates zone + all records in IPAM + pushes to DNS server
- **Export**: Export zone as RFC 1035 zone file or JSON
- Bulk import of multiple zones via ZIP of zone files

---

## 8. Discovery / Reconciliation

### IP Discovery

A scheduled Celery task performs network scanning to discover IPs that are in use but not recorded in IPAM.

Methods:
- **Ping sweep** (ICMP) — pure Python asyncio, no external tools required
- **ARP scan** — requires the scanner to run on-subnet or have access to ARP tables
- **SNMP polling** — poll ARP tables from routers (future phase)

Results update `IPAddress.last_seen_at` and flag `status = discovered` for IPs not in IPAM.

### Reconciliation Report

Available at `GET /api/v1/ipam/subnets/{id}/reconciliation`:

| Category | Description |
|---|---|
| In IPAM, not discovered | Allocated but no recent ping response |
| Discovered, not in IPAM | Active IP not tracked |
| DHCP lease, no IPAM record | Lease IP falls outside known subnets |
| IP status mismatch | IPAM says "available" but IP is active |

---

## 9. Utilization Tracking

- `Subnet.utilization_percent` is computed and stored (updated on every IP change + on schedule)
- `IPBlock.utilization_percent` is computed by rolling up child subnet utilization
- Alerts are configured in the notification system when utilization exceeds thresholds (default: warn at 80%, critical at 95%)

---

## 10. IP Search

Global search across all IP resources:

- Search by IP address (exact or CIDR contains)
- Search by hostname (prefix, suffix, regex)
- Search by MAC address
- Search by custom field value (for indexed fields)
- Search by tag key/value
- Filters: status, subnet, space, block, assigned user/group

Search is implemented via PostgreSQL full-text search + `inet` operators, not a separate search engine.

---

## 11. UI Conventions

### Combined IPAM Tree View

The left-side IPAM menu expands into a unified tree view. There are no separate "IP Spaces" and "Subnets" pages — everything is a single hierarchical tree:

```
IPAM
├── Corporate (IPSpace)
│   ├── 10.0.0.0/8  [IPBlock]
│   │   ├── 10.0.1.0/24  [Subnet — Servers VLAN 10]  ← click → IP list
│   │   └── 10.0.2.0/24  [Subnet — Workstations]
│   └── (free: 10.1.0.0/8 – 10.255.0.0/8)
└── OOB (IPSpace)
    └── 192.168.0.0/16  [IPBlock]
        └── 192.168.1.0/24  [Subnet]
```

Clicking a subnet opens its IP address list panel on the right. The IP address list is **collapsible** — useful for large subnets (e.g., WiFi /16) where listing every IP is slow. A toggle "Show IP list" defaults based on subnet size (auto-collapse if > 1000 IPs).

### Subnet Column Customization

The subnet list table supports:
- **Selectable columns**: user chooses which columns to display; preferences are persisted per user
- **Sortable**: click any column header to sort ascending/descending
- **Filterable**: per-column filter chips (e.g., status = active, utilization > 80%)
- **Bulk edit**: select multiple subnets → edit common fields in bulk (name, tags, custom fields, status, VLAN ID)

The same column customization applies to IP address lists, DHCP scope lists, and DNS zone/record lists.

### Gateway Auto-Assignment

When creating a subnet, the **first usable IP** (network address + 1) is automatically designated as the gateway (`status = reserved`, `description = "Gateway"`). This can be:
- Changed to any other IP in the subnet
- Deleted if no gateway is needed (e.g., transit link)
- Overridden during import

### Parent/Child Setting Inheritance

Settings can be defined at the **IPSpace** or **IPBlock** level and inherited by child subnets, with per-subnet overrides:

| Setting | Inheritable |
|---|---|
| `domain_name` | Yes — subnet inherits space/block domain, can override |
| `dns_servers` | Yes |
| `dhcp_server_group_id` | Yes |
| `tags` | Merged (parent tags + subnet tags) |
| `custom_fields` | Merged (parent defaults + subnet overrides) |

Inheritance display: the UI shows inherited values in muted text with a "Inherited from IPBlock: 10.0.0.0/8" tooltip. Overriding a field shows an "Override" badge.

### Multiple Domains per Subnet

A subnet can be associated with multiple DNS domains. When assigning an IP address, the user selects which domain the hostname record belongs to:

```
Subnet: 10.0.1.0/24
  Domains: [corp.example.com, backup.example.com]

IPAddress: 10.0.1.42
  hostname: db-01
  domain: corp.example.com  ← selected at assignment time
  fqdn: db-01.corp.example.com (auto-computed)
```

Implementation: `SubnetDomain` junction table; `IPAddress.domain_id` (FK → DNSZone).

---

## 12. MAC Address / OUI Vendor Lookup

**Opt-in feature** — configured in Settings → IPAM → OUI Vendor Lookup. Off by default. When enabled, SpatiumDDI maintains a local copy of the IEEE OUI database to display vendor names next to MAC addresses in all IP and DHCP lease tables.

- **Source**: `https://standards-oui.ieee.org/oui/oui.csv` (~5 MB, ~35k prefixes)
- **Update schedule**: `app.tasks.oui_update.auto_update_oui_database` ticks hourly via Celery Beat; the task itself honours `PlatformSettings.oui_lookup_enabled` + `oui_update_interval_hours` (default 24 h) so cadence is UI-controlled without restarting beat.
- **Manual refresh**: `POST /api/v1/settings/oui/refresh` queues `app.tasks.oui_update.update_oui_database_now`, bypassing the interval gate. Exposed as a "Refresh Now" button in the Settings UI.
- **Storage**: `oui_vendor(prefix CHAR(6) PRIMARY KEY, vendor_name VARCHAR(255), updated_at TIMESTAMPTZ)`. `prefix` is the first three MAC octets as six *lowercase* hex chars (matches the canonical form `_normalize_mac` already produces).
- **Atomic replace**: each successful run wraps `DELETE FROM oui_vendor` + bulk `INSERT` in a single transaction so lookups always see a consistent snapshot; a failed fetch or parse leaves the previous snapshot intact.
- **Display**: MACs render as `aa:bb:cc:dd:ee:ff (Cisco Systems)` in the IP address table and DHCP leases. When OUI is disabled the `vendor` field is simply null on the wire and the UI falls back to the bare MAC.

---

## 13. Network Device Management (SNMP Polling)

SpatiumDDI can manage a registry of **network devices** (routers, switches, access points) that are polled via SNMP to gather:
- ARP table → IP-to-MAC mappings (feeds IPAM discovery)
- FDB (forwarding database) → MAC-to-switch-port mappings (shows which switch/port a device is connected to)
- Interface information → link status, speed, VLAN assignments

This is handled by a dedicated **`snmp-poller` container** (separate from the core API).

### Network Device Model

```
NetworkDevice
  id, name, hostname, ip_address
  type: enum(router, switch, ap, firewall, other)
  vendor: str (nullable, auto-populated from SNMP sysDescr)
  snmp_version: enum(v1, v2c, v3)
  -- SNMPv2c
  community: str (encrypted)
  -- SNMPv3
  security_name: str
  auth_protocol: enum(MD5, SHA, SHA256, none)
  auth_key_ref: str
  priv_protocol: enum(DES, AES, AES256, none)
  priv_key_ref: str
  -- Polling
  poll_interval_seconds: int (default 300)
  poll_arp: bool (default true)
  poll_fdb: bool (default true)
  poll_interfaces: bool (default true)
  last_poll_at: timestamp
  last_poll_status: enum(success, failed, timeout)
  -- VRF
  vrf_aware: bool (default false)
  vrfs: [str] (nullable)   -- if vrf_aware=true, poll only these VRF names; empty = poll all VRFs
```

### VRF-Aware ARP Polling

When a router has multiple VRFs, the standard ARP table (`ipNetToMediaTable`, OID `1.3.6.1.2.1.4.22`) only returns the global routing table. To get ARP entries from named VRFs, the poller must query per-VRF using SNMP **community string indexing** (SNMPv2c) or **context names** (SNMPv3):

| SNMP Version | VRF Method |
|---|---|
| SNMPv2c | Community string `<community>@<vrf-name>` (Cisco convention) or `<community>@<vrf-rd>` |
| SNMPv3 | Context name set to the VRF name in the SNMPv3 PDU header |

When `vrf_aware = true`:
- The poller iterates over each VRF in the `vrfs` list (or discovers all VRFs via `CISCO-VRF-MIB::cvVrfName` if the list is empty)
- Each VRF's ARP table is polled separately
- Results are tagged with `vrf_name` and stored in `IPAddress.vrf_name` for display in the UI
- Duplicate IP entries across VRFs are allowed (matches the IPAM model — different IPSpaces can hold the same IP range)
- The poller attempts to match each ARP entry to the correct IPSpace based on the VRF name (configurable mapping: `DeviceVRFMapping`: `device_id`, `vrf_name`, `ip_space_id`)

### IP/MAC/Port Display

In the IP address list, additional columns show:
- **MAC Vendor** (from OUI lookup)
- **Switch** (which device last reported this MAC in its FDB)
- **Port** (which interface on that switch)

Example row:
```
10.0.1.42 | db-01.corp.example.com | 00:1A:2B:3C:4D:5E (Dell) | sw-01.dc1 | Gi1/0/24 | Allocated
```

---

## 14. UI Backlog (Tracked Items)

Items marked ✅ are implemented. Remaining items are planned but not yet built.

### ✅ 14.1 Network and Broadcast Address Display

**Implemented.** When a subnet is created, `network` and `broadcast` address records are automatically inserted. They appear in the IP address table with distinct grey `network` / `broadcast` status badges, are rendered at reduced opacity, have no edit or delete buttons, and are excluded from allocation (next-available and manual). `/31` and `/32` subnets are exempt per RFC 3021.

### ✅ 14.2 Inline Editing of IP Spaces, Subnets, and IP Addresses

**Implemented.**
- **IP Space**: pencil icon on space header → edit modal (name, description). Delete is accessible from within the edit modal via a two-step confirmation (warning → checkbox confirm).
- **Subnet**: pencil icon on subnet row (hover) and in detail panel header → edit modal (name, description, gateway, VLAN ID, status).
- **IP Address**: pencil icon on each address row → edit modal (hostname, description, MAC, status). `network` and `broadcast` rows are not editable.

### ✅ 14.3 Space Tree-Table View

**Implemented.** Clicking an IP Space in the left tree now shows a hierarchical flat table in the right panel. The table renders all blocks and subnets in the space in order, with depth-based indentation showing the tree structure. Block rows use a violet `Layers` icon and appear with a subtle background; subnet rows use a blue `Network` icon. Columns: Network, Name, VLAN, Used IPs, Utilization bar, Size (total IPs), Status. Clicking a block row navigates to `BlockDetailView`; clicking a subnet row opens the subnet IP address list.

### ✅ 14.4 Blocks vs Subnets Distinction in Create Flow

**Implemented.** Separate "Add block" (Layers icon) and "Add subnet" (+ icon) buttons appear on the space header and on each block row. `CreateBlockModal` has no gateway/VLAN/DHCP fields. Blocks appear with a `Layers` icon; subnets with a `Network` icon throughout the tree and table views.

### ✅ 14.5 Breadcrumbs as Colored Pills

**Implemented.** A `BreadcrumbPills` component renders clickable colored pills above all detail panels:
- **Blue pill** = IP Space (navigates to space tree-table)
- **Violet pill** = Block or ancestor blocks (navigates to `BlockDetailView`)
- **Emerald pill** = current Subnet (non-interactive, marks current position)

When the path is deeper than 4 levels, middle items are compressed to `…`. Pills appear in `SubnetDetail`, `BlockDetailView`, and `SpaceTableView`.

### ✅ 14.6 Collapsible Left Sidebar

**Implemented.** A `«` / `»` chevron button at the sidebar footer collapses the sidebar to 56px icon-only mode. Nav items show tooltips on hover when collapsed. State is persisted to `localStorage` and restored on page load.

### 14.7 Settings Page Link

A **Settings** entry should be added to the left sidebar nav, pointing to `/settings`. Phase 1 settings include:
- Platform display name and logo
- Default IP allocation strategy (sequential / random)
- Session timeout
- Utilization warning / critical thresholds
- Auto-logout idle timeout

This maps to the `PlatformSettings` singleton model defined in `SYSTEM_ADMIN.md §6`.

### ✅ 14.8 Orphaned (Soft-Deleted) IP Addresses

**Implemented** (using status field rather than `deleted_at`). When an IP address is deleted via the UI, `DELETE /ipam/addresses/{id}` sets `status = "orphan"` rather than issuing a SQL DELETE. Orphaned addresses:
- Appear in the IP list at reduced opacity with an orange "orphan" status badge
- Show a **Restore** (RefreshCw) button that sets status back to `allocated`
- Show a **Purge** (Trash) button that permanently deletes after a confirmation modal
- `DELETE /ipam/addresses/{id}?permanent=true` hard-deletes immediately

Note: the current implementation uses `status = "orphan"` instead of a `deleted_at` column. The spec's "Show deleted toggle" and "recycle bin" concept can be revisited in a future density pass.

### ✅ 14.9 IP Space Table View (Click-Through)

**Implemented.** See §14.3 above. The space view now shows a hierarchical tree-table with both blocks and subnets, not just subnets.

---

## 15. Post-Alpha Additions (Unreleased)

Features that landed after the `2026.04.16-2` cut but before the next tag.

### ✅ 15.1 Block / Subnet Overlap Validation

`_assert_no_block_overlap()` in `backend/app/api/v1/ipam/router.py` rejects
two failure modes at create time and on the reparent path of
`update_block`:
- **Same-level duplicates** — creating `10.0.0.0/8` twice under the same
  parent (or at the space root) fails with `409 Conflict`.
- **Overlapping CIDRs** — creating `10.0.0.0/16` when a sibling
  `10.0.0.0/8` already exists at the same level fails with the same status.

Implementation is a single PostgreSQL query using the `cidr &&` operator:

```sql
SELECT network FROM ip_block
WHERE space_id = :space_id
  AND network && CAST(:network AS cidr)
  AND (parent_block_id IS NOT DISTINCT FROM :parent_block_id)
  AND (:exclude_id IS NULL OR id <> :exclude_id)
```

Subnet overlap within a parent block is already enforced by existing
`Subnet` validation.

### ✅ 15.2 Scheduled IPAM ↔ DNS Auto-Sync

Celery beat task `app.tasks.ipam_dns_sync.auto_sync_ipam_dns` runs every
60 s unconditionally; the task itself gates on three `PlatformSettings`
columns:
- `dns_auto_sync_enabled` — master on/off (default off).
- `dns_auto_sync_interval_minutes` — how often the task actually syncs
  (default 30 min). The task last-run timestamp is persisted so the
  cadence can be changed from the Settings UI without restarting beat.
- `dns_auto_sync_delete_stale` — opt-in deletion of auto-generated DNS
  records that no longer match an IPAM row.

The task iterates every subnet with `dns_zone_id` set and delegates the
per-subnet reconciliation to
`app.services.dns.sync_check.compute_subnet_dns_drift` +
`app.api.v1.ipam.router._apply_dns_sync` — the same code paths that power
the manual "Sync now" button, so results are identical.

Admin UI: new **DNS Auto-Sync** section in `/admin/settings` with
enable-toggle, interval input, and delete-stale checkbox.

### ✅ 15.3 Shared Zone Picker + Bulk-Edit DNS Zone

`ZoneOptions` is a shared React component that renders a DNS-zone
`<select>` dropdown with the subnet's primary zone first and an
`<optgroup label="Additional zones">` separator for the rest. Used in:

- **Allocate IP** modal (CreateAddressModal).
- **Edit IP** modal (EditAddressModal).
- **Bulk-edit IPs** modal (when the "DNS zone" opt-in toggle is enabled).

The picker is restricted to the subnet's explicit primary + additional
zones when any are pinned. If the subnet only has a DNS group assigned
(no per-zone pinning), the picker falls back to every forward zone in the
group (reverse zones are filtered out).

`IPAddressBulkChanges.dns_zone_id` on
`POST /api/v1/ipam/addresses/bulk-edit` routes each selected IP through
`_sync_dns_record` for move / create / delete, so bulk re-homing an IP
range to a different zone updates DNS in the same request.

### ✅ 15.4 Bulk-Edit per-field opt-in

Every field on the bulk-edit-IPs modal has a sibling checkbox. Unchecked
fields are left untouched for every selected row; checked fields are
applied. Tags support both a **merge** mode (union with existing) and a
**replace-all** mode selected via a radio underneath the tags input. This
replaces the earlier behaviour where any modified field applied to every
selected row regardless of intent.

### ✅ 15.5 Inherited-Field Placeholders

`EditSubnetModal` and `EditBlockModal` show every custom-field input with
an HTML `placeholder` sourced from the first ancestor that defines the
field. A small "inherited from block `<name>`" or "inherited from space
`<name>`" badge appears next to the input. Typing a value overrides the
inheritance; clearing the input restores it.

Backed by:
- `GET /api/v1/ipam/subnets/{id}/effective-fields` (existing).
- `GET /api/v1/ipam/blocks/{id}/effective-fields` (new — parity endpoint
  added in Wave D).

### ✅ 15.6 Partial IPv6 Support

Storage and most UI paths support IPv6 today. Specifically:

- `Subnet.total_ips` widened to `BigInteger` (migration `e3c7b91f2a45`)
  so a `/64` (`2⁶⁴` addresses) fits; `_total_ips()` clamps at `2⁶³ − 1`
  for anything larger.
- `DHCPScope.address_family` column (migration `d7a2b6e9f134`). The Kea
  driver renders either a `Dhcp4` or `Dhcp6` config block from the same
  scope rows. Dhcp6 option-name translation is still a TODO.
- Subnet create skips the v6 broadcast row; `_sync_dns_record` emits AAAA
  forward records + PTR in `ip6.arpa` reverse zones.
- `GET /api/v1/ipam/blocks/{id}/available-subnets` accepts `/8`–`/128`
  with an address-family guard; the frontend's "Find by size" splits the
  prefix pool into v4 (`/8–/32`) and v6 (`/32, /40, /44, /48, /52, /56,
  /60, /64, /72, /80, /96, /112, /120, /124, /127, /128`) and filters to
  prefixes strictly longer than the selected block's prefix.
- Create-block and create-subnet placeholder text includes an IPv6
  example (`e.g. 10.0.0.0/8 or 2001:db8::/32`).

**Remaining IPv6 TODOs:**
- `POST /api/v1/ipam/addresses/next-address` returns 409 on v6 subnets.
  Still needs an EUI-64 / hash-based `/128` allocation strategy.
- Kea Dhcp6 option-name translation in `backend/app/drivers/dhcp/kea.py`.
- Automated v6-specific test coverage.

### ✅ 15.7 IP Alias Refresh + Delete Confirmation

Two smaller UX fixes on the subnet **Aliases** tab:

- Adding or deleting an alias from the IP Edit modal now invalidates
  `["subnet-aliases", subnet_id]` in addition to the per-IP cache, so
  switching tabs no longer shows a stale alias list.
- The trash icon in the subnet Aliases tab now pops a
  `ConfirmDeleteModal` ("Delete alias `<fqdn>`? The DNS record will be
  removed.") — matching the single-step confirmation used elsewhere in
  IPAM rather than firing an unconfirmed delete.

### ✅ 15.8 Modal Focus-Ring Fix

IPAM form inputs now use `focus:ring-inset` so the 2px focus ring renders
inside the rounded border. Prevents the left / right edges of the ring
from being clipped by the modal wrapper's `overflow-y-auto`, which
browsers treat as `overflow-x: auto` in practice.

### ✅ 15.9 Mobile-Responsive Layout

- Sidebar converts to a drawer with a backdrop at `<md` breakpoints; a
  hamburger toggle appears in the `Header` component.
- All data tables in IPAM / DNS / DHCP / VLANs / admin are wrapped in
  `overflow-x-auto` with a `min-w` so wide columns scroll horizontally
  instead of overflowing the viewport.
- All modals use `max-w-[95vw]` on `<sm` so they always fit the screen.

### ✅ 15.10 Subnet / Block Resize (grow-only)

**Why this and not arbitrary CIDR edit.** A network engineer's source of
truth is the CIDR stored in SpatiumDDI. A bad edit silently orphans IP
records, breaks DHCP scopes, and invalidates reverse-zone coverage.
Resize is a **restricted, validated, audited** operation with two
explicit guarantees:

1. The new CIDR is strictly **larger** than the old (smaller prefix
   length). Shrinking is rejected with a 422 that explains the workflow
   (delete + recreate is the safer path).
2. The old CIDR is a **sub-network** of the new CIDR (``old.subnet_of(new)``
   in Python ``ipaddress`` semantics). A "resize" that would move the
   network address to an entirely different range is really a recreate
   and is rejected.

**Endpoints** (under the standard IPAM router-level permission gate —
POST requires ``write`` on subnet / ip_block):

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/ipam/subnets/{id}/resize/preview` | Blast-radius dry-run |
| POST | `/api/v1/ipam/subnets/{id}/resize` | Commit under advisory lock |
| POST | `/api/v1/ipam/blocks/{id}/resize/preview` | Same for blocks |
| POST | `/api/v1/ipam/blocks/{id}/resize` | Same for blocks |

**Validation rules** (every rule re-run at commit time — TOCTOU guard):

- Grow-only, same address family (v4 ↔ v6 is a recreate, not a resize).
- Old CIDR ⊂ new CIDR.
- New CIDR must fit inside the **parent block** (for subnets) or **parent
  block** (for child blocks). If the parent is too small we return 422
  with "resize the parent block first" — we do **not** chain-resize the
  parent. A silent recursive resize is how a network tree gets a hole
  punched through it.
- No overlap with siblings or cousins anywhere in the same ``IPSpace``.
- Block resize: every descendant block + subnet must still fit inside
  the new CIDR. Mathematically redundant given rule 2 but re-checked.
- ``space_id`` is invariant — resize never moves a resource across spaces.

**Preview response** (``SubnetResizePreviewResponse``) surfaces the
"blast radius" so the operator knows what they're about to touch:

- Old vs. new network / broadcast IPs + total IP count delta.
- Current gateway + suggested new first-usable (caller decides whether
  to move it).
- Placeholders split into two buckets:
  - ``placeholders_default_named`` — the ``network`` / ``broadcast`` rows
    with no user-set hostname. These are the rows the commit can safely
    recycle to the new boundaries.
  - ``placeholders_renamed`` — rows at old boundaries where the user has
    set a hostname (e.g. ``anycast-vip``). **Always preserved**; the
    commit never touches them regardless of
    ``replace_default_placeholders``.
- Affected counts: IPs in subnet, DHCP scopes / pools / static
  assignments, auto-generated DNS records, active leases.
- Reverse zones: which already exist for the old CIDR, which will be
  created for the new CIDR (``ensure_reverse_zone_for_subnet`` is
  called idempotently on commit).
- ``conflicts`` — non-empty means the commit will 4xx; the UI disables
  the confirm button.
- ``warnings`` — non-blocking items (DHCP pools won't auto-expand, the
  netmask change, and for any network-address shift a dedicated "update
  your ACLs / router docs" reminder).

**Commit request** (``SubnetResizeCommitRequest``):

| Field | Default | Effect |
|---|---|---|
| ``new_cidr`` | *required* | The target CIDR. |
| ``move_gateway_to_first_usable`` | ``false`` | If true, set gateway to the new first-usable IP (``new_net.network_address + 1`` for v4 ≤/30, v6 ≤/126). Otherwise leave gateway untouched — the old gateway IP is guaranteed to be inside the new CIDR because old ⊂ new. |
| ``replace_default_placeholders`` | ``true`` | Delete the unchanged "network"/"broadcast" rows at the old boundaries and re-create them at the new boundaries. Renamed rows are preserved regardless. |

**Concurrency.** Commit wraps the full mutation in
``pg_try_advisory_xact_lock(ns, crc32(id))``; a concurrent resize on the
same resource returns **423 Locked**. The preview does *not* take the
lock — it is read-only and would only serialise legitimate parallel
read load.

**Audit.** One ``AuditLog(action="resize")`` row per commit, with
``old_value`` ``{network, gateway, total_ips}`` and ``new_value``
``{network, gateway, total_ips, reason: "user_resize",
placeholders_deleted, placeholders_created, dhcp_servers_notified}``,
and ``resource_display`` ``"{old_cidr} → {new_cidr}"``.

**DHCP / DNS side-effects.**

- For every agent-based DHCP server with a scope on the resized subnet,
  the control plane rebuilds the ``ConfigBundle``, bumps
  ``config_etag``, and enqueues a pending ``apply_config`` op (mirroring
  the subnet-delete path). Agentless drivers (Windows DHCP read-only)
  are skipped — they have no write surface.
- ``ensure_reverse_zone_for_subnet`` runs after the CIDR mutation so any
  new reverse-zone coverage is created. Idempotent when the zone
  already exists.
- Forward A/AAAA and PTR records for IPs inside the old CIDR are *not*
  touched — their values and names are unchanged by a grow.

**UI.** A new **Resize…** button sits next to **Edit** on both the
subnet header (``IPAMPage.tsx`` subnet detail) and the block header.
The modal (``frontend/src/pages/ipam/ResizeModals.tsx``) runs a
preview → confirm flow with:
- Live preview of old → new network / broadcast / gateway / IP count.
- Collapsible lists of affected DHCP scopes / DNS records / leases.
- A big yellow banner reminding the operator that clients, routers,
  and documentation outside SpatiumDDI need to be updated by hand.
- A **type-to-confirm** gate: the confirm button only enables after the
  user has typed the new CIDR exactly into a text input. Conflicts
  surfaced by the preview hide the confirm button entirely; there is
  no force-commit.

**Out of scope** (deliberate non-goals):

- Shrinking — explicit 422 rejection with guidance.
- Cross-space moves — rejected; space is invariant.
- Chain-resizing parent blocks — rejected; the operator must resize the
  parent first.
- Auto-expanding DHCP pools into the new address space — pools stay
  where they are; the preview flags this as a warning.
- DNS record value mutations — a grow doesn't change any record's
  ``name``/``value``; only new reverse-zone coverage is backfilled.

### ✅ 15.11 IP Assignment Collision Warnings

Two non-fatal guardrails on IP create / update. Both fire at the API
layer (so Terraform, Ansible, and ad-hoc scripts get the same
treatment as the UI); both are confirmable — they're warnings, not
hard rejections. The user's options either way are "fix the input" or
"do it anyway".

| Collision | Trigger |
|---|---|
| **FQDN** | Same ``(lower(hostname), forward_zone_id)`` on another IP anywhere in SpatiumDDI. Common accident: two people naming a host ``web``. Occasionally deliberate: round-robin A records. |
| **MAC** | Same normalised MAC anywhere in IPAM. Usually means the MAC was cloned / moved and the old row should be decommissioned before re-use. |

**Server side.** ``_normalize_mac`` canonicalises colons / dashes /
dots / bare-hex input to 12 lowercase hex chars before comparison so
``AA:BB:CC:DD:EE:FF`` and ``aabb.ccdd.eeff`` collide as expected.
``_check_ip_collisions`` joins through ``DNSZone`` + ``Subnet`` to
return a list of warning dicts with the existing IP, subnet, and
(for FQDN collisions) the published FQDN.

A ``force: bool = False`` field is added to ``IPAddressCreate`` /
``IPAddressUpdate`` / ``NextIPRequest``. When ``force=false`` and a
collision exists the endpoint returns **409** with
``detail = {warnings: [...], requires_confirmation: true}``. Clients
re-submit with ``force=true`` to proceed.

On update, the check only runs for fields the client explicitly set
(``model_dump(exclude_unset=True)``), so editing an unrelated field
on an IP that happens to share an FQDN with another row won't surface
a warning. ``exclude_ip_id=ip.id`` keeps the row from colliding with
its own current state.

**UI.** Shared ``CollisionWarning`` type + amber
``CollisionWarningBanner`` in ``IPAMPage.tsx``. Both the allocate and
edit modals parse the 409 body, render one line per collision (FQDN +
existing IP + subnet, or MAC + existing IP + hostname + subnet), and
flip the submit button to "Allocate anyway" / "Save anyway". Editing
any collision-relevant field clears the pending warning so the next
submit re-checks fresh.

**What's intentionally not a collision.**

- Same FQDN in different zones (e.g. ``web.corp.example.com`` vs.
  ``web.staging.example.com``) — those are distinct records.
- PTR collisions — every IP gets at most one PTR, handled separately
  by the Sync DNS classifier.
- Empty MAC / empty hostname — nothing to collide with.

### ✅ 15.12 DHCP Pool Awareness in IP Listing & Allocation

The subnet IP listing now shows **where DHCP pools begin and end**,
and the allocation paths refuse to hand out IPs that live inside a
**dynamic** pool — those are owned by the DHCP server, not IPAM.

**IP listing.** The subnet table renders ▼ "Start of ``<type>`` pool"
and ▲ "End of ``<type>`` pool" rows interleaved with the IP rows,
computed client-side from the pool ranges. Colour by pool type:

| Pool type | Accent | Meaning |
|---|---|---|
| ``dynamic`` | cyan | DHCP server hands these out first-come-first-served |
| ``reserved`` | violet | Reserved for future static allocation |
| ``excluded`` | zinc | Carved out of the dynamic range — operator may manually allocate |

Pool markers render even when no IPs have been assigned inside the
range, so the operator sees pool extents as first-class structure.

**Backend gates.** Three helpers in ``backend/app/api/v1/ipam/router.py``:

- ``_load_dynamic_pool_ranges(db, subnet_id)`` joins
  ``DHCPPool → DHCPScope → Subnet`` and returns packed int ranges
  for every ``pool_type == "dynamic"`` pool on the subnet.
- ``_ip_int_in_dynamic_pool(ip_int, ranges)`` — cheap contains check
  used by both the write path and the preview endpoint.
- ``_pick_next_available_ip(db, subnet, strategy)`` — hoisted out of
  ``allocate_next_ip`` so the commit path and the new preview
  endpoint share the same "skip dynamic ranges" semantics.

**Allocation rules.**

| Path | Behaviour |
|---|---|
| ``POST /subnets/{id}/addresses`` (manual) | **422** if ``body.address`` lands inside a dynamic pool. Excluded / reserved pools are still allowed — they don't race with the DHCP server on lease grants. |
| ``POST /subnets/{id}/next`` | ``_pick_next_available_ip`` skips dynamic ranges during its linear search. |
| ``GET  /subnets/{id}/next-ip-preview?strategy=sequential\|random`` | Read-only peek. Returns ``{address, strategy}``; ``address: null`` means IPv6 (not supported) or exhausted subnet. No lock, no write. |

**UI.** ``AddAddressModal`` in **next** mode fetches the preview on
open and shows ``Next available: 10.0.1.42 (skips dynamic DHCP
pools)`` as an emerald highlight, or a destructive "No free IPs in
this subnet" line with submit disabled when the subnet is exhausted.
In **manual** mode the typed address is checked client-side against
the dynamic ranges: an inline red warning renders + the submit button
disables. The server-side 422 is still the authoritative check — the
client check is just a better round-trip.

### ✅ 15.13 Subnet-Scoped IP Address Import

The space-scoped importer (§7) creates blocks + subnets from a
vendor export; it does not import *addresses*. The subnet-scoped
importer handles the far more common "dump IPs out of phpIPAM /
NetBox / Infoblox / a CSV and load them into SpatiumDDI" migration
case.

**Endpoints:**

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/ipam/import/addresses/preview` | Parse + dry-run validation |
| POST | `/api/v1/ipam/import/addresses/commit` | Apply with `fail` / `skip` / `overwrite` |

**Parser.** Auto-routes CSV / JSON / XLSX rows by header — ``address``
or ``ip`` columns make a row an **address**, ``network`` makes it a
**subnet** (rejected by the address importer; space-scoped importer
handles that case). Unrecognised columns drop into
``custom_fields`` so a raw vendor export works without rename passes.

**Validation per row.** Each IP must fall inside the targeted
subnet's CIDR. Rows outside are rejected at preview. The strategy
controls the collision behaviour when a row matches an existing IP
by ``(subnet_id, address)``:

| Strategy | On match |
|---|---|
| ``fail`` | Reject the whole batch (default). |
| ``skip`` | Leave the existing row untouched, count as skipped. |
| ``overwrite`` | Apply the import row's fields. |

**DNS sync.** Rows with a ``hostname`` route through
``_sync_dns_record(..., action="create")`` so A + PTR records publish
through the same RFC 2136 / WinRM path the UI uses. Reverse zones
are backfilled on commit.

**Audit.** One ``AuditLog(action="import")`` row per commit with
counts of created / updated / skipped / failed.

**UI.** ``AddressImportModal`` + a combined **Import / Export**
dropdown on the subnet header (`SubnetImportExportButton`) replacing
the older single-purpose export button.

## 16. Rules & constraints

Server-side validations that reject requests with a human-readable
error. Clients should surface the response `detail` — the IPAM UI
already wires this up for every delete / allocate / create flow.

### Delete guards

- **IP space delete refused when non-empty.** A space with any blocks
  or subnets can't be dropped; clear them first. `409` at
  `backend/app/api/v1/ipam/router.py:1592`.
- **Block delete refused when non-empty.** A block with child blocks
  *or* subnets returns `409` with a breakdown (*"Block 10.0.0.0/8
  still contains 3 child block(s) and 5 subnet(s)"*).
  `backend/app/api/v1/ipam/router.py:1824`.
- **Subnet delete refused when non-empty.** A subnet with user-owned
  IPs (anything other than `network`, `broadcast`, `orphan`, or
  DHCP-lease-mirrored `auto_from_lease` rows) or any DHCP scope
  attached is rejected with `409`. Pass `?force=true` to cascade;
  the same pre-delete cleanup (WinRM remove-scope, Kea bundle
  rebuild) still runs so nothing is orphaned on a running server.
  `backend/app/api/v1/ipam/router.py:2492`.

### Block hierarchy

- **Block cannot be its own parent.** `parent_block_id` must not
  equal the block's own id. `422` at
  `backend/app/api/v1/ipam/router.py:1706`.
- **Reparenting can't create a cycle.** Moving a block into one of
  its own descendants is caught before commit; same rule applies via
  drag-and-drop in the UI (checked client-side too, but the server
  is authoritative). `422` at
  `backend/app/api/v1/ipam/router.py:1725`.
- **Block must fit inside its parent block.** Child CIDR must be
  fully contained in the parent's CIDR. Enforced on create + on
  reparent. `422` at `:1717`.
- **Block overlap at the same level.** Sibling blocks in the same
  space cannot have overlapping CIDRs (duplicates or otherwise).
  Checked via `_assert_no_block_overlap`. `422` at `:1654`.

### Subnets

- **Subnet must fit inside its parent block.** `422` at `:2303`.
- **Subnet overlap within a space.** Two subnets in the same IP space
  cannot overlap, regardless of which block they sit under. Checked
  via `_assert_no_overlap`. `422` at `:2047`.
- **Gateway must be a valid IP inside the subnet.** Malformed IPs
  return `422` at `:2054`; a parseable IP outside the CIDR returns
  `422` at `:2056`.
- **VLAN reference must resolve.** A `vlan_ref_id` that doesn't
  point to an existing VLAN returns `404` at `:2067`.
- **VLAN ID must match the referenced VLAN.** If both `vlan_id` and
  `vlan_ref_id` are supplied, the tag must match the referenced
  VLAN's tag. `422` at `:2071`.

### IP allocation

- **IP must be inside the subnet's CIDR.** `422` at `:3497`. Same
  check applies in the import path.
- **IP inside a dynamic DHCP pool is refused.** Manual allocation
  inside an active `dynamic` pool is blocked — DHCP owns those
  addresses. Move the pool to `reserved` / `excluded` or shrink it
  first. `422` at `:3507`. `GET /subnets/{id}/next-ip-preview`
  honours the same skip.
- **IP already allocated in the subnet.** Duplicate address in the
  same subnet returns `409` at `:3531`.
- **Malformed IP.** Non-parseable address in create / import paths
  returns `422` at `:3493`.
- **`static_dhcp` status requires a MAC.** Creating an IP with
  `status="static_dhcp"` without a `mac_address` is rejected — a
  static reservation needs something to match on. `422` at `:3518`.
- **Collision warnings are soft, not hard.** Duplicate hostname +
  forward zone, or duplicate MAC across subnets, returns `409` with
  `{"warnings": [...], "requires_confirmation": True}`; the client
  can retry with `force=true` to override. Unlike the rules above,
  this one isn't a permanent block.

### Enum validators

Pydantic field validators — all return `422` with the offending
value and the allowed set. Kept compact because the error messages
speak for themselves.

- `IPSpace.color` → `VALID_SPACE_COLORS` (same 8 swatches as zone
  colour). `backend/app/api/v1/ipam/router.py:965`.
- `Subnet.status` → allowed set (`active`, `deprecated`, etc). `:1146`.
- `Subnet.ddns_hostname_policy` → see `docs/features/DHCP.md §13`.
  `:1121`.
- `NextIPRequest.strategy` → `sequential` or `random`. `:1437`.
- Alias `record_type` → `CNAME` or `A`. `:1302`. Alias rows with no
  `name` are also rejected. `:1310`.

### CIDR form

- **Invalid CIDR notation.** Malformed CIDR strings are rejected at
  `:1139`.
- **CIDR with host bits set.** Non-strict CIDRs (e.g. `10.0.0.1/24`)
  are rejected with a message suggesting the normalised form
  (`10.0.0.0/24`). `:1135`.
- **Prefix length can't exceed the address family.** `/33+` for IPv4
  and `/129+` for IPv6 are rejected in the available-subnets query.
  `:1871`.
- **Available-subnets `prefix_len` must be strictly smaller than the
  block.** Asking for a `/24` inside a `/24` returns `422` — there's
  nothing to divide. `:1879`.

### Import / DNS linkage

- **IPAM import payload shape.** `POST /import/...` rejects requests
  without a top-level `payload` object. `422` at
  `backend/app/api/v1/ipam/io_router.py:117`.
- **DNS zone link must resolve.** Setting `dns_zone_id` on a subnet
  requires the zone to exist *in the subnet's configured view* —
  linking a zone from a different view returns `404` at `:4220`.
