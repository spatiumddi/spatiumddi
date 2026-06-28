# SpatiumDDI IPAM — Review through a Senior Network Engineer's eyes

## Who I am & what I want

I've run IPAM for a large multi-VRF network for years — Infoblox at the
old shop, phpIPAM and NetBox more recently. On a heavy day I'm touching
50–100 subnets: carving new blocks, reclaiming stale space, fixing VLAN
tags, reconciling DNS, exporting for an audit. What I care about:

- **Speed and keyboard efficiency.** Every modal-with-three-clicks where a
  Tab + inline edit would do is friction multiplied by 100.
- **Dense, correct information.** I want to see utilization, VLAN, VRF,
  role, status at a glance. I don't want columns that hide the thing I'm
  actually managing.
- **Correct CIDR / capacity math.** I will notice immediately when a /64
  utilization number is nonsense.
- **VRF / VLAN / ASN as first-class citizens**, not afterthoughts.
- **Respect for my expertise** — no hand-holding wizards for things I do
  in my sleep, no wasted confirmation steps, but DO protect me from the
  genuinely irreversible.

I do NOT need an "Ask AI" button on every screen. I need bulk ops that
work and a table I can scan.

## Page-by-page walkthrough

### Spaces tree (left sidebar)

First impression is good — Space → Block → Subnet is the right mental
model and matches how I think (VRF-ish container → aggregate → leaf).
But:

- The **green dot next to every subnet has no legend.** In the tree it
  means... what? Status? Reachability? Sync state? I'm guessing, and I
  hate guessing in an IPAM tool. The subnet-detail "Seen" dots have a
  documented 4-state meaning (alive<24h / stale / cold / never) — if the
  tree dot is the same thing, tell me; if it's something else, definitely
  tell me.
- **`auto:10.50.0.0/24` blocks** are leaking an internal naming
  convention into my navigation. I know what an auto-created container
  block is, but a fresh operator will think someone fat-fingered a name.
  At minimum render the `auto:` as a muted badge, not part of the label.
- Sidebar header icons (refresh / upload / +) are **icon-only with no
  labels.** Upload = import what, exactly? Spaces? A CSV? I'd hover, but
  on a 100-subnet day I shouldn't have to.
- No **search/filter box in the tree.** With dozens of spaces and
  hundreds of blocks this tree becomes a scroll-hunt. NetBox's prefix
  search is the thing I reach for constantly; its absence here is the
  single biggest day-to-day speed gap.

### Space detail (level 1)

The combined **blocks-AND-subnets-in-one-table** is a defensible choice
and the columns (Network / Name / Router / VLAN / Used IPs / Utilization
/ Size / Status / Environment) are the right ones. Sortable Network is
exactly what I want.

Problems:

- **`VRF / BGP — not configured (Edit Space to add)`** as a header line
  is honest but reads like a nag. For someone who runs VRFs, burying VRF
  behind "Edit Space" (a modal) means I can't see at a glance which space
  maps to which VRF/RD across my estate. VRF should be a column or a
  prominent chip, not a parenthetical.
- Blocks show **only Network + Size**, blank everywhere else. Half my
  table is empty cells. A block has a meaningful utilization (allocated
  child space / total) — Infoblox and NetBox both show it. Showing blanks
  makes the aggregate level feel like dead weight.
- **`Router` column** showing the literal string `"NetBox import"` (from
  the migration) is provenance pollution in an operational column. And
  the tooltip elsewhere even says Router is a "Legacy tag — assign a
  Router/VLAN from the Edit modal." If it's legacy, why is it a
  first-class column competing for width with VLAN?

### Block detail (level 2)

The **ALLOCATION MAP with Band/Treemap toggle** is genuinely nice — this
is the phpIPAM "visual subnet map" done better, and the saturated-portion
= % allocated encoding reads fast once you know it. "Plan allocation…"
and "Find Free…" are the right verbs. Aggregation-suggestion badge is a
power feature I'd actually use.

But:

- **`netbox_id 1   netbox_is_pool false`** raw provenance chips on the
  detail header are developer debris. I do not care that this came from
  NetBox row 1, and `netbox_is_pool false` is double-negative noise.
  Collapse all import provenance into one "Imported from NetBox" chip
  with details on hover.
