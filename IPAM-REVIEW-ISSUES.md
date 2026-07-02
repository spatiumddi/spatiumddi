# IPAM review 2026-07-02 — issue creation tracker

Source: full IPAM review sweep (backend router/models, services/tasks, frontend, product coverage).
Check off each item with its issue number as created. Labels: bugs get `bug` + severity
(`critical`/`high`/`medium`/`low`) + component (`api`/`frontend`/`worker`); improvements get
`enhancement`; features get `enhancement` + `idea`.

> **Fixes implemented on branch `ipam-review-2026-07-02`:** the critical + all
> high-severity bugs — #489–#502 (B1–B14). Verified: ruff / black / mypy,
> eslint, `tsc -b` + vite build, migration-shape linter, single alembic head.
> DB-backed pytest runs in CI (no Postgres in the dev sandbox). Medium/low bugs,
> improvements, and features remain open.

## Bugs

- [x] #489 B1 (critical, api) Next-IP allocation materializes entire host list — DoS. `_pick_next_available_ip` router.py:763 (v6 seq: `list(net.hosts())[:max_search]` on /64 → 2^64 objs; nothing enforces ">= /112") and :804 (v4: both arms fully materialize; /8 = 16.7M objs). Hangs holding subnet FOR UPDATE lock. Fix: itertools.islice.
- [x] #490 B2 (high, api) Raw-SQL overlap checks bypass soft-delete filter → 30-day phantom conflicts. `_assert_no_overlap` router.py:169, `_assert_no_block_overlap` :207, available-subnets :3065, resize.py:310/383/425/468 all `text()` SQL without `deleted_at IS NULL` (ORM hook only rewrites ORM SELECTs). Trash subnet → recreate/grow neighbor → 409 naming invisible row. Also block-utilization CTE :287 counts trashed subnets. Same class as shipped DHCP fix b67286f. create_block :2818 does it right.
- [x] #491 B3 (high, api) Soft-deleted IP space holds unique name → 500 on recreate. create_space ORM pre-check passes, full unique index `ix_ip_space_name` fires IntegrityError. Needs partial unique index `WHERE deleted_at IS NULL`. update_space rename has no pre-check at all. router.py:2456, models/ipam.py:114.
- [x] #492 B4 (high, api+dns-agent) Hostname rename leaks old A/AAAA RRset on live nameserver. `_sync_dns_record` update branch (router.py:1300) renames DB row, ships one `update` op with only new name; bind9 agent does replace(new_name) — old RRset never deleted; drift compare is DB-vs-IPAM so invisible. agent/dns/.../bind9.py:766.
- [x] #493 B5 (high, api+frontend) Per-IP DNS zone override lost on unrelated edits. Server: update_address passes zone_id=None on hostname-only edit; `_sync_dns_record` prefers subnet effective zone over forward_zone_id → record moves zones (router.py:6418, :1090). Client: EditAddressModal pre-selects subnet primary zone not address.forward_zone_id → no-op Save moves record, or creates one for "no DNS" IPs (IPAMPage.tsx:8155).
- [x] #494 B6 (high, api) Template carve_children snaps CIDRs downward → overlapping subnets. templates.py:352-397: cursor advances, next child ip_network(strict=False) masks down; [/26,/25] on /24 → /25 overlaps /26; inserted with no overlap check. Also carved subnets total_ips=0 forever + no multicast kind detection.
- [x] #495 B7 (high, api) Subnet importer commit has no overlap validation. ipam_io/importer.py:350-488 exact-CIDR match only; 10.0.0.0/23 into space with 10.0.0.0/24 commits overlap; auto-parent blocks skip `_assert_no_block_overlap` (:426). NetBox committer does it right (netbox_import/commit.py:513).
- [x] #496 B8 (high, api) `dns_split_horizon` never serialized on blocks — toggle flips itself off on next save. `_block_to_response` router.py:2652 omits key → response always False. #25 block-level split-horizon unusable via API.
- [x] #497 B9 (high, api) create_subnet never validates subnet fits inside block (reparent path does). router.py:3615. 192.168.1.0/24 into 10.0.0.0/16 block corrupts free-space math.
- [x] #498 B10 (high, api) bulk-allocate commit 500s when `description` omitted. Schema None → ORM constructor suppresses default → NOT NULL violation, batch rolls back. router.py:7011/:7294, models/ipam.py:832.
- [x] #499 B11 (high, api) bulk-delete bypasses auto_from_lease mirror guard (single delete 409s :6739, bulk edit skips :8320; bulk-delete :8104 has no check) — hard-delete retracts live DDNS records for active leases.
- [x] #500 B12 (high, frontend) reserved_until drifts by UTC offset each Edit open→save. toISOString().slice(0,16) (UTC) into datetime-local (local). IPAMPage.tsx:8064 init, :8178 save.
- [x] #501 B13 (high, frontend) Gap-marker rows computed from *filtered* list — filters fabricate clickable "free" ranges over allocated IPs. tableRows IIFE IPAMPage.tsx:4720, gaps :4776. Suppress when hasActiveFilter.
- [x] #502 B14 (high, api+frontend) Cannot clear fields via edit; delete flows misstate permanence. FE: `value || undefined` + backend exclude_unset = silent no-op clearing hostname/MAC/desc/gateway (IPAMPage.tsx:8182, 7250, 11102; inline editor does `|| null` right :5848). BE: update_space/block/subnet exclude_none + hand-picked re-inject → cannot clear vrf_id/customer_id/gateway/ddns_* (router.py:2509/:2945/:4410). Also single-subnet delete says "permanent, cannot be undone" but is a soft-delete to Trash (client never sends permanent=true); bulk flow says Trash correctly (IPAMPage.tsx:7390 vs 13758).
- [x] #503 B15 (medium, api) IPv6 subnet import overflows BIGINT total_ips — importer lacks the 2^63-1 clamp (router.py:145 has it). Any /64 import 500s whole commit. importer.py:451.
- [x] #504 B16 (medium, api) Import robustness cluster: non-lowercase headers ("IP") silently import 0 rows (parser.py:171/289; "IP Address" routes to subnet branch); export→import round trip rejects legit statuses available/discovered/placeholders (importer.py:518 vs export.py:43 contract); unvalidated mac_address/gateway → DataError 500 mid-commit (:594/:366); strategy="fail" intra-payload duplicate aborts after earlier rows already pushed live records to agentless Windows DNS (:857).
- [x] #505 B17 (medium, api) Plan apply bypasses create_subnet invariants: no multicast kind detection, no network/broadcast/gateway placeholder rows, no reverse-zone auto-create, no gateway containment. plans.py:686-718.
- [x] #506 B18 (medium, api) IPv6 subnets never get network/gateway placeholder rows — gate `prefixlen < 31` is v4 logic, v6 branch dead; should be < 127. router.py:3711-3760.
- [x] #507 B19 (medium, api) GET /subnets/{id}/effective-dns stops at root block; `_resolve_effective_dns` falls through to space → UI shows "no DNS" while records publish into space zone. router.py:4237 vs :317.
- [x] #508 B20 (medium, api) Coarse router RBAC gate: write:nat_mapping or write:custom_field grants can create/modify spaces/blocks/subnets (any-of gate router.py:78, no per-type inline checks on structural handlers; plans.py:36 gates ip_block only but creates Subnets).
- [x] #509 B21 (medium, api+frontend) Bulk-edit staleness: bulk_edit_addresses never recomputes utilization nor clears reserved_until on status change (router.py:8232); FE bulk-delete misses ["subnets"] invalidation (IPAMPage.tsx:9826); subnet detail header is a snapshot never refreshed after IP mutations (:14490).
- [x] #510 B22 (medium, worker) Device-profiling Celery dispatch before commit — run_scan finds no row, no-ops without retry; queued scan permanently occupies 1 of 4 per-subnet auto-profile slots. auto_profile.py:149/216, nmap/runner.py:399.
- [x] #511 B23 (medium, api) NAT mappings 500 on malformed IP literals (no validation; cast to INET DataError). nat.py:61/:290/:340.
- [x] #512 B24 (medium, api) Subnet soft-delete skips collect_wake for DHCP channels (12s tick); permanent delete bulk-drops DNSRecord rows without enqueue_record_op → agentless Windows DNS keeps serving deleted records. operations_risky.py:246/:295.
- [x] #513 B25 (medium, frontend) IPv6 drag-and-drop re-parent always fails — cidr.ts has no v6 containment, fails closed "does not fit inside". IPAMPage.tsx:14140/:14156.
- [x] #514 B26 (medium, frontend) Select-all / shift-range ignore per-row RBAC write gate (address sets #103/#449) → bulk ops partially 403. IPAMPage.tsx:5389/:5800 vs checkbox :5770.
- [x] #515 B27 (medium, worker) Discovery double-dispatch race — last_discovery_at stamped at completion; >60s sweeps re-dispatched each beat tick; concurrent reconcile collides on unique constraint. ipam_discovery.py:118-152.
- [x] #516 B28 (low, frontend) Frontend polish grab-bag: static-DHCP chained create misreports as allocation failure + invites dup retry (:3181); IPDetailModal zone fields always "—" (dead zoneNameById prop); EditSpaceModal/delete mutations no onError (silent failures :10368/:4815); BulkEditSubnetsModal raw div not shared draggable Modal (:13829); DnsSyncModal fires reverse-zone backfill on open (:6465); status dropdowns omit available/discovered (:2819/:8041); skipped-rows feedback 1.2s flash (:9397); cidrSize wrong for v6 (:12795); SyncMenu isPending hardwired false (:4916).

