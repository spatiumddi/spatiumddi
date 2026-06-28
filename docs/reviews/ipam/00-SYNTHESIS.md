# SpatiumDDI IPAM — Consolidated UX Review

**Synthesized from 10 persona reviews:** expert network engineer, first-time
user, LAN administrator, novice NOC tech, accessibility specialist, visual/UX
designer, NetBox migrant, mobile/responsive on-call user, information
architect, enterprise-scale admin.

Signal strength = how many personas independently raised a theme. Where
personas genuinely disagree, the disagreement is preserved rather than
averaged.

---

## Prioritized theme table (highest impact first)

| # | Theme | Severity | Raised by (n) | Recommendation |
|---|---|---|---|---|
| 1 | **Bare "0" in IP detail modal** (REVERSE DNS ZONE / DNS-DHCP LINKAGE / counters) — looks broken, erodes trust | blocker | 9 | Fix the falsy-render bug; render zone NAME or em-dash; never surface a raw id/count. **(BUG)** |
| 2 | **IPv6 /64 capacity math** — "0 / 9,223,372,036,854,776,000" (2^63 signed overflow, not 2^64) + meaningless 0% bar | blocker | 9 | For prefixes > ~/100 suppress denominator + % bar; show "/64 · N allocated"; fix the overflow. **(BUG + UX)** |
| 3 | **No global / tree search** — can't jump to a CIDR, hostname, MAC, or IP; tree is the only way in | blocker (at scale) / high | 5 | Add a tree quick-filter now; ship a global Cmd-K search across all spaces deep-linking to subnet/IP. |
| 4 | **Raw NetBox provenance leaking** — `netbox_id`, `netbox_is_pool false` chips on block header | high | 8 | Collapse to one "Imported from NetBox" chip; hide negative booleans; move raw fields behind a Provenance disclosure. |
| 5 | **"NetBox import" string in the Router column** — provenance masquerading as a typed device value | high | 7 | Stop writing import source into Router; render unmapped values as "—" or "unmapped (imported)". **(BUG-adjacent)** |
| 6 | **Unexplained green status dot in the tree** — no legend, no tooltip, conflated with the 4-state Seen dot | medium | 9 | Add tooltip/legend; if it is Seen-recency reuse that 4-state key; make the two dot systems visually distinct. |
| 7 | **Status conveyed by color alone** (Seen dots, DNS "• in sync") — fails WCAG 1.4.1, invisible on touch (no hover) | blocker (a11y/mobile) / high | 4 | Shared `<StatusTag>` icon+text+color primitive; render the word in-cell, never a tooltip-only dot. |
| 8 | **Mixed block+subnet table** — shared columns blank for ~half the rows; reads as missing data, not "N/A for blocks" | high | 6 | Add a Type column / row-type badge, or split into stacked "Child blocks" / "Subnets" tables; show block-level utilization. |
| 9 | **VRF/BGP header treatment** — "not configured" nags newcomers, hides VRF from experts; lost-on-import for migrants | high | 6 | Collapse to a quiet chip when unset; promote VRF to a column/chip when set; map NetBox VRFs on import. |
| 10 | **Wide tables break on mobile** (10–12 cols) — on-call essentials (Status/Seen) scroll off-screen right | blocker (mobile) | 2 | Stacked card layout < ~640px showing Address · Hostname · Status · Seen-as-text; collapse the rest behind tap. |
| 11 | **No inline editing / multi-step allocate** — every hostname/tag/status fix is a modal round-trip | high | 2 | Double-click-to-edit cells; put Hostname + MAC in the Allocate modal so a new host is one form, one save. |
| 12 | **No estate-wide filtering** — can't answer "subnets > 90% util" across spaces; no pagination/virtualization cue | high (at scale) | 1 | Estate-wide Subnets query page with column filters + saved views (model on the existing Stale-IP report). |
| 13 | **Icon-only controls without names** — sidebar refresh/upload/+, per-row pencil/trash, copy icon | high (a11y) / low | 5 | Add accessible names incl. row identity ("Edit 10.1.0.5"); tooltips on sidebar icons; clarify what "upload" imports. |
| 14 | **`auto:` prefix in tree labels** — machine token glued to CIDR; reads as debug/junk | medium | 7 | Render `auto:` as a muted badge + tooltip, not part of the name. |
| 15 | **Inconsistent toolbar grammar across levels** — flat buttons → dropdown/Tools-overflow on drill-down | high (design) | 2 | One level-invariant toolbar grammar applied identically at Space/Block/Subnet. |
| 16 | **Destructive vs read-only actions look identical** — Sync DNS / Merge / Resize undistinguished from Export | high (novice) | 2 | Visually separate mutating vs read-only; danger-style + blast-radius confirm on destructive items. |
| 17 | **Jargon without inline help** — "aggregation suggestion / supernet", "Address Sets / RBAC-scoped slices", "Find Free", Role taxonomy | medium | 5 | Plain-language tooltips that don't explain jargon with more jargon; one-line definitions where the term appears. |
| 18 | **Tree / modal / tab ARIA + focus** — likely div-based tree, draggable-modal focus loops, placeholder-as-label | high (a11y) | 1 | Audit shared tree + Modal once; real `role="tree"`, dialog focus trap/return, real labels not placeholders. |
| 19 | **Flat IP-detail field grid** — 10 fields spanning 4 concepts with no subheads | low | 1 | Group into Identity / DNS & DHCP / Lifecycle subheads. |
| 20 | **Role concept is half-wired** — filter chips at block level, per-IP "— None —" dropdown, "Legacy tag" tooltip | medium | 5 | Unify/clarify Role scope (IP vs subnet vs block); resolve legacy-vs-current ambiguity; map on import not into tags. |

