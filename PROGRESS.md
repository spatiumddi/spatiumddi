# Branch progress: `feat/security-wave`

Bundles three security/compliance roadmap issues that share the
auth/RBAC surface but are independently shippable. Order is
smallest-first so each can land standalone if context runs out.

## Resume protocol

1. `git checkout feat/security-wave`
2. Read this file from top to bottom.
3. The first unchecked box under "Checkpoints" is the next thing
   to do.
4. Each issue is its own commit (or commit chain) — never
   squash; that lets us resume mid-issue.
5. When an issue's "Done when" criteria are all checked, run
   `make ci`, then commit with the issue number in the title
   (`feat(security): #75 subnet classification tags`), then move
   to the next issue.
6. When all three issues land + `make ci` passes, merge to `main`
   (no force-push, no rebase) and delete this branch.

## Issues bundled

- **#75 subnet classification tags** (smallest, ~1 commit)
- **#74 API-token scopes** (medium, ~1–2 commits)
- **#69 2FA / MFA for local users** (largest, ~2–3 commits — model + login flow + UI)

---

## Issue #75 — Subnet classification tags

### Spec (from issue)

> First-class `pci_scope` / `hipaa_scope` / `internet_facing`
> boolean flags on subnet (versus free-form custom fields) + a
> Compliance dashboard filtered by them. Common ask in regulated
> verticals — auditors love being able to ask "show me every PCI
> subnet, who owns it, when was it last changed."

### Implementation plan

- **Data model.** Three new boolean columns on `subnet`:
  `pci_scope`, `hipaa_scope`, `internet_facing` — all default
  `false`, indexed individually for the dashboard filter. Keep
  them at the subnet level only (not block / space) — the ask is
  precise scope tagging, not inherited tagging. If operators
  want inheritance later it's an additive follow-up.
- **Schemas.** Add the three booleans to `SubnetCreate`,
  `SubnetUpdate`, `SubnetResponse`. No special validators — they
  default `false`.
- **Migration.** Single migration adds the three columns + the
  three indexes; chain off the current head.
- **Filters on `/api/v1/ipam/subnets`.** Three new query params
  matching the column names. AND-combined with existing filters.
- **Compliance dashboard.** New page at `/admin/compliance` (or
  `/admin/compliance-dashboard`) — three cards (PCI / HIPAA /
  Internet-facing), each listing its tagged subnets with
  network / VRF / owner / last-changed-at columns. Click-through
  to the subnet detail.
  - Server-side: piggyback on the existing list endpoint with
    the new filters; add `last_changed_at` derived from the
    `audit_log` `subnet.updated` events (LATERAL join, capped
    one per subnet).
  - Frontend: read-only page, no mutations, link out only.
- **Permissions.** No new permission needed — the dashboard
  reads `subnet`; existing `read` on `subnet` covers it.
- **Audit log.** Tag flips audit-log normally through the
  existing `subnet.updated` shape — no new audit action.

### Checkpoints

- [ ] Migration: add `pci_scope` / `hipaa_scope` / `internet_facing` to `subnet`
- [ ] Model: add the three columns to `app/models/ipam.py::Subnet`
- [ ] Schemas: add to `SubnetCreate` / `SubnetUpdate` / `SubnetResponse`
- [ ] Router: extend `GET /api/v1/ipam/subnets` filters
- [ ] Frontend: add the three checkboxes to Edit Subnet modal
- [ ] Frontend: add `/admin/compliance` page with three filtered tables
- [ ] Sidebar: add "Compliance" entry under Administration (or top-level Tools)
- [ ] `make ci` clean
- [ ] Single commit `feat(security): #75 subnet classification tags`

### Done when

All checkpoints checked + commit landed.

---

## Issue #74 — API-token scopes

### Spec (from issue)

> Per-token grants (read-only / IPAM-only / DNS-only / DHCP-only
> / agent-only). Today tokens are full-access JWT-equivalents.
> Storage: `APIToken.scopes` JSONB list checked at the auth
> layer alongside the existing permission gates.

