# IPAM Review — NetBox Migrant

## Who I am & what I want

I run NetBox today. My world is organized around **prefixes** (with a
**role**, a **status**, a **VRF**, a **tenant**, a **site**, and a **VLAN**),
**IP addresses** (with DNS name, status, role, NAT inside/outside), and a
clean **aggregate → prefix → IP** containment hierarchy. I just ran the
SpatiumDDI NetBox importer and clicked into IPAM to answer one question:
**did my data come across intact, and can I still find things the way I think
about them?**

What I care about, in order:
1. My prefixes/IPs landed in the right place and nothing silently dropped.
2. My **roles** (Data / Voice / Management / Guest) are still first-class and
   filterable — that's how I segment everything.
3. My **VRFs and tenants** map to *something* I can navigate by.
4. The import is invisible once it's done — I should see *my* data, not the
   importer's bookkeeping.

## Page-by-page walkthrough

### Spaces tree (left sidebar)
First reaction: "IP Spaces" is a new word. I quickly map **Space ≈ VRF/tenant
container, Block ≈ aggregate, Subnet ≈ prefix** — that's mostly fine and the
3-level tree matches my aggregate→prefix instinct. But I immediately see
`auto:10.50.0.0/24` blocks the importer invented. In NetBox I had a clean
aggregate list; here there are synthetic parent blocks with an `auto:` prefix
bleeding into the tree label. That's the importer talking to me, and I didn't
ask it to. The green dot on subnets has no legend in the tree — I don't know if
it's status, sync, or utilization.

### Space detail (level 1)
The header line "VRF / BGP — not configured (Edit Space to add)" is the first
real disappointment. **My NetBox VRFs should have populated this.** I had RDs
and route-targets on those VRFs. If the importer mapped tenants→Customers (per
the docs) and VRFs→Spaces, why does every space say VRF "not configured"? Either
the VRF didn't come across or it's stored somewhere this header doesn't read.
Coming from NetBox this reads as **data loss** until proven otherwise.

The combined block+subnet table is reasonable. "Environment" as the last column
is unfamiliar — in NetBox that'd be a custom field or a role. I don't see a
**Tenant** or **Customer** column anywhere, which is odd given the importer
claims to create Customers from tenants.

### Block detail (level 2)
This is where the import leak is loudest. Under the CIDR title sit raw chips:
**`netbox_id 1   netbox_is_pool false`**. This is pure NetBox internals — a
primary key and a NetBox-specific boolean — surfaced as if they were real
attributes of *my* network. `netbox_is_pool` in NetBox means "treat this prefix
as a pool (no network/broadcast reservation)"; dumping it as a dead chip
labeled `netbox_is_pool false` is meaningless to anyone who didn't write the
importer. It should either drive behavior (skip .0/.255 reservation) or not be
shown.

On the upside: the **role filter chips [Data][Voice][Management][Guest]** here
are exactly the NetBox role concept and I'm glad they exist. The ALLOCATION MAP
with Band/Treemap is nicer than anything NetBox gives me. The "1 aggregation
suggestion" badge is a genuinely good idea.

### Subnet detail (level 3)
Best page. CIDR + status pill + name reads like a NetBox prefix detail. Gateway/
VLAN/Total/Allocated/Utilization summary is clean. The dashed-emerald GAP row
("245 free · click to allocate") is better than NetBox's "Available" rows.

