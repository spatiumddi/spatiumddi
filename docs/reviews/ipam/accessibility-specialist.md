# IPAM Accessibility (WCAG 2.1 AA) Review — SpatiumDDI

## Who I am & what I want

I'm an accessibility specialist. I audit against WCAG 2.1 AA, and I test
every screen three ways: (1) keyboard only, no mouse — Tab/Shift-Tab,
Enter/Space, arrow keys, Esc; (2) with a screen reader (NVDA/VoiceOver),
listening to what each control announces; (3) for color independence —
I desaturate the screen and check whether anything that mattered just
vanished. My non-negotiables here: status that doesn't rely on color
alone (WCAG 1.4.1), 4.5:1 text contrast / 3:1 non-text contrast (1.4.3,
1.4.11), a visible focus indicator on everything operable (2.4.7),
logical focus order with no traps (2.4.3 / 2.1.2), name/role/value on
every custom control (4.1.2), and accessible names on icon-only buttons
(2.5.3 / 4.1.2). A dense network tool like IPAM is exactly where these
break, because the whole UI is built out of tiny colored dots, pills,
icon buttons, and a deep tree — none of which are accessible by default.

What I want: to navigate the Space → Block → Subnet tree, read an IP
table, and operate the IP detail / edit modals entirely from the
keyboard and with a screen reader, and to never need to perceive color
to understand "is this IP alive?" or "is DNS in sync?".

## Page-by-page walkthrough

**IP Spaces tree (left sidebar).** This is the spine of IPAM and my
first worry. A tree of Space → Block → Subnet with expand/collapse is a
classic ARIA `tree` widget, and they almost never get implemented as
one. The little green dot next to each subnet has "unexplained meaning"
even to sighted users — so it certainly has no text equivalent for a
screen reader, and it fails color-alone. The refresh / upload / + icons
in the sidebar header are icon-only; I need to confirm they have
accessible names. The auto-block label "auto:10.50.0.0/24" reads fine.
My core question: can I expand a Space with the Right-arrow / collapse
with Left-arrow and move with Up/Down, and does the SR announce
"Acme-Prod, tree item, collapsed, level 1, 3 of 5"? If this is a pile
of nested `<div onClick>`s, the tree is unusable by keyboard.

**Space detail (level 1).** Header line "VRF / BGP — not configured
(Edit Space to add)" is good plain-language. The action buttons have
text labels — good. The combined block+subnet table has 10 columns
(checkbox, Network, Name, Router, VLAN, Used IPs, Utilization, Size,
Status, Environment). Two issues jump out: (1) "Utilization" is shown
as a percentage AND likely a colored mini-bar — if the bar carries
meaning beyond the number that's a color-redundancy check; the number
saves it, good. (2) Sortable "Network" column — does the header expose
`aria-sort` and is it a real `<button>` inside `<th>`? Sort state is
typically conveyed by a caret glyph only, which is color/shape with no
programmatic state. The "Status" column ("active") is text — good,
that's the right pattern, color can decorate it.

**Block detail (level 2).** Breadcrumb "pills" — are these a real
`<nav aria-label="Breadcrumb">` with an ordered list, and is the
current page marked `aria-current="page"`? The raw metadata chips
"netbox_id 1   netbox_is_pool false" are visual noise but readable.
The yellow "1 aggregation suggestion" badge — yellow on white is my
contrast red flag; amber/yellow text or badges routinely fail 4.5:1.
The **ALLOCATION MAP** is my biggest concern on this page: a striped
horizontal band with legend "Child blocks / Subnets / Free (saturated
portion = % allocated)". This is a graphical data viz encoding
allocation by color + stripe pattern. For a screen reader this band is
almost certainly an empty/meaningless `<div>` — there's no text
alternative for "X% allocated, child blocks occupy this range." It
needs an accessible summary (1.1.1). The [Band]/[Treemap] toggle — is
it a proper toggle/tablist with pressed state? The ROLE filter chips
[Data][Voice][Management][Guest] are toggle buttons; they must expose
`aria-pressed` and not signal "selected" by background color alone.

