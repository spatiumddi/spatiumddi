# Issue #528 — DNSBL/RBL monitoring — COMPLETE

Branch: `issues-523-524-527-528-530`. NOT committed/pushed. All verified.

## Migration
- Rev `c9f2e1a4d7b6`, down_revision `a1c7f3e9b284` (bgp_hijack — was single head).
- File: `backend/alembic/versions/c9f2e1a4d7b6_dnsbl_monitoring.py`
- Additive. Creates `dnsbl_list`, `dnsbl_pinned_ip`, `dnsbl_listing`.
- Adds PlatformSettings cols: `dnsbl_monitoring_enabled`(bool F), `dnsbl_check_interval_hours`(int 24), `dnsbl_sweep_last_run_at`(ts null), `dnsbl_query_resolvers`(jsonb null). NOTE: the previous agent had already added the first two column *models* to settings.py; migration creates all four; I added the last two to the model.
- Seeds `feature_module` row `security.dnsbl`=TRUE.
- Downgrade drop-ops appended to `backend/alembic/migrations_lint_baseline.txt` (7 lines at final post-black line numbers 199/201/203 drop_table, 204-207 drop_column). Linter passes.
- VERIFIED: `alembic upgrade head` applied clean through full chain on a scratch DB; all 3 tables + 4 settings cols + feature_module seed present.

## Model
- `backend/app/models/dnsbl.py` — kept the previous agent's coherent partial (DNSBLList / DNSBLPinnedIP / DNSBLListing + SOURCE_* consts). Already wired into `app/models/__init__.py` (import + __all__) by prev agent. Ran black over it.

## Feature module (#14)
- `security.dnsbl`, group Security, default_enabled=True (discovery; zero off-prem calls until master switch + a list enabled). Added to `app/services/feature_modules.py` MODULES.
- Router gated: `app/api/v1/router.py` include of `dnsbl_router` with `Depends(require_module("security.dnsbl"))` at prefix `/dnsbl`.

## Service layer
- `app/services/dnsbl/__init__.py` (exports seed_dnsbl_catalog)
- `app/services/dnsbl/catalog.py` — CATALOG (6 lists: Spamhaus ZEN, Barracuda, SpamCop, SORBS, UCEPROTECT L1, PSBL) + idempotent `seed_dnsbl_catalog()` keyed on zone_suffix, is_builtin=True, all seeded disabled, preserves operator `enabled`.
- `app/services/dnsbl/sweep.py` — pure `reversed_octets`/`dnsbl_query_name`/`is_ipv4`; `derive_candidates` (4 sources, precedence pinned>nat_egress>internet_facing>ipam, private/CGNAT/v6 skipped via `app.services.ipam.classify.is_private_ip`); `check_one` (dnspython `dns.asyncresolver`, A+TXT, NXDOMAIN=not listed, errors recorded not raised); `_apply_result` latch; `run_sweep`; `check_ip_now`.

## Alert (latch)
- `RULE_TYPE_IP_BLOCKLISTED = "ip_blocklisted"` in `app/services/alerts.py` (+ in RULE_TYPES frozenset).
- Recurring-condition pattern: `_matching_ip_blocklisted_subjects` + elif branch `subject_type="ip_blocklist"` → shared open/resolve loop auto-latches + auto-resolves on delist. No custom evaluator needed.
- `seed_ip_blocklisted_alert_rule()` seeded DISABLED, wired in `app/main.py` startup.

## Task
- `app/tasks/dnsbl_sweep.py::sweep_dnsbl`, beat `dnsbl-daily-sweep` crontab(hour=4,minute=30) in `app/celery_app.py`; also added to worker `include=[...]`. Gated on module + `dnsbl_monitoring_enabled`; `autoretry_for=(SQLAlchemyError, ConnectionError, socket.gaierror, OSError)`; jitter throttle in run_sweep; stamps `dnsbl_sweep_last_run_at`.
- DNS resolver lib: **dnspython** (already a dep).

## API — `/api/v1/dnsbl/*` (`app/api/v1/dnsbl/router.py`)
- gated `require_resource_permission("dnsbl")`; audited mutations.
- GET/POST/PUT/DELETE `/lists` (builtin undeletable), GET/POST/DELETE `/pinned`, GET `/listings`, GET `/listings/by-ip/{ip}`, POST `/check`, GET/PUT `/settings`.
- Added `{"action":"admin","resource_type":"dnsbl"}` to Network Editor builtin role in `app/main.py`. Viewer wildcard read covers reads.

## MCP tools (`app/services/ai/tools/dnsbl.py`, module=security.dnsbl; registered in tools/__init__.py)
- `find_blocklisted_ips` default_enabled=True
- `count_blocklisted_ips` default_enabled=True
- `find_dnsbl_lists` default_enabled=True
- `propose_pin_ip_for_dnsbl` default_enabled=False (delegates to operation `pin_ip_for_dnsbl` added in `app/services/ai/operations.py`, required_permission ("write","dnsbl"))

## Frontend
- `frontend/src/lib/api.ts` — `dnsblApi` + types.
- `frontend/src/pages/admin/DNSBLPage.tsx` — sweep settings (master enable/interval/last-run), blocklisted overview, pinned IPs CRUD, catalog table w/ per-list enable + registration/QPS notes.
- `frontend/src/App.tsx` — route `/admin/dns-blocklists`.
- `frontend/src/components/layout/Sidebar.tsx` — NavItem "DNS Blocklists" under Notifications group, `module:"security.dnsbl"`.
- `frontend/src/pages/ipam/IPDetailModal.tsx` — `ReputationSection` panel (per-list status + "Check now"), IPv4-only, module-gated.
- Build + prettier + eslint all clean.

## Docs
- `docs/features/IPAM.md` §13a — catalog, candidate-set derivation, rate-limit/registration notes, sweep, alert, surfaces.

## Tests
- `backend/tests/test_dnsbl.py` — 16 tests, ALL PASS (reversed-octet, candidate derivation public/private/v6/internet_facing/nat_egress/pinned/precedence, check_one listed/NXDOMAIN/timeout with mocked resolver, run_sweep persistence + delist-resolves + error-preserves-state, alert latch/auto-resolve). DNS mocked; ran against container PG (172.21.0.8, creds spatiumddi/spatiumddi, `TEST_DATABASE_URL`).

## Verification notes / unverified
- Tests run with `-p no:xdist --no-cov` against the running `spatiumddi-postgres-1` container (no host 5432; used container IP).
- Migration applied end-to-end on scratch DB — OK.
- NOT runtime-tested: the live Celery beat dispatch and real DNS lookups (mocked by design). The IP-detail Reputation panel + admin page verified only via `tsc`/build, not a live browser click-through.
- host lacks `python`/`psql`; used `.venv` python + asyncpg.
