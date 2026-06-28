# IPAM Review — First-Time User

## Who I am & what I want

I just got handed a login for SpatiumDDI. I've used a spreadsheet to track IPs
before, and maybe poked at phpIPAM once. I know what an IP address and a subnet
are. I do **not** know what *this product* means by a "Space," a "Block," or an
"Address Set," and I've never heard "Find Free," "Reconciliation," or
"Aggregation suggestion" in this context. My goal right now is dead simple:
**I want to record a network and start assigning IP addresses.** I'm judging one
thing — *can I figure out what to do first without reading a manual?*

---

## Page-by-page walkthrough

### Sidebar "IP Spaces" tree
First thing I see is a left tree labeled **"IP Spaces"** with entries like
"Acme-Prod" and "Corporate." I don't know what an "IP Space" is — is it a tenant?
a region? a VRF? Nothing tells me. I expand one and get **Blocks** (a
stacked-squares icon) and under those, **Subnets** (a tree icon). So the hierarchy
is Space → Block → Subnet, but I had to reverse-engineer that by clicking. There's
no one-line "A Space contains Blocks, which contain Subnets" anywhere.

Each subnet has a little **green dot** next to it. I have no idea what it means.
(I later learn the *table's* "Seen" dots mean wire-recency — but this sidebar dot
is unexplained, and my brain assumes "green = good / healthy," which may be wrong.)

I see auto-created entries like **"auto:10.50.0.0/24"**. The `auto:` prefix looks
like debug output leaking into my UI. Did I create that? Should I delete it?

The sidebar header has three bare icons (refresh / upload / +). On hover I'd hope
for tooltips; as a newcomer I'm guessing "+" makes... a Space? A Block? Unclear.

**Biggest gap: what do I click first?** If my org is brand new, presumably the tree
is empty. There's no described empty state ("No IP spaces yet — create your first
one") with a primary button and a sentence explaining what a Space is. That's the
single most important screen for me and it's the one with the least guidance.

### Space detail (level 1)
Header shows the space name and a line: **"VRF / BGP — not configured (Edit Space
to add)."** As a first-timer this is intimidating — VRF and BGP are advanced
networking concepts and they're the *first* thing the page tells me about my space.
It reads like the page is mostly for network engineers, not me.

The table lists **Blocks AND Subnets together** with columns Network / Name /
Router / VLAN / Used IPs / Utilization / Size / Status / Environment. Blocks only
fill in Network + Size; subnets fill the rest. So half the cells are blank
depending on row type, and nothing labels which rows are blocks vs subnets except
that I have to notice the empty cells. I can't tell the two apart at a glance.

Buttons: **[Sync DNS] [Export] [Find Free…] [Edit Space] [Add Block] [+ Add
Subnet]**. Two creation buttons (Add Block / Add Subnet) and I don't know which I
need. "Find Free…" — find free *what*? I'd guess free IPs, but I haven't allocated
anything yet, so why would I look for free space first?

### Block detail (level 2)
Title is a raw CIDR. Below it: **"netbox_id 1   netbox_is_pool false"** chips.
This is internal provenance metadata from some NetBox import — it means nothing to
me and looks like a database dump bled into the header. A "1 aggregation
suggestion" yellow badge appears; I don't know what aggregation is or whether I'm
supposed to act on the suggestion.

The **ALLOCATION MAP** with [Band]/[Treemap] toggle is actually kind of cool — a
visual of what's used. But the legend "Free (saturated portion = % allocated)" is
a confusing sentence; I read it three times. The **ROLE filter chips
[Data][Voice][Management][Guest]** appear with no context — are these filters? tags
I should assign? Clicking them filters, I assume, but a brand-new block has nothing
to filter.

So many buttons now: Sync DNS, Export, Find Free…, Edit, Resize…, Move…, Add
Block, + New Subnet. The density is high and I don't know which 1–2 matter for my
"just add a subnet" goal.

### Subnet detail (level 3) — finally something I understand
This is the **best page for me.** The summary row is human:
**Gateway · VLAN · Total IPs 254 · Allocated 11/254 · Utilization 4%.** That's
exactly the mental model I have. The IP table is clear: Address, Hostname, MAC,
Status, etc.

The **greyed ".0 Network address" / ".255 Broadcast address" / gateway
"reserved"** rows are a genuinely nice touch — they teach me the protocol reality
without me having to know it. And the **dashed-emerald GAP row**
"10.1.0.10 – 10.1.0.254 · 245 free · click to allocate" is *fantastic* — it tells
me exactly what to do next. This is the only place in IPAM where the UI clearly
guides a first action. If the rest of the product onboarded me this well I'd be
happy.

Tabs [IP Addresses][DHCP Pools][Aliases][Address Sets][NAT][Trend] — "Address
Sets" and "Aliases" mean nothing to me yet, but at least they're tabs I can ignore.

### IP detail modal
Clicking an IP gives a clean read-only panel. Mostly fine. But two fields show a
bare **"0"** with no label/units: **REVERSE DNS ZONE** shows "0" and **DNS / DHCP
LINKAGE** shows "0". I cannot tell if that's a count, an ID, an error, or
"nothing." That looks broken.

The empty states here are actually *good*: NETWORK DISCOVERY says "add the upstream
switch in /network and wait a polling cycle," and DEVICE PROFILE explains when
auto-profiling triggers. Those teach me something. (Ironically the deep,
advanced features have better onboarding copy than the top-level Space concept.)

