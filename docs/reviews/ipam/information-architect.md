# SpatiumDDI IPAM — Information Architecture Review

## Who I am & what I want

I'm an information architect / data-density analyst. I don't care whether the
buttons are rounded — I care whether the *model* is right and whether the
screen *teaches* the model to someone seeing it cold. My questions for any IPAM:

1. Is **Space → Block → Subnet** the correct containment model, and does every
   screen reinforce that hierarchy rather than blur it?
2. When two entity types share one table (blocks AND subnets), can I tell them
   apart at a glance, and do the columns mean the same thing for both rows?
3. Are the **columns** carrying their weight — Router, VLAN, Environment, Seen,
   DHCP Pool — or are they decoration?
4. Are the **tabs** (6 on the subnet) the right set, and in defensible order?
5. Does the **IP-detail field grid** group fields by concept (identity / DNS /
   DHCP / lifecycle), or is it a flat dump?
6. Do quantities **degrade gracefully** at IPv6 scale, or do I get
   `9,223,372,036,854,776,000`?

## Page-by-page walkthrough

### Spaces tree (left rail)
The Space → Block → Subnet tree is exactly the right spine, and putting it in a
persistent left rail is the correct call — it's the one place the full
containment model is visible at once. Two things immediately undercut it:

- The green dot on subnets has **no legend anywhere in the tree**. As an IA I
  read a colored dot as "carries state," but I can't decode it without hovering
  elsewhere. A status affordance with no key is noise, not signal.
- Auto-created blocks render their *internal identity* as the label:
  `auto:10.50.0.0/24`. That's a provenance fact leaking into the name slot. The
  tree is the navigation index; it should show a human label, with "auto" as a
  badge or muted tag, not as a prefix glued to the CIDR.

### Space detail (level 1)
The header line "VRF / BGP — not configured (Edit Space to add)" is good IA — it
names an empty concept *and* tells me how to fill it. More empty states should
do this.

My core structural complaint lives here: **one table holds both blocks and
subnets**, and the columns don't mean the same thing across row types. A block
populates only Network + Size; a subnet populates Used IPs / Utilization /
Status / Environment. So six of ten columns are systematically blank for half
the rows. That reads as "missing data" when it's actually "not-applicable." The
two row types are *different nouns* — a block is a container, a subnet is a leaf
address range. Mixing them in one flat grid with shared columns forces the
reader to first classify each row, then re-interpret each column. Either split
into two stacked tables ("Child blocks" / "Subnets," which the Block-detail
allocation legend already names) or add an explicit Type column as the first
data column so the eye sorts rows before reading values.

### Block detail (level 2)
The ALLOCATION MAP with Band/Treemap toggle is the strongest IA artifact in the
whole section — it answers "how full is this container and what's inside it" in
one glance, and the legend ("Child blocks / Subnets / Free") finally names the
two child types the level-1 table refused to distinguish. The ROLE filter chips
(Data/Voice/Management/Guest) are a real taxonomy and a good one.

But the header dumps raw provenance as first-class chips:
`netbox_id 1   netbox_is_pool false`. That's a foreign system's primary key and
a boolean flag sitting at the same visual altitude as the CIDR title. As an IA
this is a layer violation — import bookkeeping is metadata-about-the-record, not
an attribute of the network. It belongs in a collapsed "Provenance / Import"
disclosure, not the masthead. And `netbox_is_pool false` shows a *negative*
boolean, which carries no information at all — only show it when true.

### Subnet detail (level 3)
The summary stats row (Gateway · VLAN · Total · Allocated 11/254 · Util 4%) is
well-chosen and correctly ordered: location → identity → capacity. Good.

The six tabs — **IP Addresses / DHCP Pools / Aliases / Address Sets / NAT /
Trend** — are a defensible set but the ordering mixes axes. IP Addresses, DHCP
Pools, and Address Sets are all "ways of carving this subnet's address space";
Aliases and NAT are "mappings that reference addresses"; Trend is analytics. I'd
group: *space carving* (IP Addresses, DHCP Pools, Address Sets) → *mappings*
(Aliases, NAT) → *analytics* (Trend). As shipped, Address Sets is stranded
between DHCP Pools and Aliases, splitting the two pool-ish concepts.

