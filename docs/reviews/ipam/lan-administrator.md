# IPAM Review — LAN Administrator (daily repetitive ops)

## Who I am & what I want

I run a campus/office LAN. I'm in this tool dozens of times a day doing the
same five things:

1. Allocate the next free IP for a new host and stamp its hostname + MAC.
2. Reserve a gateway / fixed-function address.
3. Find a free /24 (or smaller) when someone asks for a new segment.
4. Check that DNS and DHCP are actually in sync after I change something.
5. Glance at whether an IP is actually live on the wire before I reuse it.

I don't care about VRF/BGP, NetBox provenance, or treemaps. I care about
**clicks-per-task** and **not having to re-type things I just typed**. If a
flow takes 6 clicks when it could take 2, that's hundreds of wasted clicks a
week for me. Speed and muscle memory are everything.

## Page-by-page walkthrough

### Spaces tree (left sidebar)
First thing I see: a tree of Space > Block > Subnet. Fine. But every subnet has
a little **green dot of unexplained meaning**. I stare at it. Is that
utilization? Health? DNS-sync? Live-on-wire? No tooltip I can find at the tree
level. For something I see on every row all day, "mystery green dot" is exactly
the kind of small friction that adds up. And the auto-blocks named
`auto:10.50.0.0/24` are noise to me — I didn't make those, they clutter my tree.

To get to my daily workspace (a specific /24) I have to expand Space, expand
Block, then click the subnet. Three drill-downs every single time. There's a
refresh / upload / + in the sidebar header but **no search box** to jump
straight to "10.1.0.0/24" or "Office-LAN". I know exactly where I'm going and
the tree makes me click my way there.

### Space detail (level 1)
Header wastes its most valuable line on `VRF / BGP — not configured (Edit Space
to add)`. I will never configure that. That nag sits at the top of a page I open
constantly. The table mixing blocks AND subnets in one list is OK but the
columns I actually want (Used IPs / Utilization) are blank for blocks, so half
the rows have holes. `[Find Free…]` up here is genuinely useful for my
"find a free /24" task — good.

