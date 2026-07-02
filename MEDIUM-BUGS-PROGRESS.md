# IPAM medium bugs — work tracker (branch `ipam-medium-bugs`)

Working through the medium-severity findings from the 2026-07-02 IPAM review
(#503–#515). Commit after each item so disk state is always current and a fresh
session can resume. `#516` (B28) is **low** (frontend polish grab-bag) — out of
scope for this branch.

Verify gate before each push: host `ruff`/`black`/`mypy` (venv), `eslint`,
`prettier --check src`, `tsc -b`/`vite build` (frontend), migration-shape linter,
single alembic head. No Postgres in the sandbox → DB-backed pytest runs in CI.

Current single alembic head (before this branch): `b1f7c3a92e04`.

## Status

- [x] #503 B15 (api) IPv6 subnet import overflows BIGINT total_ips — importer.py lacks the 2^63-1 clamp (`_total_ips` in resize.py has it). Any /64 import 500s whole commit. `backend/app/services/ipam_io/importer.py` ~L451.
- [x] #504 B16 (api) Import robustness cluster: case-insensitive headers ("IP"/"IP Address"), export→import status round-trip rejection, unvalidated mac_address/gateway → 500 mid-commit, strategy="fail" intra-payload dup aborts after side effects. `parser.py`, `importer.py`, `export.py`.
- [ ] #505 B17 (api) Plan apply bypasses create_subnet invariants: multicast kind, network/broadcast/gateway placeholder rows, reverse-zone auto-create, gateway containment. `backend/app/api/v1/ipam/plans.py` ~L686.
- [ ] #506 B18 (api) IPv6 subnets never get network/gateway placeholder rows — gate `prefixlen < 31` is v4 logic; should be `< 127`. `router.py` create_subnet auto-address branch (~L3711 pre-merge; re-grep).
- [ ] #507 B19 (api) GET /subnets/{id}/effective-dns stops at root block; `_resolve_effective_dns` falls through to space → UI shows "no DNS" while records publish into space zone. `router.py` ~L4237 vs the resolver ~L317.
- [ ] #508 B20 (api,rbac) Coarse router gate: write:nat_mapping / write:custom_field can create/modify spaces/blocks/subnets (any-of gate, no per-type inline checks on structural handlers). `router.py` gate ~L78; `plans.py` gates ip_block only but creates Subnets.
- [ ] #509 B21 (api,frontend) Bulk-edit staleness: bulk_edit_addresses never recomputes utilization nor clears reserved_until on status change; FE bulk-delete misses ["subnets"] invalidation; subnet detail header snapshot never refreshed. `router.py` bulk_edit_addresses; `IPAMPage.tsx`.
- [ ] #510 B22 (worker) Device-profiling Celery dispatch before commit — run_scan finds no row, no-ops without retry; queued scan occupies 1 of 4 per-subnet slots forever. `backend/app/services/profiling/auto_profile.py` ~L149/216; `nmap/runner.py` ~L399.
- [ ] #511 B23 (api) NAT mappings 500 on malformed IP literals (cast to INET DataError). `backend/app/api/v1/ipam/nat.py` ~L61/290/340.
- [ ] #512 B24 (api) Subnet soft-delete skips collect_wake for DHCP channels (12s tick); permanent delete bulk-drops DNSRecord rows without enqueue_record_op → agentless Windows DNS keeps serving. `backend/app/services/ai/operations_risky.py` ~L246/295.
- [ ] #513 B25 (frontend) IPv6 drag-and-drop re-parent always fails — cidr.ts has no v6 containment, fails closed "does not fit inside". `frontend/src/lib/cidr.ts`; `IPAMPage.tsx` handleDragEnd.
- [ ] #514 B26 (frontend,rbac) Select-all / shift-range ignore per-row RBAC write gate (address sets) → bulk ops partially 403. `IPAMPage.tsx` select-all + shift-range vs checkbox render.
- [ ] #515 B27 (worker) Discovery double-dispatch race — last_discovery_at stamped at completion; >60s sweeps re-dispatched each tick; concurrent reconcile collides on unique constraint. `backend/app/tasks/ipam_discovery.py` ~L118.

## Commit log (fill as we go)

- #503 + #504 → commit (importer BIGINT clamp; parser case-insensitive headers; status round-trip; MAC/gateway validation; intra-payload dup pre-flight)