# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **GitHub Org:** https://github.com/spatiumddi  
> **Docs:** https://spatiumddi.github.io/spatiumddi/  
> **License:** Apache 2.0  
> **Package:** `spatiumddi` on PyPI  
> **Container registry:** `ghcr.io/spatiumddi/*`  

> **Read this file first.** This is the entry point for all Claude Code sessions on the SpatiumDDI project. It defines the project scope, the document map, and the non-negotiable conventions every generated file must follow.

---

## What Is SpatiumDDI?

SpatiumDDI is a production-grade, open-source **all-in-one DDI (DNS, DHCP, IPAM)** platform. It does not merely configure external DDI servers — it manages and runs the DHCP and DNS service containers directly. The control plane (FastAPI + PostgreSQL) is the source of truth; all managed service containers (Kea, BIND9) are deployed and configured by SpatiumDDI.

It can be deployed as individual containers, a full Docker Compose stack, a Kubernetes application, or as a **self-contained OS appliance image**. Supported on `linux/amd64` and `linux/arm64` (all Docker images must be built multi-arch).

It is designed to serve both power users (network engineers) and delegated department admins via a granular, group-based permission system. Every feature available in the UI is also available via REST API.

---

## Document Map

Always read the relevant spec doc(s) before writing code for a feature area.

| Document | What It Covers |
|---|---|
| `CLAUDE.md` | Index, conventions, non-negotiables |
| `docs/GETTING_STARTED.md` | Recommended setup order — server groups → zones / scopes → subnets → addresses |
| `docs/ARCHITECTURE.md` | System topology, component relationships, HA design |
| `docs/DATA_MODEL.md` | All database models, relationships, field definitions |
| `docs/API.md` | REST API conventions, pagination, error format, versioning |
| `docs/DEVELOPMENT.md` | Coding standards, test requirements, CI pipeline |
| `docs/OBSERVABILITY.md` | Logging (centralized + UI viewer), metrics, health dashboard, alerting |
| `docs/TROUBLESHOOTING.md` | Recovery recipes: accidentally deleted agent rows, password reset, subnet delete refused |
| `docs/features/IPAM.md` | IP Space/Block/Subnet/Address management, VLAN/VXLAN, custom fields, import/export, tree UI |
| `docs/features/DHCP.md` | DHCP servers, scopes, pools, static assignments, DDNS, caching, Windows DHCP (Path A) |
| `docs/features/DNS.md` | DNS servers, zones, records, views, server groups, blocking lists, DDNS, zone tree, Windows DNS (Path A + B), sync-with-servers reconciliation |
| `docs/features/AUTH.md` | Authentication, LDAP/OIDC/SAML, roles, group-scoped permissions, API tokens |
| `docs/features/ACME.md` | ACME DNS-01 provider — acme-dns-compatible HTTP surface for LE / public-CA cert issuance |
| `docs/PERMISSIONS.md` | RBAC permission grammar (`{action, resource_type, resource_id?}`), builtin roles, wildcards |
| `docs/features/SYSTEM_ADMIN.md` | System config, health dashboard, notifications, backup/restore, service control |
| `docs/deployment/APPLIANCE.md` | OS appliance build, base OS selection, licensing |
| `docs/deployment/DNS_AGENT.md` | DNS agent/container architecture — image layout, auto-registration, config sync, K8s shape |
| `docs/deployment/DOCKER.md` | Docker Compose setup, ports, first-time setup, TLS, HA, password reset |
| `docs/deployment/KUBERNETES.md` | Helm chart, operators, HPA, Ingress |
| `docs/deployment/BAREMETAL.md` | Ansible playbooks, systemd services, Patroni |
| `docs/deployment/WINDOWS.md` | Windows Server prerequisites — WinRM, service accounts (DnsAdmins / DHCP Users), firewall, zone dynamic-updates; shared by Windows DNS + Windows DHCP |
| `k8s/README.md` | Kubernetes manifest usage, HA PostgreSQL (CloudNativePG), Redis Sentinel |
| `k8s/base/` | Core K8s manifests (namespace, API, worker, frontend, migrate job) |
| `k8s/ha/` | HA add-ons: CloudNativePG cluster, Redis Sentinel, Patroni Compose |
| `docs/drivers/DHCP_DRIVERS.md` | Kea + Windows DHCP driver internals |
| `docs/drivers/DNS_DRIVERS.md` | BIND9 + Windows DNS (Path A + B) driver internals, incremental update strategy |

---

## Technology Stack (Summary)

| Layer | Technology |
|---|---|
| Backend API | Python 3.12+, FastAPI, SQLAlchemy 2.x (async), Alembic |
| Task Queue | Celery + Redis |
| Frontend | React 18 + TypeScript, Vite, shadcn/ui, Tailwind, React Query |
| Database | PostgreSQL 16 (HA via Patroni or CloudNativePG) |
| Cache / Sessions | Redis 7 |
| Auth | python-jose + bcrypt (local), ldap3 (LDAP), authlib (OIDC), python3-saml (SAML), pyrad (RADIUS), tacacs_plus (TACACS+); Fernet for secrets at rest |
| Logging | structlog → JSON → centralized log store (Loki / Elasticsearch) |
| Metrics | Prometheus + Grafana; InfluxDB v1/v2 push export |
| Containerization | Docker (multi-stage, amd64+arm64), Docker Compose, Kubernetes + Helm |
| Appliance OS | Alpine Linux (containers/appliance), Debian Stable (bare-metal ISO) |
| Logo / Assets | `docs/assets/logo.svg`, `docs/assets/logo-icon.svg` — also copied to `frontend/src/assets/` |

---

## Repo Layout

```
backend/app/            FastAPI app
  api/v1/               HTTP route handlers (ipam/, dns/, dhcp/, auth/, ...)
  models/               SQLAlchemy 2.x async models
  services/             Business logic (dns/, dhcp/, dns_io/, ipam_io/)
  drivers/dns/          DNS backend abstraction + BIND9 impl
  drivers/dhcp/         DHCP backend abstraction + Kea impl
  tasks/                Celery tasks (dns_health, dhcp_health, sweep_expired_leases, …)
  core/, db.py, config.py, celery_app.py
backend/alembic/        Migrations (tracked in git — do not re-add to .gitignore)
frontend/src/
  pages/                Top-level routes (ipam/, dns/, dhcp/, admin/, settings/)
  components/           Shared UI; shadcn/ui primitives under components/ui/
  lib/api.ts            All API clients (ipamApi, dnsApi, dhcpApi, …)
  hooks/                Incl. useSessionState (sessionStorage-backed useState)
agent/dns/              Standalone DNS agent (Python) + BIND9 container image
agent/dhcp/             Standalone DHCP agent (Python) + Kea container image
k8s/base/               Core manifests (api, worker, frontend, migrate)
k8s/{dns,dhcp}/         Per-service StatefulSets + services
k8s/ha/                 CloudNativePG, Redis Sentinel, Patroni
charts/spatiumddi/      Umbrella Helm chart (API + FE + worker + beat + migrate + Postgres/Redis subcharts + optional DNS/DHCP agents)
scripts/seed_demo.py    Demo data seeder
docs/                   Specs + Jekyll site (served at spatiumddi.github.io)
```

---

## Absolute Non-Negotiables

These rules apply to every file Claude Code generates. No exceptions.