### Edit IP modal
Tabbed [Details][Network][Scan with Nmap]. Reasonable. **DNS Aliases** helper text
("Extra records pointing to this IP… removed automatically when the IP is purged")
is clear and friendly — good example of the tone the rest of IPAM should use.
The **Role "— None —"** dropdown and the "Legacy tag — assign a Router/VLAN from
the Edit modal" tooltip elsewhere hint there are two ways to do the same thing
(legacy vs new), which is confusing for someone with no history here.

### Real data quirks I tripped on
- A /64 IPv6 subnet shows **"Used IPs 0 / 9,223,372,036,854,776,000."** That giant
  number is comical and useless to me; a /64 should just say "huge / not
  enumerable." It made me distrust the numbers on the page.
- Router column literally shows the text **"NetBox import"** — that's a data source,
  not a router. Looks like a bug.

---

## Findings

| Severity | Area | Issue | Suggestion |
|---|---|---|---|
| blocker | Sidebar / initial state | No guidance on what to do first; "IP Space / Block / Subnet" never defined; likely-empty tree has no guiding empty state | Add a first-run empty state in the tree + Space detail: one sentence defining Space→Block→Subnet and a primary "Create your first IP Space" button with inline help |
| high | Space detail header | First thing shown is "VRF / BGP — not configured" — advanced jargon greets every newcomer and signals "this is for network engineers only" | Hide VRF/BGP behind an "Advanced" disclosure; lead the header with something a newcomer needs (e.g. "0 subnets · add your first") |
| high | Block detail header | Raw "netbox_id 1 / netbox_is_pool false" provenance chips look like leaked DB internals | Move import provenance to a collapsed "Source" detail or a small info tooltip; never show raw column names like `netbox_is_pool` in the header |
| high | Space/Block table | Blocks and Subnets share one table with half-empty rows and no visible type label; can't tell them apart | Add a "Type" badge column (Block / Subnet) or visually group/section them; or use the row icon inline |
| high | IP detail modal | REVERSE DNS ZONE and DNS/DHCP LINKAGE render a bare "0" — reads as broken/missing | Show a real label/value ("Not linked", a zone name, or hide the field when empty); never render a context-free "0" |
| medium | Sidebar | "auto:10.50.0.0/24" prefix looks like debug output; unclear if user-deletable | Render auto-created blocks with a subtle "auto" badge + tooltip "Created automatically by import/discovery" instead of an `auto:` text prefix |
| medium | Sidebar | Green dot on each subnet is unexplained (and green reads as "healthy", not its real meaning) | Add a tooltip/legend; if it's the same "Seen" recency state, label it the same way as the table's Seen column |
| medium | Space detail buttons | "Find Free…" is the wrong first action for an empty space and the label doesn't say "free what" | Rename to "Find free space…" with a tooltip; de-emphasize until the space actually has allocations |
| medium | IPv6 subnet | "/64 → Used 0 / 9,223,372,036,854,776,000" is meaningless and erodes trust in the numbers | For very large subnets show "Enumerable space too large" or a relative %, not the literal count |
| medium | Block detail | "1 aggregation suggestion" badge + "ALLOCATION MAP" + role chips appear with zero explanation of what aggregation/roles are | Add hover help defining "aggregation" in one line; gate the suggestion badge to a clickable explainer |
| low | Router column | Shows literal "NetBox import" text where a router is expected | Don't put the import source in the Router cell; leave blank or show "—" |
| low | Allocation Map legend | "Free (saturated portion = % allocated)" is hard to parse | Reword: "Shaded = allocated, light = free" |
| low | Edit IP modal | "Legacy tag" vs new Router/VLAN field implies two ways to do one thing — confusing with no history | Add a one-line note explaining the migration, or hide legacy affordances for fresh installs |

## What works well

- **Subnet detail summary row** (Gateway · VLAN · Total · Allocated · Utilization)
  matches a newcomer's mental model perfectly — clearest screen in IPAM.
- **The dashed-emerald GAP row** ("245 free · click to allocate") is the single
  best onboarding affordance in the product — it literally tells me my next step.
- **Greyed Network/Broadcast/reserved rows** teach protocol reality without
  requiring me to already know it. Lovely touch.
- **Empty-state copy on NETWORK DISCOVERY and DEVICE PROFILE** in the IP modal is
  genuinely instructive — it explains *why* it's empty and *how* to populate it.
- **Friendly helper text on DNS Aliases** in the Edit modal sets a good tone.
- **Allocation Map (Band/Treemap)** is a nice at-a-glance visual, once you decode
  the legend.

## My one big idea

Bring the quality of the *deep* empty states (the IP modal's "add the upstream
switch and wait a polling cycle" copy) **up to the top level.** Right now the
hardest, most advanced screens onboard me better than the very first one. Add a
**first-run guided empty state** to the IP Spaces tree and Space detail that (1)
defines Space → Block → Subnet in one plain sentence with the same icons used in
the tree, and (2) offers a single primary "Create your first network" path that
walks Space → Block → Subnet → first IP. Pair that with hiding the VRF/BGP and
NetBox-provenance noise behind an "Advanced/Source" disclosure so a newcomer isn't
greeted by jargon they can't act on. Make the rest of IPAM as self-explanatory as
that one beautiful gap row already is.