## Improvements

- [x] #517 I1 (enhancement, api+frontend) Server-side pagination + search for addresses/subnets/blocks + table windowing. listAddresses has no params; /16 renders ~65k <tr> with per-row ContextMenu, no useMemo; all filtering client-side, no cross-subnet search. ?q=&status=&limit=&offset= + windowing. Highest-value item.
- [x] #518 I2 (enhancement) IPv6 parity umbrella: gap rows/pool columns unhinted no-op on v6, B18 placeholders, B25 drag-drop, cidrSize, B15 import overflow, B1 sequential guard.
- [x] #519 I3 (enhancement, frontend) Column sorting on IP/block/space tables (filters exist in 4 modes, no sort; Seen/vendor/hostname).
- [x] #520 I4 (enhancement) Cross-subnet bulk operations from filter results (selection confined to one subnet; "deprecate every IP tagged env=legacy" has no surface).
- [x] #521 I5 (enhancement, worker) Utilization recount sweep — NetBox import + lease mirrors never maintain allocated_ips (0% heatmap until interactive edit); importer.py:912 comment claims a sweep exists that doesn't.
- [x] #522 I6 (enhancement) Perf niggles: dhcp_lease_cleanup full subnet scan per unmatched stale lease (:66); ipam_dns_sync walks every subnet (pre-filter zone-bound); FE precompute pool int bounds (:4594); 220ms deferRowActivate delay (:516).
- [x] #523 I7 (enhancement, api) API symmetry: subnet-scoped tokens read all via list endpoints; preview_next_ip ignores address-set delegation (:7427); allocate_next_ip lacks public-facing warning + extra_zone_ids (:7484 vs :6141); CSV export formula-injection hardening.