- The **ROLE filter chips [Data][Voice][Management][Guest]** are great,
  but role is filterable here yet (per the Edit-IP modal) set per-IP as a
  "— None —" dropdown. Where do block/subnet roles get set? The model
  feels half-wired.
- The legend "Child blocks / Subnets / Free (saturated portion = %
  allocated)" is doing a lot of work in small text. Fine for me after one
  read; worth a hover-explainer.

### Subnet detail (level 3)

This is where I live, and it's mostly strong. The summary row (Gateway ·
VLAN · Total IPs · Allocated 11/254 · Utilization 4%) is exactly the
density I want. The IP table columns are well chosen, the greyed
network/broadcast/gateway rows are correct and respectful of how the
address space actually works, and the **dashed-emerald GAP row
("10.1.0.10 – 10.1.0.254 · 245 free · click to allocate")** is a
genuinely excellent touch — that's the kind of "see the hole, fill the
hole" affordance Infoblox never gave me. Delight.

`DNS · in sync` per row and the **Seen** dots are good operational signal.

Friction:

- **No inline editing.** To fix a hostname or a tag I open a modal. For
  one IP, fine. For the 30 IPs I'm cleaning up after a migration, that's
  30 modal round-trips. Double-click-to-edit-cell would change my day.
- **The /64 IPv6 subnet shows "Used IPs 0 / 9223372036854776000" and
  Size "9,223,372,036,854,776,000".** This is wrong on two counts: (1) a
  /64 has 2^64 ≈ 1.8e19 addresses, and that number is 2^63 — it's
  overflowing into a signed 64-bit and showing half the real value; (2)
  showing a raw 19-digit integer and a 0% utilization bar for an IPv6
  /64 is meaningless. Every serious IPAM shows IPv6 as "/64" with
  allocated-count, never a denominator. This will make an IPv6 operator
  distrust the whole tool's math.
- **Tabs [IP Addresses][DHCP Pools][Aliases][Address Sets][NAT][Trend]**
  — six tabs is a lot, and Aliases/Address Sets/NAT are niche enough that
  they push the thing I use 95% of the time (IP Addresses) into
  competing for attention. Fine, but consider a "more" overflow.
- **"Ask AI"** sitting in the primary button row next to [Edit] and
  [+ Allocate IP] is button-bar real estate I'd rather give to a power
  action. I'm never going to ask an LLM about a /24 I can read myself.

### IP detail modal (read-only)

Clean 2-col grid, copy icon on the IP, status pill — good. But:

- **`REVERSE DNS ZONE` shows a bare "0"** and **`DNS / DHCP LINKAGE`
  shows "0".** I checked: the linkage row is a falsy-render bug — when
  alias_count and nat_mapping_count are both 0, the `{(a || b) && …}`
  guard renders the literal `0`. And the zone fields render the raw
  `reverse_zone_id` (a UUID/number) instead of the zone name when the
  name lookup misses. Either way, an unlabeled numeric "0" in a DDI tool
  is exactly the kind of thing that makes me file a bug and lose trust in
  the rest of the panel. **Show the zone NAME or an em-dash; never a bare
  id, never a stray 0.**
- The empty states for NETWORK DISCOVERY and DEVICE PROFILE are
  well-written and actually tell me how to populate them (add the switch
  in /network, enable subnet device-profiling). That's the right tone.

### Edit IP modal