The IP table is genuinely good IA: synthetic rows (network/broadcast/reserved)
are greyed and labeled by *role* not just status, and the dashed-emerald GAP row
("…245 free · click to allocate") turns absence-of-data into an actionable
object. That's exactly how to represent free space — as a first-class row, not a
hole. The Seen column's 4-state dot (alive/stale/cold/never, <24h / 24h–7d / >7d
/ never per `SeenDot.tsx`) is a clean orthogonal axis to lifecycle status, and
keeping it *separate* from the Status column is the right modeling decision.

### IP detail modal
The 2-col field grid is laid out as one flat list: HOSTNAME, FQDN, MAC, SUBNET,
DESCRIPTION, RESERVED UNTIL, LAST SEEN, FORWARD DNS ZONE, REVERSE DNS ZONE, DNS/
DHCP LINKAGE. These fields belong to **four distinct concepts** — identity
(hostname/FQDN/MAC), location (subnet), lifecycle (description/reserved until/
last seen), and DNS+DHCP bindings (the three zone/linkage fields). A flat grid
makes me re-derive that grouping every time. Add three subheads (Identity / DNS
& DHCP / Lifecycle) and the modal becomes self-documenting.

The **bare "0"** the screenshots show under REVERSE DNS ZONE and DNS/DHCP
LINKAGE is a real bug, and an instructive one. The code intends a dash fallback
(`reverse_zone_id ? … : dash(null)`), but `0` is falsy in JS the same as `null`
— except where an upstream value of literal `0` (a zone id of 0, or a count
field rendered raw) slips through it prints "0" instead of "—". Either way the
reader sees a naked number with no unit and no meaning. For an IA this is the
worst kind of cell: it *looks* like data, so I trust it, and it's noise. Every
empty field must render the same em-dash, and no numeric id should ever surface
in a value slot.