**Subnet detail (level 3).** The richest and most dangerous screen.
- The title row "10.1.0.0/24  active  Office-LAN" — status as a pill
  with text "active": good, color-independent.
- The **tabs** [IP Addresses][DHCP Pools][Aliases][Address Sets][NAT]
  [Trend] must be a real `role="tablist"` with arrow-key navigation,
  `aria-selected`, and `tabpanel` association. shadcn Tabs usually get
  this right — I'd verify, but it's the one widget likely to pass.
- The **IP table** has 12+ columns plus per-row pencil/trash. Two hard
  fails I can predict: (a) **Seen column** = colored dots only; (b)
  **DNS column** = "• in sync" where the bullet is a colored dot and
  "in sync"/"Missing in DNS"/"Mismatched" status leans on color. The
  per-row **pencil/trash** icon buttons need accessible names that
  include the row identity ("Edit 10.1.0.5", not just "Edit").
- The **greyed network/broadcast/reserved rows** (.0, .255, gateway)
  are dimmed muted text — this is my #1 contrast bet: greyed-out rows
  on a dark/light surface almost always drop under 4.5:1. WCAG has no
  exemption for "disabled-looking" informational rows; these aren't
  disabled controls, they're data, so 1.4.3 applies.
- The dashed-emerald **GAP row** "10.1.0.10 – 10.1.0.254 · 245 free ·
  click to allocate" is clever visually but: it's encoded by a dashed
  EMERALD border (color + style), the text says "click to allocate"
  (mouse-only verb — fails 2.1.1 if it's not keyboard-focusable and
  Enter-activatable), and a screen reader needs to know this is an
  actionable region, not a data row. Is it a `<button>`/`<a>` or a
  `<tr onClick>`? If the latter, it's invisible to keyboard + SR.

**IP detail modal.** Read-only panel. Modal accessibility checklist:
does focus move into the dialog on open, is it `role="dialog"`
`aria-modal="true"` with an `aria-labelledby` pointing at the IP
header, is focus trapped, does Esc close, and does focus return to the
triggering row on close? The header IP + **copy icon** is icon-only —
needs "Copy IP address". The status pill + dot is fine (text present).
The 2-col field grid is readable, BUT the observed quirk where
**REVERSE DNS ZONE shows a bare "0"** and **DNS/DHCP LINKAGE shows
"0"** is an accessibility problem too, not just UX: a screen reader
announces "Reverse DNS zone, 0" which is meaningless — that's a
name/value mismatch (4.1.2) and an info clarity failure. The empty
states ("No network discovery data…", "No active profile yet…") are
excellent plain-language and they reference paths/toggles clearly.

**Edit IP modal.** Tabs [Details][Network][Scan with Nmap]. Form
fields mostly have visible labels (Hostname, Description (Optional),
MAC Address, Status, Role) — good, as long as the label is
programmatically associated (`<label for>` / `aria-labelledby`), not
just visually adjacent. The **DNS Aliases** row "[CNAME v] dropdown +
'alias name (e.g. www, mail)' + [+ Add]" — the text input relies on
placeholder text as its only label; placeholder is NOT an accessible
name and disappears on input (fails 1.3.1/3.3.2 / "Label in Name").
The "(empty clears field)" / "Auto-assigned if blank" helper texts are
good but must be wired via `aria-describedby` to actually be announced.

## Findings