- Tabs [Details][Network][Scan with Nmap] — sensible. DNS Aliases inline
  with a CNAME dropdown + "+ Add" is good; the helper text ("removed
  automatically when the IP is purged") is exactly the kind of
  consequence-disclosure I want.
- **Role is a "— None —" dropdown here** but a filter-chip set at the
  block level — confirm these are the same taxonomy. If block roles and
  IP roles are different concepts sharing the word "Role," that's a trap.
- Custom Fields showing "Owner" with good helper text — fine.

## Findings

| Severity | Area | Issue | Suggestion |
|---|---|---|---|
| Blocker | Subnet detail / IPv6 capacity | /64 shows "0 / 9223372036854776000" — that's 2^63 (signed-int overflow), not 2^64; raw 19-digit denominator + 0% bar is meaningless for IPv6 | For prefixes ≤ /64 (or below some host threshold) drop the denominator and % bar entirely; show "/64 · N allocated". Fix the integer math so it never overflows. |
| High | IP detail modal / DNS-DHCP fields | REVERSE DNS ZONE renders a bare "0" and DNS/DHCP LINKAGE renders "0" — falsy-render bug + raw id leaking instead of zone name | Render the zone NAME (fall back to em-dash, never the id); fix the linkage guard so 0+0 shows an em-dash, not literal 0 |
| High | Spaces tree | No search/filter box — finding a prefix in a large estate is a scroll-hunt | Add a prefix/name search at the tree top (NetBox-style); ideally jump-to-CIDR |
| High | Subnet detail / IP table | No inline cell editing — every hostname/tag/status fix is a modal round-trip; brutal at scale | Double-click-to-edit on Hostname/Description/Tags/Status; keep modal for full edit |
| Medium | Space + Block detail / provenance | Raw `netbox_id`, `netbox_is_pool false`, and `Router = "NetBox import"` leak import internals into operational columns/headers | Collapse to one "Imported from NetBox" chip with details on hover; keep provenance out of the Router column |
| Medium | Space detail / VRF | VRF/BGP is a parenthetical header nag behind "Edit Space"; VRF operators can't see RD/VRF at a glance across spaces | Promote VRF to a column or prominent chip on the space list; surface RD |
| Medium | Spaces tree / status dot | Green dot per subnet has no legend; meaning unknown | Add a tooltip/legend; if it's the Seen state, reuse the documented 4-state key |
| Medium | Space detail / block rows | Blocks render blank Used IPs/Utilization — half the table is empty | Compute and show block-level utilization (child-allocated / total) |
| Low | Sidebar header / icons | refresh/upload/+ are icon-only; "upload" target ambiguous | Add tooltips/labels; clarify what upload imports |
| Low | Subnet detail / button bar | "Ask AI" occupies primary-action real estate next to Edit / Allocate IP | Demote Ask AI into a "Tools ▾" or overflow; give the slot to a power action |
| Low | Block detail / Role model | Role filterable at block level but set as per-IP "— None —" dropdown — unclear if same taxonomy / where block roles are set | Document/unify the Role concept; clarify scope (IP vs subnet vs block) |
| Nit | Spaces tree / auto blocks | `auto:10.50.0.0/24` leaks naming convention into nav labels | Render `auto:` as a muted badge, not part of the name |

## What works well

- **The dashed-emerald GAP row** ("X – Y · N free · click to allocate")
  is the best single idea on the subnet page — instant "see the hole,
  fill the hole." More IPAM tools should steal this.
- **Greyed network/broadcast/gateway rows** with correct statuses —
  respects how the address space actually works instead of pretending
  every offset is allocatable.
- **Allocation Map (Band/Treemap) + Aggregation suggestions + Find Free**
  — real power features I'd use weekly, presented compactly.
- **Subnet summary row** density (Gateway · VLAN · Total · Allocated ·
  Util%) is exactly right.
- **Empty states that tell me the fix** (device-profile / network-
  discovery) — well-written, no fluff.
- **Per-row DNS sync indicator + Seen dots** — good operational signal
  without a separate dashboard trip.

## My one big idea

**Make the IP table editable in place, and make capacity math
trustworthy for IPv6.** Those are the two things that decide whether a
power user adopts or abandons an IPAM tool. Inline cell editing on the
subnet IP table (double-click Hostname/Tags/Status/Description, Tab to
next, Enter to commit) turns my 30-modal migration cleanup into a 2-minute
spreadsheet pass — that's the difference between "tolerable" and "I'll
move my whole org to this." And a /64 that proudly displays a
signed-int-overflowed denominator with a 0% bar is the kind of math error
that makes an experienced operator quietly distrust every other number on
the screen. Fix IPv6 to show "/64 · N allocated" with no fake
denominator, and you keep the trust the GAP row and allocation map
earned.
