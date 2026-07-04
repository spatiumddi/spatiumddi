# Code-review fixes — issue #527 BGP hijack monitoring

Branch: `issues-523-524-527-528-530` (uncommitted). No git commit/push. No alembic
run against shared DB — migration edited in place (unreleased) + model kept in sync.
Tests exercised in the `api` container against an isolated `spatiumddi_test_bgpfix`
DB (create_all fixture): **16 passed** (stable across repeated isolated runs; the
transient deadlocks seen mid-work were cross-process contention on the shared base
test DB from other agents, not these tests).

## Finding 1 — CASCADE destroyed hijack evidence → SET NULL

`bgp_hijack_detection.tracked_prefix_id` FK was `ON DELETE CASCADE`; the poll prunes a
tracked prefix (`db.delete`) the instant it drops out of RIPEstat/ROA sources — exactly
what happens to a victim prefix mid-hijack — which cascaded away every open detection +
orphaned its AlertEvent. Changed FK `ondelete` to `SET NULL` in both:
- `backend/app/models/bgp_monitor.py` — `BGPHijackDetection.tracked_prefix_id`
  `mapped_column` (~line 132-146, with explanatory comment).
- `backend/alembic/versions/a1c7f3e9b284_bgp_hijack_monitoring.py` — the
  `sa.ForeignKeyConstraint(["tracked_prefix_id"], ...)` (~line 149-155).

Confirmed survivability: `resolve_stale_detections` keys off `asn_id` + `last_seen_at`;
the alert matcher (`app/services/alerts.py::_matching_bgp_hijack_subjects`) renders off
`tracked_prefix` (CIDR string), `observed_prefix`, `observed_origin_asn`,
`expected_origin_asn`, `severity`, `id` — none reference `tracked_prefix_id`. So a
NULL-FK detection stays open/latched and keeps alerting.

Test added: `test_pruned_tracked_prefix_keeps_open_detection` — deletes the tracked
prefix, asserts the open detection survives with `tracked_prefix_id IS NULL`,
`resolved_at IS NULL`, intact `tracked_prefix`/`asn_id`, that `resolve_stale_detections`
doesn't resolve it before the window, and that the alert evaluator still opens an
AlertEvent for the NULL-FK row.

## Finding 2 — /bgp not feature-module gated (non-negotiable #14)

`backend/app/api/v1/router.py` (~line 138): the `bgp_router` include had no gate while
the sibling `asns_router` used `require_module("network.asn")`. BGP folds into the
existing `network.asn` module (its MCP tools are already tagged `module="network.asn"`;
its UI is a tab on the ASN detail page). Added
`dependencies=[Depends(require_module("network.asn"))]` to the include. `Depends` +
`require_module` already imported.

Test added: `test_bgp_router_gated_by_network_asn_module` — superadmin token, inserts a
`FeatureModule(id="network.asn", enabled=False)`, invalidates the feature-module cache,
asserts `GET /api/v1/bgp/asn/64500/announced-prefixes` → 404 (gate fires before any
RIPEstat call).

## Finding 3 — poll refresh ignored configured cadence

`backend/app/tasks/bgp_hijack_poll.py::_run_poll` step (1) called
`refresh_tracked_prefixes_for_asn` (→ RIPEstat announced-prefixes HTTP call) for every
public ASN on every hourly beat, ignoring `bgp_monitoring_interval_hours`. Gated it —
schema-free — on the same per-prefix `next_check_at` the evaluation step already uses:

- Added an aggregate query building `refresh_due_by_asn: dict[asn_id, bool]` =
  `count(*) FILTER (WHERE next_check_at IS NULL OR next_check_at <= now) > 0` grouped by
  `asn_id`. An AS absent from the map (no tracked prefixes yet) defaults to due →
  refreshes promptly for a newly-tracked ASN; an AS whose prefixes are all still within
  their interval is skipped. Because every prefix of one AS shares the same
  `next_check_at` (all bumped in lockstep by step 2), refresh fires at most once per
  interval per AS. Idempotent.
- Loop skips `if not refresh_due_by_asn.get(asn_row.id, True)`; added an `asns_refreshed`
  counter to the log line + return dict.
- Added `func` to the `sqlalchemy` import.

Test added: `test_poll_refresh_gated_by_cadence` — first `_run_poll` refreshes the
newly-tracked AS once (`asns_refreshed == 1`, `prefixes_added == 1`); a second `_run_poll`
immediately after does NOT re-call refresh (spy count stays 1, `asns_refreshed == 0`).

## Finding 4 — active hijack auto-resolved during a RIPEstat outage

In `_run_poll`, every evaluated ASN was added to `touched_asn_ids` and then
`resolve_stale_detections` ran off `last_seen_at`. A soft RIPEstat outage returns
`evaluate_tracked_prefix(...)["unavailable"] == True` without bumping `last_seen_at`, so an
outage longer than `DEFAULT_DELIST_WINDOW` (12h) aged an ongoing hijack's detection past
the cutoff and auto-resolved it — clearing the alert while the hijack continued.

Fix (`backend/app/tasks/bgp_hijack_poll.py`): renamed `touched_asn_ids` →
`resolvable_asn_ids` and only add `tracked.asn_id` when `not summary["unavailable"]` (i.e.
the AS had at least one available evaluation this pass). Stale-resolution runs only for
resolvable ASNs. `evaluate_tracked_prefix` already returns the `unavailable` flag.

Tests added:
- `test_poll_does_not_resolve_when_unavailable` — open detection with `last_seen_at` past
  the delist window + a full RIPEstat outage (all fetches `available: False`); asserts
  `detections_resolved == 0` and `resolved_at` stays NULL.
- `test_poll_resolves_when_available_and_delisted` (control) — data available + hijacker
  origin gone; asserts `detections_resolved == 1` and `resolved_at` set.

## Efficiency fix — derive_rpki_status full-scanned the ROA table

`backend/app/services/bgp/hijack_monitor.py::derive_rpki_status` (~line 100) selected ALL
ROA rows (join ASN) on every call — once per unexpected origin per prefix per pass. Added
a Postgres `cidr >>=` covering filter `.where(ASNRpkiRoa.prefix.op(">>=")(str(obs)))` so
only ROAs whose prefix covers the observed prefix are fetched (different address families
never contain each other, so it also does the version filter). The Python `supernet_of`
re-check stays as a defensive belt → behaviour byte-identical. Existing tests
`test_rpki_status_{unknown,invalid,valid}_*` cover the invalid-vs-unknown-vs-valid
selection and still pass.

## Files touched
- backend/app/models/bgp_monitor.py
- backend/alembic/versions/a1c7f3e9b284_bgp_hijack_monitoring.py
- backend/app/api/v1/router.py (bgp_router include only)
- backend/app/tasks/bgp_hijack_poll.py
- backend/app/services/bgp/hijack_monitor.py
- backend/tests/test_bgp_hijack_monitor.py

## Verification
- `ruff check` (app+tests scope, matches CI `ruff check app tests`) — clean. (The
  migration's Alembic `Union` boilerplate trips UP007, but CI does not lint
  `alembic/versions/`.)
- `black --check` on all 6 files — clean.
- `mypy` (host venv) on bgp_hijack_poll.py, hijack_monitor.py, bgp_monitor.py — clean.
- `scripts/lint_migrations.py` — OK (±5-line fuzz absorbs the finding-1 comment shift; no
  re-baseline needed).
- pytest `tests/test_bgp_hijack_monitor.py` — 16 passed (isolated DB).
