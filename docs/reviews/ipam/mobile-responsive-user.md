# IPAM Review — On-call admin, 2am, on a phone

## Who I am & what I want

I'm the on-call admin. It's 2:14am, a phone woke me, I'm in bed with a 6"
screen and one thumb. PagerDuty says "DHCP exhaustion on Office-LAN" or
"host 10.1.0.45 went rogue." I do NOT want to plan subnets, edit VRFs, or
admire a treemap. I want exactly three things, fast:

1. **Find one IP / one subnet** and see: is it allocated, to whom (hostname),
   what MAC, and is it alive on the wire right now.
2. **See utilization** of a named subnet so I know if "exhaustion" is real.
3. **Make one tiny change** — flip a status to `quarantine`, free an IP,
   maybe fix a hostname — and get back to sleep.

Everything else is daytime-at-a-desk work. My lens for this whole product
tonight: can I do those three things one-handed without pinch-zooming and
horizontal-scrolling a 12-column table? Mostly: no.

## Page-by-page walkthrough

**IP Spaces tree (left sidebar).** On desktop this is the nav spine. On my
phone, per the responsive pattern this app uses, the sidebar collapses into a
hamburger drawer — fine. But the tree itself is Space → Block → Subnet, three
levels deep, with tiny disclosure carets and tiny icons (stacked-squares vs
tree). Tapping the right caret vs the row label with a thumb is a coin-flip.
And the green dot next to subnets has *no explanation anywhere I can tap* —
at 2am an unexplained green dot is noise. Auto-created entries like
`auto:10.50.0.0/24` read like garbage rows to a half-asleep brain. The drawer
also eats the whole screen, so I lose the detail I was looking at.

**Space detail (level 1).** Header line "VRF / BGP — not configured (Edit
Space to add)" is desktop chrome I don't care about tonight; it's burning a
whole row of my tiny viewport. Then a row of SIX buttons: Sync DNS, Export,
Find Free…, Edit Space, Add Block, +Add Subnet. On a phone those either wrap
into a 2-3 row pile or run off-screen. None of them is the thing I want
(search/jump to an IP). The combined block+subnet table has **10 columns**
(checkbox, Network, Name, Router, VLAN, Used IPs, Utilization, Size, Status,
Environment). On a 360px screen that's a horizontal-scroll nightmare — I can
see Network and maybe Name, and everything that actually tells me "is this
full?" (Used IPs / Utilization / Status) is two swipes to the right, off
where I can't see the row I'm scrolling. Classic wide-table-on-mobile death.

**Block detail (level 2).** Breadcrumb pills (Space > block) are actually
*good* on mobile — compact, tappable, tell me where I am. But the title is a
raw CIDR and right under it are provenance chips "netbox_id 1 netbox_is_pool
false" — meaningless to me on call, taking vertical space I'd rather spend on
data. The ALLOCATION MAP with Band/Treemap toggle + "Plan allocation…" is
pure daytime planning; the striped band is hard to read small and impossible
to interpret quickly. Seven buttons here (Sync DNS/Export/Find Free/Edit/
Resize/Move/Add Block/+New Subnet). Then the same overloaded table again.

**Subnet detail (level 3).** This is where I actually live on call, and it's
the most painful. The summary stats row — "Gateway · VLAN · Total IPs ·
Allocated 11/254 · Utilization 4%" — is *exactly* what I need, but on a phone
those 5 stats either wrap weirdly or scroll. SIX tabs (IP Addresses, DHCP
Pools, Aliases, Address Sets, NAT, Trend) won't fit; they'll scroll
horizontally and I'll fat-finger "Aliases" trying to hit "IP Addresses."
Then **6+ action buttons** (Refresh, Sync▾, Import/Export▾, Tools▾, Ask AI,
Edit, +Allocate IP). The IP table is the worst offender: **11 columns**
(checkbox, Address, Hostname, MAC, Description, Tags, Status, DHCP Pool, DNS,
Seen, Network) plus per-row pencil/trash. To answer "who has .45 and is it
alive" I have to scroll right past Hostname/MAC/Description/Tags to reach
Status and the Seen dot — and the Seen dot's meaning is **tooltip-only**
(confirmed: `title=`/`aria-label=` in SeenDot.tsx). There's no hover on a
phone. So the single most on-call-relevant signal — "is this host alive,
stale, or cold" — is *invisible* to me unless I long-press and hope a tooltip
fires. The dashed-emerald GAP row "10.1.0.10 – .254 · 245 free · click to
allocate" is genuinely nice and survives mobile well.

**IP detail modal (tap a row).** It opens as a *draggable* modal (confirmed
`useDraggableModal`). On touch, "draggable by the title bar" means every time
I try to scroll the modal content with my thumb starting near the top, I drag
the whole window instead. The 2-column field grid (HOSTNAME, FQDN, MAC,
SUBNET, … REVERSE DNS ZONE, DNS/DHCP LINKAGE) collapses badly at 360px — two
columns of long values like FQDNs and MACs means each cell is ~150px and
wraps mid-value. And the bugs bite hardest here on a small screen where I
can't cross-check: REVERSE DNS ZONE shows a bare **"0"** and DNS/DHCP LINKAGE
shows **"0"** — at 2am I genuinely cannot tell if that means "zone id 0,"
"zero records," or "broken." Four header buttons (Ask AI, Scan with Nmap,
Edit, Delete, x) crammed next to a copy icon and status pill — Delete and x
sitting together is a misclick waiting to happen on a thumb.

