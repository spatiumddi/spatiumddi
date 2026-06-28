# IPAM Visual / Interaction Design Review — SpatiumDDI

## Who I am & what I want

I'm a visual / interaction designer. When I land on a screen, the first thing my eye does is look for *rhythm*: do the header buttons line up and read the same way across sibling pages? Are stat numbers aligned to a grid? Does spacing breathe consistently, or does every card invent its own padding? I care about visual hierarchy (what should I see first?), consistency (does "the same thing" look the same everywhere?), and information density (is this scannable or is it a wall of mono-spaced strings?).

My north star for an IPAM tool: the three drill-down levels (Space → Block → Subnet) should feel like *one product zooming in*, not three pages a different person built. The user should never have to re-learn the header, the table, or the vocabulary as they descend the tree.

## Page-by-page walkthrough

### Spaces tree (left sidebar)
First impression: dense and legible, good. The Space → Block → Subnet nesting with distinct icons (stacked-squares for blocks, tree for subnets) is a clean way to signal level. But two things snag me immediately:

- The little **green dot** next to subnets has no legend, no tooltip, no key. As a designer I assume color *means* something (health? sync? utilization?). An unexplained colored dot is a visual promise the UI doesn't keep. I'd either explain it or remove it.
- Auto-created blocks labelled **`auto:10.50.0.0/24`** leak an implementation prefix into a human-facing tree. The `auto:` token is a machine concept; it reads like a debug string sitting next to nicely-named spaces like "Acme-Prod."

The header icons (refresh / upload / +) are icon-only with presumably tooltips — fine for a dense rail, consistent with the rest.

### Space detail (level 1)
Header reads: space name + a subdued line **"VRF / BGP — not configured (Edit Space to add)"**. The empty-state-as-subtitle pattern is actually nice — it tells me what's missing and how to fix it inline. I like it.

Button row: `[Sync DNS] [Export] [Find Free…] [Edit Space] [Add Block] [+ Add Subnet]`. Reads left-to-right reasonably (reads → edits → adds), and the `+` primary is last. Good baseline. **But hold this in your head**, because the Subnet level breaks it.

The combined **blocks + subnets in one table** is a defensible density choice, but visually it's ragged: blocks fill only Network + Size, subnets fill Used IPs / Utilization / Status / Environment. So I'm staring at a table with lots of half-empty rows. The eye reads "missing data" not "different row type." There's no visual differentiator (no row tint, no type badge, no icon in the Network cell) telling me "this row is a *block*, that's why it has no utilization." That's a hierarchy failure inside the table.

### Block detail (level 2)
Breadcrumb pills (Space > block) — good, consistent affordance. Title = CIDR.

Then the thing that makes me wince as a designer: **raw provenance chips on the detail header** — `netbox_id 1   netbox_is_pool false`. This is a backend field name and a boolean dumped onto a human header. `netbox_is_pool false` is exactly the kind of string that should *never* reach a polished header — snake_case key + literal `false`. If provenance matters, it belongs as a single tasteful "Imported from NetBox" chip (with detail on hover or in an Info/Provenance section), not two raw key/value pairs competing with the CIDR title for attention.

The **ALLOCATION MAP** with `[Band]/[Treemap]` toggle and the legend "Child blocks / Subnets / Free (saturated portion = % allocated)" is genuinely the best-designed element on these pages — it's a real visualization with a real legend, and the toggle is a clean interaction. Delight point.

The yellow **"1 aggregation suggestion"** badge is good information scent. Role filter chips `[Data][Voice][Management][Guest]` are clean.

Button row here: `[Sync DNS] [Export] [Find Free…] [Edit] [Resize…] [Move…] [Add Block] [+ New Subnet]`. Note it's already drifting from Space level — "Edit Space" became "Edit," and we've gained Resize/Move. Acceptable drift (those ops only exist at block level), but watch the count: 8 buttons is getting crowded.

### Subnet detail (level 3) — the consistency break
This is where the product stops feeling like one zooming view. The button row is now: `[Refresh] [Sync v] [Import / Export v] [Tools v] [Ask AI] [Edit] [+ Allocate IP]`.