Two NetBox-migrant snags: (1) **Router column shows the literal text "NetBox
import"** on imported subnets — that's the importer's provenance string sitting
in a real operational field that should hold an actual device/gateway. I'll
trip over this when I try to reconcile gateways. (2) The **IPv6 /64** shows
"Used IPs 0 / 9,223,372,036,854,776,000" and Size "9.2 quintillion". NetBox
just says "/64" and doesn't pretend to count host addresses. The giant number
is noise (and it's even rounded wrong — 2^63, not 2^64).

VLAN "200 (voice)" is nicely done — that's the role hint inline, good.

### IP detail modal
Mostly familiar. But **"Reverse DNS zone" shows a bare `0`** and **"DNS / DHCP
linkage" shows `0`** — I confirmed in the component these fields render a raw FK
id when the zone-name lookup misses, so a stale/zero id from import leaks
through as a naked number with no label or units. After a NetBox import where
reverse zones may not exist yet, every IP shows `0` here, which looks broken.

NETWORK DISCOVERY and DEVICE PROFILE empty states are clear and even tell me
*how* to populate them — better onboarding than NetBox. No complaint there.

### Edit IP modal
Relief: **Role** is a real dropdown here (— None —), and there's a "MAC
history" button NetBox doesn't have. But I notice a tooltip elsewhere:
"Legacy tag — assign a Router/VLAN from the Edit modal." Combined with the
"NetBox import" string in the Router column, it looks like the importer dumped
some of my data into **tags** rather than structured fields, and now I'm being
told to manually re-key it. That's migration friction NetBox users will feel
immediately.

## Findings

| Severity | Area | Issue | Suggestion |
|---|---|---|---|
| high | Block detail header | Raw `netbox_id 1` / `netbox_is_pool false` provenance chips shown as if they were real network attributes | Hide import internals from the detail header. Surface provenance once, subtly (e.g. a single "Imported from NetBox #1" badge with a tooltip), and make `netbox_is_pool` actually drive .0/.255 reservation rather than render a dead boolean. |
| high | Subnet table / Router column | Imported subnets show literal text "NetBox import" in the Router field — a provenance string polluting an operational column | Don't write the import source into Router. Leave Router empty and store source in a dedicated read-only "Imported from" field; show a small NetBox chip instead. |
| high | Space detail header | "VRF / BGP — not configured" on every space after import — NetBox VRFs/RDs/RTs appear lost or unmapped | Map NetBox VRFs into the Space's VRF binding during import and populate this header; if intentionally separate, say "VRF: <name> (imported)" so it doesn't read as data loss. |
| high | IP detail modal | "Reverse DNS zone" and "DNS / DHCP linkage" render a bare `0` (raw FK id leaking when zone-name lookup misses) | Treat falsy/zero ids as empty and render the dash; never print a raw id as a value. |
| medium | Spaces tree | Synthetic `auto:10.50.0.0/24` blocks with importer prefix clutter the tree | Drop the `auto:` label prefix; mark synthetic parents with an icon/tooltip ("auto-created to hold imported prefixes") instead of leaking it into the name. |
| medium | Edit modal / tags | Imported attributes landed in tags ("Legacy tag — assign Router/VLAN from Edit modal") forcing manual re-keying | Map NetBox structured fields (role, VLAN, gateway) to structured fields on import, not tags; offer a bulk "promote legacy tags" action. |
| medium | Subnet detail | IPv6 /64 shows "Used 0 / 9,223,372,036,854,776,000" and a quintillion-size column | For prefixes larger than /64-ish, suppress host counts and show "/64 — host counting disabled" like NetBox; also the value is 2^63, not 2^64. |
| medium | Space detail table | No Tenant/Customer column despite importer creating Customers from tenants | Add an optional Customer/Tenant column so NetBox tenant structure is visible after import. |
| low | Spaces tree | Green status dot on subnets has no legend in the tree | Add a hover tooltip or a small legend; reuse the Seen/Status color key already defined elsewhere. |
| low | Vocabulary | "Space / Block / Subnet / Environment" vs NetBox "VRF / Aggregate / Prefix / Role" | Add a one-line "Coming from NetBox?" mapping note on the import result screen and/or tooltips on the column headers. |
| low | Post-import UX | No visible import summary ("X prefixes, Y IPs, Z skipped") once I'm in IPAM | Persist a dismissible import-report banner/link on first IPAM visit after a commit. |

## What works well

- **Role chips [Data][Voice][Management][Guest]** on the block detail and a real
  **Role** dropdown in Edit IP — the NetBox role concept survived and is
  filterable. This is the single most important thing for a NetBox user and it's
  here.
- **VLAN "200 (voice)"** inline rendering — role context right where I need it.
- The **dashed-emerald GAP / "click to allocate"** rows beat NetBox's available-
  prefix UX.
- **ALLOCATION MAP (Band/Treemap)** and the **aggregation suggestion** badge are
  genuinely better than anything in NetBox.
- **Empty states with instructions** (Network Discovery, Device Profile) tell me
  exactly how to populate them — friendlier onboarding than NetBox.

## My one big idea

**Make the import invisible once it's done.** Right now the importer's
bookkeeping (`netbox_id`, `netbox_is_pool`, the "NetBox import" Router string,
`auto:` block names, tags-instead-of-fields) leaks into real operational fields,
so my clean NetBox dataset looks polluted the moment it lands. Do a proper
**field mapping at import time** — VRF→Space VRF binding, tenant→Customer column,
role→Role, gateway→Router, pool flag→reservation behavior — and collapse all
provenance into a single, quiet, read-only "Imported from NetBox #N" badge with
a tooltip. The payoff: a NetBox migrant opens IPAM and sees *their* network in
SpatiumDDI's nicer UI, not the importer's scratch notes.
