# IPAM Review — Enterprise Admin at Scale (10k+ subnets)

## Who I am & what I want

I run IPAM for a large org: tens of IP spaces, thousands of blocks, 10k+ subnets,
hundreds of thousands of allocated addresses across IPv4 and IPv6. My day is not
"create one subnet." My day is:

- Jump to a *specific* subnet or IP in under 5 seconds without remembering which
  space/block it lives under.
- Answer questions that span the whole estate: "show me every subnet over 90%
  utilization," "where does 10.84.17.5 live," "which subnets haven't been touched
  in a year."
- Trust that the tree, the tables, and the maps don't fall over (or scroll
  forever) when the dataset is large.
- Do bulk things safely — allocate ranges, deprecate stale IPs, reconcile drift —
  with guardrails and a clear blast radius.

My core test of this product: **does it stay usable as a navigation and search
problem at 10k subnets, or is it a tool designed for 10 subnets with a tree bolted
on?** I navigate by search and by saved filters, almost never by clicking down a
tree.

## Page-by-page walkthrough

### IP Spaces tree (left sidebar)
First thing I look for: a **global search box at the top of the sidebar**. I don't
see one described — just refresh / upload / + icons. That's an immediate red flag.
At 10k subnets a Space → Block → Subnet tree is an *infinite scroll*. Acme-Prod
alone could be hundreds of blocks each with dozens of subnets. Expanding to find
`10.84.17.0/24` by eye is a non-starter. I need to type "10.84.17" or "Office-LAN"
and teleport.

The little green dot on subnets with "unexplained meaning" bugs me — at scale,
every pixel of signal needs a legend or a tooltip, otherwise it's noise I learn to
ignore. The `auto:10.50.0.0/24` auto-block labels are honest provenance, but in a
tree of thousands they add visual clutter; I'd want to collapse/hide auto-created
scaffolding.

No mention of tree **virtualization** or lazy-loading. If the sidebar renders
every space/block/subnet node into the DOM, the browser will choke. I'd want to
confirm nodes are loaded on expand and the list is windowed.

### Space detail (level 1)
The combined block+subnet table with sortable Network, plus Utilization / Used IPs
/ Status / Environment columns is genuinely useful — this is the altitude I work
at. Sortable Network is good. But:
- **No visible pagination/virtualization cue.** A flat space can hold thousands of
  rows. Is this windowed? Is there a row count? "Showing 1–200 of 4,312"? Without
  that I don't trust it's showing me everything.
- **No column filter for Utilization.** My #1 recurring query is "subnets > 90%."
  I can sort by Utilization, but sort ≠ filter, and sort only helps within one
  space. I have dozens of spaces.
- The "VRF / BGP — not configured (Edit Space to add)" line is fine for one space
  but it's prime real estate repeated on every space header.

### Block detail (level 2)
The ALLOCATION MAP (Band/Treemap toggle, "Plan allocation…", role filter chips
[Data][Voice][Management][Guest], "Find Free…") is the strongest part of the IPAM
UX for *planning within a block*. I like Find-Free, Resize, Move, and the
aggregation-suggestion badge — those are real enterprise capacity-management
features, not toy features.

The wart: raw provenance chips on the header — `netbox_id 1`,
`netbox_is_pool false`. That's a database column leaking into operator chrome. At
scale I imported from NetBox; I do NOT want `netbox_id`/`netbox_is_pool` shouting
at me on every block I open. Hide it behind a "Provenance" disclosure or a metadata
tab.