1. **API-first**: Every UI action must work via REST API
2. **Async throughout**: No synchronous DB or network calls in request handlers
3. **Permissions enforced server-side**: The API always validates authorization independently of the UI
4. **Audit everything**: Every mutation is written to the append-only `audit_log` before the response is returned
5. **Config caching on agents**: DHCP and DNS containers must cache their last-known-good config locally and operate from cache if the control plane is unreachable
6. **No hardcoded secrets**: All credentials via env vars or mounted secrets
7. **Structured logs always**: Every log line is valid JSON with `timestamp`, `level`, `service`, `request_id`
8. **Incremental DNS updates**: DNS record changes use RFC 2136 DDNS or driver API — never a full server restart
9. **Idempotent tasks**: All Celery tasks must be safe to retry
10. **Driver abstraction**: DHCP and DNS backend logic never leaks into the service layer
11. **Multi-arch builds**: All Docker images must support `linux/amd64` and `linux/arm64`
12. **K8s manifests stay current**: When adding or changing services, update `k8s/base/` manifests and `k8s/README.md` to reflect the change

---

## Cross-cutting Patterns

Three patterns recur across the DNS and DHCP subsystems. Know these before adding a backend feature.

1. **Driver abstraction.** `backend/app/drivers/{dns,dhcp}/base.py` defines an ABC + neutral dataclasses (`ScopeDef`, `ZoneDef`, `ConfigBundle`, etc). Concrete drivers (`bind9.py`, `kea.py`) render backend-specific config from those dataclasses. The services layer only speaks to the ABC via the driver registry — never import a concrete driver from a service.

2. **ConfigBundle + ETag long-poll.** The control plane assembles a `ConfigBundle` from DB state and hashes it to a sha256 ETag (`backend/app/services/{dns,dhcp}/config_bundle.py`). The agent long-polls `/config` with its last-seen ETag; the server blocks until the ETag changes (or timeout) and only then returns a new bundle. When you add a field that affects rendered config, verify it flows into the bundle so the ETag shifts — otherwise agents will not pick up the change.