### Implementation plan

- **Data model.** Add `APIToken.scopes` JSONB column (default
  `[]`, meaning "no scope restriction — fall through to the
  user's normal RBAC permissions"). Migration adds the column.
- **Scope vocabulary.** Define a closed enum of scopes:
  `read`, `ipam:write`, `dns:write`, `dhcp:write`, `agent`.
  - `read` — restricts to GETs only across everything.
  - `ipam:write` — restricts mutations to `/api/v1/ipam/*`,
    `/api/v1/vlans`, `/api/v1/vrfs`.
  - `dns:write` — restricts mutations to `/api/v1/dns/*`.
  - `dhcp:write` — restricts mutations to `/api/v1/dhcp/*`.
  - `agent` — restricts to the agent endpoints
    (`/api/v1/dns/agents/*`, `/api/v1/dhcp/agents/*`).
  - Empty `scopes` list = no restriction (fall through to RBAC).
  - Multiple scopes = union (any scope match passes).
- **Auth-layer enforcement.** Add a `_check_token_scope` helper
  that runs after the user resolves and BEFORE the RBAC check.
  Inputs: token row + request method + request path. If the
  token has any scopes set and none match the request, return
  401 with `error: "token scope insufficient"`. The existing
  permission gates still run on top.
- **Schemas.** `APITokenCreate` / `APITokenResponse` carry
  `scopes`; the create form lets the operator multi-select.
- **Frontend.** Token-create modal gains a scope multi-select
  (chips). Token-list table shows scopes as a chip column.
- **Audit log.** No new action; existing `api_token.created` /
  `api_token.revoked` already capture the row.

### Checkpoints

- [ ] Migration: add `scopes` JSONB column to `api_token`
- [ ] Model: add `scopes: list[str]` to `APIToken`
- [ ] Service: define `TOKEN_SCOPE_VOCABULARY` + `scope_matches_request`
- [ ] Auth: add `_check_token_scope` to the token-auth path
- [ ] Schemas: `scopes` on Create / Response
- [ ] Router: `POST /admin/api-tokens` accepts + persists scopes
- [ ] Frontend: scope multi-select in create modal + chip column in list
- [ ] Tests: scope-mismatch returns 401; empty-scopes falls through
- [ ] `make ci` clean
- [ ] Single commit `feat(security): #74 API-token scopes`

### Done when

All checkpoints checked + commit landed.

---

## Issue #69 — 2FA / MFA for local users

### Spec (from issue)

> TOTP enrolment via `pyotp` with recovery codes.
> `User.totp_secret` (Fernet-encrypted) + `User.mfa_enabled`.
> Login flow: password → TOTP prompt → JWT. Optional WebAuthn /
> FIDO2 in a follow-up.

### Implementation plan

- **Dependencies.** Add `pyotp` to `backend/pyproject.toml` +
  pin in `backend/requirements.txt`. No frontend dep — `qrcode`
  is generated server-side as an SVG data URL OR the otpauth URI
  is shown for manual entry.
- **Data model.** Three new columns on `user`:
  - `totp_secret_encrypted: bytes | None` — Fernet-encrypted
    base32 TOTP seed; null until enrolment commits.
  - `mfa_enabled: bool = false` — flips true when the user
    completes enrolment by submitting a valid first code.
  - `recovery_codes_encrypted: bytes | None` — Fernet-encrypted
    JSON list of 10 single-use recovery codes; null until
    enrolment.
- **Migration.** Adds the three columns; `mfa_enabled` defaults
  `false` so existing users are unaffected.
- **Enrolment endpoints** (under `/api/v1/auth/mfa`):
  - `POST /enroll/begin` — generates a fresh secret + recovery
    codes, returns `{secret, otpauth_uri, recovery_codes}`.
    Stores the candidate secret on the user row but does NOT
    flip `mfa_enabled` yet.
  - `POST /enroll/verify` — body `{code: str}`. Validates
    against the candidate secret with a ±1 step window. On
    success: persists the secret + encrypted recovery codes,
    flips `mfa_enabled = true`, audit-logs `mfa.enabled`.
  - `POST /disable` — body `{password: str, code: str}`. Both
    must validate. Clears all three columns, audit-logs
    `mfa.disabled`.
  - `POST /recovery-codes/regenerate` — body
    `{password: str, code: str}`. Returns a fresh set of 10
    codes; replaces the encrypted blob.
- **Login flow change.** `POST /api/v1/auth/login`:
  - If user has `mfa_enabled = false`: existing behaviour
    (returns JWT immediately).
  - If user has `mfa_enabled = true`: returns `200 {mfa_required:
    true, mfa_token: <short-lived JWT>}` instead of the access
    token. The `mfa_token` is a 5-minute JWT with claim
    `purpose: "mfa"` — useless for any other endpoint.
- **`POST /api/v1/auth/login/mfa`** — body
  `{mfa_token: str, code: str}` OR `{mfa_token: str,
  recovery_code: str}`. Validates the code (with ±1 step window
  for TOTP, or single-use consume for recovery). On success
  returns the real access token + refresh token. On failure,
  count attempts; lock after 5 failures via the existing
  account-lockout machinery (already-shipped).
- **Recovery-code consumption.** Recovery codes are stored
  hashed (sha256) so a leaked DB doesn't expose them. Consume
  by deleting the matching hash from the list and persisting;
  if the list empties, force-regenerate on next successful
  login.
- **Frontend.**
  - Settings → Account → "Two-factor authentication" panel:
    Enable button → flow: show QR + manual entry secret + a
    "Enter code from your app" input + recovery codes panel
    that operator must acknowledge before completing.
  - Login page: when API returns `mfa_required`, swap to a TOTP
    input with an "Use a recovery code instead" toggle.
  - Disable / regenerate flows behind password-confirm modals.
- **Permissions / superadmin.** No special bypass — superadmin
  can still enable / disable their own MFA but cannot bypass
  another user's TOTP. Admins disabling another user's MFA is a
  separate audit-logged action: `POST /admin/users/{id}/mfa/
  reset` (audit-logged as `mfa.reset_by_admin`, requires
  password reauth) — useful when an operator loses their
  device. Defer to follow-up if scope creeps; not in v1.
- **Audit log.** New actions: `mfa.enabled`, `mfa.disabled`,
  `mfa.recovery_used`, `mfa.recovery_regenerated`,
  `mfa.login_succeeded`, `mfa.login_failed`.

### Checkpoints

- [ ] Add `pyotp` dependency
- [ ] Migration: add `totp_secret_encrypted` / `mfa_enabled` / `recovery_codes_encrypted` to `user`
- [ ] Model: extend `User` with the three columns
- [ ] Service: `app/services/mfa.py` — generate secret, generate codes, hash codes, verify TOTP, consume recovery code
- [ ] Router: `app/api/v1/auth/mfa.py` — enrol begin / verify / disable / regenerate
- [ ] Auth: split login into password-step + mfa-step; mint short-lived `purpose: mfa` token
- [ ] Frontend: Settings → Account MFA panel
- [ ] Frontend: login page handles `mfa_required` redirect to TOTP input
- [ ] Tests: enrol → verify → login flow; recovery-code consume; disable
- [ ] `make ci` clean
- [ ] Commit chain: `feat(security): #69 MFA — model + service`, `feat(security): #69 MFA — login flow`, `feat(security): #69 MFA — frontend`

### Done when

All checkpoints checked + commit chain landed.

---

## Final close-out

When all three issues land:

- [ ] CHANGELOG.md draft entry for the security wave
- [ ] CLAUDE.md "Current state" updated
- [ ] docs/SHIPPED.md entries for #69 / #74 / #75 (move from CLAUDE.md pending list)
- [ ] docs/PERMISSIONS.md updated if MFA changes the auth surface
- [ ] Merge `feat/security-wave` to `main` (no force-push, no rebase)
- [ ] Close issues #69 / #74 / #75 with release link
