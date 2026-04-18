# Changelog

All notable changes to SpatiumDDI are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/); versioning uses [CalVer](https://calver.org/) (`YYYY.MM.DD-N`).

---

## Unreleased

Post-2026.04.18-1 audit sweep + the DDNS pipeline.

### Added

**DDNS — DHCP lease → DNS A/PTR reconciliation**
- Migration `e1f2a3b4c5d6` adds four subnet-level DDNS control fields:
  `ddns_enabled` (default False — opt-in),
  `ddns_hostname_policy` (`client_provided` | `client_or_generated` |
  `always_generate` | `disabled`),
  `ddns_domain_override` (publish into a different zone), and
  `ddns_ttl` (override the zone's TTL for auto-generated records).
  Independent of `DHCPScope.ddns_enabled` — that pair still drives
  Kea's native DDNS hook.
- New service `app/services/dns/ddns.py` with `resolve_ddns_hostname`,
  `apply_ddns_for_lease`, and `revoke_ddns_for_lease`. Static-assignment
  hostnames always win over policy; client hostnames are sanitised to
  RFC 1035 labels and truncated at 63 chars; generated hostnames use
  the last two IPv4 octets (`dhcp-20-5` for `10.1.20.5`) or the low
  32 bits hex for IPv6.
- Integration points: `services/dhcp/pull_leases.py` fires DDNS after
  each mirrored IPAM row (agentless lease-pull path);
  `tasks/dhcp_lease_cleanup.py` calls `revoke_ddns_for_lease` before
  deleting the mirrored row.
- Idempotent — repeat polls over the same lease state are a no-op.
- `_sync_dns_record` is lazy-imported from the IPAM router at call
  time to dodge a module-load cycle.
- `SubnetCreate` / `SubnetUpdate` / `SubnetResponse` + `Subnet`
  frontend type gain the four DDNS fields.
- New `DdnsSettingsSection` React component — enable toggle, policy
  dropdown, domain-override input, TTL input, live preview of what
  `always_generate` would produce for the subnet's first IP. Wired
  into `CreateSubnetModal` and `EditSubnetModal`.
- Docs: `features/DNS.md §7` rewritten to describe the shipped
  implementation (architecture diagram, subnet fields, policy
  semantics, static override, idempotency, enable walkthrough).

### Fixed

- K8s worker queue mismatch — `k8s/base/worker.yaml` listed
  `ipam,default`; compose widened to `ipam,dns,dhcp,default` in the
  Windows release. DNS + DHCP health sweeps + scheduled sync tasks
  were silently hanging on K8s.
- Windows DNS TLSA dispatch — `_SUPPORTED_RECORD_TYPES` listed TLSA
  (RFC 2136 handles it fine via dnspython) but `_ps_apply_record`
  raised ValueError for TLSA, so creating a TLSA record on a
  Windows server with credentials failed unpredictably. Added
  `_WINRM_UNSUPPORTED_RECORD_TYPES`; `apply_record_change` now falls
  back to RFC 2136 for those types even when credentials are set.
- `DHCPPage.tsx` lease-sync handler was invalidating
  `["ipam-addresses"]` which matches nothing; changed to
  `["addresses"]` (broad match) so the subnet-level address list
  refreshes after lease sync mirrors new rows.
- Frontend `DHCPPool` type now declares optional
  `existing_ips_in_range` so `CreatePoolModal` no longer needs an
  `as any` cast.

### Changed

- Kea driver gains **Dhcp6 option-name translation** — new
  `_KEA_OPTION_NAMES_V6` map + `_DHCP4_ONLY_OPTION_NAMES` set;
  `_render_option_data` takes `address_family` and routes
  accordingly. v4-only options (`routers`, `broadcast-address`,
  `mtu`, `time-offset`, `domain-name`, tftp-*) are dropped from v6
  scopes with a warning log instead of being emitted under the
  wrong space (which Kea would reject on reload). Scope / pool /
  reservation / client-class renderers all thread `address_family`
  through. Closes the Phase 1 Dhcp6 TODO.

---

## 2026.04.18-1 — 2026-04-18

The **Windows Server integration** release. Adds agentless drivers for
Windows DNS (Path A — RFC 2136, always available; Path B — WinRM +
PowerShell for zone CRUD and AXFR-free record pulls) and Windows DHCP
(Path A — WinRM lease mirroring + per-object scope / pool / reservation
write-through). IPAM gains full DHCP server-group inheritance parallel to
the existing DNS model, a two-action delete (Mark as Orphan / Delete
Permanently), and a right-click context menu across every top-level
module. Settings gets a per-section "Reset to defaults" button, the two
DNS sync sections were renamed with a layer diagram showing which
boundary each one reconciles, and three new doc sets (Getting Started,
Windows Server setup, DHCP driver spec) land alongside a redrawn
architecture SVG.

### Added

**DNS — Windows Server driver (agentless, Path A + B)**
- `WindowsDNSDriver` (`backend/app/drivers/dns/windows.py`) implementing
  record CRUD for `A / AAAA / CNAME / MX / TXT / PTR / SRV / NS / TLSA`
  over RFC 2136 via `dnspython`. Optional TSIG; GSS-TSIG and SIG(0) are
  Path B follow-ups.
- `AGENTLESS_DRIVERS` frozenset + `is_agentless()` in the DNS driver
  registry. `record_ops.enqueue_record_op` short-circuits straight to the
  driver for agentless servers instead of queueing for a non-existent
  agent; logs a warning when a record op is dropped for lack of a
  primary.
- **Path B (credentials required)** — `DNSServer.credentials_encrypted`
  (Fernet-encrypted WinRM dict, same shape as Windows DHCP) unlocks
  `Add-DnsServerPrimaryZone` / `Remove-DnsServerZone` for zone CRUD and
  `Get-DnsServerResourceRecord`-based record pulls that sidestep the
  AD-integrated zone AXFR ACL which otherwise returns REFUSED. All
  PowerShell paths are idempotent — guard on `Get-DnsServerZone
  -ErrorAction SilentlyContinue` before acting. Record writes still ride
  RFC 2136 to avoid paying the PowerShell-per-record cost.
- **Write-through for zones** — `_push_zone_to_agentless_servers` pushes
  zone create / delete to Windows *before* the DB commit; a WinRM
  failure surfaces as HTTP 502 and rolls back, so DB and server never
  drift. Mirrors the Windows DHCP write-through pattern.
- **Shared AXFR helper** — `app/drivers/dns/_axfr.py` now used by both
  BIND9 and the Windows RFC path. Filters SOA + apex NS and absolutises
  `CNAME / NS / PTR / MX / SRV` targets.
- `POST /dns/test-windows-credentials` — runs
  `(Get-DnsServerSetting -All).BuildNumber` as a cheap probe; wired into
  the server create modal's "Test Connection" button.
- Migration `d3f1ab7c8e02_windows_dns_credentials.py`.

**DHCP — Windows Server driver (agentless, Path A)**
- `WindowsDHCPReadOnlyDriver` (`backend/app/drivers/dhcp/windows.py`)
  speaks WinRM / PowerShell against the `DhcpServer` module. Reads:
  `Get-DhcpServerv4Lease` for lease monitoring,
  `Get-DhcpServerv4Scope` + options + exclusions + reservations for
  scope topology pulls. Writes (per-object, idempotent): `apply_scope`
  / `remove_scope` / `apply_reservation` / `remove_reservation` /
  `apply_exclusion` / `remove_exclusion`.
- `services/dhcp/windows_writethrough.py` pushes scope / pool / static
  edits to Windows before DB commit — same rollback guarantee as the
  Windows DNS path.
- `AGENTLESS_DRIVERS` + `READ_ONLY_DRIVERS` sets on the DHCP driver
  registry. The `/sync` bundle-push endpoint rejects read-only drivers;
  the UI hides "Sync / Push config" and substitutes "Sync Leases" +
  per-object CRUD instead.
- Scheduled Celery beat task `app.tasks.dhcp_pull_leases` (60 s cadence;
  gates on `PlatformSettings.dhcp_pull_leases_enabled` /
  `_interval_minutes`). Upserts leases by `(server_id, ip_address)` and
  mirrors each lease into IPAM as `status="dhcp"` + `auto_from_lease=
  True` when the IP falls inside a known subnet. The existing lease-
  cleanup sweep handles expiry uniformly.
- Admin UX: transport picker (`ntlm` / `kerberos` / `basic` / `credssp`),
  "Test WinRM" button, Windows setup checklist (security group + WinRM
  enablement), partial credential updates that preserve the stored blob
  across transport changes. Agentless servers auto-approve on create.
- Migration `b71d9ae34c50_windows_dhcp_support.py`.

**IPAM → DHCP server-group inheritance**
- New `dhcp_server_group_id` on `IPSpace` / `IPBlock` / `Subnet`, plus
  `dhcp_inherit_settings` on Block / Subnet — mirrors the existing DNS
  pattern.
- Three `/effective-dhcp` endpoints walk Space → Block → Subnet; subnet
  resolution falls through to the space when no block overrides.
- `CreateScopeModal` prefills the server from the effective group,
  restricts the dropdown to that group, and exposes an override
  checkbox. Space / Block / Subnet modals gain a DHCP section parallel
  to `DnsSettingsSection`.
- Migration `a92f317b5d08_ipam_dhcp_server_group_inheritance.py`.

**DNS — bi-directional zone reconciliation**
- Group-level "Sync with Servers" button iterates every enabled server,
  auto-imports zones found on the wire but missing from SpatiumDDI
  (skipping system zones TrustAnchors / RootHints / Cache), pushes
  DB-only zones back via `apply_zone_change`, then pulls records
  (AXFR for BIND9 / Windows Path A, `Get-DnsServerResourceRecord` for
  Path B) and reconciles against DB state. Additive-only — never
  deletes on either side.
- Dedup keys fold `CNAME / NS / PTR / MX / SRV` to canonical absolute
  FQDNs so IPAM-written (FQDN-with-dot) and AXFR-read (bare label)
  values no longer duplicate. Out-of-zone glue records are filtered.

**DNS server enable/disable**
- `DNSServer.is_enabled` — user-controlled pause flag separate from
  health-derived `status`. Disabled servers are skipped by the health
  sweep, bi-directional sync, and the record-op dispatcher.
- Migration `c4e8f1a25d93_dns_server_is_enabled.py`.
- `dhcp_health` + `dns` health tasks refactored to per-task async
  engines (fixes "Future attached to a different loop" when the worker
  queue is widened) and now call `driver.health_check()` for agentless
  drivers so the dashboard stops showing "never checked" for Windows
  DHCP / DNS. Compose worker queues re-widened to
  `ipam,dns,dhcp,default`.

**IPAM — two-action delete + cache propagation**
- Allocated-IP delete now offers two distinct actions: **Mark as
  Orphan** (amber — keeps the row, clears ownership metadata) and
  **Delete Permanently** (destructive). No double-confirm — the two
  coloured buttons are the confirmation.
- Every IPAM mutation that invalidates `["addresses", …]` now also
  invalidates `["dns-records"]`, `["dns-group-records"]`, and
  `["dns-zones"]`. A newly-created PTR shows up in the reverse-zone
  record list without a full page reload.

**Settings — per-section reset + DNS sync renames**
- `GET /settings/defaults` introspects column defaults from the
  `PlatformSettings` model (single source of truth — no frontend drift).
- Per-section **Reset to defaults** button populates only that section's
  fields; Save is still required so the user can back out.
- Renamed *DNS Auto-Sync* → **IPAM → DNS Reconciliation** and
  *DNS Server Sync* → **Zone ↔ Server Reconciliation**. Each gets a
  three-pill layer diagram (IPAM → SpatiumDDI DNS ↔ Windows / BIND9)
  with the relevant arrow highlighted.

**Delete guards + bulk actions + right-click menus**
- IP space, IP block, DNS server group, DHCP server group: **409** on
  delete if populated (plain text error with count).
- Subnet delete now cascades DHCP scope cleanup to Windows
  (`push_scope_delete`) and Kea (`config_etag` bump + `DHCPConfigOp`).
- DNS ZonesTab: compact table replaces card-per-zone; checkbox column
  + bulk delete toolbar. IPAM space table: bulk-select leaf blocks.
- Right-click context menus across IPAM IP rows, IPAM space headers,
  DNS zone tree + record rows, DHCP scope / static / lease rows, VLAN
  rows.
- DNS group picker: single-select dropdown; Additional Zones hidden
  behind a themed `<details>` expander.
- VLAN router delete: two-step confirmation with checkbox.
- `ConfirmDestroyModal` / `DeleteConfirmModal` surface 409 errors
  inline.
- Space-table refresh button: `forceRefetch` instead of invalidate.
- Migration `e5b831f02db9` enforces `subnet.block_id NOT NULL` and
  fixes FK drift from SET NULL to RESTRICT.

**Auth — provider form UX**
- Auth-provider form defaults to `is_enabled=True` and
  `tls_insecure=False`. Pre-save "Test Connection" probe validates
  before creation (instead of after a save that might fail at login
  time). Applies to all five provider types.

**UI — selection persistence**
- IPAM / DNS / DHCP selection (subnet / zone / server) now survives tab
  switches. IPAM + DNS had a race in `useStickyLocation`'s restore
  effect; DHCP had no URL backing at all and was pure in-memory state.
  Both fixed; DHCP gets `spatium.lastUrl.dhcp` + a `setSelection()`
  wrapper that mirrors into `?group=…&server=…`.

**Documentation**
- [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) — recommended
  setup order from fresh install to allocating the first IP, with three
  topology recipes (all-SpatiumDDI / hybrid Windows DNS / hybrid
  Windows DNS + DHCP).
- [`docs/deployment/WINDOWS.md`](docs/deployment/WINDOWS.md) — shared
  Windows-side checklist: WinRM enablement, transport / port matrix,
  firewall rules, service accounts (`DnsAdmins` / `DHCP Users`), zone
  dynamic-update settings, diagnosis recipe with a pywinrm snippet,
  hardening checklist.
- [`docs/drivers/DHCP_DRIVERS.md`](docs/drivers/DHCP_DRIVERS.md) —
  filled in the driver spec CLAUDE.md was already pointing at. Kea
  agented + Windows DHCP agentless, with `AGENTLESS_DRIVERS` /
  `READ_ONLY_DRIVERS` classification.
- README — Windows Server DNS/DHCP feature bullet; Architecture
  section reframed around agented vs agentless split; doc-index
  refreshed.
- [`docs/assets/architecture.svg`](docs/assets/architecture.svg) —
  redrawn. Two-lane data-plane split: agented (`dns-bind9` + `dhcp-kea`
  with sidecar-agent pills) vs agentless (Windows DNS Path A/B,
  Windows DHCP Path A read-only); scheduled-sync arrow from Beat →
  agentless lane.
- [`features/DNS.md`](docs/features/DNS.md) — new §12 "Sync with
  Servers" reconciliation, §13 Windows DNS Path A + B, §14 scheduled
  reconciliation jobs.
- [`features/DHCP.md`](docs/features/DHCP.md) — new §15 Windows DHCP
  Path A.
- [`drivers/DNS_DRIVERS.md`](docs/drivers/DNS_DRIVERS.md) — removed
  orphaned PowerDNS stub. New §3 Windows DNS driver with both paths,
  write-through pattern, shared AXFR helper. Section numbering cleaned
  up (1–6).
- `docs/index.md`, `CLAUDE.md` — document maps point at the new files.

### Fixed

- `ipam.create_space` — return 409 on duplicate `ip_space` name instead
  of letting `UniqueViolationError` surface as a bare 500. Matches the
  pre-check pattern already in DHCP server-group CRUD; demo-seed
  retries are idempotent again.
- `frontend/src/lib/api.ts` — `ipamApi.updateBlock`'s Pick was missing
  `dhcp_server_group_id` and `dhcp_inherit_settings`, so the prod
  `tsc -b && vite build` failed even though dev `tsc --noEmit` passed.
- **Subnet inheritance editing bug** — editing a subnet back to
  "inherit from parent" used to still push records to the previously-
  pinned server. The inheritance walk now goes subnet → block
  ancestors → space and respects `dns_inherit_settings` at every level.
  Same walk applied in `services/dns/sync_check.py`.
- **Login crash on external user group assignment** — LDAP / OIDC /
  SAML logins were throwing `MissingGreenlet` during the group-
  membership replace step of `sync_external_user`. Fixed by:
  (1) adding `AsyncAttrs` mixin to `Base` so models expose
  `awaitable_attrs`; (2) awaiting `user.awaitable_attrs.groups` before
  assigning `user.groups = groups` in
  `backend/app/core/auth/user_sync.py` — SQLAlchemy's collection
  setter computes a diff against the currently-loaded collection, and
  that lazy-load under AsyncSession would otherwise raise.
- **`is_superadmin` vs RBAC wildcard mismatch** —
  externally-provisioned users with the built-in Superadmin role got
  403 from every `require_superadmin`-gated endpoint because the
  legacy `User.is_superadmin` flag defaults False and
  `sync_external_user` never flipped it. `require_superadmin` now
  admits either the legacy flag *or* any user whose groups → roles
  include an `action=*, resource_type=*` permission. Function-local
  import of `user_has_permission` dodges the circular import against
  `app.api.deps`.
- **Dynamic-lease mirrors are read-only** — `auto_from_lease=true` IPs
  now return 409 from update / delete endpoints and are skipped by
  bulk-edit. Prevents manual edits from being overwritten on the next
  lease pull.
- **IP delete cascades to DHCP static reservation** — on Windows the
  FK was set to NULL, orphaning the reservation. Now cascades
  correctly.
- **Tree chevrons** — DHCP / DNS server-group sidebars + VLANs tree
  swapped to the IPAM `[+] / [−]` boxed toggle for consistency. DNS
  zone-tree folder icons left alone.
- **DNS group expand-stuck** — selecting a zone no longer latches its
  group's expanded state.
- **IPAM address list** gains a `tags` column rendering clickable chips
  that populate the tag filter.
- **`seed_demo.py`** creates DNS group + zone and DHCP server group
  *first*, then wires both into the IP Space so blocks/subnets inherit
  by default.

### Security

- **CodeQL alert #13 (CWE-601, URL redirection from remote source)**
  closed. Previous attempts added `_safe_error_suffix()` and then a
  `urlparse`-based sanitiser defence, neither of which CodeQL's taint
  tracker recognises as sanitisers. Replaced with a closed-set
  allowlist: all redirect reasons are now selected from
  `_LOGIN_ERROR_REASONS` (a frozenset of literals); anything else
  becomes `"unknown"`. The three interpolation sites that previously
  threaded IdP error codes / exception reason fields into f-strings
  now pass fixed literals. Actual IdP error strings still land in the
  server log + audit row — only the URL-visible part is generic.

### Changed

- Backend + frontend: `make lint` now mandatory before push —
  CI-mirroring `make ci` target (added in 2026.04.16-3) catches
  formatter drift before it hits GitHub Actions.

---

## 2026.04.16-3 — 2026-04-16

Third same-day iteration. First wave of substantive post-alpha work:
external auth providers (LDAP / OIDC / SAML + RADIUS / TACACS+ with
backup-server failover), group-based RBAC enforced across every API
router, partial IPv6 (storage + UI + Kea Dhcp6), inherited-field
placeholders on edit modals, mobile-responsive layout, IPAM block/subnet
overlap validation, scheduled IPAM↔DNS auto-sync, bulk-edit DNS zone
assignment, shared zone-picker dropdown with primary / additional
grouping, and a `make ci` target that mirrors GitHub Actions locally.

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
