# IPAM Review — Junior NOC Technician

## Who I am & what I want

I'm a junior NOC tech. I know what a subnet, a gateway, a VLAN, and a CIDR
are. I can ping things and read an ARP table. But I'm brand new to IPAM
tooling, and honestly I'm a little scared of this app. My #1 fear is that I
click the wrong thing on a production subnet and take something down, and
then I'm the person who broke prod on week three. What I want from this UI:

- Tell me plainly which buttons are safe (read-only) and which are dangerous.
- Explain the words I don't know — "aggregation suggestion", "orphan",
  "reconciliation", "supernet", "VRF/BGP", "address set" — right where they
  appear, not in docs I have to go hunt for.
- When I'm about to do something destructive, make me stop and confirm with
  language a beginner understands.
- Don't show me cryptic numbers with no label that make me wonder if I broke
  something.

## Page-by-page walkthrough

### IP Spaces tree (left sidebar)
First thing I see is a tree: Space → Block → Subnet. Okay, that hierarchy
makes sense to me even as a newbie — it reads like a filing cabinet. But each
subnet has a little **green dot** with no explanation. Is that "healthy"?
"online"? "selected"? I have no idea, and there's no legend in the sidebar.
I also see blocks named **"auto:10.50.0.0/24"**. What does "auto:" mean? Did
the system create that? Can I delete it? Am I allowed to touch it? Nervous.

The sidebar header has three bare icons — refresh, an upload-looking arrow,
and a "+". No labels, no tooltips that I can tell. The upload one worries me
the most: upload *what*? Could I overwrite the whole tree by clicking it?

### Space detail (level 1)
Header says **"VRF / BGP — not configured (Edit Space to add)"**. I have a
vague idea VRF is a routing thing and BGP is a routing protocol, but I have
zero idea what configuring them *here* does or whether I'm supposed to. The
fact that it's front-and-center makes me feel like I'm missing a required
step. A one-liner like "Optional — only needed if you run multiple isolated
routing tables" would calm me right down.

The button row — **[Sync DNS] [Export] [Find Free…] [Edit Space] [Add Block]
[+ Add Subnet]** — mixes safe and scary with no visual difference.
"Export" I'd guess is safe. "Find Free…" sounds safe (finding, not changing).
But "Sync DNS" — does that *push* changes to real DNS servers? On prod? That
feels like it could have real-world consequences and it's styled identically
to Export. I'd want the read-only ones to look obviously different from the
ones that change live infrastructure.

The combined block+subnet table is actually nice — one place to see Network,
Used IPs, Utilization, Status, Environment. Sortable Network column is great.

### Block detail (level 2)
Breadcrumb pills are good — I always know where I am. But right under the CIDR
title I see raw chips: **"netbox_id 1   netbox_is_pool false"**. That's
database/import gunk leaking onto my screen. I don't know what NetBox is and
I definitely don't know what "is_pool false" should mean to me. It reads like
a debug field someone forgot to hide.

A yellow badge says **"1 aggregation suggestion"**. I click it (curiosity).
The tooltip says *"Sibling subnets that would pack into a clean supernet."*
I don't know what a "supernet" is, and "pack into" doesn't tell me what
happens if I act on it. Will it **merge/delete** my subnets? That word
"suggestion" plus an action button is exactly the kind of thing I'd be afraid
to touch because I can't tell if accepting it is destructive.

The **ALLOCATION MAP** with [Band]/[Treemap] is genuinely cool and the legend
("Child blocks / Subnets / Free") helps. The **ROLE filter chips**
[Data][Voice][Management][Guest] are friendly and self-explanatory — love
those.

### Subnet detail (level 3)
This is the page I'd live in, and mostly it's good. The summary row —
**Gateway · VLAN · Total IPs · Allocated 11/254 · Utilization 4%** — is
exactly what I want at a glance. The IP table is readable.

Special rows are handled really well: the **.0 "Network address"** and
**.255 "Broadcast address"** are greyed out and labelled, so I won't try to
assign them — that actively protects a beginner like me. The dashed-emerald
**GAP row** ("10.1.0.10 – 10.1.0.254 · 245 free · click to allocate") is
lovely; it tells me where the free space is AND what clicking does. Best
single piece of UX on the page.

The **Seen** column dots — when I hover, the tooltip says "Alive — last seen
2h ago via dhcp" / "Stale" / "Cold" / "Never seen on the wire". That's
**great** — clear, plain English, tells me the source. (The sidebar green dot
should steal this tooltip treatment.)

The **DNS column "• in sync"** is reassuring, and hovering gives
"DNS records match IPAM" — good.

But the tabs across the top — **[IP Addresses] [DHCP Pools] [Aliases]
[Address Sets] [NAT] [Trend]** — "Address Sets" means nothing to me. The
tooltip ("Named, RBAC-scoped slices of this subnet address space") uses
*more* jargon ("RBAC-scoped slices") to explain jargon. As a junior I'd just
avoid that tab entirely.

The **[Sync v] [Tools v]** dropdowns hide actions I can't preview. "Tools"
could contain Merge/Split/Resize (per the tooltips: "Merge contiguous sibling
subnets") — those are destructive and they're buried in an innocuous-looking
"Tools" menu. I'd want destructive items in there clearly flagged (red text /
warning icon).

### IP detail modal (read-only)
Mostly fine and I appreciate it's read-only by default — header buttons
[Ask AI][Scan with Nmap][Edit][Delete][x]. But the field grid shows
**REVERSE DNS ZONE = "0"** and **DNS / DHCP LINKAGE = "0"** — bare numbers
with no label or unit. As a nervous newbie, a lone "0" makes me think
something is broken or unset and I'd go ask a senior "is this IP okay?" when
probably nothing is wrong. Either show a real value, a name, or "None".