The NETWORK DISCOVERY and DEVICE PROFILE empty states are excellent — both name
the missing concept *and* the exact action/precondition to populate it ("add the
upstream switch in /network and wait a polling cycle"). This is the template the
green-dot and bare-0 cases should follow.

### The IPv6 scale problem
A /64 showing **Used IPs 0 / 9,223,372,036,854,776,000** and Size
`9,223,372,036,854,776,000` is an IA failure, not just an aesthetic one. That
number (a) is wrong — a /64 is 2^64 ≈ 1.8e19, this is 2^63 capped at JS
`Number.MAX_SAFE_INTEGER`-ish precision, so it's *lying* — and (b) is
uncountable by a human, so a denominator and a "0%" utilization against it
convey nothing. IPv6 capacity should be expressed in **prefix terms** ("/64 —
host enumeration N/A") or as an order-of-magnitude ("~1.8×10¹⁹"), never as a
literal integer. Utilization-% against a /64 is meaningless and should be
suppressed for prefixes larger than, say, /100.

### Cross-cutting: NetBox import as literal column data
A Router column reading the literal string **"NetBox import"** is a category
error — Router is a typed reference to a device, and the cell is showing where
the row *came from*. Same family as the provenance chips on the block header.
Imported-but-unmapped values should render as a muted "unmapped (imported)"
placeholder, not masquerade as real attribute data. VLAN "200 (voice)" by
contrast is exactly right — value plus human role inline.

## Findings

| Severity | Area | Issue | Suggestion |
|---|---|---|---|
| high | IP detail modal / field grid | REVERSE DNS ZONE and DNS/DHCP LINKAGE render a bare "0" — a numeric id/count leaking into a value slot, indistinguishable from real data | Normalize every empty field to the same em-dash; never render a zone id or count integer in a value cell; guard against literal-`0` falling through the dash fallback |
| high | IPv6 subnet capacity | /64 shows "Used 0 / 9,223,372,036,854,776,000" and Size as a literal 19-digit integer — uncountable and numerically wrong (capped, not 2^64) | Express large prefixes in prefix terms ("/64 — host count N/A") or order-of-magnitude; suppress Used/Size integers and the 0% utilization bar for prefixes wider than ~/100 |
| high | Space/Block detail table | Blocks and subnets share one table with shared columns; 6 of 10 columns are systematically blank for block rows, reading as missing data | Split into two stacked tables (Child blocks / Subnets) OR add a Type column as the first data column so row class is read before values |
| medium | Block detail header | Raw import provenance (`netbox_id 1`, `netbox_is_pool false`) shown as first-class chips at title altitude; negative boolean carries no info | Move provenance into a collapsed "Import / Provenance" disclosure; suppress false booleans; never put a foreign PK at masthead level |
| medium | Spaces tree | Subnet green dot has no legend anywhere in the tree — a state affordance with no key | Add a hover/legend mapping the dot to its meaning (status? sync? seen?); if it duplicates the Status column, consider dropping it |
| medium | Subnet detail tabs | Tab order mixes axes — Address Sets is stranded between DHCP Pools and Aliases, splitting the two pool-like concepts from the mapping concepts | Reorder by concept: space-carving (IP Addresses, DHCP Pools, Address Sets) → mappings (Aliases, NAT) → analytics (Trend) |
| medium | Router column / NetBox import | Router cell shows literal "NetBox import" — provenance masquerading as a typed device reference | Render unmapped imported values as muted "unmapped (imported)"; keep typed columns typed |
| low | Spaces tree | Auto-blocks labeled `auto:10.50.0.0/24` — provenance prefix glued into the name slot | Show the CIDR/name clean; render "auto" as a muted badge, not a label prefix |
| low | IP detail modal grouping | 10-field grid is flat; spans four concepts (identity / location / DNS+DHCP / lifecycle) with no subheads | Add 3 subheadings (Identity · DNS & DHCP · Lifecycle) so the modal self-documents |
| low | Block detail | "1 aggregation suggestion" yellow badge is good, but its relationship to the ALLOCATION MAP isn't spatially clear | Anchor the badge to the allocation map header so the suggestion reads as "about this map" |

## What works well

- **Space → Block → Subnet is the correct containment model**, and the
  persistent left tree is the right place to make the full hierarchy legible.
- The **ALLOCATION MAP (Band/Treemap)** with its "Child blocks / Subnets / Free"
  legend is the best single IA artifact here — it answers occupancy and
  composition in one glance and finally names the two child types.
- **Free space as a first-class object**: the dashed-emerald GAP row
  ("…245 free · click to allocate") models absence as an actionable entity
  instead of a silent hole. Exactly right.
- **Seen as an orthogonal axis** (4-state dot, lifecycle status kept separate) is
  a clean modeling decision — reachability and administrative status are
  genuinely different dimensions and shouldn't be conflated.
- **Self-explaining empty states** on Network Discovery and Device Profile —
  they name the concept and the exact precondition to populate it. This is the
  pattern the rest of the section should adopt.
- The **subnet summary stats row** is correctly ordered: location → identity →
  capacity.

## My one big idea

**Stop overloading one grid with two nouns, and make capacity scale-aware.**
The single highest-leverage IA change is to stop forcing blocks and subnets
through one table with one column set — split them (the allocation legend already
proves the system knows they're different) — and to teach every quantity column
to *degrade gracefully* at IPv6 scale (prefix-terms, not literal 19-digit
integers, with utilization suppressed where it's meaningless). Those two moves
fix the same root disease in two places: the UI currently renders the *storage
shape* of the data (every column for every row, every integer in full) instead
of the *conceptual shape* (this is a container vs a leaf; this prefix is too
large to enumerate). Get the conceptual shape on screen and the bare-0s, the
blank columns, and the uncountable denominators all resolve as symptoms of the
same fix.