**Edit IP modal.** Three tabs (Details/Network/Scan with Nmap) again risk
horizontal scroll. The actual fields I'd want at 2am — Status dropdown, maybe
Hostname — are fine, but they're buried under DNS Aliases and Custom Fields
sections I have to scroll past. The footer [Cancel][Save] is correct and
sticky-ish, good.

**The IPv6 quirk.** A /64 showing "Used IPs 0 / 9223372036854776000" and Size
"9,223,372,036,854,776,000" — that 19-digit number alone is wider than my
phone screen and forces horizontal scroll on a row that should just say
"/64 — effectively unlimited." On mobile this single cell breaks the table.

## Findings

| Severity | Area | Issue | Suggestion |
|---|---|---|---|
| blocker | Subnet detail / IP table | 11-column table forces horizontal scroll; Status + Seen (the on-call essentials) are off-screen right while you scroll a row you can't see | Add a mobile card/stacked layout under ~640px: each IP = one card showing Address, Hostname, Status pill, Seen state as text+color. Hide MAC/Tags/Description/DHCP Pool/Network behind a tap-to-expand |
| blocker | Subnet detail / Seen column | "Seen" alive/stale/cold/never is a color dot with a *tooltip-only* label — no hover exists on touch, so the most on-call-relevant signal is unreadable on a phone | Render a text chip next to the dot ("alive 3m", "cold 9d") or make the dot tappable to reveal the label; never rely on `title=` for primary meaning |
| high | IP detail modal | Modal is draggable by its title bar — on touch, scrolling from the top drags the whole window instead of scrolling content | Disable drag below a breakpoint (or require a dedicated drag handle); make the modal a full-screen sheet on mobile |
| high | IP detail modal | REVERSE DNS ZONE and DNS/DHCP LINKAGE render a bare "0" with no label/units — undebuggable at 2am on a phone | Show a name or "none"/"not linked"; never surface a raw numeric id as a field value |
| high | Space/Block/Subnet headers | 6-8 action buttons per header pile up / overflow on a narrow screen; the action I want (find an IP) isn't among them | Collapse secondary actions into one "⋯ Actions" menu; keep only +Allocate (or a search field) visible; promote a global IP search |
| high | All IPAM levels | No fast "jump to IP / search address" entry point — I must drill Space→Block→Subnet then horizontally scroll to find one host | Add a persistent search box ("find 10.1.0.45 or hostname") that deep-links straight to the IP detail modal |
| medium | IPv6 subnets | "/64 → 9,223,372,036,854,776,000" in Total/Size/Used columns is wider than the screen and breaks the row | Render huge address spaces as "/64 (~unlimited)" or abbreviate; don't print the full 19-digit integer |
| medium | Subnet detail / tabs | 6 tabs and 6 action buttons each scroll horizontally and invite fat-finger taps | Make tabs a dropdown or 2-row wrap on mobile; default to IP Addresses; demote Trend/Aliases/Address Sets |
| medium | Block detail header | Provenance chips ("netbox_id 1 netbox_is_pool false") and the ALLOCATION MAP eat vertical space above the data I need | Hide provenance + allocation map behind a collapsed "Details" disclosure on mobile |
| medium | IP detail modal header | Delete and the close "x" sit adjacent — easy thumb misfire that destroys a record | Separate destructive actions; put Delete behind the ⋯ menu, keep x in a clear corner |
| low | IP Spaces tree | Green subnet dot is unexplained; tiny carets/icons are hard to tap; auto:CIDR rows look like junk | Add a one-line legend; enlarge tap targets to ~44px; style auto-created rows distinctly with a tooltip-free label |
| low | Space detail | "VRF / BGP — not configured (Edit Space to add)" burns a header row that's irrelevant on call | Hide config-prompt rows on mobile or move below the data |

## What works well

- **Breadcrumb pills** (Space > block > subnet) are compact, tappable, and
  orient me instantly — keep these exactly as-is on mobile.
- **The subnet summary stats** (Allocated 11/254 · Utilization 4%) are
  precisely the at-a-glance answer I need for "is exhaustion real" — just make
  them survive a narrow viewport.
- **The dashed-emerald GAP row** "10.1.0.10 – .254 · 245 free · click to
  allocate" is genuinely delightful and reads fine small.
- **Status pills** are colored and labeled (not color-only), so unlike the
  Seen dot they actually work on a phone.
- **Sticky footer [Cancel][Save]** in modals — correct, thumb-reachable.

## My one big idea

Ship a **mobile "on-call mode" for the IP table**: below ~640px, replace the
11-column grid with a stacked card list where each IP shows only the four
things that matter at 2am — Address, Hostname, Status pill, and Seen state as
a *text+color chip* (not a tooltip dot) — plus a single tap to open a
full-screen (non-draggable) detail sheet. Pair it with a persistent
"find an IP or hostname" search at the top of IPAM that deep-links straight to
that sheet. That one change turns the entire IPAM section from "pinch, zoom,
and swipe sideways forever" into something I can actually triage from bed.