| Severity | Area | Issue | Suggestion |
|---|---|---|---|
| blocker | Subnet detail / Seen column (SeenDot.tsx) | Status is a bare `<span>` colored dot (emerald/amber/rose/grey) with state conveyed by **color only**. The meaning lives only in `title`/`aria-label` — invisible to keyboard-only and color-blind sighted users. Fails WCAG 1.4.1 Use of Color. The span isn't focusable, so `title` never appears for keyboard users either. | Add a non-color cue: a short text token next to/under the dot ("alive"/"stale"/"cold"/"never") or distinct icon shapes (●/◐/○). Keep `aria-label`. Make the cell text-readable so desaturation doesn't destroy it. |
| blocker | Subnet detail / DNS column | "• in sync" / "Missing in DNS" / "Mismatched" encode sync state with a colored bullet. The tooltip strings ("DNS records match IPAM"/"Missing in DNS"/"Mismatched") confirm the states exist but the in-table glyph leans on color. Fails 1.4.1. | Always render the word ("In sync"/"Missing"/"Mismatched") in the cell, not just a colored dot; use an icon with distinct shape (✓ / ! / ≠) in addition to color. |
| blocker | Subnet detail / GAP "click to allocate" row | Dashed-emerald row says "click to allocate" — a mouse-only affordance. If it's a `<tr onClick>` it's not keyboard-focusable/activatable (2.1.1 Keyboard) and a screen reader can't tell it's actionable (4.1.2). The dashed-emerald styling is the only signal it differs from data rows (1.4.1). | Make it a real `<button>` (or row with `role="button"`, `tabindex=0`, Enter/Space handler). Accessible name: "Allocate an IP in 10.1.0.10–10.1.0.254 (245 free)". Add a non-color cue (icon/label), not just dashed emerald. |
| high | IP Spaces sidebar / tree | Space→Block→Subnet tree likely built from nested clickable divs, not an ARIA `tree`. Without `role="tree"/"treeitem"`, `aria-expanded`, `aria-level`, and arrow-key navigation it is not keyboard-operable and SR-announceable. Fails 2.1.1 / 4.1.2 / 2.4.3. | Implement as a real tree: `role="tree"` container, `treeitem` rows with `aria-expanded`, `aria-level`, `aria-selected`; Up/Down move, Right/Left expand/collapse, Enter activates. The unexplained green subnet dot also needs a text/aria meaning. |
| high | Greyed .0/.255/gateway rows | Network/broadcast/reserved rows render as dimmed muted text. Greyed informational text almost always falls below 4.5:1 (1.4.3). These are data, not disabled controls, so no exemption applies. | Raise muted-text color to meet 4.5:1 in both themes; convey "Network/Broadcast/Reserved" via the existing text label + a non-color marker, not just dimming. Verify with a contrast checker in dark AND light. |
| high | Per-row + modal icon buttons | Pencil/trash per IP row and the copy icon in the IP detail header are icon-only. If their accessible name is generic ("Edit"/"Delete") or absent, SR users get "button" with no context. Fails 2.5.3 / 4.1.2. | Give each a name that includes the target: "Edit 10.1.0.5", "Delete 10.1.0.5", "Copy IP address 10.1.0.5". Use `aria-label`; ensure a visible focus ring (2.4.7). |
| high | Edit IP modal / DNS Aliases + filters | The alias-name input and the tag-filter inputs ("Filter addresses by tag…") rely on placeholder text as their only label. Placeholder is not an accessible name and vanishes on typing (1.3.1 / 3.3.2). | Add a real `<label>` (visually-hidden if needed) or `aria-label` to every text input. Wire helper text ("Auto-assigned if blank", "(empty clears field)") via `aria-describedby`. |
| high | Block detail / ALLOCATION MAP band + treemap | The striped allocation band and treemap encode allocation by color + stripe pattern with no text alternative (1.1.1) and likely fail 1.4.11 non-text contrast between the Child-blocks/Subnets/Free segments. SR users get nothing. | Add an accessible summary ("64% allocated: 12 subnets, 3 child blocks, 245 addresses free") as visually-hidden text or a caption; ensure ≥3:1 contrast between adjacent segments; don't rely on stripe-vs-solid alone. |
| medium | All modals (IP detail, Edit, Create/Edit Space/Block/Subnet, Confirm-Destroy, etc.) | Need to verify dialog semantics across the ~25 modals: `role="dialog"` + `aria-modal`, `aria-labelledby` to the title, focus moved in on open, focus trap, Esc to close, focus returned to trigger on close (2.4.3 / 2.1.2 / 4.1.2). Draggable modals especially risk a broken focus loop. | Audit the shared `Modal`/`useDraggableModal` once — fixing it there fixes all IPAM modals. Confirm the drag handle is not the only way to operate and doesn't steal focus order. |
| medium | IP detail modal / "0" values | REVERSE DNS ZONE and DNS/DHCP LINKAGE render a bare "0" with no units/label context. SR announces "Reverse DNS zone, 0" — meaningless; an info-clarity + name/value failure (4.1.2). | Render an explicit empty/none state ("Not configured" / "—") or the real value with units; never surface a raw numeric id as a field value. |
| medium | Block detail / yellow badge + sort carets | The yellow "1 aggregation suggestion" badge risks failing 4.5:1; sortable column carets convey sort by glyph/color with no `aria-sort` state. (1.4.3 / 4.1.2) | Darken the amber badge to pass contrast; add `aria-sort="ascending|descending|none"` on sortable `<th>` and make the header a real button. |
| medium | ROLE filter chips + [Band]/[Treemap] toggle | Toggle chips ([Data][Voice][Management][Guest]) and the Band/Treemap switch convey selected state by background color. Without `aria-pressed`/`aria-selected` the state is invisible to SR and to color-blind users (1.4.1 / 4.1.2). | Use `aria-pressed` (toggle buttons) or a `role="tablist"` for Band/Treemap; add a non-color selected cue (underline/checkmark/border). |
| low | Wide IP table (12+ columns) | Many-column table on narrow viewports forces horizontal scroll; need `<th scope="col">`, a `<caption>` or `aria-label` on the table, and the scroll container must be keyboard-scrollable + focusable (1.3.1 / 2.1.1). | Ensure proper `scope` attributes and a table caption; verify the horizontal-scroll wrapper is reachable by keyboard (tabindex=0 + role/region+label). |
| low | IPv6 huge-number cells | "/64 … 9,223,372,036,854,776,000" reads as a 19-digit string to a screen reader — exhausting and unhelpful. | For huge counts, show "~9.2 quintillion (/64)" with the precise value in `title`/`aria-describedby`, or label as "effectively unlimited". |

