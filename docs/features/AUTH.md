# Auth & Permissions

> **Implementation status (Unreleased, post-`2026.04.16-2`):** Local auth
> (JWT + rotating refresh token), five external identity provider types
> configured at runtime from the admin UI (LDAP, OIDC, SAML, RADIUS,
> TACACS+), failover to backup servers on LDAP / RADIUS / TACACS+,
> group-based RBAC enforced on every API router, and five builtin roles
> seeded at startup. Deferred: API tokens, forced password-change policy,
> SCIM provisioning.

## Overview

SpatiumDDI's auth stack has three layers:

1. **Authentication** — verifying who you are. Local password login, plus
   any mix of enabled external providers. All external providers funnel
   through a unified `sync_external_user()` that creates / updates the
   local `User` and replaces their group membership from the provider's
   group mappings. A login with **no** group mapping match is rejected.
2. **Session** — the login handler issues a short-lived JWT access token
   (15 min default) and a long-lived rotating refresh token stored as a
   bcrypt-hashed row in `user_session`.
3. **Authorization** — every API router uses a permission helper
   (`require_permission` / `require_resource_permission` /
   `require_any_permission`) to check that the caller's groups carry a
   role with a matching permission entry. See [PERMISSIONS.md](../PERMISSIONS.md)
   for the grammar.

```
┌────────────┐  1. login          ┌──────────────────────┐
│  browser   │──────────────────▶ │   /auth/login        │
└────────────┘                    │                      │
      ▲                           │  local password ──▶  │  verify_password
      │                           │  LDAP / RADIUS /     │──▶ authenticate_*
      │                           │    TACACS+           │
      │ 2. access + refresh       │                      │
      └───────────────────────────│  OIDC / SAML: 302    │──▶ redirect to IdP
                                   └──────────────────────┘
                                            │
                                            ▼
                                   sync_external_user
                                   → upsert User
                                   → replace group membership
                                   → reject if no mapping match
```

## Local auth

- `POST /auth/login` accepts JSON `{username, password}`. Returns
  `{access_token, refresh_token, force_password_change}`.
- Passwords are `bcrypt` hashed. New users default `force_password_change=True`
  so the UI redirects them to `/change-password` before they can do anything
  else.
- Access token expires in 15 min. Refresh token expires in 30 days and
  rotates on every use.
- `admin` / `admin` is seeded on first start with `force_password_change=True`.
  Reset the password from the CLI with the one-liner in the README.

## External identity providers

All providers are configured at runtime from `/admin/auth-providers`. The
`AuthProvider` row carries:

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | |
| `name` | str | Display name shown on the login page. |
| `type` | str | `"ldap"`, `"oidc"`, `"saml"`, `"radius"`, or `"tacacs"`. |
| `is_enabled` | bool | Disabled providers are skipped at login. |
| `priority` | int | Lower = tried first (password-flow fallthrough). |
| `auto_create_users` | bool | Create missing users at first login. |
| `auto_update_users` | bool | Update email / display name on every login. |
| `config` | jsonb | Public config (see per-provider fields below). |
| `secrets_encrypted` | bytes | Fernet-encrypted secrets dict. |

**Secrets at rest.** Everything in `secrets_encrypted` is wrapped with
Fernet. The encryption key is derived from `settings.credential_encryption_key`
(explicit) or `SHA-256(secret_key)` (fallback), so a fresh deploy that only
sets `SECRET_KEY` still works. See `backend/app/core/crypto.py`.

**Group mappings.** Each provider owns a list of `AuthGroupMapping` rows
(`external_group` → `internal_group_id`). The unified sync in
`backend/app/core/auth/user_sync.py` resolves the user's provider-reported
groups case-insensitively and **rejects the login if no mapping matches**.

### LDAP

Driver: `backend/app/core/auth/ldap.py` (`ldap3`).

Flow per login:
1. Bind as the service account (`config.bind_dn` + `secrets.bind_password`).
2. Search under `config.user_base_dn` using
   `config.user_filter` (must contain the `{username}` placeholder).
3. Bind as the returned DN + the user's password to verify credentials.
4. Extract groups from the `memberOf` attribute (or `config.attr_member_of`).

Key config fields:

| Field | Default | Notes |
|---|---|---|
| `host` | — | FQDN or IP of the primary directory. |
| `port` | 636 (SSL) / 389 | |
| `backup_hosts` | `[]` | List of `"host"` or `"host:port"` entries. Bracketed IPv6 (`[::1]:389`) allowed. |
| `use_ssl` | `true` | `ldaps://` on port 636. |
| `start_tls` | `false` | Upgrade a plain connection after bind. |
| `bind_dn` | — | Service account DN. |
| `user_base_dn` | — | Search base for user lookups. |
| `user_filter` | `(&(objectClass=user)(sAMAccountName={username}))` | Must contain `{username}`. |
| `attr_username` / `attr_email` / `attr_display_name` | `sAMAccountName` / `mail` / `displayName` | |
| `attr_member_of` | `memberOf` | |
| `tls_ca_cert_file` | — | Container path to a custom CA. |