Compare across levels:
- **Sync**: Space/Block show a flat **`[Sync DNS]`** button. Subnet shows a **`[Sync v]`** *dropdown menu*. Same conceptual action, two different control types, two different labels.
- **Find Free**: Space/Block show a flat **`[Find Free…]`** button. At Subnet it's presumably *inside* **`[Tools v]`**. So the same family of operations is sometimes a top-level button and sometimes buried in an overflow menu — and the user has to re-scan to find it at each level.
- **Export**: flat `[Export]` at Space/Block, folded into `[Import / Export v]` at Subnet.

The *logic* (more actions exist deeper, so they get collapsed into menus) is understandable, but the *visual experience* is that the toolbar reshuffles under you on every drill-down. I can't build muscle memory. As a designer I'd want a consistent toolbar grammar: pick "always menus" or "always flat buttons with overflow," and apply one rule at all three levels.

The **summary stat row** ("Gateway 10.1.0.1 · VLAN 10 · Total IPs 254 · Allocated 11/254 · Utilization 4%") with middot separators is tidy and scannable — I like this more than a card grid would be here.

The **IP table** is the strongest table in the section: status pills, the dashed-emerald **GAP row** ("10.1.0.10 – 10.1.0.254 · 245 free · click to allocate") is a lovely, inviting affordance — it turns empty space into a call-to-action. The greyed `.0`/`.255`/gateway rows are well-handled visually (muted = not actionable). The **Seen** dots are good *if* the tooltip is discoverable; the dot has a real `title` (confirmed: "Alive — last seen … via dhcp") but there's still no on-screen legend, same unexplained-color problem as the sidebar dot, and the two dot systems (sidebar green dot vs Seen-column 4-color dots) are unrelated yet visually identical — a viewer will assume they mean the same thing.

### IP detail modal
Clean 2-col field grid, good label/value rhythm. Two real defects:

1. **The bare "0".** The persona saw numeric "0" values with no label. Confirmed in the markup: the counters row is guarded by `{(addr.alias_count || addr.nat_mapping_count) && (…)}`. When both are `0`, JS evaluates `0 || 0 → 0`, and React *renders that literal `0`* on the page. So a clean IP with no aliases/NAT shows a stray "0" floating under the field grid. This is a classic React falsy-render bug surfacing as a visual artifact. (The "Reverse DNS zone: 0" the persona noted is likely the same family — a numeric id leaking where a name/dash should be, though that field is dash-guarded in code; the counters "0" is the clear culprit.)
2. **Empty states are wordy but good.** "No network discovery data — add the upstream switch in /network and wait a polling cycle." and the Device Profile empty state are *genuinely helpful* — they tell me the cause and the fix. I'd only tighten the typography (they're long sentences in a tight panel).

### Edit IP modal
Tabbed `[Details][Network][Scan with Nmap]` — consistent with the rest of the app's modal-tabs pattern, good. The "DNS Aliases" helper text and "No aliases." empty state are clear. "CUSTOM FIELDS" → Owner with helper text is fine. No major visual issues here; this modal is the most consistent surface in the section.

## Findings