### Subnet detail (level 3)
This page is well-built for a single subnet: summary stats (Gateway/VLAN/Total/
Allocated/Utilization), tabs (IP Addresses / DHCP Pools / Aliases / Address Sets /
NAT / Trend), the GAP row ("10.1.0.10 – 10.1.0.254 · 245 free · click to
allocate") is a lovely touch, and the Seen-dot column is exactly the staleness
signal I need. Bulk Allocate and the Tools menu are good.

But this is *per subnet*. Everything good here is locked to one /24. None of it
answers a cross-estate question.

The IPv6 quirk is the clearest "designed for /24s" tell: a /64 showing **Used IPs
0 / 9,223,372,036,854,776,000** and Size 9.2 quintillion. Utilization % is
meaningless for a /64; "Allocated 11 / 254" is the right mental model for v4 and
absurd for v6. For v6 I want "N addresses allocated" and "no utilization bar," not
a fake denominator.

### IP detail modal
Decent read-only panel. Two concrete data bugs visible: **REVERSE DNS ZONE shows a
bare "0"** and **DNS / DHCP LINKAGE shows "0"** — numeric values with no label or
units. At scale these unlabeled zeros will generate support tickets ("why does my
reverse zone say 0?"). The NETWORK DISCOVERY and DEVICE PROFILE empty states are
well-worded and tell me exactly what to do.

### Edit IP modal
Clean. DNS Aliases, Role dropdown, MAC history, Owner custom field — all sensible.
The "Legacy tag — assign a Router/VLAN from the Edit modal" tooltip and the literal
`NetBox import` text in a Router column tell me migration leftovers are surfacing in
operator-facing fields. At my scale that's thousands of rows carrying "NetBox
import" as a router name.

## Findings

| Severity | Area | Issue | Suggestion |
|---|---|---|---|
| blocker | Spaces tree / global nav | No global search to jump to a subnet/IP/hostname across all spaces. A Space→Block→Subnet tree is unusable as the primary nav at 10k subnets. | Add a prominent global search/command-palette (Cmd-K) that searches CIDR, name, hostname, MAC, tag across all spaces and deep-links to the subnet/IP. This is the single most important scale feature. |
| high | Space/Block tables | No visible pagination/virtualization or total-count cue ("Showing 1–200 of N"). At scale I can't tell if the table is complete or truncated. | Show row counts + page controls; confirm tables are virtualized/server-paginated. Persist sort/filter in the URL. |
| high | Cross-estate filtering | No way to answer "all subnets with utilization > 90%" across spaces. Sort is per-space and not a filter. | Add an estate-wide subnet search page with column filters (utilization range, status, role, VLAN, tag, environment) — the Stale-IP report proves the pattern exists; replicate it for utilization/capacity. |
| high | IPv6 modeling | /64 shows Used 0 / 9.2 quintillion and a meaningless Utilization %. The whole "X / total + %" model assumes small v4 subnets. | For prefixes larger than ~/100, drop the denominator + % bar; show "N addresses allocated" and a capacity descriptor instead. |
| medium | IP detail modal | REVERSE DNS ZONE and DNS/DHCP LINKAGE render a bare "0" — unlabeled numeric with no units. Will generate confusion/tickets at volume. | Show the actual zone name (or "— none —") and a human linkage label; never render a raw count/ID with no label. |
| medium | Block detail header | Raw `netbox_id`/`netbox_is_pool` provenance chips clutter every imported block. | Move import provenance behind a "Provenance"/"Metadata" disclosure or tab; don't show raw DB column names in chrome. |
| medium | Migration leftovers | Literal "NetBox import" text shows in the Router column; "Legacy tag" tooltips. Thousands of rows carry import artifacts as real values. | Post-import cleanup pass / a filter to surface and bulk-fix import-stub values; don't let provenance masquerade as operator data. |
| medium | Sidebar tree | Unexplained green subnet dot + `auto:` block clutter add noise at scale; no way to collapse auto-created scaffolding. | Add a legend/tooltip for the dot; add a toggle to hide auto-generated blocks; ensure tree is lazy-loaded + virtualized. |
| low | Bulk-allocate cap | Bulk allocate is capped (1024) and Stale-deprecate at 5000 — fine, but a 10k-subnet operator hits caps constantly and must re-run manually. | Surface "capped — N remaining, run again" clearly (Stale report does this) and consider a queued/async bulk job for very large ranges. |
| low | Space header | "VRF / BGP — not configured" repeated per space consumes header space when most spaces won't use it. | Collapse to a single quiet chip; only expand when configured. |
| nit | Subnet utilization | "0 / 256" and "0%" are clear, but no quick "near-full" visual at the list level beyond the bar. | Add an at-a-glance capacity badge (e.g. red ≥90%) and make it filterable. |

## What works well

- **Find-Free, Resize, Move, Aggregation suggestions, Plan Allocation, Allocation
  Map (Band/Treemap)** — these are genuine capacity-management tools, not toys.
  This is the part that signals the team understands enterprise IPAM.
- **Stale-IP Report** is exactly the cross-subnet hygiene view I need, *and* it has
  the right scale ergonomics: space filter, server pagination (200/page), a
  bulk-deprecate cap with a "capped — run again" signal, and reversible action.
  This is the template the rest of IPAM should follow.
- **Seen-dot staleness signal + GAP rows** ("245 free · click to allocate") are
  excellent operator affordances.
- Empty states (NETWORK DISCOVERY, DEVICE PROFILE) are specific and actionable —
  they tell me the exact next step.

## My one big idea

**Make estate-wide search + saved filtered views the primary navigation model, not
the tree.** Ship a global Cmd-K search (CIDR / name / hostname / MAC / tag across
all spaces, deep-linking straight to the subnet or IP) AND a top-level "Subnets"
query page with column filters and saved views — modeled directly on the Stale-IP
Report you already built. Then the tree becomes a nice-to-have browse affordance
instead of the only way in. At 10k subnets, whoever owns search owns the product;
right now the tree is the front door and it doesn't scale.