### OIDC

Driver: `backend/app/core/auth/oidc.py` (`authlib`).

Flow:
1. Login page calls `GET /auth/{provider_id}/authorize` — 302 redirects to
   the IdP's authorization endpoint with a signed-JWT state+nonce cookie
   (`oidc_flow`).
2. IdP redirects back to `/auth/{provider_id}/callback` with an authorization
   code.
3. Backend validates the state+nonce cookie, exchanges the code, validates
   the ID token via `authlib.jose` (discovery + JWKS cached), and redirects
   to `/auth/callback#token=…&refresh=…` on the frontend.
4. Frontend's `LoginCallbackPage` consumes tokens from the URL hash and
   routes into the app.

Key config fields:

| Field | Notes |
|---|---|
| `discovery_url` | e.g. `https://accounts.google.com/.well-known/openid-configuration` |
| `client_id` | From the IdP. |
| `scopes` | Array. Defaults to `["openid", "profile", "email", "groups"]`. |
| `claim_username` / `claim_email` / `claim_display_name` / `claim_groups` | Claim names to pull from the ID token. |

Secrets: `client_secret`.

### SAML

Driver: `backend/app/core/auth/saml.py` (`python3-saml`).

Flow:
1. Login page calls `GET /auth/{provider_id}/authorize` — builds an
   HTTP-Redirect `AuthnRequest` and 302s to the IdP's SSO URL.
2. IdP POSTs the `SAMLResponse` back to `/auth/{provider_id}/callback`
   (ACS endpoint).
3. Backend consumes the assertion and redirects to `/auth/callback#token=…`.
4. `GET /auth/{provider_id}/metadata` returns SP metadata XML so admins
   can register SpatiumDDI at the IdP.

Key config fields:

| Field | Notes |
|---|---|
| `idp_metadata_url` | Optional — backend can pull IdP details automatically. |
| `idp_entity_id` / `idp_sso_url` / `idp_slo_url` | Set these when you don't provide a metadata URL. |
| `idp_x509_cert` | Base64 or PEM — used to verify the assertion. |
| `sp_entity_id` | Defaults to the app URL. |
| `attr_username` / `attr_email` / `attr_display_name` / `attr_groups` | SAML attribute names. |

Secrets: `sp_private_key` (PEM, optional — only needed for signed requests).

### RADIUS

Driver: `backend/app/core/auth/radius.py` (`pyrad`).

Flow: one UDP round-trip per login. The driver sends `Access-Request` with
`User-Name` + `User-Password` (pyrad encrypts). `Access-Accept` with group
info lifted from the reply attribute (`Filter-Id` by default) is a success;
`Access-Reject` is bad credentials; timeout / MAC mismatch raises
`RADIUSServiceError` so the caller can fall through to the next provider.

Key config fields:

| Field | Default | Notes |
|---|---|---|
| `server` | — | Primary RADIUS host. |
| `port` | `1812` | Auth port. |
| `backup_servers` | `[]` | List of `"host"` or `"host:port"` entries. |
| `timeout` | `5` | Seconds per attempt. |
| `retries` | `3` | Per server, before failing over. |
| `nas_identifier` | `"spatiumddi"` | Stamped into every Access-Request. |
| `attr_groups` | `"Filter-Id"` | Attribute that carries group info. |
| `dictionary_path` | — | Optional extra RADIUS dictionary file path. |

Secrets: `secret` (shared secret — bytes).

### TACACS+

Driver: `backend/app/core/auth/tacacs.py` (`tacacs_plus`).

Flow:
1. `client.authenticate(username, password)` over TCP; `valid=True` is a
   success.
2. `client.authorize(username)` round-trip pulls AV pairs. `priv-lvl`
   numeric values are surfaced as `priv-lvl:N` so admins can map e.g.
   `priv-lvl:15` → `Admins` in the group-mapping UI.

Key config fields:

| Field | Default | Notes |
|---|---|---|
| `server` | — | Primary TACACS+ host. |
| `port` | `49` | |
| `backup_servers` | `[]` | List of `"host"` or `"host:port"` entries. |
| `timeout` | `5` | Seconds. |
| `attr_groups` | `"priv-lvl"` | AV pair used for group mapping. |

Secrets: `secret`.

## Backup server failover (LDAP / RADIUS / TACACS+)