| Severity | Area | Issue | Suggestion |
|---|---|---|---|
| high | Subnet detail / toolbar | Header button grammar is inconsistent across levels: flat `[Sync DNS]`/`[Find Free…]`/`[Export]` at Space/Block become `[Sync v]` menu, `[Tools v]` overflow, `[Import / Export v]` menu at Subnet. Same actions, different control types — breaks muscle memory on every drill-down. | Pick ONE toolbar grammar for all three levels. E.g. always show a small set of flat primaries (Refresh, Sync, Add) + one consistent `[Tools v]` overflow that contains the same families everywhere. Keep label + control type identical level-to-level. |
| high | Block detail / header | Raw provenance chips `netbox_id 1   netbox_is_pool false` — snake_case backend keys + literal `false` rendered on a human-facing detail header, competing with the CIDR title. | Replace with one tasteful "Imported from NetBox" chip; move raw id/is_pool into an Info/Provenance section or hover. Never render a literal boolean as header text. |
| high | IP detail modal | A stray literal **`0`** renders under the field grid for IPs with no aliases/NAT — caused by `{(alias_count || nat_mapping_count) && …}` where `0 || 0 → 0` and React prints the 0. | Coerce to boolean: `{((alias_count ?? 0) > 0 || (nat_mapping_count ?? 0) > 0) && …}`. Verify the "Reverse DNS zone: 0" sighting isn't a sibling id-leak. |
| medium | Spaces tree + Subnet table | Two unrelated colored-dot systems (sidebar single green dot vs Seen-column 4-color dots) look identical but mean different things, and the sidebar dot has no legend at all. | Give the sidebar dot a tooltip/legend or remove it. Make the two dot vocabularies visually distinct (shape/size) if they must coexist, and add a small "Seen" legend near the IP table. |
| medium | Space/Block table | Mixed block+subnet table has many half-empty rows (blocks fill only Network+Size); reads as missing data, not as a different row type. | Add a clear row-type signal: a Block/Subnet badge or icon in the Network cell, and/or a subtle row tint for block rows, so empty cells read as "N/A for blocks." |
| medium | Spaces tree | Auto-created blocks show machine prefix `auto:10.50.0.0/24` in a human tree. | Drop the `auto:` prefix from the display label; convey "auto-created" with a small muted icon/badge + tooltip instead. |
| low | IPv6 subnet | A /64 shows "Used IPs 0 / 9,223,372,036,854,776,000" and Size "9.2e18" — a wall of digits that destroys the stat row's scannability and is also a rounded/wrong value. | For huge address families, render "/64 (2^64 addresses)" or "≈1.8×10^19" instead of a 19-digit integer; show utilization as "—" or "negligible" rather than 0%. |
| low | Block detail | 8 header buttons (`Sync DNS, Export, Find Free…, Edit, Resize…, Move…, Add Block, +New Subnet`) is crowded and increases per-level toolbar drift. | Collapse secondary block ops (Resize/Move/Find Free) into a `[Tools v]` overflow — which also helps unify the grammar with the Subnet level. |
| low | Subnet detail / Router col | Router column shows literal text "NetBox import" and tag system has a "Legacy tag" tooltip — provenance/legacy strings appearing as data values. | Don't put import-source strings in the Router column; show real router or a dash. Visually distinguish legacy tags (muted/strikethrough or a "legacy" pill). |
| nit | IP detail modal | Helpful but long empty-state sentences ("…add the upstream switch in /network and wait a polling cycle.") sit in a tight panel. | Keep the helpfulness; tighten to two lines with the action ("Add upstream switch") as a subtle link, body text smaller/muted. |
| nit | Subnet stats | Middot-separated stat row is good, but "Allocated 11/254" and "Utilization 4%" duplicate the same fact in two forms. | Consider merging into one ("11 / 254 · 4%") to free horizontal space and reduce redundancy. |

## What works well

- **Allocation Map (Band/Treemap toggle + legend)** at the block level is the standout — a real visualization with a real legend and a clean toggle. This is the visual quality bar the rest of the section should rise to.
- **The dashed-emerald GAP row** in the IP table ("245 free · click to allocate") is a delightful, inviting affordance that turns dead space into an action. Best micro-interaction in the section.
- **Empty-state-as-subtitle** ("VRF / BGP — not configured (Edit Space to add)") and the modal empty states are genuinely helpful — they name the cause and the fix instead of just saying "no data."
- **Breadcrumb pills + the middot summary stat row** give a consistent, scannable sense of place and at-a-glance status at the subnet level.
- **Greyed network/broadcast/gateway rows** correctly signal "not actionable" through muting — good use of de-emphasis.

## My one big idea

**Adopt a single, level-invariant toolbar grammar across Space / Block / Subnet.** Right now the same operations (Sync, Export, Find Free) shape-shift between flat buttons, dropdown menus, and overflow `Tools` at different levels, so the toolbar visually reshuffles on every drill-down and I can't build muscle memory. Define one rule — e.g. a fixed left cluster `[Refresh] [Sync v] [+ Add …]` plus one consistent `[Tools v]` overflow whose contents are the union of level-relevant ops — and apply it identically at all three levels. The label, the control type, and the position of each action stay constant as you zoom in. That one change makes the three pages finally read as *one product zooming in* rather than three pages with three toolbars, and it's the highest-leverage consistency fix available here.
