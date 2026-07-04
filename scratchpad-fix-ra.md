# IPv6 RA / rogue-RA review fixes (#524)

Branch: issues-523-524-527-528-530 (uncommitted). No git commit/push, no alembic
run against shared dev DB. Unreleased migration edited in place to stay in sync
with the model.

## Finding 3 — radvd shipped even when the feature module is disabled (non-negotiable #14 dormancy)

- `backend/app/services/dhcp/config_bundle.py`
  - Added import `from app.services.feature_modules import is_module_enabled`.
  - `build_config_bundle` (~L266-282): gated the RA render loop on
    `await is_module_enabled(db, "ipv6.router_advertisements")`. When the module
    is off, `ra_configs` stays `[]` → `render_radvd_conf([])` → `radvd_conf == ""`.
  - ETag still shifts on/off because `compute_etag` (drivers/dhcp/base.py L291-292)
    already hashes `ra_configs` + `radvd_conf` (empty vs non-empty changes the hash),
    so the agent picks up the toggle and stops radvd (see Finding 5).
- The `ipv6.router_advertisements` ModuleSpec is already registered
  (feature_modules.py L141, default-enabled) and the router include is gated
  (router.py L203), so this is the last un-gated surface.

Test: `backend/tests/test_radvd_render.py`
  - `test_module_enabled_ships_radvd_conf` — module on + ra_enabled ipv6 scope →
    non-empty radvd_conf, 1 ra_config.
  - `test_module_disabled_empties_radvd_conf` — module off → `radvd_conf == ""`,
    `ra_configs == ()`, and `off.etag != on.etag`.
  - (Toggles the `feature_module` row directly + `invalidate_cache()`, restores in
    `finally`, matching the test_multicast.py pattern.)

## Finding 5 — disabling RA never stops radvd (stale RAs advertised forever)

- `agent/dhcp/spatium_dhcp_agent/radvd_apply.py`
  - Added `_stop()` (mirrors `_reload()`): reads the pidfile, SIGTERM the pid
    gracefully, treats `ProcessLookupError` as already-stopped, then blanks the
    managed config (`_atomic_write(path, "")`) so the disable is durable across
    restarts. No-op when there's no/invalid pidfile (radvd not running).
  - `apply_radvd`: empty/whitespace `radvd_conf` now calls `_stop()` (was an early
    return that deliberately left the last-good config running). Docstring rewritten
    to explain empty = intentional disable (last ra_enabled scope off, or module
    toggled off), distinct from control-plane-unreachable where the cached non-empty
    bundle keeps serving (non-negotiable #5 preserved for the NON-empty path).
  - `__all__` now exports `_stop`.

Test: `agent/dhcp/tests/test_radvd_apply.py` (new — DB-free, mocks `os.kill`/pidfile)
  - empty config → SIGTERM sent + config blanked; whitespace config → stop;
    empty + no pidfile → no-op; not-managed → no-op; already-gone pid → config blanked.
  - Ran: `5 passed`.

## Finding 8 — rogue-RA masked across L2 segments (link-local identity)

(a) Identity now includes source_mac so two physical routers sharing fe80::1 don't
    collapse:
  - `backend/app/models/dhcp.py` (RAObservedRouter): UniqueConstraint changed
    `("group_id","source_ip")` → `("group_id","source_ip","source_mac")`, renamed
    `uq_ra_observed_group_ip` → `uq_ra_observed_group_ip_mac`, with a comment on the
    NULL-distinct rationale.
  - `backend/alembic/versions/b7e4d1a92c30_ipv6_ra_management.py`: same 3-col
    UniqueConstraint (downgrade drops the whole table, so unaffected).
  - `backend/app/services/dhcp/ra_detection.py` `record_observations`: `existing`
    lookup now matches on (group_id, source_ip, source_mac) with an explicit
    `source_mac IS NULL` filter when the observed MAC is None (its own bucket).

(b) Allowlist tightening in `classify_router`/`_classify`:
  - An entry pinning a MAC only blesses observations with that MAC; an IP-only
    entry (no MAC) still blesses by IP (operator's explicit choice). So an
    allowlisted (ip, macA) no longer auto-classifies (ip, macB) as expected.
  - MAC-normalization path preserved (`_norm_mac`).
  - Residual limitation (spoofed-MAC rogue on same link can't be distinguished)
    documented in the `_classify` docstring.

Efficiency fix (behaviour-identical):
  - `record_observations` loads the group's `RARouterAllowlist` ONCE and passes it
    into `classify_router(..., allow=allow)`; `classify_router` gained an optional
    `allow` param (falls back to querying when None, so existing callers/tests keep
    working). Pure logic split into `_classify(allow, ip, mac)`.

Tests: `backend/tests/test_rogue_ra.py`
  - `test_distinct_mac_same_ip_gets_two_rows` — same fe80::1, two MACs → 2 rows.
  - `test_null_mac_is_its_own_bucket` — NULL-MAC + real-MAC on same IP → 2 rows.
  - `test_allowlisted_mac_does_not_bless_different_mac` — (ip,macA) blesses macA,
    not macB.
  - `test_ip_only_allowlist_blesses_any_mac` — IP-only entry still matches any MAC.

## Verification

- `ruff check` + `black --check`: all pass (backend files run under backend/pyproject
  line-length 100; agent under agent/dhcp/pyproject).
- `mypy app/services/dhcp/config_bundle.py app/services/dhcp/ra_detection.py`: clean
  (used `collections.abc.Sequence` for the allow param to satisfy `.scalars().all()`).
- `python3 -m py_compile radvd_apply.py`: OK.
- Agent test `tests/test_radvd_apply.py`: 5 passed.
- Backend DB-backed tests (test_rogue_ra.py, test_radvd_render.py) use the create_all
  `db_session` fixture but were NOT executed here — no Postgres reachable in this
  sandbox and the shared dev DB is off-limits (concurrent agents). They were written
  to the established fixture/pattern and are statically clean.