## What works well

- **Status as text, not just color, in the right places.** Subnet
  "active" status pill and the table "Status" column use the word — that's
  the correct color-independent pattern, and it shows the team knows how
  to do it. The fix for Seen/DNS is to extend this same habit.
- **Excellent plain-language empty states.** "No network discovery data —
  add the upstream switch in /network and wait a polling cycle." and
  "No active profile yet. Auto-profiling triggers on a fresh DHCP lease…"
  are clear, actionable, and read perfectly on a screen reader.
- **Visible field labels on most form fields** (Hostname, MAC Address,
  Status, Role) rather than placeholder-only — the alias/filter inputs are
  the exception, not the rule.
- **Helpful descriptive tooltips/helper text** ("Find unused CIDRs in this
  block", "(empty clears field)", "Auto-assigned if blank") — good raw
  material; they just need `aria-describedby` wiring to be announced.
- **Tabs** likely inherit shadcn's accessible `role="tablist"` behavior —
  one of the few complex widgets I expect to pass out of the box.

## My one big idea

**Kill "color-alone" across IPAM with one shared `<StatusTag>` primitive
— icon + text + color, never color alone — and adopt it for Seen, DNS
sync, status pills, role chips, and the allocation legend.** Right now
the most information-dense parts of IPAM (the Seen dots and DNS "in sync"
bullets in every IP row) are pure-color spans whose meaning lives only in
a `title` attribute that keyboard and color-blind users never receive.
A single primitive that always renders a distinct-shape icon + a short
text label, with color as decoration on top, fixes WCAG 1.4.1 everywhere
at once, makes the tables scannable when desaturated, and gives screen
readers a real name to announce. Pair it with one audit of the shared
`Modal`/tree components (focus management + ARIA roles) and IPAM goes
from "fails the first three things I test" to genuinely AA-conformant.
