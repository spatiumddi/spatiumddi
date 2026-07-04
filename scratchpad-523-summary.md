# Issue #523 — ipam: API symmetry cleanups — implementation summary

Branch: `issues-523-524-527-528-530`. No migration, no feature module (as specified).
All four sub-items implemented, tested, and host-lint-clean (ruff + black).

## Files changed

### Backend source
- `backend/app/core/permissions.py`
- `backend/app/api/v1/ipam/router.py`
- `backend/app/services/ipam_io/export.py`

### Tests
- `backend/tests/test_ipam_io.py` (extended)
- `backend/tests/test_ipam_space_block_token_scope.py` (extended)
- `backend/tests/test_ipam_next_ip_parity.py` (new)

---

## Sub-item 1 — Subnet-token list-vs-get inconsistency

The by-id reads gate via `_enforce_subnet_token_scope`, but `GET /subnets`,
`/blocks`, `/spaces` returned every row. Now filtered to the subnet(s) a
subnet-scoped token (#374) is bound to, plus their parent block/space.

- `backend/app/core/permissions.py`
  - Added `token_scoped_resource_ids(user, resource_type) -> set[str] | None`
    (the list-endpoint companion to `token_scope_allows`). Mirrors the same
    grant-matching semantics: returns `None` for sessions / plain tokens / a
    wildcard grant on the type (no filtering), else the bound instance ids
    (empty set = scoped only to other resource types). Added to `__all__`.
- `backend/app/api/v1/ipam/router.py`
  - Imported `token_scoped_resource_ids`.
  - Added router-local `_token_subnet_scope_uuids(user)` (right after
    `_enforce_subnet_token_scope`) — converts the string ids to UUIDs, `None`
    = no filter.
  - `list_spaces` (~`GET /spaces`): when scoped, `WHERE IPSpace.id IN (SELECT
    Subnet.space_id WHERE Subnet.id IN scoped)`.
  - `list_blocks` (~`GET /blocks`): when scoped, `WHERE IPBlock.id IN (SELECT
    Subnet.block_id WHERE Subnet.id IN scoped AND block_id IS NOT NULL)`.
  - `list_subnets` (~`GET /subnets`): when scoped, `WHERE Subnet.id IN scoped`.
  - Unscoped callers (sessions / plain tokens) are unchanged — the helper
    returns `None` and no filter is applied.

Note (verified): a token scoped to a *different* resource type (e.g.
`dns_zone`) never reaches these handlers at all — the router-level
`require_any_resource_or_scoped` coarse gate already 403s it. So the empty-set
branch is defensive; the reachable narrowing case is a genuine subnet token.

## Sub-item 2 — `preview_next_ip` ignores address-set delegation

`preview_next_ip` (`GET /subnets/{id}/next-ip-preview`) now computes the same
`allowed_ranges` the commit path (`allocate_next_ip`) uses:
`subnet_writable = user_has_permission(..., "write", "subnet", id)`,
`set_ranges = _load_writable_set_ranges(...)`,
`allowed_ranges = None if subnet_writable else set_ranges`, and passes it to
`_pick_next_available_ip`. Preview and commit now agree on the candidate.
(Preview stays read-only — it does not 403; an address-set-less caller simply
sees `address=None`, which matches the commit path's 403.)

## Sub-item 3 — `allocate_next_ip` missing guard + schema parity

- `NextIPRequest` schema gained `extra_zone_ids: list[str] = []` and
  `decom_date: date | None = None` (parity with `IPAddressCreate`).
- `allocate_next_ip` (`POST /subnets/{id}/next`):
  - Added the `_check_public_facing_warnings` guard (issue #25), placed after
    the candidate is picked + write-permission check (the check keys off the
    concrete chosen address). Same `force` gate + `_collision_http_exc` 409
    shape as `create_address`.
  - Threads `extra_zone_ids=body.extra_zone_ids` and `decom_date=body.decom_date`
    onto the new `IPAddress`. `extra_zone_ids` flows into `_sync_dns_record`'s
    split-horizon fanout exactly as in `create_address`; the audit `new_value`
    (a `body.model_dump(...)`) now includes both fields automatically.

## Sub-item 4 — CSV/XLSX formula-injection hardening

- `backend/app/services/ipam_io/export.py`
  - Added `_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")` and
    `_sanitize_cell(value)` — prefixes a leading single quote when a *string*
    cell starts with a trigger; numbers / None / bool pass through unchanged.
  - Applied to every user-controlled cell in both paths:
    - CSV: `_addresses_to_csv` (address + custom-field cells) and `_to_csv`
      (subnet + custom-field cells).
    - XLSX: `_to_xlsx` subnets sheet (base + custom-field cells), blocks sheet
      (network/name/description), addresses sheet (base + custom-field cells).
  - Headers left untouched (developer-controlled).

---

## Tests added

`backend/tests/test_ipam_io.py`
- `test_sanitize_cell_prefixes_dangerous_values` — unit test of every trigger +
  safe/non-string pass-through.
- `test_export_csv_sanitizes_subnet_cells` — subnet name/description/custom
  field prefixed in CSV.
- `test_export_csv_sanitizes_address_cells` — addresses-only CSV hostname/
  description/custom field prefixed.
- `test_export_xlsx_sanitizes_cells` — subnets, blocks, and addresses sheets
  all sanitized (reads back with openpyxl).

`backend/tests/test_ipam_space_block_token_scope.py`
- `test_unscoped_session_lists_everything` — regression guard (no-op for
  sessions).
- `test_subnet_scoped_token_lists_only_its_subtree` — subnet token sees only
  its subnet + parent block + parent space; the sibling subtree is invisible.
- `test_dns_zone_scoped_token_denied_on_ipam_lists` — documents that a
  dns_zone-only token is 403'd by the coarse gate before reaching the lists.

`backend/tests/test_ipam_next_ip_parity.py` (new)
- `test_next_ip_threads_extra_zone_ids_and_decom_date` — both fields persist on
  the created row via `POST /next`.
- `test_next_ip_public_facing_guard` — private IP into a public-facing zone →
  409 with `requires_confirmation` + `public_facing_private_ip` warning;
  `force=True` allocates.
- `test_preview_and_commit_agree_on_candidate` — preview address == committed
  address.

## Verification

- `ruff check` + `black --check` clean on all six changed files (host tooling).
- Ran in the dev api container against Postgres (files `docker cp`'d in, image
  is baked not mounted):
  - `test_ipam_next_ip_parity.py` + `test_ipam_space_block_token_scope.py` +
    `test_ipam_io.py` → **21 passed**.
  - Plus `test_api_token_resource_grants.py` + `test_ipam_spaces.py` as
    regression → all green (33 passed in the combined run).

## Not verified / notes
- mypy not run (per instructions — Docker mypy skipped; code kept
  type-consistent with surroundings, e.g. `set[uuid.UUID] | None` return).
- Did not run the full suite — only the IPAM-relevant files above.
- No git commit / push performed (per instructions).