---

## Likely bugs (defects, not preferences)

1. **Bare "0" in IP detail modal.** Confirmed in two reviews against
   `IPDetailModal.tsx:373`: `{(addr.alias_count || addr.nat_mapping_count) && (…)}`
   — when both are 0, `0 || 0 → 0` and React renders the literal `0`. Fix:
   `((alias_count ?? 0) > 0 || (nat_mapping_count ?? 0) > 0)`.
2. **REVERSE DNS ZONE / DNS-DHCP LINKAGE render a raw FK id (often `0`)** when
   the zone-name lookup misses. After a NetBox import (no reverse zones yet),
   every IP shows `0`. Fix: treat falsy/zero ids as empty → em-dash; render the
   zone NAME, never the id.
3. **IPv6 /64 capacity overflow.** Shows `9,223,372,036,854,776,000` = 2^63
   (signed-int / JS MAX_SAFE_INTEGER artifact), not the correct 2^64 ≈ 1.8e19.
   The number is *wrong*, plus a 0% utilization bar against it is meaningless.
4. **"NetBox import" written into the Router column** — the importer stamped a
   provenance string into a typed operational field. Category error that will
   break gateway reconciliation; affects thousands of rows at scale.
5. *(Suspected)* **NetBox VRF/tenant not mapped on import** — every imported
   space reads "VRF — not configured" and there is no Tenant/Customer column,
   despite the importer claiming tenant→Customer. Reads as data loss to migrants.

---

## Quick wins (low effort, high value)

- Fix the counters falsy-render `0` bug (one-line guard).
- Em-dash fallback for empty zone/linkage fields; never print a raw id.
- Suppress IPv6 host count + % bar for very large prefixes; show "/64 · N allocated".
- Stop writing "NetBox import" into Router; render "—".
- Collapse `netbox_id` / `netbox_is_pool false` into one "Imported from NetBox" chip.
- Render `auto:` as a muted badge instead of a label prefix.
- Add tooltip/legend to the tree green dot (or reuse the Seen 4-state key).
- Tooltips + accessible names on sidebar refresh/upload/+ and per-row pencil/trash.
- Real `aria-label`s on alias/filter inputs (placeholder is not a label).
- Plain-English rewrites: "aggregation suggestion", "Address Sets", "Find free space…".
- Collapse the VRF/BGP "not configured" nag to a quiet chip when unset.
- Render in-cell text for Seen + DNS-sync states (don't rely on tooltip-only dot).

## Big bets (structural / redesign)

- **Global Cmd-K search + estate-wide Subnets query page** with column filters
  and saved views — make search the primary nav, not the tree (scale story).
- **Tree-at-scale**: virtualization / lazy-load on expand + an inline quick-filter.
- **Mobile "on-call mode"**: stacked card IP list < 640px + full-screen
  (non-draggable) detail sheet + persistent IP/hostname search.
- **Inline cell editing** on the IP table (double-click Hostname/Tags/Status/Desc).
- **One-form allocate** (Hostname + MAC in the Allocate modal).
- **Shared `<StatusTag>` primitive** (icon+text+color) adopted everywhere to kill
  color-alone — fixes WCAG 1.4.1 + touch + scannability in one move.
- **Level-invariant toolbar grammar** across Space/Block/Subnet.
- **Split the block+subnet table** into two nouns (or a first-class Type column).
- **Invisible-import**: full field mapping at import time (VRF→Space, tenant→
  Customer column, gateway→Router, role→Role, pool flag→reservation behavior).
- **Accessibility pass** on shared tree + Modal (ARIA tree, focus trap/return).

---

## Genuine disagreements (do NOT average away)

- **Density vs guidance.** The expert network engineer and enterprise admin
  want *more* density, inline editing, no hand-holding, and explicitly do **not**
  want the "Ask AI" button taking primary real estate. The first-time user and
  novice NOC tech want *less* density, first-run empty-state guidance, jargon
  defined inline, and clear safe-vs-dangerous styling. → Resolve with
  progressive disclosure (dense default + opt-in guidance/help layer), not a
  single compromise altitude.
- **VRF prominence.** Expert/migrant want VRF promoted to a column/chip and
  visible at a glance; first-timer/novice/on-call want it *hidden* because it
  reads as intimidating or irrelevant. → Show prominently only when configured;
  collapse to nothing/quiet chip when unset.
- **Reserved (.0/.255/gateway) rows.** Novice and first-timer love them as a
  protective teaching affordance; LAN admin wants a "hide reserved rows" toggle
  to cut scroll on a /24. → Keep by default, add a sticky per-user hide toggle.
- **"Ask AI" button.** Experts want it demoted out of the primary action row;
  no persona defended its current prominence. → Demote into a Tools/overflow.

---

## What consistently works well (preserve)

- **Dashed-emerald GAP row** ("245 free · click to allocate") — cited by every
  persona as the best single affordance; turns a hole into a call-to-action.
- **Greyed network/broadcast/gateway rows** — protective + teaches protocol reality.
- **Subnet summary stat row** (Gateway · VLAN · Total · Allocated · Util%) — right density, right order.
- **Allocation Map (Band/Treemap) + Find Free / Resize / Move / Aggregation suggestion** — real capacity tools.
- **Seen 4-state dot** as an axis orthogonal to lifecycle status (modeling is right; presentation needs text).
- **Self-explaining empty states** (Network Discovery / Device Profile) — name the cause AND the fix; the template the rest of IPAM should adopt.
- **Status as text pills** (not color-only) where used — the correct pattern to extend.
