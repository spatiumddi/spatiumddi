# REST API

> **Implementation status (shipped):** Every UI action is backed by a
> REST endpoint under the `/api/v1` prefix (non-negotiable #1). The
> surface is a FastAPI app with bearer-token auth (session JWTs +
> `sddi_*` API tokens with coarse scopes and per-resource grants),
> server-side RBAC enforcement, feature-module gating that returns
> `404` for disabled modules, and an MCP (Model Context Protocol)
> JSON-RPC endpoint for the Operator Copilot. Interactive Swagger /
> ReDoc are served from the running API. List endpoints come in two
> shapes — a bare JSON array (the core IPAM/DNS/DHCP routers) and an
> `{items, total, limit, offset}` envelope (the network-modeling
> routers); both are documented below.

This document covers the cross-cutting API conventions. For
authentication providers (LDAP / OIDC / SAML / RADIUS / TACACS+), MFA,
sessions and the token model in depth see
[`features/AUTH.md`](features/AUTH.md); for the permission grammar and
built-in roles see [`PERMISSIONS.md`](PERMISSIONS.md).

---

## 1. Base URL & Versioning

All application endpoints are mounted under a single version prefix:

```
/api/v1
```

The version router family is assembled in
[`backend/app/api/v1/router.py`](../backend/app/api/v1/router.py) and
mounted by [`backend/app/main.py`](../backend/app/main.py):

```python
app.include_router(api_v1_router, prefix="/api/v1")
```

A handful of routes live **outside** `/api/v1` because they are
infrastructure rather than application surface:

| Path | Purpose | Auth |
|---|---|---|
| `/health/live` | Liveness probe — 200 if the process is up | none |
| `/health/ready` | Readiness probe — DB connectivity + **schema-at-head** + Redis | none |
| `/health/startup` | Same logic as `/health/ready`, for slow-start k8s containers | none |
| `/health/platform` | Per-component rollup (db / redis / celery workers / beat) for the dashboard | none |
| `/.well-known/acme-challenge/{token}` | HTTP-01 ACME challenge (issue #438) | none |
| `/metrics` | Prometheus exposition (when `PROMETHEUS_METRICS_ENABLED`) | none |

`/api/v1` is the only API version. When the surface changes shape in a
non-additive way a `/api/v2` prefix would be introduced alongside it;
today there is exactly one version. New routers are inserted into
`router.py` in **alphabetical order** so the generated docs list
sections A → Z.

---

## 2. Interactive Documentation

The FastAPI app serves the standard interactive docs and the raw
OpenAPI schema (configured in `create_app()` in
[`backend/app/main.py`](../backend/app/main.py)):

| Route | What it serves |
|---|---|
| `/api/docs` | Swagger UI |
| `/api/redoc` | ReDoc |
| `/api/openapi.json` | The OpenAPI 3.x schema document |

Note the `/api/` prefix on the docs routes — they are **not** under
`/api/v1`. The schema is auto-generated from the Pydantic request /
response models on every route, so it always reflects the running
build. Each router carries a `tags=[...]` label (alphabetised in
`router.py`) so the Swagger / ReDoc sidebar groups endpoints by
feature area.

---

## 3. Authentication

The API authenticates via the HTTP **`Authorization: Bearer <token>`**
header. Two credential kinds are accepted on the same header, resolved
in [`backend/app/api/deps.py`](../backend/app/api/deps.py)
(`get_current_user`):

1. **Session JWT** — a short-lived access token issued by
   `POST /api/v1/auth/login`.
2. **API token** — a long-lived `sddi_*` token issued by
   `POST /api/v1/api-tokens`, for scripts and machine clients.

Tokens that start with the `sddi_` prefix are routed straight to the
API-token validator; everything else is JWT-decoded. A missing or
invalid credential returns `401`; an authenticated-but-disabled user
returns `403`.

### 3.1 Login

`POST /api/v1/auth/login`

```json
{ "username": "admin", "password": "admin" }
```

On success (and no MFA) the response is:

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "9c2f...e1",
  "token_type": "bearer",
  "force_password_change": true,
  "mfa_required": false,
  "mfa_token": null
}
```

When the local user has TOTP enabled, login instead returns a
challenge with **no tokens**, and the caller must complete the second
factor at `POST /api/v1/auth/login/mfa`:

```json
{
  "access_token": null,
  "refresh_token": null,
  "token_type": "bearer",
  "force_password_change": false,
  "mfa_required": true,
  "mfa_token": "eyJ...type=mfa...(5-minute TTL)"
}
```

The access token's default lifetime is **15 minutes**
(`ACCESS_TOKEN_EXPIRE_MINUTES`); the refresh token's is **7 days**
(`REFRESH_TOKEN_EXPIRE_DAYS`). The default credentials on a fresh
install are `admin` / `admin` with `force_password_change=true` — the
API bars a forced-change account from every endpoint except the
password-recovery allowlist (`/auth/change-password`, `/auth/logout`,
`/auth/me`, `/auth/password-policy`) until the password is rotated.

### 3.2 Refresh & rotation

`POST /api/v1/auth/refresh`

```json
{ "refresh_token": "9c2f...e1" }
```

returns a fresh `{ access_token, refresh_token, token_type,
force_password_change }`. Refresh is **rotating**: the presented
refresh token's session is revoked and a new session (and new refresh
token) is issued. Each access token carries the issuing session's UUID
as its `jti` claim, so revoking the session (`POST /api/v1/auth/logout`
or a superadmin force-logout via the sessions surface) invalidates
every access token minted from it on the next request.

### 3.3 API tokens

API tokens are minted at `POST /api/v1/api-tokens`. The raw token is
returned **exactly once** in the create response (the DB stores only a
sha256 hash plus a short display prefix). They authenticate on the same
`Authorization: Bearer` header as session JWTs.

Two narrowing mechanisms apply, both enforced in `deps.py`
*before* RBAC:

- **Scopes** (`scopes: [...]`) — a coarse, closed vocabulary defined in
  [`backend/app/services/api_token_scopes.py`](../backend/app/services/api_token_scopes.py):
  `read`, `ipam:write`, `dns:write`, `dhcp:write`, `agent`. An empty
  list means no scope restriction. A non-empty list is checked against
  the request method + path; a request matching none of the token's
  scopes returns `401 "Token scope insufficient for this request"`.
  `read` permits only safe methods (`GET` / `HEAD` / `OPTIONS`) — plus
  a JSON-RPC `POST` to the read-only MCP endpoint.
- **Per-resource grants** (`resource_grants: [...]`) — bind a token to a
  specific `subnet` or `dns_zone` instance with an
  `{action, resource_type, resource_id}` triple (issue #374). At
  mint time the issuer must already hold the grant ("a token cannot
  grant more than its creator"), and the bound resource must exist.

A token can never exceed its owner's RBAC: the scope / grant check is
an *additional* gate, applied on top of the owning user's permissions.

See [`features/AUTH.md`](features/AUTH.md) for the full auth provider,
MFA, and session model.

---

## 4. Authorization (RBAC)

Authorization is enforced **server-side, independently of the UI**
(non-negotiable #3). Permissions are `{action, resource_type,
resource_id?}` triples with wildcard support; the helpers
(`require_permission` / `require_any_permission` /
`require_resource_permission` and friends) live in
`backend/app/core/permissions.py`. Most routers apply the gate at the
router-include level — e.g. the IPAM router carries a
`require_any_resource_or_scoped(...)` dependency over all of its IPAM
resource types — so every handler under the prefix is gated uniformly.
Superadmins (the legacy `User.is_superadmin` column **or** a group →
`{*, *}` wildcard role grant) bypass these checks.

A caller can introspect its own effective grants at
`GET /api/v1/auth/me/permissions`. The full grammar, built-in roles
(Superadmin / Viewer / IPAM-DNS-DHCP Editors / Auditor / Compliance
Editor / Change Approver / …), and wildcard semantics are in
[`PERMISSIONS.md`](PERMISSIONS.md).

---

## 5. Feature-Module Gating

Top-level resource families can be turned off as **feature modules**
(non-negotiable #14). A disabled module's router is gated with
`Depends(require_module("..."))` and returns **`404 Not Found`** — not
`403` — so the API surface mirrors an air-gapped deployment where the
feature simply isn't installed
([`backend/app/services/feature_modules.py`](../backend/app/services/feature_modules.py)):

```json
{ "detail": "Feature 'network.circuit' is disabled." }
```

Modules are **default-enabled** ("operators can't disable what they
don't know exists"), except off-prem / secret-touching surfaces which
declare `default_enabled=False`. Examples of gated prefixes (from
`router.py`):

| Prefix | Module id |
|---|---|
| `/api/v1/ai` | `ai.copilot` |
| `/api/v1/circuits` | `network.circuit` |
| `/api/v1/cloud` | `integrations.cloud` |
| `/api/v1/conformity` | `compliance.conformity` |
| `/api/v1/kubernetes` | `integrations.kubernetes` |
| `/api/v1/nmap` | `tools.nmap` |
| `/api/v1/saved-views` | `ui.saved_views` |

Operators toggle modules via the admin feature-modules surface under
`/api/v1/admin`.

---

## 6. Pagination

There are two list shapes in the codebase. New routers should use the
envelope; the legacy core routers return bare arrays.

### 6.1 `{items, total, limit, offset}` envelope (preferred)

The network-modeling and newer routers (circuits, services, ASNs,
VRFs, overlays, multicast, TLS certs, …) accept `limit` + `offset`
query params and return a typed envelope. From
[`backend/app/api/v1/circuits/router.py`](../backend/app/api/v1/circuits/router.py):

```
GET /api/v1/circuits?limit=100&offset=0
```

```json
{
  "items": [ /* … CircuitRead objects … */ ],
  "total": 312,
  "limit": 100,
  "offset": 0
}
```

`limit` is validated per-endpoint (commonly `ge=1, le=500`, with a few
routers allowing higher ceilings); `offset` is `ge=0`. `total` is the
unpaginated row count so clients can compute page counts. These
endpoints also expose resource-specific filter params (e.g.
`provider_id`, `status`, `search`, `tag`) alongside the page controls.

### 6.2 Bare array (core IPAM / DNS / DHCP)

The core IPAM, DNS, and DHCP list endpoints return a plain JSON array
of the resource. For example `GET /api/v1/ipam/spaces` is declared with
`response_model=list[IPSpaceResponse]` and returns:

```json
[
  { "id": "…", "name": "Corporate", "is_default": true, "…": "…" },
  { "id": "…", "name": "DMZ", "is_default": false, "…": "…" }
]
```

These predate the envelope convention; the client filters/sorts these
in the browser or via query params on the specific endpoint.

**Exception — address listing (#517, 2026.07.04-1).**
`GET /api/v1/ipam/subnets/{id}/addresses` stays backward-compatible
(still a bare `list[IPAddressResponse]`) but now accepts optional
`q` / `hostname` / `mac` / `sort` / `order` / `limit` / `offset`, and
sets an `X-Total-Count` response header (CORS-exposed) when the result
is windowed. For **cross-subnet** address queries use the envelope
endpoints `GET /api/v1/ipam/addresses/search` (paginated, joined
subnet/space context) and `GET /api/v1/ipam/addresses/search/ids`
(a capped id list for select-all-matches). Both are permission-scoped
in SQL.

---

## 7. Request / Response Examples

### 7.1 Create an IP space

`POST /api/v1/ipam/spaces`

Request body (`IPSpaceCreate` — only `name` is required; everything
else has a default):

```json
{
  "name": "Corporate",
  "description": "Primary internal routing domain",
  "is_default": true,
  "color": null,
  "tags": { "env": "prod" }
}
```

Response — `201 Created`, `IPSpaceResponse`:

```json
{
  "id": "4f6c2a9e-3b1d-4e7a-9c2f-1a2b3c4d5e6f",
  "name": "Corporate",
  "description": "Primary internal routing domain",
  "is_default": true,
  "tags": { "env": "prod" },
  "color": null,
  "dns_group_ids": [],
  "dns_zone_id": null,
  "dns_additional_zone_ids": [],
  "ddns_enabled": false,
  "ddns_hostname_policy": "client_or_generated",
  "created_at": "2026-06-27T12:00:00Z",
  "modified_at": "2026-06-27T12:00:00Z"
}
```

Creating a space whose `name` already exists returns `409 Conflict`
with `{"detail": "An IP space named 'Corporate' already exists"}`.
Every mutation is written to the append-only `audit_log` before the
response returns (non-negotiable #4).

### 7.2 Read the running version (public)

`GET /api/v1/version` is unauthenticated (the login page calls it for
the release-check banner); the host-identity fields are only populated
for authenticated callers:

```json
{
  "version": "2026.06.25-1",
  "latest_version": null,
  "update_available": false,
  "latest_release_url": null,
  "latest_checked_at": null,
  "release_check_enabled": true,
  "latest_check_error": null,
  "appliance_mode": false,
  "appliance_version": null,
  "appliance_hostname": null
}
```

---

## 8. Errors

Application errors use FastAPI's standard error envelope: a JSON object
with a `detail` field and the appropriate HTTP status code.

```json
{ "detail": "Invalid credentials" }
```

`detail` is usually a human-readable string, but some handlers return a
structured object — e.g. a password-policy failure returns
`{"detail": {"reason": "password_policy", "errors": [...]}}`, and an IP
collision returns `{"detail": {"warnings": [...], "requires_confirmation": true}}`.

Common status codes across the surface:

| Status | Meaning |
|---|---|
| `400 Bad Request` | Semantically invalid input (e.g. wrong current password) |
| `401 Unauthorized` | Missing / invalid / expired credential, or insufficient token scope |
| `403 Forbidden` | Authenticated but not permitted (RBAC denial, superadmin-only, disabled account, forced password change) |
| `404 Not Found` | Resource doesn't exist **or** its feature module is disabled |
| `409 Conflict` | Uniqueness / overlap / collision conflict |
| `422 Unprocessable Entity` | Pydantic request-body / query-param validation failure |
| `429 Too Many Requests` | Per-IP login rate limit tripped |
| `503 Service Unavailable` | Maintenance mode, transient DB-connection-closed (carries `Retry-After`), or a failing readiness check |

A `422` validation error carries FastAPI's structured field-error list:

```json
{
  "detail": [
    {
      "type": "missing",
      "loc": ["body", "name"],
      "msg": "Field required"
    }
  ]
}
```

Validation, permission, and auth errors are raised as typed
`HTTPException`s and turned into clean 4xx responses by FastAPI's own
machinery. Anything that slips past every handler is caught by a
last-resort exception handler in `main.py` that records the failure for
the diagnostics surface and returns a generic
`500 {"detail": "Internal Server Error"}` (it never echoes the
exception text). Transient DB-connection-closed errors (e.g. during a
backup restore) are converted to a `503` with `Retry-After: 1` so agent
long-polls back off rather than cascading.

### Request correlation

Every request gets an `X-Request-ID` (read from the inbound header or
minted as a UUID) bound into the structured logs and echoed back on the
response header, so a client can correlate a response with the
server-side log lines for that request (non-negotiable #7).

---

## 9. CORS, Trusted Hosts, Maintenance Mode

- **CORS** — origins come from the `CORS_ORIGINS` env var (comma-
  separated; default `*`). With a wildcard, credentialed CORS is
  disabled by construction (the API authenticates via the
  `Authorization` header, not cookies). Pin explicit origins to enable
  `allow_credentials`.
- **Trusted hosts** — `TRUSTED_HOSTS` (default `*`) gates the inbound
  `Host` header; set it to your real hostnames to harden against
  Host-header injection / DNS-rebinding.
- **Maintenance mode** (issue #57) — when enabled, mutating requests
  are short-circuited with `503` (superadmin + exempt paths bypass);
  reads pass through. The banner state is surfaced on
  `/health/platform`.

---

## 10. MCP (Operator Copilot) Endpoint

The Operator Copilot exposes a **Model Context Protocol** server over
JSON-RPC 2.0 (the MCP spec's "Streamable HTTP" transport) at:

```
/api/v1/ai/mcp
```

It is part of the `/api/v1/ai` router, gated by the `ai.copilot`
feature module — disable the module and the endpoint 404s. It shares
the same auth surface as the rest of the API: a session JWT for browser
clients, or an API token for external MCP clients (Claude Desktop /
Cursor / any MCP-speaking client). External clients use a token with
the `read` scope, which is explicitly allowed to `POST` the JSON-RPC
frame even though it is a write method.

A bare `GET /api/v1/ai/mcp` returns server identity (handy for a sanity
check; auth still required):

```json
{
  "server": { "name": "spatiumddi", "version": "1.0.0" },
  "protocol_version": "2025-06-18",
  "available_tools": 0,
  "transport": "streamable_http"
}
```

`POST /api/v1/ai/mcp` handles one JSON-RPC request (or a batch).
Supported methods are `initialize`, `notifications/initialized`,
`tools/list`, `tools/call`, and `ping`; `resources/*`, `prompts/*`,
`sampling/*`, and `completion/*` return method-not-found. A
`tools/list` call returns the registry's read-only tools — the
hundreds of `find_*` / `count_*` reads, deliberately excluding
`propose_*` write tools so the MCP surface never even stages a mutation
(writes go through the C2-gated `/api/v1/ai/proposals` apply flow in
the chat UI instead). Per-tool default-enabled state and feature-module
filtering both apply, so the advertised set reflects what the operator
has actually turned on.

---

## See also

- [`features/AUTH.md`](features/AUTH.md) — auth providers, MFA, sessions, token model
- [`PERMISSIONS.md`](PERMISSIONS.md) — RBAC grammar, built-in roles, wildcards
- [`deployment/DOCKER.md`](deployment/DOCKER.md) — ports, env vars, first-time setup
- [`features/IPAM.md`](features/IPAM.md) · [`features/DNS.md`](features/DNS.md) · [`features/DHCP.md`](features/DHCP.md) — per-feature endpoint detail
