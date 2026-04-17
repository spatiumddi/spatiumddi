# Changelog

All notable changes to SpatiumDDI are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/); versioning uses [CalVer](https://calver.org/) (`YYYY.MM.DD-N`).

---

## Unreleased

First wave of substantive post-alpha work. External auth providers (LDAP /
OIDC / SAML + RADIUS / TACACS+ with backup-server failover), group-based
RBAC enforced across every API router, partial IPv6 (storage + UI + Kea
Dhcp6), inherited-field placeholders on edit modals, mobile-responsive
layout, IPAM block/subnet overlap validation, scheduled IPAM↔DNS auto-sync,
bulk-edit DNS zone assignment, shared zone-picker dropdown with primary /
additional grouping, and a `make ci` target that mirrors GitHub Actions
locally.

### Added

**Auth — Wave A (external identity providers)**
- `AuthProvider` + `AuthGroupMapping` models with Fernet-encrypted secrets
  (`backend/app/core/crypto.py`). Admin CRUD at `/api/v1/auth-providers`
  with per-type structured forms on `AuthProvidersPage`.
- **LDAP** — `ldap3`-based auth (`backend/app/core/auth/ldap.py`).
  Password-grant fallthrough from `/auth/login`. TLS / LDAPS / StartTLS
  support with optional CA cert path.
- **OIDC** — authorize / callback redirect flow with signed-JWT state+nonce
  cookie, discovery + JWKS caching, `authlib.jose` ID-token validation.
  Login page renders enabled providers as "Sign in with …" buttons.
- **SAML** — `python3-saml` SP flow: HTTP-Redirect AuthnRequest, ACS POST
  binding, `GET /auth/{provider_id}/metadata` for IdP-side SP metadata.
- Unified user sync (`backend/app/core/auth/user_sync.py`) creates / updates
  `User` rows, replaces group membership from group mappings, and
  **rejects logins with no mapping match** (configurable per provider).

**Auth — Wave B (network-device protocols)**
- **RADIUS** — `pyrad` driver (`backend/app/core/auth/radius.py`).
  Built-in minimal dictionary; extra vendor dicts via `dictionary_path`.
  Group info from `Filter-Id` / `Class` by default.
- **TACACS+** — `tacacs_plus` driver (`backend/app/core/auth/tacacs.py`).
  Separate `authorize()` round-trip pulls AV pairs; numeric `priv-lvl`
  values are surfaced as `priv-lvl:N` for group mapping.
- Both share the same password-grant fallthrough as LDAP via
  `PASSWORD_PROVIDER_TYPES = ("ldap", "radius", "tacacs")`.
- Per-provider "Test connection" probe in the admin UI returns
  `{ok, message, details}` for all five provider types.

**Auth — backup-server failover (LDAP / RADIUS / TACACS+)**
- Each password provider's config now accepts an optional list of backup
  hosts (`config.backup_hosts` for LDAP, `config.backup_servers` for
  RADIUS/TACACS+). Entries can be `"host"` or `"host:port"`; bracketed
  IPv6 literals (`[::1]:389`) are supported. The UI adds a "Backup hosts /
  servers" textarea (one per line).
- LDAP uses `ldap3.ServerPool(pool_strategy=FIRST, active=True,
  exhaust=True)` — dead hosts are skipped for the pool's lifetime.
- RADIUS and TACACS+ iterate primary → backups manually. A definitive
  auth answer (Accept / Reject, `valid=True/False`) stops iteration;
  network / timeout / protocol errors fail over to the next server.
- All backups share the primary's shared secret and timeout settings.

**Auth — Wave C (group-based RBAC enforcement)**
- Permission grammar `{action, resource_type, resource_id?}` with wildcard
  support; helpers in `backend/app/core/permissions.py`
  (`user_has_permission`, `require_permission`, `require_any_permission`,
  `require_resource_permission`).
- Five builtin roles seeded at startup: Superadmin, Viewer, IPAM Editor,
  DNS Editor, DHCP Editor.
- `/api/v1/roles` CRUD + expanded `/api/v1/groups` CRUD with role/user
  assignment. Router-level gates applied across IPAM / DNS / DHCP / VLANs
  / custom-fields / settings / audit. Superadmin always bypasses.
- `RolesPage` + `GroupsPage` admin UI. See `docs/PERMISSIONS.md`.

**Auth — Wave D UX polish**
- Per-field opt-in toggles on bulk-edit IPs (status / description / tags /
  custom-fields / DNS zone individually) plus a "replace all tags" mode.
- `EditSubnetModal` + `EditBlockModal` now surface inherited custom-field
  values as HTML `placeholder` with "inherited from block/space `<name>`"
  badges. New `/api/v1/ipam/blocks/{id}/effective-fields` endpoint for
  parity with the existing subnet endpoint.

**IPv6 (partial)**
- `DHCPScope.address_family` column (migration `d7a2b6e9f134`) + Kea
  driver `Dhcp6` branch renders a v6 config bundle from the same scope
  rows. Dhcp6 option-name translation TODO is flagged in
  `backend/app/drivers/dhcp/kea.py`.
- `Subnet.total_ips` widened to `BigInteger` (migration `e3c7b91f2a45`)
  so a `/64` (`2^64` addresses) fits. `_total_ips()` clamps at `2^63 − 1`.
- Subnet create skips the v6 broadcast row; `_sync_dns_record` emits AAAA
  + PTR in `ip6.arpa`.
- `/blocks/{id}/available-subnets` accepts `/8–/128` (was `le=32`) with
  an explicit address-family guard. Frontend "Find by size" splits the
  prefix pool into v4 (`/8–/32`) and v6 (`/32, /40, /44, /48, /52, /56,
  /60, /64, /72, /80, /96, /112, /120, /124, /127, /128`) and dynamically
  filters to prefixes strictly longer than the selected block's prefix.
- `/ipam/addresses/next-address` returns 409 on v6 subnets (EUI-64 / hash
  allocation is a future enhancement).
- IPAM create-block / create-subnet placeholders now include an IPv6
  example next to the IPv4 one (`e.g. 10.0.0.0/8 or 2001:db8::/32`).

**IPAM — block / subnet overlap validation**
- `_assert_no_block_overlap()` rejects same-level duplicates and CIDR
  overlaps in `create_block` and in the reparent path of `update_block`.
  Uses PostgreSQL's `cidr &&` operator for a single-query overlap check.

**IPAM — scheduled IPAM ↔ DNS auto-sync**
- Opt-in Celery beat task `app.tasks.ipam_dns_sync.auto_sync_ipam_dns`
  (`backend/app/tasks/ipam_dns_sync.py`). Beat fires every 60 s; the task
  gates on `PlatformSettings.dns_auto_sync_enabled` +
  `dns_auto_sync_interval_minutes`, so cadence changes in the UI take
  effect without restarting beat. Optional deletion of stale auto-
  generated records (`dns_auto_sync_delete_stale`).
- Settings UI: new **DNS Auto-Sync** section on `/admin/settings`
  (enable / interval / delete-stale toggle).

**IPAM — shared zone picker + bulk-edit DNS zone**
- New `ZoneOptions` component (`frontend/src/pages/ipam/IPAMPage.tsx`)
  renders the primary zone first, then an `<optgroup label="Additional
  zones">` separator. Used in Create / Edit / Bulk-edit IP modals.
- Zone picker is restricted to the subnet's explicit primary + additional
  zones when any are pinned; falls back to every zone in the group only
  when the admin picked a group without pinning specific zones.
- `IPAddressBulkChanges.dns_zone_id` — bulk-editing a set of IPs routes
  every selected address through `_sync_dns_record` for move / create /
  delete.

**IPAM — mobile responsive**
- Sidebar becomes a drawer on `<md` with backdrop + `Header` hamburger
  toggle.
- 10+ data tables wrapped in `overflow-x-auto` with `min-w` so wide
  columns scroll horizontally instead of overflowing the viewport.
- All modals sized `max-w-[95vw]` on `<sm`.

**IPAM — IP aliases polish**
- Adding or deleting an alias now also invalidates
  `["subnet-aliases", subnet_id]`, so switching to the Aliases tab after
  an add/delete no longer shows a stale list.
- Delete alias from the subnet Aliases tab now pops a single-step
  `ConfirmDeleteModal` ("Delete alias `<fqdn>`? The DNS record will be
  removed.") matching the standard IPAM delete flow.

**Developer tooling**
- `make ci` — new Makefile target that runs the exact three lint jobs
  CI runs (`backend-lint`: ruff + black + mypy; `frontend-lint`: eslint +
  prettier + tsc; `frontend-build`: `npm run build`). Backend checks run
  inside the running `api` container; ruff/black/mypy are installed on
  first run if missing.
- `.github/ISSUE_TEMPLATE/{bug_report,feature_request,config}.yml` and
  `.github/pull_request_template.md` — structured issue + PR templates
  with dropdown areas (IPAM / DNS / DHCP / Auth / RBAC / Audit / UI / API
  / Deployment / Docs), repro steps, a private Security Advisory link,
  and a test-plan checklist.

### Changed

- IPAM modal input focus ring switched to `focus:ring-inset` so the 2px
  ring draws inside the border. Prevents horizontal clipping by the
  modal's `overflow-y-auto` container (browsers clamp `overflow-x` when
  `overflow-y` is set), which previously cut the left edge of any focused
  box in the Create / Edit Block / Subnet forms.
- `CLAUDE.md` phase roadmap updated to reflect Waves A–D. Tech-stack Auth
  row now lists actual deps (`python-jose + bcrypt`, `ldap3`, `authlib`,
  `python3-saml`, `pyrad`, `tacacs_plus`, `Fernet`).

### Fixed

- `user_sync._matched_internal_groups` used one `res` variable name for
  two `db.execute()` calls with different result types, tripping mypy
  after the dev extras finally ran in `make ci`. Renamed to `map_res` /
  `group_res`.
- CI lint was still failing on `main` after `f38d533` — residual ruff
  warnings (20) and prettier issues (12 files). Now clean; `make ci`
  passes end-to-end.
- SAML ACS handler: `SAMLResponse` / `RelayState` form fields kept their
  spec-mandated casing; added `# noqa: N803` so ruff stops complaining.

### Security

- CodeQL alert #13 (CWE-601, URL redirection from remote source): the
  OIDC callback interpolated the IdP-provided `error` query parameter
  directly into the `/login?error=…` redirect. The redirect target was
  already a relative path (so no open-redirect in practice) but the
  tainted value still flowed into the URL. Added `_safe_error_suffix()`
  to strip any provider-supplied error code down to `[a-z0-9_]` (max 40
  chars) and applied it at every `f"…_{error}"` / `f"…_{exc.reason}"`
  site in the OIDC and SAML callback handlers.

---

## 2026.04.16-2 — 2026-04-16

First post-alpha iteration — same-day follow-up to the alpha. Adds IP
aliases across the stack, multi-select/bulk ops on the IP address table,
an always-visible per-column filter row on the audit log, a DNS zone
tree that can create sub-zones with a click, and switches the base
Compose file to pull release images from GHCR.

### Added

**IPAM**
- IP aliases — Allocate/Edit IP modal supports extra CNAME/A records tied to the IP. Auto-deleted on IP purge.
- `+N aliases` pill next to the hostname in the subnet IP table when an IP has user-added aliases (new `alias_count` on `IPAddressResponse`).
- New "Aliases" subnet tab listing every CNAME/A alias in the subnet (name · type · target · IP · host · delete). `GET /ipam/subnets/{id}/aliases`.
- Multi-select on the subnet IP table with a bulk-action bar inline on the tab row (no banner push-down). `POST /ipam/addresses/bulk-delete` (soft → orphan or permanent) and `POST /ipam/addresses/bulk-edit` (status, description, tags *merge*, custom_fields *merge*). System rows auto-excluded.
- Reverse-zone backfill — dedicated button on Space / Block / Subnet headers (`POST /ipam/{scope}/{id}/reverse-zones/backfill`). Also backfills opportunistically on every IP allocation.
- DHCP Pool membership column on subnet IP table — cyan/violet/zinc badge per IP shows which pool (dynamic/reserved/excluded) it falls in.
- Bulk orphan cleanup modal on subnet header.
- `IPAddress.auto_from_lease` column distinguishes DHCP-lease-mirrored rows from manual allocations (migration `e2a6f3b8c1d4`).

**DNS**
- Real RPZ blocklist rendering in the BIND9 agent — `response-policy { } break-dnssec yes`, CNAME trigger zone files (nxdomain/sinkhole/redirect/passthru). Wildcards block both apex and subdomains.
- Blocklist entries get a `reason` column (migration `b4d1c9e2f3a7`) and per-entry `is_wildcard` toggle (defaults true).
- Inline edit for blocklist entries + exceptions (`PUT .../entries/{id}`, `PUT .../exceptions/{id}`).
- Blocklist page reorganized into red **Blocked Domains** and green **Allow-list** sections.
- DNS records table: always-visible edit/delete, clickable record name, single-step delete confirm, multi-select bulk delete (IPAM records excluded).
- Zone tree folder click → Create-Zone modal pre-filled with the parent suffix (e.g. clicking `example.com` opens "New zone `*.example.com`"). TLD folders (org/com/net/…) just toggle, don't prompt. Zone names in the tree render without the trailing dot.
- DNS agent re-bootstraps on 404 (not just 401) — recovers from stale server rows.

**DHCP**
- Pool overlap validation + existing-IP warning on pool create.
- Static DHCP ↔ IPAM sync (creates `status=static_dhcp` rows, fires DNS sync on create/update/delete).
- Lease → IPAM mirror: active leases create `dhcp` rows; expired leases remove them (`auto_from_lease` flag only).
- Celery `sweep_expired_leases` task (every 5min) catches missed lease events.
- Force-sync coalesces repeated clicks into one pending op.
- Kea agent: UDP socket mode for relay-only deployments; `/run/kea` perms; lease op acks via heartbeat.
- DHCP scope options default-prefill from Settings (DNS/NTP/domain/lease-time).
- Static assignments moved from DHCP Pools tab into IPAM Allocate IP flow.

**Audit log**
- Per-column filter row on `/admin/audit` — User/Summary/IP text inputs, Action/Resource/Result dropdowns, always visible, Clear-all X in the actions column. Backend adds `resource_display` / `result` / `source_ip` query params.

**Platform**
- Base `docker-compose.yml` now pulls release images from GHCR (`ghcr.io/spatiumddi/spatiumddi-{api,frontend}`, `ghcr.io/spatiumddi/dns-bind9`, `ghcr.io/spatiumddi/dhcp-kea`); pin with `SPATIUMDDI_VERSION=<tag>` in `.env`.
- `docker-compose.dev.yml` is a standalone self-contained file that keeps `build:` stanzas for local dev builds — use `docker compose -f docker-compose.dev.yml …` or `export COMPOSE_FILE=docker-compose.dev.yml`.
- Jekyll docs site config (`docs/_config.yml`, `docs/index.md`).
- CHANGELOG; alpha banner; clickable screenshot thumbnails in README.
- Seed script (`scripts/seed_demo.py`).
- Alembic migrations now tracked in git (were `.gitignore`d — CI was broken).
- `COMPOSE_PROFILES` documented.

### Changed

- `CLAUDE.md` slimmed to a navigational entry point — Phase 1 / Waves 1–5 / DHCP Wave 1 implemented-lists moved to this CHANGELOG; added a Repo Layout section and a Cross-cutting Patterns section (driver abstraction, ConfigBundle+ETag long-poll, agent bootstrap/reconnection).

### Fixed

- Full audit of IPAM/VLANs/DNS/DHCP frontend ↔ backend API contracts; 10+ mismatches fixed.
- `allocate_next_ip` — `FOR UPDATE` on outer join, now `of=Subnet` + `.unique()`.
- Workflow permissions hardened (CodeQL alerts resolved).
- Ruff (import sort, unused `datetime.UTC`), Black (4 files), Prettier (3 files) — unblocked CI.

---

## 2026.04.16-1 — Alpha

First public release. **Alpha quality** — expect rough edges and breaking changes between releases.

### Added

**IPAM**
- Hierarchical IP management: spaces, blocks (nested), subnets, addresses
- Subnet CIDR validation with "Did you mean?" hints
- Next-available IP allocation (sequential / random)
- Soft-delete IP addresses (orphan → restore / purge)
- Bulk orphan cleanup modal on subnet view
- Subnet-by-size search ("Find by size" in create modal)
- Per-column filters on address & block tables
- Drag-and-drop reparenting of blocks and subnets
- Free-space band on block detail with click-to-create
- Import/export (CSV, JSON, XLSX with preview)
- Bulk-edit subnets (tags, custom fields)
- Custom field definitions per resource type
- DNS assignment at space / block / subnet level with inheritance
- IPAM ↔ DNS drift detection and reconciliation (subnet / block / space scope)
- DNS sync indicator column on IP address table
- DHCP pool membership column on IP address table

**DNS**
- Server groups, servers, zones, records — full CRUD
- BIND9 driver with Jinja templates, TSIG-signed RFC 2136 dynamic updates
- Agent runtime: bootstrap (PSK → JWT), long-poll config sync with ETag, on-disk cache
- Container image: `ghcr.io/spatiumddi/dns-bind9` (Alpine 3.22, multi-arch)
- Zone tree with nested sub-zone display
- Zone import/export (RFC 1035 parser, color-coded diff preview)
- Server health checks (heartbeat staleness → SOA fallback)
- ACLs, views, trust anchors
- Blocking lists (RPZ) with feed refresh, bulk-add, exceptions
- Query logging configuration (file / syslog / stderr)
- DNS defaults in Settings (TTL, zone type, DNSSEC, recursion)

**DHCP**
- Kea driver + agent runtime (bootstrap, long-poll, lease tail, local cache)
- Container image: `ghcr.io/spatiumddi/dhcp-kea` (Alpine 3.22, multi-arch)
- Server groups, servers, scopes, pools, static assignments, client classes
- DHCP options editor with NTP (option 42) as first-class field
- Pool overlap validation on create and resize
- Existing-IP-in-range warning on pool creation
- Scope auto-binds to sole server; gateway + settings defaults pre-filled
- Static DHCP ↔ IPAM sync (status=static_dhcp, DNS forward/reverse)
- DHCP defaults in Settings (DNS servers, domain, NTP, lease time)
- UDP socket mode for relay-only deployments (no broadcast / no NET_RAW)

**VLANs**
- Routers and VLANs with full CRUD
- Subnet ↔ VLAN association (router + VLAN columns in IPAM views)
- Delete protection when subnets still reference a VLAN/router

**Auth & Users**
- Local auth with JWT + refresh token rotation
- Forced password change on first login
- User management (create, edit, reset password, delete)

**Platform**
- Dashboard with utilisation stats, top subnets, VLAN/DNS/DHCP status sections
- Global search (Cmd+K / Ctrl+K) across IPs, hostnames, MACs, subnets
- Settings page (branding, allocation, session, DNS/DHCP defaults, utilisation thresholds)
- Audit log viewer with action/result badges and filters
- Docker Compose with `dns` and `dhcp` profiles (`COMPOSE_PROFILES=dns,dhcp`)
- Kubernetes manifests (StatefulSets, services, PVCs)
- GitHub Actions CI (lint, type-check, test) + release workflow (multi-arch images, GitHub Release)

### Security
- Workflow permissions hardened (CodeQL alerts resolved)
- All mutations audited before commit
- Agent re-bootstraps on 401/404 (no stale-token loops)

---

_For the full commit history, see the [GitHub compare view](https://github.com/spatiumddi/spatiumddi/commits/main)._