## New features

- [x] #524 F1 IPv6 RA management (managed radvd rendered from subnet data: prefix, M/O flags, RDNSS) + rogue-RA detection (v6 twin of shipped rogue-DHCP probe).
- [x] #525 F2 NAC-lite: MAB RADIUS responder answering switch MAC-auth from the IPAM MAC allowlist (accept/reject + optional VLAN attribute).
- [x] #526 F3 Managed TFTP/HTTP boot service + ZTP (per-device config templates via DHCP opts 66/67; completes PXE profiles story).
- [x] #527 F4 BGP prefix-hijack detection via RIS Live for tracked ASNs/prefixes (origin-AS mismatch alert; extends ASN alert family).
- [x] #528 F5 DNSBL/RBL monitoring for public + NAT egress IPs (daily sweep, ip_blocklisted alert rule).
- [x] #529 F6 Auto-drawn L2/L3 topology map from LLDP + SNMP FDB/ARP (global view; reuse SD-WAN SVG approach).
- [x] #530 F7 GeoDNS / topology-aware steering for GSLB pools (BIND views + GeoIP ACLs; dovetails with DNS-views wiring).
- [x] #531 F8 DANE/TLSA + SSHFP record automation from cert monitor + DNSSEC (drift alert on cert-rotated-record-stale).
- [x] #532 F9 Private ACME CA for internal names (issue certs to LAN services via ACME against internal zones; root distributable from UI).
- [x] #533 F10 Wake-on-LAN action on IP detail modal / device rows (Tools family).

## Docs

- [x] #534 D1 CLAUDE.md pending-roadmap staleness cleanup (#22 #24 #40 #42 #44 #46 #129 no longer open; Windows Path B remainder lives in #444).