3. **Agent bootstrap + reconnection.** The agent joins with a pre-shared key (`DNS_AGENT_KEY` / `DHCP_AGENT_KEY`), exchanges it for a rotating JWT, and caches the JWT on disk. On **401 or 404** the agent re-bootstraps from the PSK (the 404 case covers stale server rows after a control-plane reset). The local config cache under `/var/lib/spatium-{dns,dhcp}-agent/` lets the service keep running if the control plane is unreachable (non-negotiable #5).

---

## Project Phase Roadmap

| Phase | Focus | Status |
|---|---|---|
| 1 | Core IPAM, local auth, user management, audit log, Docker Compose | **Done** — LDAP/OIDC/SAML + RADIUS/TACACS+ auth, group-based RBAC enforcement, bulk-edit tags/CF, inherited-field placeholders, mobile-responsive UI, and full IPv6 allocation all landed |
| 2 | DHCP (Kea), DNS (BIND9), DDNS, zone/subnet tree UI | **Done** — DNS core, Kea DHCPv4, subnet-level DDNS, agent-side Kea DDNS, block/space DDNS inheritance, and per-server zone serial reporting all landed |
| 3 | DNS views, server groups, blocking lists, VLAN/VXLAN, system admin panel, health dashboard | **In Progress** — DNS views storage, groups, blocklists, health checks, Trivy-clean + kind-AXFR acceptance tests landed; DNS Views end-to-end split-horizon wiring still ⬜ (see Future Phases) |
| 4 | OS appliance image, Terraform/Ansible providers, SAML, notifications, backup/restore, ACME (DNS-01 provider + embedded client) | **In Progress** (SAML SP landed in Wave A.4; alerts framework landed; appliance, providers, backup, ACME still pending) |
| 5 | Multi-tenancy, IP request workflows, import/export, advanced reporting | Not started |

### Current state

SpatiumDDI cut its alpha release `2026.04.16-1` on 2026-04-16 with IPAM, DNS (BIND9), and DHCP (Kea) all shipping. Subsequent releases landed Windows Server integration (`2026.04.18-1`, 2026-04-18), the **performance + polish + visibility** release (`2026.04.19-1`, 2026-04-19) — batched WinRM dispatch, DDNS pipeline, the Logs surface, subnet/block resize, subnet-scoped IP import, DHCP pool awareness, collision warnings, sync modals, dashboard heatmap, draggable modals, standardised header buttons — the 2026.04.20 IPv6 + DDNS closure work, and the **Kea HA + group-centric DHCP** release (`2026.04.21-2`, 2026-04-21) which shipped the full three-wave Kea HA story: end-to-end HA shake-out (peer URL resolution, port split, `status-get`, bootstrap reload), group-centric DHCP data model (scopes / pools / statics / classes live on `DHCPServerGroup`; HA is implicit with ≥ 2 Kea members), agent rendering fix (every prior Kea install was silently rendering `subnet4: []` due to a wire-shape bug), `PeerResolveWatcher` self-healing for peer IP drift, supervised Kea daemons, and standalone agent-only compose files for distributed deployments. For the full list see `CHANGELOG.md`. The forward-looking work is below.

### Auth waves A–D (landed after `2026.04.16-2`)

**Wave A — external auth providers.** GUI-configured LDAP / OIDC / SAML replacing the old env-var stubs.
- `AuthProvider` + `AuthGroupMapping` tables; Fernet-encrypted secrets (`backend/app/core/crypto.py`).
- Admin CRUD at `/api/v1/auth-providers` with per-type structured forms.
- **LDAP** — `ldap3`-based auth in `backend/app/core/auth/ldap.py`; wired into `/auth/login` as a password-grant fallthrough.
- **OIDC** — authorize / callback redirect flow with signed state+nonce cookie, discovery + JWKS caching, `authlib.jose` ID-token validation; login page lists enabled providers as "Sign in with …" buttons.
- **SAML** — `python3-saml` SP-side flow with HTTP-Redirect AuthnRequest, ACS POST binding, SP-metadata endpoint.
- Unified user sync at `backend/app/core/auth/user_sync.py`: creates/updates Users, replaces group membership with mapped groups, **rejects logins with no mapping match**.

**Wave B — RADIUS + TACACS+.** `pyrad` and `tacacs_plus` drivers added; share the same password-grant fallthrough as LDAP via `PASSWORD_PROVIDER_TYPES`. Admin test-connection probe for each.

**Backup servers for LDAP / RADIUS / TACACS+.** Each password provider's config now accepts an optional list of backup hosts (`config.backup_hosts` for LDAP, `config.backup_servers` for RADIUS/TACACS+). Each entry is `host` or `host:port`. LDAP uses `ldap3.ServerPool(pool_strategy=FIRST, active=True, exhaust=True)`; RADIUS and TACACS+ iterate the primary then backups manually, failing over on timeout / network error and stopping on any definitive auth answer. All backups share the primary's shared secret and timeout settings.

**Wave C — group-based RBAC enforcement.** Permission model (`{action, resource_type, resource_id?}`) with wildcard support; `user_has_permission()` / `require_permission()` / `require_any_permission()` / `require_resource_permission()` helpers in `backend/app/core/permissions.py`. Five builtin roles seeded at startup (Superadmin, Viewer, IPAM / DNS / DHCP Editor). `/api/v1/roles` CRUD + expanded `/api/v1/groups` CRUD with role/user assignment. Router-level gates applied across IPAM / DNS / DHCP / VLANs / custom-fields / settings / audit. Superadmin always bypasses. `RolesPage` + `GroupsPage` admin UI. See `docs/PERMISSIONS.md`.

**Wave D — UX polish + partial IPv6.**
- Per-field opt-in toggles on bulk-edit IPs (status/description/tags/CF/DNS zone individually) plus a "replace all tags" mode.
- `EditSubnetModal` + `EditBlockModal` now show inherited custom-field values as HTML `placeholder` with "inherited from block/space `<name>`" badges; `/api/v1/ipam/blocks/{id}/effective-fields` added for parity with the subnet endpoint.
- Mobile responsive — sidebar becomes a drawer on `<md` with backdrop, `Header` hamburger toggle, 10+ data tables wrapped in `overflow-x-auto` with `min-w`, all modals sized `max-w-[95vw]` on `<sm`.
- IPv6 partial — `DHCPScope.address_family` column + Kea driver `Dhcp6` branch; subnet create skips the v6 broadcast row; `_sync_dns_record` emits AAAA + PTR in `ip6.arpa`; `/next-address` returns 409 on v6 (EUI-64/hash allocation is a future enhancement). Dhcp6 option-name translation now lands in `backend/app/drivers/dhcp/kea.py` via `_KEA_OPTION_NAMES_V6` + `_DHCP4_ONLY_OPTION_NAMES`; v4-only options (`routers`, `broadcast-address`, `mtu`, `time-offset`, `domain-name`, tftp-*) are dropped from v6 scopes with a warning log.

### IPAM polish (shipped alongside the waves)

- **Block overlap validation** — `_assert_no_block_overlap` rejects same-level duplicates and CIDR overlaps in `create_block` + the reparent path in `update_block`.
- **Scheduled IPAM ↔ DNS auto-sync** — opt-in Celery beat task `app.tasks.ipam_dns_sync.auto_sync_ipam_dns`. Beat fires every 60 s; the task itself gates on `PlatformSettings.dns_auto_sync_enabled` + `dns_auto_sync_interval_minutes`, so cadence changes in the UI take effect without restarting beat. Optionally deletes stale auto-generated records.
- **Shared `ZoneOptions` dropdown** (`frontend/src/pages/ipam/IPAMPage.tsx`) — renders primary zone first, `<optgroup label="Additional zones">` below; applied in Create / Edit / Bulk-edit IP modals. Zone picker is restricted to the subnet's explicit primary + additional zones when any are pinned.
- **Bulk-edit DNS zone** — new `dns_zone_id` field on `IPAddressBulkChanges`; each selected IP routes through `_sync_dns_record` for move / create / delete.

### 2026.04.19-1 landings (performance, polish, visibility)

- **Batched WinRM dispatch.** `apply_record_changes` on DNSDriver + `apply_reservations` / `remove_reservations` / `apply_exclusions` on DHCPDriver. Windows drivers override with real batching: DNS at `_WINRM_BATCH_SIZE = 6` ops/chunk (ceiling given `pywinrm.run_ps` encodes UTF-16-LE + base64 through `powershell -EncodedCommand` as a single 8191-char CMD.EXE line; see comment in `drivers/dns/windows.py`), DHCP at `_WINRM_BATCH_SIZE = 30`. Each chunk ships a compact data-only JSON payload + one shared PS wrapper with per-op try/catch. BIND9 / Kea inherit the batch interface via the default loop impls. 40-record Sync DNS went from ~3 min to ~5 s.
- **Logs surface.** New `/logs` page and `api/v1/logs/router.py`. Two tabs:
  - **Event Log** — `POST /logs/query` runs `Get-WinEvent -FilterHashtable` server-side via `app/drivers/windows_events.py`. Drivers expose inventory through `available_log_names()` + `get_events()`: `WindowsDNSDriver` returns `DNS Server` + `Microsoft-Windows-DNSServer/Audit`; `WindowsDHCPReadOnlyDriver` returns `Operational` + `FilterNotifications`. Filters keyed into React Query so tab entry + filter changes auto-fetch; Refresh button calls `refetch()`.
  - **DHCP audit** — `POST /logs/dhcp-audit` reads `C:\Windows\System32\dhcp\DhcpSrvLog-<Day>.log` over WinRM via `app/drivers/windows_dhcp_audit.py`. UTF-16 + ASCII both handled. Event-code → human label map; unknown codes come through as `Code <n>`.
- **IPAM subnet + block resize.** Grow-only. Preview + commit endpoints at `/ipam/subnets/{id}/resize/{preview,commit}` and `/ipam/blocks/{id}/...`. Preview returns blast-radius summary + `conflicts[]`; commit requires typed-CIDR confirmation + holds a pg advisory lock + re-runs every validation pre-mutation. Default-named network/broadcast placeholder rows recreated at new boundaries; renamed/DNS-bearing rows preserved. Cross-subtree overlap scan (not just siblings). `ResizeSubnetModal` / `ResizeBlockModal` in frontend.
- **Subnet-scoped IP address import.** `POST /ipam/import/addresses/{preview,commit}`. Parser auto-routes CSV / JSON / XLSX rows (`address`/`ip` → addresses, `network` → subnets); unrecognised columns drop into `custom_fields`. Validates each IP against the subnet CIDR. `AddressImportModal` + combined `Import / Export` dropdown on the subnet header.
- **DHCP pool awareness in IPAM.**
  - `_load_dynamic_pool_ranges` + `_ip_int_in_dynamic_pool` helpers in `backend/app/api/v1/ipam/router.py`. `create_address` returns 422 when `body.address` lands inside a dynamic pool (excluded/reserved pools still allow manual allocation). `_pick_next_available_ip` hoisted from `allocate_next_ip` so both the commit path and the new `GET /ipam/subnets/{id}/next-ip-preview` share the same dynamic-skip semantics.
  - Frontend `tableRows` interleaves ▼ start / ▲ end pool boundary rows with IP rows (dynamic cyan, reserved violet, excluded zinc). `AddAddressModal` "next" mode shows the preview IP inline; manual mode warns + disables submit when the typed IP hits a dynamic range.
- **IP assignment collision warnings.** `_normalize_mac` + `_check_ip_collisions` helpers + `force: bool = False` on `IPAddressCreate` / `IPAddressUpdate` / `NextIPRequest`. 409 with `{warnings, requires_confirmation}` when not forced. Update path uses `model_dump(exclude_unset=True)` so unchanged rows don't surface pre-existing collisions. Shared `CollisionWarning` + `CollisionWarningBanner` in `IPAMPage.tsx`; submit button flips to "Allocate anyway" / "Save anyway" on collision.
- **DHCP stale-lease absence-delete.** `pull_leases` now finds every active `DHCPLease` for this server whose IP wasn't in the wire response and deletes both the lease row and its `auto_from_lease=True` IPAM mirror. `PullLeasesResult` / `SyncLeasesResponse` / scheduled-task audit rows gain `removed` + `ipam_revoked` counters. The time-based `dhcp_lease_cleanup` sweep still handles between-poll expiry.
- **Sync menu + DHCP sync modals.** Replaces the standalone "Sync DNS" button on the subnet detail with a `[Sync ▾]` dropdown (DNS / DHCP / All). `DhcpSyncModal` fans out `POST /dhcp/servers/{id}/sync-leases` across every unique server backing a scope in the subnet, shows per-server counters. `SyncAllModal` combines DHCP results + DNS drift summary in one modal with a "Review DNS changes…" button that chains into the existing `DnsSyncModal`.
- **Refresh buttons** on DNS zone records, IPAM subnet detail, and the VLANs sidebar — each invalidates every relevant React Query key.
- **Dashboard rewrite.** Six KPI cards + **Subnet Utilization Heatmap** (every managed subnet = one grid cell coloured by utilization, click-through to IPAM) + Top Subnets + Live Activity feed (15 s auto-refresh, action-family colour coding) + DNS/DHCP service panel. **Time-series panels landed post-release** (2026-04-22 metrics MVP) — two Recharts cards under the activity row render DNS query rate + DHCP traffic from agent-driven `metric_sample` tables.
- **Draggable modals.** Seven per-page `function Modal({...})` copies collapsed into a single `<Modal>` at `frontend/src/components/ui/modal.tsx` + `use-draggable-modal.ts` (utility split out so Vite fast-refresh doesn't warn on mixed exports). Title bar is a drag handle; backdrop is `bg-black/20` so the page behind stays readable; Esc closes. Custom modal shapes (header with border-b + footer slot) use `useDraggableModal(onClose)` + `MODAL_BACKDROP_CLS` directly. Migrated across admin, DNS, DHCP, VLANs, IPAM + `ResizeModals` + `ImportExportModals` + inline `DnsSyncModal`.
- **Standardised header buttons.** `<HeaderButton>` primitive with three variants (`secondary` / `primary` / `destructive`) on a shared `inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm` base. Logical left→right ordering applied everywhere: `[Refresh] [Sync …] [Import] [Export] [misc reads] [Edit] [Resize] [Delete] [+ Primary]`. DNS / DHCP / VLANs were smaller (`text-xs`); all bumped to match IPAM's dominant size.

### 2026.04.20 roadmap completions

Phase 1 IPv6 closure + the Phase 2/3 DDNS / zone-state / CI-hardening items all landed in this window.

- **Full IPv6 `/next-address`** — EUI-64 + random /128 + sequential modes selected via `Subnet.ipv6_allocation_policy`; `_eui64_from_mac` in `backend/app/api/v1/ipam/router.py` implements RFC 4291 §2.5.1 Modified EUI-64 (u/l bit flip + `fffe` insertion); random /128 uses `secrets.randbits` with collision retry; dynamic-pool respect applies on v6 too. Test coverage in `backend/tests/test_ipv6_allocation.py` includes the RFC 4291 Appendix A worked example. Closes Phase 1.
- **DDNS pipeline (subnet-level)** — `Subnet.ddns_enabled` / `ddns_hostname_policy` / `ddns_domain_override` / `ddns_ttl`; `services/dns/ddns.py` resolves hostname per policy and calls the same `_sync_dns_record` path static allocations use; `pull_leases.py` + `dhcp_lease_cleanup.py` are the two integration points.
- **Agent-side lease-event DDNS for Kea** — `apply_ddns_for_lease` + `revoke_ddns_for_lease` wired into `POST /api/v1/dhcp/agents/lease-events` (commit `bad8cf3`), so Kea lease events drive DNS updates with the same semantics as the poll-based Windows DHCP path.
- **Block/space inheritance for DDNS settings** — `IPSpace` + `IPBlock` carry the four DDNS fields; `Subnet` / `IPBlock` carry `ddns_inherit_settings`; `services/dns/ddns.resolve_effective_ddns` walks subnet → block → space and is consulted by both the hostname resolver and the apply path (commit `a29d4fe`).
- **Per-server zone serial reporting** — `DNSServerZoneState` table + `POST /dns/agents/zone-state` for agents (agent reports after each successful apply in `agent/dns/spatium_dns_agent/sync.py`) + `GET /dns/groups/{gid}/zones/{zid}/server-state` for the UI + `ZoneSyncPill` on the zone detail header showing per-server convergence against the current SOA serial.
- **Trivy-clean + kind-AXFR acceptance tests for the agent images** — Trivy now enforces HIGH/CRITICAL (with `ignore-unfixed: true`) on both `build-dns-images.yml` and `build-dhcp-images.yml`; kind-based installation + `dig version.bind CH TXT` smoke test runs on PR via the new `.github/workflows/agent-e2e.yml` — spins up a kind cluster via `helm/kind-action@v1`, installs the umbrella chart with `dnsAgents.enabled=true`, port-forwards the API for `/health/live`, and checks the DNS agent pod isn't crash-looping.

### Future Phases — Tracked Items

- ✅ **Windows DNS — Path A (RFC 2136, agentless)** — `WindowsDNSDriver`
  in `backend/app/drivers/dns/windows.py`. Record CRUD only (A / AAAA /
  CNAME / MX / TXT / PTR / SRV / NS / TLSA) via dnspython over RFC 2136;
  zones are managed externally in Windows DNS Manager. Optional TSIG
  signing; GSS-TSIG and SIG(0) are Path B. Control plane sends updates
  directly; `record_ops.enqueue_record_op` short-circuits the agent queue
  for servers whose driver is in `AGENTLESS_DRIVERS`.
- ⬜ **Windows DNS — Path B (WinRM + PowerShell, full CRUD)** — zone
  creation / edit / delete, view config, server-level options. Uses
  `pypsrp`/`pywinrm` to invoke the `DnsServer` PowerShell module on the
  DC. Requires WinRM-over-HTTPS, a service account in `DnsAdmins`, and a
  credential-handling UI in the server form. Secure-only DDNS zones
  become manageable via GSS-TSIG once Kerberos ticket acquisition lands.
- ✅ **Windows DHCP — Path A (WinRM, read-only lease monitoring)** —
  `WindowsDHCPReadOnlyDriver` in `backend/app/drivers/dhcp/windows.py`.
  Implements `get_leases` via `Get-DhcpServerv4Scope` /
  `Get-DhcpServerv4Lease` over WinRM (`pywinrm`). All write methods
  (`apply_config`, `reload`, `restart`, `validate_config`) raise
  `NotImplementedError` — Path A is strictly read-only. Credentials are
  stored Fernet-encrypted on `DHCPServer.credentials_encrypted`. Driver
  registry gains `AGENTLESS_DRIVERS` + `READ_ONLY_DRIVERS` sets mirroring
  the DNS side. Scheduled Celery beat task
  `app.tasks.dhcp_pull_leases.auto_pull_dhcp_leases` fires every 60 s;
  task gates on `PlatformSettings.dhcp_pull_leases_enabled` /
  `_interval_minutes`. Leases are upserted by `(server_id, ip_address)`
  and mirrored into IPAM as `status="dhcp"` + `auto_from_lease=True` rows
  when the lease IP falls inside a known subnet; the existing lease-
  cleanup sweep handles expiry uniformly. Manual "Sync Leases" button
  on the server detail header for agentless drivers. Beat ticks every
  10 s and the per-run interval is stored in seconds
  (`PlatformSettings.dhcp_pull_leases_interval_seconds`, default 15 s)
  so operators can tune near-real-time IPAM population — Windows
  DHCP has no streaming primitive, so short-interval polling is the
  practical upper bound without putting an agent on the DC.
- ⬜ **Windows DHCP — Path B (WinRM + PowerShell, full CRUD)** — scope
  / reservation / client-class / option CRUD via `Add-DhcpServerv4Scope`,
  `Add-DhcpServerv4Reservation`, etc. Layered on top of Path A in the
  same driver class. Service account must be in `DHCP Administrators`
  rather than `DHCP Users`. Much bigger scope than DNS Path B since
  there's no wire-level admin protocol; every scope field becomes a
  cmdlet call.
- ⬜ IP discovery — ping sweep + ARP scan Celery task; flags `discovered` status; reconciliation report (see `docs/features/IPAM.md §8`)
- ✅ **OUI/vendor lookup** — opt-in IEEE OUI database fetched by
  `app.tasks.oui_update.auto_update_oui_database` (hourly beat, task
  honours `PlatformSettings.oui_lookup_enabled` +
  `oui_update_interval_hours`, default 24 h). `oui_vendor(prefix
  CHAR(6) PK, vendor_name, updated_at)` replaced atomically each run
  so lookups always see a consistent snapshot. `services/oui.py`
  exposes `bulk_lookup_vendors` + `normalize_mac_key`; IPAM's
  `list_addresses` and DHCP's `list_leases` use them to attach a
  `vendor` field. Settings → IPAM → OUI Vendor Lookup carries the
  toggle + interval + "Refresh Now" (queues
  `update_oui_database_now`). MACs render as `aa:bb:cc:dd:ee:ff
  (Cisco Systems)` in the IP table + DHCP leases; feature off =
  vendor null + UI falls back to bare MAC.
- ⬜ SNMP polling / network device management — ARP table polling for IP discovery (see `docs/features/IPAM.md §13`)
- ✅ **API tokens with auto-expiry** (Phase 1 close-out) — `APIToken`
  model already existed; this session wires the create/list/revoke
  router, extends `get_current_user` to accept `spddi_*` bearer
  tokens alongside JWTs, tracks `last_used_at`, and adds an admin UI
  at `/admin/api-tokens`. Tokens are hashed at rest (SHA-256) and
  shown in plaintext once at creation.
- ✅ **Syslog + event forwarding** — every successful `AuditLog`
  commit is optionally forwarded to an external syslog target
  (RFC 5424 over UDP / TCP) and/or a generic HTTP webhook. Hook is
  a SQLAlchemy `after_commit` session listener in
  `services/audit_forward.py`; delivery is fire-and-forget on a
  dedicated asyncio task so audit writes never block on network I/O.
  Configured in Settings; targets live on `PlatformSettings`
  (single syslog + single webhook for now — multi-target moves to a
  dedicated table when a second customer asks).
- ⬜ **DNS Views — end-to-end split-horizon wiring** — `DNSView`
  model + CRUD ship today, but the BIND9 driver doesn't wrap zones
  in `view { match-clients …; zone { … }; }` blocks and record
  CRUD has no `view_id` assignment UI. The storage side is ready;
  what's missing is driver rendering + record-level view selection
  + UI binding on the record form. Phase 3.
- ✅ **DHCP state failover (Kea HA)** — under the group-centric data
  model (shipped 2026.04.21-2), a `DHCPServerGroup` with two Kea
  members is implicitly an HA pair. HA tuning (mode, heartbeat /
  max-response / max-ack / max-unacked, auto-failover) lives on the
  group; per-peer URL lives on each `DHCPServer.ha_peer_url`.
  `DHCPFailoverChannel` is gone — merged into the group.
  `ConfigBundle` carries a `FailoverConfig` when the server's group
  is an HA pair; the agent's `render_kea.py` injects `libdhcp_ha.so`
  + `high-availability` alongside the always-loaded
  `libdhcp_lease_cmds.so` hook. Peer URLs are resolved agent-side
  (`_resolve_peer_url`) before render because Kea's Boost asio
  parser only accepts IP literals. Kea image splits ports: `:8000`
  is owned by the HA hook's `CmdHttpListener`, `:8544` by the
  operator-facing `kea-ctrl-agent`. Agent supervises an
  `HAStatusPoller` thread that calls `status-get` (Kea 2.6 folded
  HA state into the generic status command; pre-2.6 `ha-status-get`
  shapes still accepted) and POSTs state to
  `/api/v1/dhcp/agents/ha-status`. Bootstrap-from-cache issues
  `config-reload` with retry so Kea picks up HA on agent restart.
  HA config lives in the main DHCP tab's server-group edit modal;
  dashboard shows one row per HA pair with a live state dot per
  peer. Scope mirroring is automatic — all servers in a group
  render the same scopes, pools, statics, and client classes.
  **`PeerResolveWatcher`** re-resolves peer hostnames every 30s
  and triggers render + reload on IP drift, so compose
  `--force-recreate` / k8s pod restarts heal without operator
  action. Kea daemons (`kea-dhcp4` + `kea-ctrl-agent`) run under
  per-daemon supervise loops with stale-PID-file scrubbing, 5-in-
  30s crash-loop guards, and SIGTERM forwarding. **Deferred
  follow-ups:**
  - **Kea version skew guard.** The `status-get` HA shape shifted
    between Kea 2.4 and 2.6. Pairing peers on mismatched Kea
    versions is accepted today. Cheap fix: ship Kea version in the
    heartbeat, reject group membership changes if peers differ.
  - **DDNS double-write under HA.** Agent-side DDNS
    (`apply_ddns_for_lease`) doesn't gate on HA state — if the
    standby ever serves a lease (pre-sync window, partner-down),
    both peers could try to write the same RR. Kea's hook
    coordinates DHCP serving but not our DDNS pipeline.
  - **State-transition actions** (`ha-maintenance-start`,
    `ha-continue`, force-sync) — observable today but operators
    can't drive the HA state machine from the UI.
  - **Peer compatibility validation** (refuse groups with ≥ 3 Kea
    members because `libdhcp_ha.so` only supports pairs), per-pool
    HA scope tuning for load-balancing.
  - **HA e2e test.** `.github/workflows/agent-e2e.yml` stands up
    a single-agent DNS pair today; an HA DHCP variant would have
    caught the bootstrap / port-split / `status-get` / wire-shape
    regressions we hit in 2026.04.21-2.
- ✅ **DHCP MAC blocklist (group-global)** — `DHCPMACBlock` table
  hung off `DHCPServerGroup`, unique on `(group_id, mac_address)`,
  indexed on `expires_at`. Per-row fields: `mac_address` (MACADDR),
  `reason` (`rogue` / `lost_stolen` / `quarantine` / `policy` /
  `other`), `description`, `enabled`, `expires_at`, `created_at` +
  `created_by_user_id`, `updated_by_user_id`, `last_match_at`,
  `match_count`. `MACBlockDef` added to `ConfigBundle`; the control
  plane strips `enabled=False` + expired rows pre-render so the
  ETag naturally shifts on expiry transitions and agents long-poll
  pick it up. **Kea path**: agent's `render_kea.py` wraps the active
  MAC list in Kea's reserved `DROP` client class via an OR-ed
  `hexstring(pkt4.mac, ':') == '...'` expression — packets are
  silently dropped before allocation; if the operator hand-defined
  a `DROP` class the renderer steps aside rather than clobber it.
  **Windows DHCP path**: `WindowsDHCPReadOnlyDriver.sync_mac_blocks`
  diffs desired-set against `Get-DhcpServerv4Filter -List Deny` and
  ships one batched PS script per WinRM round trip. Beat tick every
  60 s (`app.tasks.dhcp_mac_blocks.sync_dhcp_mac_blocks`) reconciles
  Windows servers — Kea doesn't need the task since blocklist
  changes flow through the bundle. CRUD at `/api/v1/dhcp/server-
  groups/{gid}/mac-blocks` (list + create) + `/api/v1/dhcp/mac-
  blocks/{id}` (update + delete). List endpoint joins OUI vendor
  lookup and an `IPAddress.mac_address` cross-reference so the UI
  surfaces vendor + any IPAM rows tied to the blocked MAC.
  Frontend `MacBlocksTab` on the DHCP server detail view (mirrors
  where `ClientClassesTab` lives): filterable table, reason pills,
  status pill (active / disabled / expired), IPAM link-outs, add /
  edit modal accepting any common MAC format. Permission gate
  `dhcp_mac_block`; built-in "DHCP Editor" role gets it. Migration
  `d4a18b20e3c7_dhcp_mac_blocks`. **Deferred follow-ups:**
  - **Bulk import / paste** from CSV — the current UI is one-at-a-
    time; large blocklists need a paste-a-list path.
  - **Per-scope restriction.** Kea's class/pool pinning supports
    "block this MAC only on subnet X" — would mean dropping the
    `DROP` shortcut in favour of per-pool `client-class` + a
    per-subnet class. Windows can't do per-scope deny at all
    (deny-list is server-global). Group-global is the right
    default; per-scope is a Phase 5 precision tool.
  - **`last_match_at` + `match_count` wiring.** The columns exist
    but nothing writes to them yet. Kea has a lease-event hook and
    Windows has a `FilterNotifications` event channel we already
    surface in Logs — either can drive the counter.
  - **HA pair compatibility**: the beat task iterates every
    agentless server regardless of group HA state. Should still be
    idempotent but worth a targeted test when HA + Windows DHCP
    land together.

- ✅ **Alerts framework (v1)** — `AlertRule` + `AlertEvent` tables;
  evaluator at `services/alerts.py:evaluate_all()` runs from
  `app.tasks.alerts.evaluate_alerts` on a 60 s beat tick. Two rule
  types on launch: `subnet_utilization` (honours
  `PlatformSettings.utilization_max_prefix_*` so PTP / loopback
  subnets can't trip the alarm) and `server_unreachable` (DNS /
  DHCP / any). Delivery reuses the audit-forward syslog + webhook
  send helpers against the platform-level targets; per-rule
  override of targets deferred. Admin UI at `/admin/alerts` with
  live events viewer + "Evaluate now". Email (SMTP) and SNMP trap
  channels are the remaining v2 work — needs SMTP config infra that
  doesn't exist yet, and SNMP is its own dependency footprint.
- ✅ **Dashboard time-series (MVP)** — agent-driven DNS query rate +
  DHCP traffic charts, self-contained (no Prometheus / InfluxDB
  required). BIND9 agents poll `statistics-channels` XMLv3 on
  `127.0.0.1:8053` (injected into rendered `named.conf`); Kea
  agents poll `statistic-get-all` over the existing control socket.
  Both report per-60s-bucket deltas to `POST
  /api/v1/{dns,dhcp}/agents/metrics`. Counter resets on daemon
  restart are detected agent-side (`delta < 0`) and drop the
  bucket rather than emitting a phantom spike. Storage in two
  narrow tables `dns_metric_sample` + `dhcp_metric_sample` keyed
  on `(server_id, bucket_at)`. Dashboard reads `GET
  /api/v1/metrics/{dns,dhcp}/timeseries?window={1h|6h|24h|7d}`
  with server-side `date_bin` downsampling (60 s for ≤24 h,
  5 min for 7 d). Retention by nightly `prune_metric_samples`
  Celery task (default 7 d). Migration
  `bd4f2a91c7e3_metric_samples`. **Deferred follow-ups:**
  - **Windows DNS / DHCP stats** — needs `Get-DnsServerStatistics`
    + `Get-DhcpServerv4Statistics` driver methods over WinRM.
    Chart currently shows "no data yet" for Windows-only
    deployments.
  - **Prometheus export** of the same samples — one Gauge per
    column with `server_id` label would make the existing
    `/metrics` endpoint a full-featured scrape target for
    operators who prefer Grafana.
  - **InfluxDB push export** (`InfluxDBTarget` spec in
    `docs/features/SYSTEM_ADMIN.md §8.2`) — shape exists, writer
    still pending.
  - **Per-qtype** (BIND) / **per-subnet** (Kea) breakdowns —
    `statistic-get-all` already carries per-subnet counters; the
    agent strips them for MVP. Adding them is column-only, no
    protocol change.
  - **Alert rule types `dns_query_rate` / `dhcp_lease_rate`** —
    threshold-based alerts keyed off the timeseries data.
- ⬜ **IPAM template classes** — reusable stamp templates that
  carry default tags, custom-field values, DNS / DHCP group
  assignments, and optional sub-subnet layouts. Applied to a block
  or subnet on create; existing instances can re-apply to pick up
  template drift. Phase 5 — belongs alongside advanced reporting /
  multi-tenancy, once the base inheritance story is fully bedded
  down.
- ⬜ **Move IP block / space across IP spaces** — operator-driven
  relocation of a block (and everything under it: child blocks,
  subnets, addresses) into a different `IPSpace`. Preview + commit
  endpoints under `/api/v1/ipam/blocks/{id}/move/{preview,commit}`
  mirroring the existing resize UX. **Decision points still open**
  — confirm before building:
  1. **Scope**: block-move only (recursive through descendants),
     not space-merge. Moving a whole space means just moving its
     top-level blocks one-by-one — same code path.
  2. **Integration-owned rows**: refuse when any descendant has
     `kubernetes_cluster_id` / `docker_host_id` / future-integration
     FKs set — the reconciler would immediately re-create the rows
     in the original space, so moving them is a no-op that
     desynchronises provenance. Preview flags these; commit 409s.
  3. **Atomicity**: single transaction with `SELECT … FOR UPDATE`
     on the block subtree; overlap re-check against the target
     space's existing blocks before the writes land.
  4. **Target parent**: optional — if the operator picks a parent
     block in the target space, validate the moved block is a
     strict subset of it. If omitted, moved block lands at top
     level of the target space and the standard overlap-reparent
     logic applies (can pull existing top-level siblings under it
     if it's a supernet, by the same rule `create_block` uses).
  5. **UI**: `MoveBlockModal` on the block detail header; typed
     CIDR confirmation like resize; preview returns counts
     (blocks, subnets, addresses, integration-owned blockers).
  Phase 5-ish — no urgent driver for it, but it's the natural
  cleanup tool once operators start reorganising spaces after
  integrations have seeded them.
- ✅ **ACME / Let's Encrypt — DNS-01 provider for external clients**
  — landed in the 2026.04.22-1 wave. Lets certbot / lego / acme.sh
  on a client box prove control of a FQDN hosted in (or delegated
  to) a SpatiumDDI-managed zone and issue public certs (wildcards
  included). Implementation shipped as an `acme-dns`-compatible HTTP
  surface under `/api/v1/acme/` with the following pieces:
  - **Data model** — `ACMEAccount` (`app/models/acme.py`,
    migration `ac3e1f0d8b42`): `username` + `password_hash`
    (bcrypt), UUID `subdomain`, FK `zone_id`, optional
    `allowed_source_cidrs`, `last_used_at`. Credentials shown once
    at registration; only the hash persists.
  - **Endpoints** — `POST /register` (JWT auth, gated by
    `manage_acme` / `write:acme_account`), `POST /update` +
    `DELETE /update` (acme-dns `X-Api-User` / `X-Api-Key` auth,
    subdomain must match authenticated account), `GET /accounts`
    / `DELETE /accounts/{id}` admin ops.
  - **Record write** — routes through the normal
    `enqueue_record_op` pipeline so TXT records land in the UI +
    audit log + DDNS pipeline uniformly. `subnet_id`-equivalent
    routing here is the zone's primary server.
  - **Propagation wait** — `/update` blocks up to 30 s polling
    `DNSRecordOp.state` until `applied`, so the CA's subsequent
    DNS-01 poll finds the record live. Returns 504 on timeout,
    502 on primary driver error.
  - **Wildcard support** — keeps the 2 most-recent TXT values per
    subdomain so wildcard + base cert issuance (which presents two
    different validation tokens at the same record name) works.
  - **Protocol choice** — `acme-dns` compat means certbot
    (`dns-acmedns` plugin), lego, acme.sh all work out of the box
    with no custom plugin. Delegation pattern documented in
    `docs/features/ACME.md` — operator CNAMEs
    `_acme-challenge.<their-fqdn>` to
    `<account.subdomain>.<our-acme-zone>` and delegates the small
    subzone via NS records, so a leaked credential can't rewrite
    anything outside that label.
  - **Audit** — every register / update / delete / revoke lands in
    `audit_log`. TXT values logged as a 12-char prefix only, never
    in full. Credentials never logged, hashed or otherwise.
  - **Tests** — `backend/tests/test_acme.py`, 24 tests covering
    crypto roundtrip, source-CIDR allowlist edge cases, HTTP auth
    paths, wildcard rolling window, revocation, cleanup.

  **Deferred follow-ups:**
  - **Dedicated rate-limit bucket** for `/api/v1/acme/*` + Fail2Ban-
    style temp-ban on repeated `/update` auth failures. Today the
    endpoint rides the general API rate limit (none in v1; add
    slowapi or similar when it lands).
  - **Per-op `asyncio.Event` ack channel** to replace the DB-polling
    `wait_for_op_applied` loop. ~250 ms latency savings on the
    typical path; the polling approach is simple and correct but
    opens/closes a DB session every 500 ms.
  - **Celery janitor task** for the 24 h stale TXT sweep — service-
    level function `acme.sweep_stale_txt_records` is written and
    unit-testable; wiring it into the beat schedule is pending.
  - **Metric exposure** for ACME activity (registrations,
    /update rate, sweep counts) on the admin dashboard.

- ⬜ **ACME embedded client — certs for SpatiumDDI's own services**
  — *separate from the DNS-01 provider above.* SpatiumDDI runs an
  embedded ACME client (candidate libs: `acme` / `certbot-core`
  from the certbot project, or Go's `acme/autocert` if we ever port
  chunks; Python-native is the fit today) that issues and auto-
  renews certs for:
  - the frontend HTTPS listener (today configured by hand in the
    reverse-proxy layer — appliance deployments need this turn-key);
  - BIND9 DoT / DoH listeners on the DNS agent (when those ship);
  - the Kea control agent TLS (if operators expose it externally);
  - optionally, the API's own TLS when running without an upstream
    proxy (small deployments).

  Uses our **own DNS-01 provider** (the entry above) for the
  challenge — SpatiumDDI becomes its own ACME solver, which is a
  nice dogfooding story. Or HTTP-01 for the frontend HTTPS listener
  if port 80 is reachable. Certs land in a new `Certificate` table
  (`fqdn`, `san_list`, `issued_at`, `expires_at`, `pem`,
  `chain_pem`, `private_key_pem` Fernet-encrypted) and get pushed
  to consuming agents via the same ConfigBundle mechanism the rest
  of the config flows through — so a DoT listener on BIND9 picks up
  a renewed cert without a manual deploy.

  Renewal task: Celery beat every 24 h, renews anything <30 days
  from expiry. Alert rule `certificate_expiring` fires through the
  alerts framework at <14 days (soft) and <3 days (critical) if
  renewal has failed — reuses the framework we just shipped.

  Phase 4 — natural bundle with the OS appliance item (`docs/
  deployment/APPLIANCE.md`) since shipping a self-configuring
  appliance means owning the cert story end-to-end.

- ⬜ **Cloud DNS driver family — Route 53 / Azure DNS / Cisco DNA**
  — each is its own driver module implementing the DNS driver ABC.
  Route 53 via `boto3` is the simplest entry point (stable REST
  API, well-documented record-type mapping). Azure DNS via
  `azure-mgmt-dns`. Cisco DNA is its own adventure — it's an
  enterprise controller, not a DNS service, and its "DNS" touches
  SD-Access rather than the public resolver tree. Phase 4 — pairs
  with the Terraform / Ansible providers already on the roadmap.

- ⬜ **Kubernetes integration (read-only cluster mirror).** Poll one
  or more Kubernetes clusters with a read-only service-account
  token and mirror the stable bits of cluster state into SpatiumDDI.
  **Deliberately not mirroring pod IPs** — they churn too fast to
  be useful in IPAM. The value is reserving cluster CIDRs in IPAM
  (so operators can't accidentally overlap), surfacing LoadBalancer
  VIPs with their owning `namespace/service`, and auto-generating
  DNS records for Ingress hostnames. Pure pull, SpatiumDDI never
  writes to the cluster.

  **UX shape (agreed):**
  - **Settings → Integrations** is a new top-level settings section
    that hosts one card per integration type. Each card carries its
    own independent enable toggle on `PlatformSettings` (no single
    master `integrations_enabled` flag — granular by design so
    enabling Kubernetes doesn't also enable a future Terraform
    Cloud integration). When an integration's toggle is on, its
    corresponding top-level sidebar nav item appears (flat, not
    nested under an "Integrations" meta-item — matches how DHCP /
    DNS already surface).
  - **`KubernetesCluster` rows** are the per-cluster config, not
    PlatformSettings. Each cluster binds to exactly one
    `IPAMSpace` (required — discovered IPs / blocks land there)
    and optionally one `DNSServerGroup` (for ingress → DNS sync).
    Many clusters supported, same or different space/group per
    cluster.
  - **Credentials**: API server URL + CA bundle PEM + bearer token,
    with the token Fernet-encrypted at rest alongside the other
    driver creds (`DHCPServer.credentials_encrypted`,
    `DNSServer.credentials_encrypted`).
  - **Modal** shows an embedded setup guide with the exact
    ServiceAccount / ClusterRole / ClusterRoleBinding YAML plus
    `kubectl` commands to extract the token + CA bundle. Cluster
    version + node count round-trip via a **Test Connection**
    button before save.
  - **Operator also enters pod CIDR + service CIDR** in the form —
    service CIDR is not reliably extractable from the API; pod CIDR
    could be derived from Node objects but asking is simpler and
    matches how the DHCP / DNS server forms work.

  **Phased scope:**

  - ✅ **Phase 1a — Scaffolding.** Settings → Integrations UX +
    per-integration toggle on PlatformSettings + sidebar gating.
    `KubernetesCluster` model + migration `f8c3d104e27a` + CRUD API
    + admin UI page + setup-guide modal (embedded YAML + kubectl
    extract commands) + Test Connection button that probes
    `/version` + `/api/v1/nodes` with structured error
    reporting (401/403/TLS/network distinguished).

  - ✅ **Phase 1b — Read-only reconciliation.** Every 30 s beat
    tick; per-cluster `sync_interval_seconds` (min 30 s) gates the
    actual reconcile. Gated overall by
    `PlatformSettings.integration_kubernetes_enabled`. Provenance
    via dedicated `kubernetes_cluster_id` FK on `ip_address`,
    `ip_block`, `dns_record` (migration `a917b4c9e251`); FK is
    `ON DELETE CASCADE` so removing a cluster sweeps every mirror
    row atomically. What gets mirrored:
    - Pod CIDR + Service CIDR → one `IPBlock` each under the bound
      space.
    - Node objects → `IPAddress` with `status="kubernetes-node"`,
      hostname = node name.
    - `Service` objects with `spec.type=LoadBalancer` + populated
      `status.loadBalancer.ingress[0].ip` → `IPAddress` with
      `status="kubernetes-lb"`, hostname = `<service>.<namespace>`.
    - `Ingress` objects with `status.loadBalancer.ingress[0].ip`
      → DNS **A** record per `rules[].host` in the longest-suffix-
      matching zone from the bound DNS group; `ingress[0].hostname`
      (cloud LBs) → **CNAME**. `auto_generated=True` + fixed 300 s
      TTL. Rows missing a matching subnet / zone increment
      `skipped_no_subnet` / `skipped_no_zone` on the reconcile
      summary — non-fatal, surfaced in logs + audit. Diff is
      create / update / delete (option 2a: delete, not orphan).
    **Admin UI**: "Sync Now" button per cluster (fires
    `sync_cluster_now` Celery task, bypasses interval gating) plus
    per-row `last_synced_at` / `last_sync_error` display. K8s
    client is a thin `httpx`-based REST wrapper — no
    `kubernetes-asyncio` dep.

  - ⬜ **Phase 2 — external-dns webhook provider (separate
    feature).** Implement the external-dns webhook provider HTTP
    protocol so teams already running external-dns can just point
    it at SpatiumDDI as a DNS backend. Different protocol, different
    testing story — deliberately not bundled with the pull-based
    integration above.

  **Explicit non-goals:**
  - Mirroring pod IPs (too dynamic, too noisy — the CIDR block is
    what matters).
  - Writing to the cluster (no CRD create, no annotation updates).
    If we want write-back, Phase 2's external-dns webhook is the
    right pattern — it's what ops teams already expect.
  - Managing the kubeconfig / kubectl flow — operator brings their
    own cluster-admin credentials to create the ServiceAccount; we
    only ever see the resulting read-only token.

- ✅ **Docker integration (read-only host mirror).** Poll one or
  more Docker daemons with a read-only connection and mirror the
  networks + (opt-in) containers into IPAM. Same UX shape as the
  Kubernetes integration above — `DockerHost` rows bound per-host
  to one `IPAMSpace` + optional `DNSServerGroup`, Settings →
  Integrations → Docker toggle, sidebar item gated on the toggle,
  Fernet-encrypted TLS client key, per-row Test Connection + Sync
  Now buttons, Setup guide with copy-paste TCP+TLS daemon config
  or Unix socket mount instructions.

  **Transport:** `unix` socket or `tcp` with optional mTLS. SSH
  (`docker -H ssh://`) is deferred — needs paramiko +
  `docker system dial-stdio` stream shuffling. No Docker Python
  SDK dep; we hit three Engine API endpoints (`/networks`,
  `/containers/json`, `/info`) over `httpx`.

  **What's mirrored:**
  - Every non-skipped Docker network → IPAM subnet under an
    enclosing operator block when one exists, else a cluster-
    owned wrapper block at the CIDR. Default `bridge` / `host` /
    `none` / `docker_gwbridge` / `ingress` are skipped unless
    `include_default_networks=true`. Swarm overlay networks
    always skipped (cluster-wide — would duplicate across nodes).
  - Network gateway → one `reserved`-status `IPAddress` per
    subnet (mirrors the LAN placeholder that `/ipam/subnets`
    creates for operator-made subnets).
  - Containers (opt-in via `host.mirror_containers`) → one
    `IPAddress` per (container × connected network) with
    `status="docker-container"` and hostname = either
    `<compose_project>.<compose_service>` when Docker Compose
    labels are present, else the container name. Stopped
    containers skipped unless `include_stopped_containers=true`.

  **Phase-3 placeholder (deferred).** Rich per-host management
  surface like mzac/uhld's Docker plugin: container actions
  (start/stop/restart), log streaming, shell exec (pty over
  websocket), image management, compose project up/down,
  volume browser, live events feed. Queries the stored
  connection live — no schema migration needed. Same treatment
  for Kubernetes (pod logs, shell exec, YAML apply, scaling).
  Scoped as a separate feature because it's a full management UI,
  not IPAM, and needs a websocket pipeline we don't have today.

---

## Version Scheme

SpatiumDDI uses **CalVer**: `YYYY.MM.DD-N` where N is the release number for that date (starting at 1).

- `2026.04.13-1` — first release on April 13, 2026
- `2026.04.13-2` — hotfix on the same day
- Git tags and Docker image tags follow this scheme exactly
- Release is triggered by pushing a tag matching `[0-9]{4}.[0-9]{2}.[0-9]{2}-*` (see `.github/workflows/release.yml`)

---

## Development Commands

```bash
# First-time setup
cp .env.example .env          # set POSTGRES_PASSWORD + SECRET_KEY (openssl rand -hex 32)
make build
make migrate
make up                       # production images  —  or:  make dev  (hot-reload)

# Default login: admin / admin (force_password_change=True)

# Run DNS and/or DHCP service containers too (via compose profiles):
COMPOSE_PROFILES=dns,dhcp make up

# Migrations
make migration MSG="add foo column"    # generate (autogenerate against models)
make migrate                           # apply

# Lint, typecheck, test
make lint                              # ruff + black + mypy, eslint + prettier
make ci                                # same three lint jobs CI runs (backend-lint + frontend-lint + frontend-build). Run before pushing.
make test                              # backend pytest
make test-one T=tests/test_health.py::test_liveness

# Logs
docker compose logs -f api worker
docker compose logs -f dns-bind9-dev dhcp-kea   # requires the profile to be on

# Frontend-only dev loop (outside Docker — Node 20+)
cd frontend && npm install && npm run dev

# Reset admin password (if locked out)
docker compose exec api python - <<'EOF'
import asyncio
from sqlalchemy import update
from app.core.security import hash_password
from app.db import AsyncSessionLocal
from app.models.auth import User
async def reset():
    async with AsyncSessionLocal() as db:
        await db.execute(update(User).where(User.username == "admin")
            .values(hashed_password=hash_password("NewPass!"), force_password_change=True))
        await db.commit()
asyncio.run(reset())
EOF
```

Frontend theme: dark/light/system toggle; CSS vars in `frontend/src/index.css`; toggle in Header component.

---
*See individual docs for full specifications.*