Each password provider accepts an optional list of backup hosts via
`config.backup_hosts` (LDAP) or `config.backup_servers` (RADIUS / TACACS+).
Entries are strings of the form `"host"` or `"host:port"`; bracketed IPv6
literals (`[::1]:389`) are supported. The admin UI exposes a "Backup hosts /
servers" textarea (one entry per line).

**LDAP failover** uses `ldap3.ServerPool(pool_strategy=FIRST, active=True,
exhaust=True)`:
- `active=True` — the pool checks reachability before issuing operations.
- `exhaust=True` — once a host fails it is removed for the lifetime of the
  pool, so subsequent binds in the same Connection don't keep retrying
  a dead host.

**RADIUS + TACACS+ failover** iterates primary → backups manually:
- A **definitive auth answer** (Access-Accept / Access-Reject,
  `valid=True` / `valid=False`) stops iteration. The first server that
  gives you an answer wins — failing over on a rejection is wrong.
- **Network / timeout / protocol errors** (pyrad's `MAC mismatch` on a
  bad shared secret, `socket.timeout`, `ConnectionError`, etc.) fail over
  to the next target.
- All backups share the primary's shared secret, `nas_identifier`,
  `timeout`, and dictionary.

## Test-connection probe

Every provider type exposes `backend/app/core/auth/{type}.test_connection(provider)`
which returns `{ok, message, details}` without raising. The admin UI's
"Test" button hits `POST /api/v1/auth-providers/{id}/test` to run the
probe. For LDAP + OIDC + SAML the probe does a real service bind /
discovery fetch / metadata fetch; for RADIUS + TACACS+ it sends a stub
Access-Request — a `Reject` for the bogus credentials still proves the
server is reachable and the shared secret is correct (the MAC would not
validate otherwise).

## Provider priority

When a password-grant login arrives at `/auth/login`:

1. Local credentials are tried first. If the `User` exists and has a
   hashed password, that wins (or loses) on its own.
2. Otherwise the handler iterates every enabled provider whose type is in
   `PASSWORD_PROVIDER_TYPES = ("ldap", "radius", "tacacs")` in
   `(priority, name)` order. Each `authenticate_*()` call runs in a worker
   thread (`asyncio.to_thread`) with a 20 s timeout.
3. The first definitive answer wins. Service errors (unreachable /
   misconfigured) are logged and fall through to the next provider.
4. If nothing accepted, a single `401` + a `login` audit row are emitted.

OIDC and SAML are redirect flows — they are never tried at `/auth/login`.
The login page lists every enabled OIDC/SAML provider as a "Sign in with
…" button that kicks off the redirect flow directly.

## Permission enforcement

See [PERMISSIONS.md](../PERMISSIONS.md) for the full grammar + built-in
roles. In short:

- Every router has a `Depends(require_resource_permission(<type>))`
  dependency that maps HTTP method → action (`GET`=read,
  `POST`/`PUT`/`PATCH`=write, `DELETE`=delete).
- Handlers doing resource-scoped checks also call
  `user_has_permission(user, action, resource_type, resource_id)` before
  mutating.
- `Superadmin` short-circuits every check without writing a denial
  audit row.
- `Inactive` users are refused regardless of permissions.

## Audit

Every login attempt — success, failure, LDAP service error — writes an
`AuditLog` row with `action="login"`, `result="success"|"failure"`, the
auth source (`local`, `ldap`, `oidc`, `saml`, `radius`, `tacacs`), source
IP, user agent, and for failures a `new_value.reason` string. Failed
logins for unknown usernames still get a row (`user_id=NULL`,
`user_display_name=<attempted name>`) so brute-force attempts are visible
in the audit viewer.

## API tokens

Long-lived bearer credentials for scripts, CI pipelines, and
automation. A token is equivalent to its owning user for permission
purposes — no separate RBAC surface yet. Tokens are indistinguishable
from JWTs on the wire (both use `Authorization: Bearer …`); the auth
middleware peeks at the prefix to pick the validation path.

**Wire format.** Raw tokens start with `sddi_` followed by 40
url-safe base64 characters. Operators typically see only the first
10 characters (`sddi_AbCdE`) in the UI as an identifier — this is
the `APIToken.prefix` column, sufficient to pick a token out of a
list without leaking entropy to an observer.

**At rest.** Only the SHA-256 hash is stored (`APIToken.token_hash`).
The raw value is returned exactly once from `POST /api/v1/api-tokens`
and never again — losing it means creating a new token.

**Validation path.** `app/api/deps.py:get_current_user` checks for the
`sddi_` prefix first; if present it hashes the bearer, looks the row
up by hash, enforces `is_active` and `expires_at`, then loads the
owning user and bumps `last_used_at`. JWTs take the original path. A
missing / wrong / revoked token returns the same generic 401 as an
invalid JWT to avoid confirming token existence to an attacker.

