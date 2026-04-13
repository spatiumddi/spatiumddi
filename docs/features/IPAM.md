# IPAM Feature Specification

## Overview

The IPAM module is the core of SpatiumDDI. It manages the hierarchy of IP space from broad routing domains down to individual IP addresses. All other modules (DHCP, DNS, NTP) reference IPAM resources.

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
  forward_zone_id (FK → DNSZone, nullable)    -- A records go here
  reverse_zone_id (FK → DNSZone, nullable)    -- PTR records go here
  dns_servers: inet[] (nullable)              -- override IPs to push to DHCP clients
  domain_name: str (nullable)                 -- domain suffix for DHCP option 15
  
  -- DHCP
  dhcp_scope_id (FK → DHCPScope, nullable)
  dhcp_server_group_id (FK → DHCPServerGroup, nullable)
  
  -- NTP
  ntp_servers: inet[] (nullable)              -- pushed to DHCP clients
  
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
    static_dhcp,    -- has a static DHCP assignment
    discovered,     -- seen on network but not in IPAM
    orphan,         -- was assigned, device no longer seen
    deprecated      -- was assigned, now decommissioned
  )
  hostname: str (nullable)
  fqdn: str (nullable, computed from hostname + zone)
  mac_address: macaddr (nullable)
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
- **IPAM vendor exports**: EfficientIP (partial), Infoblox CSV, SolarWinds IPAM CSV

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