### Block detail (level 2)
This is where it gets busy for a daily user. The title is a CIDR, and right
under it are raw provenance chips: `netbox_id 1   netbox_is_pool false`. That's
database internals leaking onto my screen. Means nothing to me, takes up the
prime real estate under the title. The ALLOCATION MAP with Band/Treemap toggle
and "Plan allocation…" is nice-to-have but it's the first thing I see, above the
table I actually use. `[Find Free…]` is here too (good — "Find unused CIDRs in
this block" tooltip is clear).

### Subnet detail (level 3) — my home base
This is the screen I live on. Good stuff: the summary row (Gateway · VLAN ·
Total · Allocated 11/254 · Utilization 4%) is exactly the at-a-glance I want.
The **dashed-emerald GAP row** "10.1.0.10 – 10.1.0.254 · 245 free · click to
allocate" is genuinely delightful — clicking the gap to allocate is the single
best thing in here for my workflow. The Seen column dots have real tooltips
("Alive — last seen 3m ago via dhcp") — confirmed in code, that's perfect for
my "is this IP actually live before I reuse it" check. The DNS column "• in
sync" is the at-a-glance I need for task #4.

Friction: the header has **7 controls** — [Refresh] [Sync v] [Import/Export v]
[Tools v] [Ask AI] [Edit] [+ Allocate IP]. My #1 action, "+ Allocate IP", is
buried at the far right next to Edit. "Allocate next free IP + set
hostname/MAC" is my most-repeated task and it's not the most prominent thing.
Also network/broadcast/gateway rows are always shown greyed — fine, but they
add scroll on a /24 when I just want the live hosts.

### Add/Allocate IP modal
Placeholder "Auto-assigned if blank" for the address is good — that's my
next-free flow. But after I allocate, to set hostname + MAC I'm in the
**Details** tab of the Edit modal (Hostname, Description, MAC, Status, Role,
DNS Aliases, Custom Fields). For a brand-new host I want IP + hostname + MAC in
**one** form, one save. If allocate and edit are two separate modals/saves,
that's double the round-trips on my single most frequent task.

### IP detail modal (read-only)
Click a row, get a read-only panel. The header buttons [Ask AI][Scan with
Nmap][Edit][Delete] are reasonable. BUT — confirmed in code
(`IPDetailModal.tsx:373`) — the counters row does
`{(addr.alias_count || addr.nat_mapping_count) && (…)}`, so when both are 0 it
**renders a literal `0`** on screen with no label. That's the mystery "0" in
the screenshots. Looks broken. "Reverse DNS zone" and "DNS/DHCP linkage" render
fine when populated (a dash when empty), so the stray "0" is specifically the
counters bug, not those fields. Either way, a bare unlabelled number erodes my
trust in the panel.

The NETWORK DISCOVERY and DEVICE PROFILE empty states are wordy
("Auto-profiling triggers on a fresh DHCP lease when the subnet Device
profiling toggle is enabled…"). I read that once; after that it's just vertical
noise I scroll past every time I open the modal to copy a MAC.

### Edit IP modal
Tabs [Details][Network][Scan with Nmap]. For my daily "set hostname + MAC"
this is the right place, but it's tab #1 of 3 and has DNS Aliases + Custom
Fields below the fold. The MAC field has no "looks like a MAC?" validation hint
visible, and there's no quick "copy MAC from last lease" — I often re-type a MAC
I can see in the leases. The "Role (— None —)" dropdown and the tooltip
"Legacy tag — assign a Router/VLAN from the Edit modal" are confusing: is Role
legacy or current? For a daily user that ambiguity is a paper-cut every time.

## Findings

| Severity | Area | Issue | Suggestion |
|---|---|---|---|
| high | IP detail modal | Bare unlabelled `0` renders when alias_count & nat_mapping_count are both 0 (`IPDetailModal.tsx:373` uses `count && (…)`, so `0 &&` leaks a literal 0). Looks like broken data. | Guard with `(alias_count ?? 0) > 0 \|\| (nat_mapping_count ?? 0) > 0` so the whole counters row is hidden when empty. |
| high | Subnet detail / allocate flow | Allocating a new host = Allocate IP (modal 1) then Edit (modal 2, Details tab) to set hostname + MAC. Two modals/saves for my single most frequent task. | Put Hostname + MAC fields directly in the Allocate IP modal so a new host is one form, one save. |
| high | Spaces tree | No search/jump box. Reaching a known subnet always takes 3 drill-down clicks (Space>Block>Subnet). | Add a filter/quick-jump field in the sidebar header that matches on CIDR or subnet name and deep-links. |
| medium | Subnet detail header | 7 controls; my #1 action "+ Allocate IP" is far-right next to Edit. | Make "+ Allocate IP" the primary, left-most or visually dominant button; collapse Refresh into Sync/Tools. |
| medium | Spaces tree | Green dot on every subnet has no explained meaning and no tooltip. | Add a tooltip (and ideally make color = utilization or DNS-sync, the two things I check). |
| medium | Space detail header | Top line nags `VRF / BGP — not configured (Edit Space to add)` on a page I open all day. | Hide the VRF/BGP nag unless the space actually uses VRF, or move it into Edit Space only. |
| medium | Block detail header | Raw `netbox_id` / `netbox_is_pool` provenance chips sit under the title. | Move provenance into a collapsed "Source" detail or a tooltip; don't show DB internals by default. |
| medium | Edit IP modal | "Role (— None —)" plus tooltip "Legacy tag — assign Router/VLAN from the Edit modal" is self-contradictory/ambiguous. | Clarify whether Role is deprecated; if legacy, hide it or label it "(legacy)" explicitly. |
| low | IP detail modal | NETWORK DISCOVERY / DEVICE PROFILE long empty-state paragraphs add scroll every open. | Collapse empty sections to a one-line "No discovery data" with a (?) for the full explanation. |
| low | Subnet detail IP table | Network/broadcast/gateway placeholder rows always shown; add scroll on /24 when I want live hosts. | Add a "hide reserved/placeholder rows" toggle that sticks per-user. |
| low | Edit IP modal | MAC field has no inline format hint/validation; no "copy MAC from lease". | Add MAC format hint + a one-click "use last-seen MAC" when a lease exists. |
| nit | IPv6 subnet | Used IPs shows "0 / 9223372036854776000"; Size "9,223,372,036,854,776,000". | For /64+ show "—" or "/64 (SLAAC)" instead of an unusable host count. |

## What works well

- **Click-the-gap-to-allocate** dashed-emerald GAP row ("245 free · click to
  allocate") — best feature in here for my workflow.
- **Seen dots with real tooltips** ("Alive — last seen 3m ago via dhcp") — exactly
  the live-on-wire check I need before reusing an IP.
- **DNS "• in sync" column** + the **Sync v** dropdown — my sync-check task is
  visible right where I work.
- **Subnet summary row** (Gateway · VLAN · Allocated 11/254 · Utilization) — great
  at-a-glance.
- **`[Find Free…]`** on both Space and Block detail with clear tooltips — covers my
  "find a free /24" task well.

## My one big idea

**Make "allocate a new host" a single, one-form, one-save action.** Today the
high-frequency flow is: drill three levels into the subnet → click the
far-right "+ Allocate IP" → save → re-open the row → Edit → Details tab → type
hostname + MAC → save. That's the thing I do all day and it's the slowest. Put
Hostname + MAC right in the Allocate modal (address defaults to "next free"),
make that button the dominant action on the subnet header, and let me hit it
straight from a sidebar quick-jump. That single change would cut my most-common
task from ~6 clicks and two saves down to two clicks and one save.