**Lifecycle.**
- Create via the Admin → API Tokens UI or `POST /api/v1/api-tokens`
  (JSON: `{name, description?, expires_in_days?}`). The create
  response contains the raw `token` field **once** — the UI forces a
  "copy now" dialog before it disappears.
- List via `GET /api/v1/api-tokens` (your tokens only; superadmins
  see everyone's).
- Revoke softly via `PATCH /api/v1/api-tokens/{id}` with
  `{is_active: false}` — the row stays so `last_used_at` is still
  visible for incident forensics.
- Delete hard via `DELETE /api/v1/api-tokens/{id}` — same outward
  behaviour (401 on next use), audit row written on both paths.

**TTL.** The UI defaults to 90 days and flags "Never" in amber so
operators have to opt into long-lived bearers. The backend accepts
both `expires_in_days` (UX-friendly) and `expires_at` (ISO timestamp,
for automation). Expired tokens 401 with a distinct detail message
so clients can detect "refresh me" vs "reconfigure me".

## Open items

- **Forced-password policy** — `force_password_change=True` is honoured
  on first login; a broader policy (age, rotation, history) is Phase 4.
- **SCIM provisioning** — not planned for Phase 1.
- **Per-provider signing key rotation** — manual today; automate in a
  later wave.
- **Global-scope / service-account tokens** — the `APIToken.scope`
  column supports `global`, but validation rejects them today pending
  a proper service-account user model. Token-only `allowed_paths` and
  per-token permission overrides exist on the model but aren't
  exposed on create yet.

## Rules & constraints

Server-side validations that reject requests with a human-readable
error. Clients should surface the response `detail` to the operator
rather than swallowing the failure. Permission-related rejections
(`403 forbidden`) are covered separately in `docs/PERMISSIONS.md`.

### Login & session

- **Invalid credentials.** Local password verification failure returns
  `401`. Enforced at `backend/app/api/v1/auth/router.py:305`.
- **Account disabled.** Login is refused with `403` when
  `user.is_active` is false — regardless of auth source.
  `backend/app/api/v1/auth/router.py:313`.
- **Empty external ID from IdP.** External login (LDAP / OIDC / SAML /
  RADIUS / TACACS+) with no stable external identifier in the IdP
  response is rejected — we won't create a `User` row we can't
  correlate later. `backend/app/core/auth/user_sync.py:78`.
- **No group-mapping match.** An external user whose IdP group claim
  doesn't match any `AuthGroupMapping` row for that provider is
  rejected with `401`, even if the IdP authenticated them. This is
  deliberately strict — there is no implicit "default group" fallback.
  `backend/app/core/auth/user_sync.py:83`.
- **Auto-create disabled.** First external login for a new subject is
  refused with `401` if `provider.auto_create_users=False`. An
  administrator must create the `User` row manually.
  `backend/app/core/auth/user_sync.py:112`.
- **Username collision across auth sources.** An external user whose
  `external_id` is new but whose preferred username already belongs to
  a user on a different `auth_source` is rejected to prevent silently
  hijacking an existing account.
  `backend/app/core/auth/user_sync.py:102`.
- **Refresh token invalid or expired.** Refresh is rejected with `401`
  when the token is not in the sessions table, has been revoked, or
  has passed `expires_at`. `backend/app/api/v1/auth/router.py:353`.
- **User deactivated mid-session.** A refresh request from a disabled
  user returns `401` even if the refresh token itself is still valid
  — deactivating a user revokes their session on the next refresh.
  `backend/app/api/v1/auth/router.py:359`.

### Password management

- **Password length floor.** New / changed passwords must be ≥ 8
  characters. Pydantic validator at
  `backend/app/api/v1/auth/router.py:91` — `422`.
- **Current password required.** Change-password endpoints verify
  `current_password` before accepting the new value; a mismatch
  returns `400` rather than silently succeeding.
  `backend/app/api/v1/auth/router.py:396`.

### Auth providers

- **Duplicate provider name.** Two `AuthProvider` rows with the same
  `name` are rejected with `409`, regardless of type.
  `backend/app/api/v1/auth_providers/router.py:189`.
- **Invalid provider type.** `type` must be one of the values in
  `PROVIDER_TYPES` (`local`, `ldap`, `oidc`, `saml`, `radius`,
  `tacacs_plus`). `backend/app/api/v1/auth_providers/router.py:63`.
- **Group mapping target must exist.** `AuthGroupMapping` rows reject
  `internal_group_id` values that don't resolve to an existing
  `Group`. `backend/app/api/v1/auth_providers/router.py:534`.