The **NETWORK DISCOVERY** and **DEVICE PROFILE** empty states are actually
*excellent* for me — they tell me exactly why there's no data and what to do
("add the upstream switch in /network and wait a polling cycle"). That's the
tone the whole app should use with beginners.

**[Scan with Nmap]** sitting right there worries me a little — scanning is
something I've been told to be careful about on prod networks. No warning or
"this is safe / read-only" note. I'd hesitate.

### Edit IP modal
Clean. Tabs [Details][Network][Scan with Nmap]. The DNS Aliases helper —
"Extra records pointing to this IP. Added records are removed automatically
when the IP is purged." — is a nice plain-English explanation. The Custom
Fields "Owner" helper ("Operator team or person responsible for this IP") is
friendly. The **Role "— None —"** dropdown is unexplained though — role for
what? The tooltip elsewhere calls it "Legacy tag" which is even more
confusing for a newcomer.

### Data quirks that scared/confused me
- A /64 IPv6 subnet shows **Used IPs 0 / 9223372036854776000** and Size
  "9,223,372,036,854,776,000". That giant number looks like a bug/overflow.
  A beginner can't tell if that's correct or broken. Show "/64 (vast)" or
  similar.
- Router column literally shows the text **"NetBox import"** — that's not a
  router, that's a provenance note in the wrong column. Confusing.

## Findings

| Severity | Area | Issue | Suggestion |
|---|---|---|---|
| high | Space detail / button row | Read-only actions (Export, Find Free) look identical to live-infra actions (Sync DNS). A nervous junior can't tell what's safe. "Sync DNS" especially sounds like it pushes to production. | Visually separate read-only vs mutating buttons (e.g. neutral vs amber/primary), and give Sync DNS a tooltip stating exactly what it changes and that it's previewed first. |
| high | Block detail / aggregation suggestion | "aggregation suggestion" + "pack into a clean supernet" is pure jargon; I can't tell if accepting it merges/deletes my subnets. | Reword to plain English: "These small subnets sit next to each other and could be combined into one larger subnet." Spell out that accepting only proposes a change and shows a confirm. |
| high | IP detail modal | REVERSE DNS ZONE shows bare "0" and DNS/DHCP LINKAGE shows "0" — looks like something is broken/unset. | Render a real zone name or "None / not linked"; never show a context-free numeric "0". |
| medium | Sidebar tree | Subnet green dot has no legend or tooltip; meaning is a mystery (health? selected?). | Add a tooltip/legend; reuse the clear "Seen" dot wording if it's a recency dot. |
| medium | Subnet detail / Tools menu | Destructive ops (Merge, Resize, Split) are hidden inside an innocuous "Tools v" dropdown with no danger styling. | Flag destructive menu items with red text + warning icon; keep purely-read tools visually distinct. |
| medium | Subnet detail / tabs | "Address Sets" tab is meaningless to a beginner; tooltip "RBAC-scoped slices" explains jargon with more jargon. | Plain tooltip: "Optional named groups of IPs within this subnet you can give specific people access to." |
| medium | Block detail header | Raw "netbox_id 1 / netbox_is_pool false" provenance chips leak DB/import internals onto the main screen. | Move provenance behind an info/ⓘ popover or a "Details" expander; don't surface raw field names by default. |
| medium | IPv6 subnet display | "0 / 9223372036854776000" and a 19-digit Size look like an overflow bug to a novice. | For huge IPv6 ranges show "/64 — practically unlimited" instead of the raw count. |
| low | Sidebar header icons | Three unlabeled icons (refresh/upload/+); "upload" is scary with no tooltip. | Add tooltips; clarify what "upload" imports and that it won't overwrite the tree. |
| low | Space detail / VRF-BGP banner | "VRF / BGP — not configured" reads like a required missing step. | Add "(optional — only for multi-routing-table setups)" so beginners don't feel they skipped something. |
| low | Router column | Literal text "NetBox import" appears under "Router". | Don't place provenance text in the Router column; show actual router or blank. |
| nit | Edit IP modal / Role | "Role (— None —)" unexplained; elsewhere called "Legacy tag". | Add a one-line helper describing what Role does, or hide if truly legacy. |

## What works well

- **.0/.255 greyed "Network address" / "Broadcast address" rows** — actively
  stops a beginner from assigning unusable IPs. Excellent guardrail.
- **The dashed-emerald GAP row** ("X – Y · 245 free · click to allocate") —
  tells me where free space is and exactly what clicking does. Best UX here.
- **Seen-column dot tooltips** — "Alive — last seen 2h ago via dhcp" is plain,
  honest, and tells me the source. This is the tone the whole app needs.
- **Empty states in the IP detail modal** (Network Discovery / Device Profile)
  — they explain *why* there's no data and what to do next. Beginner-perfect.
- **Breadcrumb pills + the combined block/subnet table + Allocation Map legend**
  — I always know where I am and what I'm looking at.
- **Role filter chips [Data][Voice][Management][Guest]** — self-explanatory.

## My one big idea

Add a simple, consistent **"safe vs changes-things" visual language across all
of IPAM**, plus plain-English microcopy on the jargon. Concretely: style
read-only actions (Export, Find Free, Scan, view tabs) one neutral way, and
anything that mutates real config or infrastructure (Sync DNS/DHCP, Merge,
Resize, Delete, accept-aggregation) another way with a warning icon — and
gate every destructive one behind a confirm modal that describes the blast
radius in beginner terms ("This will combine 2 subnets into one and cannot be
undone"). That single change would turn this from an app I'm scared to click
in into one I can learn on without breaking prod.
