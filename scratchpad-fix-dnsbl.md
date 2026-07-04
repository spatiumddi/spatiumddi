# DNSBL fix — finding #9 + safe efficiency fixes (issue #528)

Branch: `issues-523-524-527-528-530` (uncommitted). No git commit/push done.

## Finding #9 — `ip_blocklisted` alert never auto-resolves when an IP leaves the candidate set

**Problem:** `run_sweep` only re-checked IPs currently returned by `derive_candidates`; `_apply_result`
flips `listed=False` / sets `resolved_at` only on a fresh *not-listed* DNS answer. So a listed IP that
dropped out of the candidate set (operator unpins it, IPAM row deleted, subnet un-flagged
`internet_facing`) — or a listing on a list that got disabled — was never re-checked, kept
`listed=True`/`resolved_at=NULL`, and paged forever.

**Change:** new reconcile pass in the sweep that resolves de-scoped / disabled-list latches.

- `backend/app/services/dnsbl/sweep.py`
  - New `_reconcile_descoped(db, candidates, now)` (~line 250): SELECTs every OPEN latch
    (`listed IS TRUE` AND `resolved_at IS NULL`) joined to its list's `enabled`; for each row whose
    `str(ip)` is **not** in the current candidate set **OR** whose list is **not enabled**, sets
    `listed=False` + `resolved_at=now`. Rows for a still-candidate IP on a still-enabled list are left
    untouched, so a transient DNS error in the active sweep (which leaves the latch alone) is never
    mistaken for a delist. Returns a count.
  - `run_sweep` (~line 285) rewritten so the reconcile runs **before** the `if not enabled_lists`
    early return — this is what makes "operator disabled the only list" resolve (no enabled lists left
    to sweep, but the reconcile still runs). Added a `"resolved"` counter. A trailing `await db.commit()`
    flushes reconcile changes even when there are zero candidate iterations to trigger the per-IP commit.
  - The `ip_blocklisted` alert matcher in `alerts.py` was **left unchanged on purpose**: it already
    filters `DNSBLListing.listed.is_(True)` AND `DNSBLList.enabled.is_(True)`, so (a) once the sweep
    resolves a de-scoped row (`listed=False`) the matcher drops it, and (b) a disabled list is already
    excluded at the matcher. Resolving in the sweep keeps the audit/resolve trail correct; the matcher's
    `enabled` filter is the belt-and-braces for the disabled-list case. No scope creep into other rule
    types.

## Safe efficiency fixes (behavior identical)

- `backend/app/services/dnsbl/sweep.py`
  - **One resolver per sweep.** New `build_resolver(resolvers)` helper (~line 156) constructs a single
    `dns.asyncresolver.Resolver` (returns `None` if dnspython missing). `check_one` gained a keyword-only
    `resolver=None` param — reuses a passed-in resolver, else builds one (preserves direct/on-demand call
    behavior). `run_sweep` + `check_ip_now` build it **once** and thread it into every `check_one`, so we
    no longer pay `configure=True` (`/etc/resolv.conf` parse) per (ip, list). Per-query jitter/throttle
    (`_QUERY_DELAY_S` + jitter) kept for rate-limit friendliness.
  - **Preload listing rows once.** `run_sweep` preloads existing `dnsbl_listing` rows for the candidate
    set in a single `SELECT ... WHERE ip IN (...)` into `{(str(ip), list_id): row}`; `check_ip_now` does
    the same for its one IP. `_apply_result` gained an `existing` dict param and looks up in memory (and
    inserts newly-created rows back into it) instead of a `SELECT` per (ip, list) — mirrors
    `refresh_tracked_prefixes_for_asn`'s `existing_by_prefix`. Incremental per-IP commit + idempotency
    preserved.

## Tests — `backend/tests/test_dnsbl.py`

- Updated the 5 existing monkeypatched `check_one` fakes in the `run_sweep` tests to accept the new
  `resolver=None` kwarg.
- Added 3 tests (finding #9):
  - `test_run_sweep_resolves_when_ip_leaves_candidates` — IP listed, then its IPAM row deleted →
    next sweep auto-resolves it, and `check_one` is monkeypatched to raise if it's ever re-queried
    (proves the de-scoped IP is resolved by reconcile, not re-checked). Asserts `counters["resolved"] >= 1`.
  - `test_run_sweep_resolves_when_list_disabled` — listed IP, then the (only) list disabled →
    reconcile resolves the open latch even with no enabled lists remaining.
  - `test_run_sweep_still_listed_candidate_stays_open` — still-candidate, still-listed IP on an enabled
    list stays open across sweeps (reconcile must not touch it; `first_listed_at` preserved).
- Existing `test_check_error_preserves_prior_state` continues to cover "transient DNS error must not
  auto-resolve".

## Verification

- `ruff check` + `black --check` on both files: clean.
- `mypy app/services/dnsbl/sweep.py` (host `.venv`): Success, no issues.
- `pytest tests/test_dnsbl.py`: **19 passed** on an isolated test DB.
  - NOTE: the shared `spatiumddi_test` DB was being truncated mid-run by other agents' concurrent
    `pytest` processes (5 host pytest procs seen), which caused non-deterministic `row is None` failures
    on unrelated first-sweep asserts. Re-running against a private DB
    (`TEST_DATABASE_URL=...spatiumddi_test_dnsblfix`, created + dropped) gave a clean 19/19. The code is
    correct; the earlier failures were pure test-DB contention, not logic bugs.
- Rebuilt/recreated dev `api`/`worker`/`beat` images (source is baked, no bind mount) so the container ran
  the new code.
