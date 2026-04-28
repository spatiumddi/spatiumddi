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
| `docs/features/INTEGRATIONS.md` | Read-only Kubernetes + Docker mirror integrations; setup, semantics, dashboard surface |
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

SpatiumDDI cut its alpha release `2026.04.16-1` on 2026-04-16 with IPAM, DNS (BIND9), and DHCP (Kea) all shipping. Subsequent releases landed Windows Server integration (`2026.04.18-1`, 2026-04-18), the **performance + polish + visibility** release (`2026.04.19-1`, 2026-04-19) — batched WinRM dispatch, DDNS pipeline, the Logs surface, subnet/block resize, subnet-scoped IP import, DHCP pool awareness, collision warnings, sync modals, dashboard heatmap, draggable modals, standardised header buttons — the 2026.04.20 IPv6 + DDNS closure work, the **Kea HA + group-centric DHCP** release (`2026.04.21-2`, 2026-04-21) which shipped the full three-wave Kea HA story: end-to-end HA shake-out (peer URL resolution, port split, `status-get`, bootstrap reload), group-centric DHCP data model (scopes / pools / statics / classes live on `DHCPServerGroup`; HA is implicit with ≥ 2 Kea members), agent rendering fix (every prior Kea install was silently rendering `subnet4: []` due to a wire-shape bug), `PeerResolveWatcher` self-healing for peer IP drift, supervised Kea daemons, and standalone agent-only compose files for distributed deployments, the **integrations + observability** release (`2026.04.22-1`, 2026-04-22) that shipped Kubernetes + Docker read-only mirrors, the ACME DNS-01 provider, DHCP MAC blocklist, dashboard timeseries charts + platform-health card + collapsible sidebar, and the **Proxmox VE + polish** release (`2026.04.24-1`, 2026-04-24) which shipped the Proxmox endpoint mirror (bridges + SDN VNets + opt-in VNet-CIDR inference + per-guest Discovery modal), the shared `IPSpacePicker` quick-create component across all three integration modals, plus four UX polish fixes (real source IP behind nginx, alphabetised Integrations nav, wider Custom Fields page, search-row amber highlight), and the **network discovery + nmap** release (`2026.04.28-1`, 2026-04-28) which shipped SNMP polling of routers + switches with ARP/FDB cross-reference into IPAM (per-IP switch-port + VLAN visibility), the on-demand nmap scanner with live SSE output streaming (per-IP + `/tools/nmap` standalone page), the read-only `IPDetailModal` opened on row-click in IPAM with action buttons for Scan/Edit/Delete, sidebar regroup (core flattened, new Tools section, Administration items separated by dividers), removal of the dead Settings → Discovery section, and a linear-time rework of the BIND9 query log parser (CodeQL alert #16 closed). For the full list see `CHANGELOG.md`. The forward-looking work is below.

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
- **Logs surface.** New `/logs` page and `api/v1/logs/router.py`. Four tabs:
  - **Event Log** — `POST /logs/query` runs `Get-WinEvent -FilterHashtable` server-side via `app/drivers/windows_events.py`. Drivers expose inventory through `available_log_names()` + `get_events()`: `WindowsDNSDriver` returns `DNS Server` + `Microsoft-Windows-DNSServer/Audit`; `WindowsDHCPReadOnlyDriver` returns `Operational` + `FilterNotifications`. Filters keyed into React Query so tab entry + filter changes auto-fetch; Refresh button calls `refetch()`.
  - **DHCP audit** — `POST /logs/dhcp-audit` reads `C:\Windows\System32\dhcp\DhcpSrvLog-<Day>.log` over WinRM via `app/drivers/windows_dhcp_audit.py`. UTF-16 + ASCII both handled. Event-code → human label map; unknown codes come through as `Code <n>`.
  - **DNS Queries** *(landed post-2026.04.24)* — BIND9 query log surfaced via the agent push pipeline. The DNS agent's `QueryLogShipper` thread tails `/var/log/named/queries.log` (template-rendered when `DNSServerOptions.query_log_enabled`), batches up to 200 lines / 5 s and POSTs to `POST /api/v1/dns/agents/query-log-entries`. Lines are parsed into `dns_query_log_entry` rows (timestamp / client IP+port / qname / qclass / qtype / flags / view + raw original) by `app/services/logs/bind9_parser.py`; UI reads via `POST /logs/dns-queries` with substring / qtype / client-IP / since / max filters. 24 h retention via `prune_log_entries` Celery task — query logs are operator triage, not analytics; longer history belongs in Loki.
  - **DHCP Activity** *(landed post-2026.04.24)* — Kea DHCPv4 activity surfaced the same way. `render_kea` adds a file `output_options` (`/var/log/kea/kea-dhcp4.log`, in-process rotation `maxsize=50MB / maxver=5 / flush=true`) alongside the existing `stdout` output so `docker logs` keeps working. `LogShipper` thread → `POST /api/v1/dhcp/agents/log-entries` → `kea_parser.py` → `dhcp_log_entry` rows (severity / Kea log code / MAC / IP / transaction id + raw). UI filters: severity, log code, MAC, IP, since, raw substring. `GET /logs/agent-sources` lists `bind9` DNS + `kea` DHCP servers. Migration `d8c5f12a47b9_query_log_entries`.
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

### Major roadmap items (✅ shipped + ⬜ tracked)

This is a single chronological list of major roadmap items. Each entry
is marked ✅ when shipped or ⬜ when still pending. A shipped (✅) item
may still carry a "Deferred follow-ups" block — those are pending
sub-items that fit naturally with the parent narrative and live there
rather than moving to a separate "tracked" section. Pure-greenfield
ideas added in the 2026.04.26 brainstorm pass live in their own
categorised section further down.

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
- ✅ **SNMP polling / network device management** — vendor-neutral
  read-only polling of routers/switches via standard MIBs, with
  every result cross-referenced into IPAM. Lands the "every
  managed IP gets a heartbeat + switch-port automatically" payoff
  this whole roadmap pivots on. **Mirror scope:**
  - **Data model** (migration
    `c4e7a2f813b9_network_devices`): `network_device` (SNMP
    credentials Fernet-encrypted at rest; v1 / v2c / v3 USM all
    supported with auth + priv protocol enums), `network_interface`,
    `network_arp_entry` keyed `(device, ip, vrf)`, and
    `network_fdb_entry` keyed `(device, mac, vlan)` with the
    Postgres 15+ `NULLS NOT DISTINCT` unique index — so a single
    port can carry the same MAC across multiple VLANs (hypervisor
    with VMs in different access VLANs, IP phone with PC
    passthrough on voice + data VLANs).
  - **MIBs walked** — SNMPv2-MIB system group (sysDescr /
    sysObjectID / sysName / sysUpTime), IF-MIB `ifTable` +
    `ifXTable`, IP-MIB `ipNetToPhysicalTable` with legacy
    RFC1213 `ipNetToMediaTable` fallback, Q-BRIDGE-MIB
    `dot1qTpFdbTable` with BRIDGE-MIB `dot1dTpFdbTable` fallback.
    All standard, no vendor-specific MIBs — works on Cisco /
    Juniper / Arista / Aruba / MikroTik / OPNsense / pfSense /
    FortiNet / Cumulus / SONiC / FS.com / Ubiquiti out of the
    box.
  - **Polling pipeline** — `pysnmp` 6.x async (`bulkWalk` for
    table OIDs, ~10–50× faster than `getNext`).
    `app.tasks.snmp_poll.poll_device` runs sysinfo → interfaces
    → ARP → FDB sequentially under a per-device
    `SELECT FOR UPDATE SKIP LOCKED` so concurrent dispatches
    can't double-poll the same row. `dispatch_due_devices`
    beat-fires every 60 s and queues every active device whose
    `next_poll_at <= now`. Per-device interval default 300 s,
    minimum 60 s. Status: `success | partial | failed |
    timeout`, with `last_poll_error` populated for ops triage.
    Stale ARP entries are kept with `state='stale'` (no
    delete); `purge_stale_arp_entries` daily beat task removes
    rows older than 30 days.
  - **IPAM cross-reference** — after every successful ARP poll,
    `cross_reference_arp` finds matching `IPAddress` rows in
    the device's bound `IPSpace` and updates `last_seen_at`
    (max-merge), `last_seen_method='snmp'`, and fills
    `mac_address` only when currently NULL — operator-set MACs
    are never overwritten. When the per-device
    `auto_create_discovered=True` toggle is on (off by default;
    operator stays in control), inserts new
    `status='discovered'` rows for ARP IPs that fall inside a
    known `Subnet`. Returns counts (`updated`, `created`,
    `skipped_no_subnet`).
  - **API** — full CRUD at `/api/v1/network-devices` plus
    `POST /test` (synchronous SNMP probe, ≤10 s, returns
    `TestConnectionResult` with sysDescr + classified
    `error_kind`: `timeout | auth_failure | no_response |
    transport_error | internal`), `POST /poll-now` (queues
    immediate Celery task, returns 202 + task_id), and
    per-device list endpoints `/interfaces`, `/arp` (filter by
    ip/mac/vrf/state), `/fdb` (filter by mac/vlan/interface_id).
    The IP-detail surface gains
    `GET /api/v1/ipam/addresses/{id}/network-context` — joins
    `IPAddress.mac_address → NetworkFdbEntry → NetworkInterface
    → NetworkDevice` and returns one row per (device, port,
    VLAN, MAC) tuple so a hypervisor / IP phone surfaces every
    leg.
  - **Frontend** — top-level `/network` page in the core sidebar
    (always visible between VLANs and Logs — this is core IPAM
    functionality, not gated on a Settings → Integrations
    toggle). Per-device detail at `/network/:id` with Overview /
    Interfaces / ARP / FDB tabs, each filterable + paginated.
    Add/edit modal with SNMP-version-conditional credential
    fields (community for v1/v2c; security_name + level + auth
    + priv for v3) plus inline Test Connection (saves first on
    create, then probes against the saved row). New "Network"
    tab on the IP detail modal showing per-IP switch/port table
    sorted by `last_seen DESC`.
  - **Permissions** — single `manage_network_devices` permission
    gates all endpoints (read + write); new "Network Editor"
    builtin role gets it. Superadmin always bypasses.
  - **Tests** — 35 backend tests covering pysnmp wrapper paths
    (mocked: v1 / v2c / v3 auth construction, OID resolution,
    `ipNetToPhysical → ipNetToMedia` fallback,
    `Q-BRIDGE → BRIDGE` fallback, error classification),
    API CRUD + `/test` + `/poll-now` + the four list endpoints
    + `/network-context`, and three cross-reference paths
    (existing IP gets last_seen + MAC fill; operator-set MAC
    never overwritten; auto-create on/off; no-matching-subnet
    skip).

  **Deferred follow-ups:**
  - **CDP neighbour collection** + topology graph. LLDP shipped
    in the categorised brainstorm section below; CDP (Cisco-only)
    deferred — modern Cisco gear runs LLDP alongside CDP, so the
    standard-MIB path covers the typical case.
  - **VRF-aware ARP polling.** `network_device.v3_context_name`
    column exists for SNMPv3 context-name targeting, but the
    poller doesn't iterate per-VRF in v1. Per-VRF SNMPv2c
    community-string indexing
    (`<community>@<vrf-name>` Cisco convention) and SNMPv3
    context-name iteration are both pending.
  - **Standalone `snmp-poller` container** (per
    `docs/features/IPAM.md §13`). Today the polling lives in
    the existing Celery worker pool, which is fine to ~100
    devices on a 5-min interval. Splitting becomes interesting
    once SNMP traffic competes with the worker's other tasks
    or when the operator wants different network reachability
    for the poller (different VLAN, jumphost, etc).
  - **Beat-tick fan-out cap.** `dispatch_due_devices` queues
    every due device in one tick; at >1k devices this is a
    queue spike. Chunking + per-tick rate-limit is the cheap
    follow-up.
  - **Permission granularity** — read vs write split. Today
    `manage_network_devices` covers both; ops teams that want
    network-engineer read access without write privs need a
    `view_network_devices` companion permission.
  - **Stateless probe endpoint** — today `/test` requires the
    device row to exist; the create-then-test flow on the UI
    saves first then probes. A
    `POST /network-devices/probe` that accepts inline creds
    would let operators verify before committing the row.
  - **Frontend permission gating** — sidebar nav entry shows
    for every authenticated user (backend 403s unauthorised
    callers). A `useCurrentUser` hook + `hasPermission` check
    on the sidebar item is the polish step; depends on
    introducing those hooks (they don't exist anywhere in the
    frontend today).
  - **Vendor-specific MIBs** — CISCO-VRF-MIB for VRF
    auto-discovery, ENTITY-MIB for chassis info, vendor PoE
    MIBs. The vendor-neutral path covers the IPAM payoff;
    extensions are operator-pull additions.
  - **`/network-context` reverse-lookup pages.** "Show me every
    IP currently learned on switch X port 24" needs an
    interface-detail page or a query mode on the FDB tab.
    Today operators can filter the FDB tab by interface_id —
    the dedicated UI is a polish iteration.

  Original spec lives at `docs/features/IPAM.md §13` (the
  pre-build placeholder). The shipped behaviour matches that
  spec for the standard-MIB scope.
- ✅ **Nmap scanner** — on-demand nmap scans against any IPv4 /
  IPv6 host from the SpatiumDDI host perspective. Two entry
  points: a per-IP "Scan with Nmap" button on the IPAM IP detail
  modal, and a standalone `/tools/nmap` page for ad-hoc targets
  (including IPs that aren't in IPAM yet). Backend: `NmapScan`
  model + migration `d2f7a91e4c8b`, sanitised argv builder with
  preset table (quick / service+version / OS fingerprint /
  default-scripts / UDP top-100 / aggressive / custom), async
  subprocess runner using `-oN -` on stdout for the live SSE
  viewer + `-oX <tmpfile>` in parallel for structured XML
  parsing, Celery task on the `default` queue, and the SSE
  endpoint at `GET /api/v1/nmap/scans/{id}/stream`. SSE auth
  uses `?token=<...>` because EventSource can't set Authorization
  headers; the router has no global `Depends(get_current_user)`
  because that would 401 before the query-token resolver runs
  (each non-SSE endpoint declares its own permission dep). New
  `manage_nmap_scans` permission seeded into the existing
  Network Editor builtin role. nmap installed in the api image.
  **Deferred follow-ups:**
  - **Trigger pipeline** — auto-scan on ARP/SNMP discovery (the
    `auto_create_discovered=True` path) and alert-rule-driven
    re-scans. Phase 2.
  - **Realtime fanout** — the SSE stream polls the DB-persisted
    `raw_stdout` column every 500 ms per active viewer. Fine
    at human cadence and a handful of operators; if many
    operators end up watching live scans simultaneously
    (>~20-30) it'll show up as Postgres load. Swap to Redis
    pub/sub or `LISTEN/NOTIFY` behind the same HTTP shape.
  - **Privileged scans** — nmap runs as the api container's
    non-root user; raw-SYN (`-sS`) and unprivileged OS-detect
    silently fall back to TCP-connect. Bare-metal deployments
    can give the API process `CAP_NET_RAW` to unlock those
    modes; containerised deployments can't and shouldn't.
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

- ✅ **Kubernetes integration (read-only cluster mirror).** Poll one
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

### Integration roadmap (✅ shipped + ⬜ tracked)

Same read-only-pull reconciler shape as Kubernetes/Docker — each
one gets a `*Target` row type, Settings → Integrations toggle,
sidebar entry, and 30 s beat sweep with per-target interval
gating. Ranked by homelab/SMB test accessibility + IPAM value so
operators can exercise them in their own lab without standing up
cloud accounts. Items below are listed in roughly the order they
were originally prioritised; ✅ entries have shipped, ⬜ entries
are still pending (the ServiceNow CMDB integration in the
brainstorm section follows a different shape — bidirectional
write surface, not a read-only pull mirror).

- ✅ **Proxmox VE** — `ProxmoxNode` model + REST client
  + reconciler landed. Auth is API-token (`user@realm!tokenid`
  + UUID), token secret Fernet-encrypted at rest. One row
  covers a standalone host OR a whole cluster (PVE API is
  homogeneous across cluster members; `/cluster/status` surfaces
  cluster name + node count). Mirror scope:
  - **SDN VNets** (`/cluster/sdn/vnets/{vnet}/subnets`) →
    `Subnet` named `vnet:<vnet>` with the declared gateway.
    Authoritative over bridge-derived rows for the same CIDR —
    operator intent from PVE SDN wins. Returns 404 when SDN
    isn't installed; reconciler treats that as "no SDN" and
    keeps going.
  - **SDN VNet subnet inference** (opt-in via
    `infer_vnet_subnets` toggle, default off) — for VNets that
    exist but have no declared subnets, derive the CIDR from
    guests. Priority: (1) exact `static_cidr` from a VM's
    `ipconfigN gw=` or LXC's inline `ip=`/`gw=`; (2) /24 guess
    around guest-agent runtime IPs with a `proxmox_vnet_cidr_guessed`
    warning hinting at the `pvesh create` replacement. Solves
    the "PVE is L2 passthrough, gateway lives on upstream
    router" case where operators have many VNets with zero
    declared subnets. Migration `e5a72f14c890`.
  - Bridges + VLAN interfaces with a CIDR → Subnet (under
    enclosing operator block when present, else auto-created
    RFC 1918 / CGNAT supernet via the shared helper). Bridges
    without a CIDR skipped.
  - VM + LXC NICs → IPAddress with `status="proxmox-vm"` /
    `"proxmox-lxc"`, MAC from config. Runtime IP from QEMU
    guest-agent (when `agent=1` + agent running) or LXC
    `/interfaces`; falls back to `ipconfigN` static IP (VMs)
    or inline `ip=` (LXC); NIC silently skipped when nothing
    resolves. Link-local + loopback addresses filtered out.
  - Bridge gateway IP → `reserved` placeholder row per subnet.
  `mirror_vms` + `mirror_lxc` default **true** (PVE guests are
  long-lived, unlike Docker CI containers). **Discovery
  modal** — the reconciler writes a `last_discovery` JSONB
  snapshot on every successful sync (category counters + a
  per-guest list with single top-level `issue` code + operator-
  facing `hint`); admin page gets a magnifier-icon button per
  endpoint that opens a filterable Discovery modal showing
  agent-state pills + IPs-mirrored split + copy-ready fix
  hints like "install qemu-guest-agent inside the VM". Default
  filter is `Issues` so operators land on what needs attention.
  Migration `e7b3f29a1d6c`. Covered by 38 tests:
  `test_proxmox_client.py` (NIC + ipconfig + SDN-id parsing) +
  `test_proxmox_reconcile.py` (pipeline end-to-end, SDN
  subnet merge, VNet inference with both static-CIDR and
  runtime-IP paths, cascade delete). Migration
  `d1a8f3c704e9` (base) + `e5a72f14c890` (infer toggle) +
  `e7b3f29a1d6c` (discovery payload).
  **Deferred follow-ups:**
  - **Phase 2 per-cluster management surface** — VM / LXC
    start/stop/shutdown, console access, live migrate,
    snapshot, backup browser. Mirrors the Kubernetes /
    Docker Phase-3 pattern; needs websocket pipeline we
    don't have today.
  - **Pool / resource-tag awareness.** PVE has a "pool" object
    for grouping resources and an arbitrary tag system; neither
    surfaces in IPAM today. Low-effort to add as custom-field
    passthrough once an operator asks.
  - **Cluster-quorum alerting.** `/cluster/status` carries a
    `quorate` bool — wire that into the alerts framework so an
    HA cluster losing quorum pages operators.

- ⬜ **UniFi Network Application** *(tier 1 — biggest LAN
  inventory win).* Per-controller row, API-key auth on modern
  UniFi OS (Site Manager API), cookie+CSRF fallback on older
  controllers. Multi-site aware. Mirror:
  - Networks / VLANs → Subnets (respect `subnet` + `vlan`
    fields; VLANs land as `VLAN` rows too, re-using the existing
    VLAN table).
  - Active clients → IPAddress rows with MAC + hostname + OUI
    vendor + fixed_ip flag, refreshed every 30 s.
  - DHCP fixed IPs (reservations) → IPAddress rows with
    `status="reserved"` so the UI shows them as static.
  The highest-value integration for home/SMB operators — plug a
  new device into the network, it shows up in IPAM with correct
  VLAN + vendor + hostname with zero operator effort. Design
  wrinkle: UniFi's two API shapes (legacy controller vs. new
  Site Manager) need two transport implementations; shared
  reconciler on top. Per-site or per-network opt-in gates the
  client-mirror so a noisy guest SSID doesn't fire-hose IPAM.

- ✅ **Tailscale — Phase 1: device mirror.** Per-`TailscaleTenant`
  row, PAT token + tailnet name (default `-`), Fernet-encrypted
  at rest. 60 s default sync interval (Tailscale rate-limits
  `/api/v2` at 100 req/min, so 30 s floor is the same as the
  other integrations). The reconciler hits
  `GET /api/v2/tailnet/{tn}/devices?fields=all` and mirrors each
  device's `addresses[]` (both IPv4 in `100.64.0.0/10` and IPv6
  ULA in `fd7a:115c:a1e0::/48`) as IPAddress rows under the
  bound IPAM space. The CGNAT + IPv6-ULA blocks are
  auto-created on first sync — operator can override the CGNAT
  CIDR per tenant for non-default tailnets. Per-row shape:
  - `status="tailscale-node"`, hostname = device FQDN
    (`<host>.<tailnet>.ts.net`), MAC null (no L2 on the overlay).
  - `description` = `<os> <clientVersion> — <user>`.
  - `custom_fields` = `{os, client_version, user, tags,
    authorized, last_seen, expires, key_expiry_disabled,
    update_available, advertised_routes, enabled_routes,
    node_id}`.
  - Tailnet domain is auto-derived from the first device's FQDN
    (no separate config field; mirrors the uhld pattern).
  Read-only-ish: integration-owned status + the
  `user_modified_at` lock keep operator edits sticky across
  reconciles (same pattern as Proxmox / Docker / Kubernetes).
  Provenance via `tailscale_tenant_id` FK on
  `ip_address`/`ip_block`/`subnet` with `ON DELETE CASCADE`.
  Subnet-router routes (`enabledRoutes`) are stored in
  custom_fields today; promoting them to first-class IPBlock
  rows is a follow-up. Test Connection probe + Sync Now button
  in the admin page mirror the other integrations.

- ✅ **Tailscale — Phase 2: synthetic tailnet DNS surface.**
  Implemented as Option 2 from the original plan (synthetic
  `DNSZone` materialised by the reconciler). When a
  `TailscaleTenant` has `dns_group_id` bound, every reconcile
  pass derives `<tailnet>.ts.net` from the first device FQDN,
  upserts a `DNSZone` with `is_auto_generated=True` and
  `tailscale_tenant_id=<tenant>`, and materialises one A / AAAA
  `DNSRecord` per device address. Records also carry
  `auto_generated=True` + the tenant FK. **Read-only enforcement**:
  `update_zone` / `delete_zone` / record CRUD reject writes
  with 422 when `tailscale_tenant_id IS NOT NULL`; UI renders a
  "Tailscale (read-only)" badge near the zone title and disables
  the Edit / Delete / Add Record buttons; the per-record lock
  badge in the records table branches on `tailscale_tenant_id`
  to read "Tailscale" instead of "IPAM" for synthesised rows.
  **Diff semantics**: keyed on `(name, record_type, value)` —
  removed devices have their records deleted on the next sync;
  idempotent on stable input. **Conflict safety**: a pre-
  existing operator-managed zone with the same name is left
  untouched, with a `summary.warnings` entry that surfaces in
  the audit log. **Filtering**: expired-key devices skipped per
  Phase 1's `skip_expired` toggle; devices with no FQDN or
  foreign tailnet suffix are skipped without error. **Bonus**:
  because we land actual `DNSRecord` rows, the existing BIND9
  render path picks them up automatically — non-Tailscale LAN
  clients can resolve `<host>.<tailnet>.ts.net` through
  SpatiumDDI's managed BIND9 with no forwarder plumbing. TTL
  is 300 s (short by design — IP assignments shift after re-auth).
  Migration `e6f12b9a3c84_tailscale_phase2_dns`.

  **Deferred follow-ups:**
  - **Per-tenant zone-name override.** Today we use the auto-
    derived `<tailnet>.ts.net`. Some operators run Tailscale
    with a custom split-DNS arrangement and want the synthetic
    zone published under a different name (e.g.
    `tailnet.internal`). Adding a `synthetic_zone_name` column
    on `TailscaleTenant` (defaulting to null = derive) would
    cover that without much code.
  - **Subnet-router routes (`enabled_routes`) → first-class
    `IPBlock` rows.** Phase 1 stores them in `custom_fields`;
    promoting them to real IPBlocks (one per route, FK to
    tenant) would let the IPAM tree show "this CIDR is reachable
    via tailnet device X." Worthwhile if operators start
    advertising LAN subnets through Tailscale.
  - **BIND9 forwarder zone for `100.100.100.100`.** Optional
    alternative for operators who don't want SpatiumDDI's BIND9
    serving the records itself but DO want to forward `*.ts.net`
    queries through MagicDNS (which only listens on the local
    Tailscale daemon — works only if the BIND9 host is itself
    on Tailscale).

- ⬜ **OPNsense** *(tier 1 — firewall-of-choice for labs).*
  Per-`OPNsenseRouter` row, API key + secret auth over HTTPS.
  REST endpoints: `/api/dhcpv4/leases/searchLease`,
  `/api/dhcpv4/settings/getReservation`,
  `/api/diagnostics/interface/getInterfaceConfig`,
  `/api/interfaces/vlan_settings/get`. Mirror LAN / VLAN /
  OPT* interfaces → Subnets, DHCP leases → IPAddress
  (`status="dhcp"`, same shape as Kea mirror), static mappings →
  `status="reserved"`. ARP table endpoint as a secondary
  population source for devices with static-IP-outside-DHCP.

- ⬜ **pfSense** *(tier 1 — paired with OPNsense).* Same scope
  as OPNsense but through either the FauxAPI package or the
  pfsense-api REST add-on. Auth shape differs (FauxAPI key-based,
  HMAC-signed request body; pfsense-api JWT or API-key header).
  Shared reconciler with OPNsense once both drivers are done —
  the data model's the same (interfaces + DHCP + ARP).

- ⬜ **MikroTik RouterOS 7** *(tier 2).* Native REST API on v7,
  or the legacy API/API2 on v6. Per-`MikrotikRouter` row.
  Mirror: interface addresses (`/ip/address`) → Subnets, DHCP
  leases (`/ip/dhcp-server/lease`) → IPAddress, ARP table
  (`/ip/arp`) as a fallback population source. Narrower
  audience than OPNsense/pfSense but very common in some lab
  + WISP shops.

- ⬜ **Incus / LXD** *(tier 2 — Docker-adjacent).* Same shape as
  the Docker integration — `/1.0/networks` + `/1.0/instances`
  over HTTPS with client cert auth. Different runtime, same
  reconciler architecture. Likely share ≥ 80 % of the code with
  `services/docker/reconcile.py` via a common base — refactor
  when the second container-runtime integration lands, not
  before.

- ⬜ **HashiCorp Nomad** *(tier 2 — Kubernetes alt).* Read-only
  ACL token, `GET /v1/allocations?status=running`. Mirror
  running allocations with a resolved IP → IPAddress with
  `status="nomad-alloc"`, hostname = `<job>.<group>.<task>`,
  plus the Nomad network CIDR as a Subnet. Smaller install base
  than K8s but architecturally close — Docker/K8s reconciler
  pattern slots right in.

- ⬜ **NetBox read-only import (one-shot)** *(tier 2 — migration
  tool, not continuous sync).* Pull existing IPAM state out of
  a NetBox install so the operator can evaluate SpatiumDDI
  without re-entering their inventory. Not an ongoing
  reconciler — a UI-driven import at `Admin → Import →
  NetBox` that reads prefixes + IP addresses + VLANs +
  tenants (mapped to IP spaces) and stamps them into IPAM
  with `custom_fields.imported_from="netbox"`. Differs from
  every other entry here — it's migration tooling, not
  integration.

- ⬜ **Cloud VPC family (AWS / Azure / GCP / Hetzner / DO /
  Linode / Vultr)** *(tier 3 — lab-inaccessible, but roadmap
  coherent).* Same shape: per-account/subscription row with
  access-key + region scope, mirror VPCs/VNets → IP spaces,
  subnets → Subnets, EC2/VM instances → IPAddress rows. Cloud
  DNS driver family already on the roadmap above is the
  write-side counterpart. Gating for later — no lab relevance
  for the Proxmox/UniFi/Tailscale/OPNsense operator base we're
  aiming at first.

**Explicit non-goals for the integrations shelf:**

- **VMware vCenter / ESXi.** Bigger enterprise audience, but
  vCenter's SOAP-heavy + licensed API makes it a significantly
  bigger dev effort than the tier 1 candidates. Revisit only if
  a deployment specifically needs it.
- **SNMP device polling** as an integration. Already tracked as
  its own line item above (IPAM ARP discovery); belongs with
  ping-sweep / ARP-scan, not the read-only integration shelf.
- **WireGuard raw config.** No API — config files only. Belongs
  in a manual-import flow if at all.

### Future ideas — categorised (added 2026.04.26)

Brainstorm pass that catalogues standard IPAM / DDI features
operators of comparable tools (Infoblox, EfficientIP, NetBox,
phpIPAM, SolarWinds IPAM) expect but SpatiumDDI doesn't yet
ship. Sketched at enough depth to start work without
re-deriving the design — pick by impact, not by section order.
None of these have started; everything below is ⬜.

#### Discovery & network awareness

- ✅ **LLDP neighbour collection** — vendor-neutral via
  LLDP-MIB (IEEE 802.1AB) `lldpRemTable`. Per-device opt-in
  `poll_lldp` toggle (default on); polled as a 5th step after
  ARP / FDB in `app.tasks.snmp_poll.poll_device`.
  `network_neighbour` table keyed
  `(device_id, interface_id, remote_chassis_id, remote_port_id)`
  with absence-delete every poll so stale neighbours fall off
  cleanly. Captures remote chassis ID + port ID (with subtype
  decoding — MAC addresses formatted, interface names left
  raw), system name + description, port description, and a
  decoded capabilities bitmask (Bridge / Router / WLAN AP /
  Phone / Repeater / Other / Station / DocsisCableDevice).
  API: `GET /api/v1/network-devices/{id}/neighbours` with
  `sys_name` (ilike) / `chassis_id` / `interface_id` filters.
  Frontend: new "Neighbours" tab on the network device detail
  view, with vendor-aware enable hints (Cisco IOS / NX-OS,
  Junos, Arista EOS, ProCurve / Aruba, MikroTik RouterOS,
  OPNsense / pfSense) when no rows are present. Migration
  `b9e4d2a17c83_network_neighbour`. **Deferred:** CDP polling
  (Cisco-only — LLDP usually runs alongside on modern gear);
  topology graph rendering (the data is captured, the graph UI
  isn't built yet); IP cross-reference via
  `lldpRemManAddrTable`; per-port enrichment via
  `lldpLocPortIfIndex` to resolve LLDP's own port numbering to
  ifIndex (today the local port is recorded by SNMP-side
  ifIndex via the FDB / interface walk, not LLDP's local
  port-num index).
- ⬜ **NetFlow / sFlow ingestion** — bind a UDP listener
  (sflowtool / goflow2 sidecar, or pure-Python decoder) and
  aggregate to per-IP byte / packet / flow counters in 5-minute
  buckets. Writes to a `traffic_sample` table parallel to
  `metric_sample`. Surfaces "active vs allocated" pivot — IPs
  in IPAM that haven't seen a flow in N days are surfacing
  candidates for stale-IP cleanup.
- ⬜ **mDNS / Bonjour / WSD passive discovery** — agent-style
  listener that joins the multicast group on each managed
  subnet and records hostnames + service-types announced
  (`_workstation._tcp`, `_printer._tcp`, etc). Best run inside
  a DHCP agent pod since the agent already has L2 reach. Cheap
  incremental population.
- ✅ **Switch-port mapping in the IP table (column-level).** The
  IPAM IP table now carries a "Network" column showing
  `<device> · <port> [VLAN N]` for the most-recent FDB hit on
  each IP's MAC, with a `+N more` badge + hover tooltip listing
  every (device, port, VLAN) tuple when the MAC is learned in
  multiple places (hypervisor host + VMs across access VLANs,
  trunk ports). Backed by a batched
  `GET /api/v1/ipam/subnets/{id}/network-context` endpoint that
  returns `{ip_address_id: NetworkContextEntry[]}` in one round
  trip — no N+1 fan-out per page-of-IPs. The per-IP detail
  modal's "Network" tab still works as the deep-dive surface.
  **Deferred follow-ups:**
  - Reverse "click MAC → all IPs on this port" drilldown
    starting from an interface row in the network detail page.
    Useful when an operator's looking at a switch port and
    wants to see "what's plugged in here?".
  - Per-user column show/hide (lives under the broader Saved
    Views work in UX polish below).
  - Filter input on the Network column (the column header has
    no filter today; operators can still filter via the device
    detail page's ARP/FDB tabs).
- ⬜ **Reverse-DNS auto-population** — Celery beat sweeps IP rows
  where `hostname IS NULL` and issues a PTR lookup against the
  configured resolvers. Hostname is filled with the trailing
  single-label only (the FQDN goes in `description`). Skipped
  for integration-owned rows whose hostname is authoritative
  from upstream.
- ⬜ **CGNAT (RFC 6598) awareness** — first-class recognition of
  `100.64.0.0/10` as carrier-grade NAT space, distinct from
  RFC 1918 / public space. Quick-paste preset already exists
  in the CIDR calculator; the rest is operator-facing
  semantics: a "CGNAT" badge on subnets that fall in the
  block, exclusion from public-space reporting, and a hint
  in the New Subnet modal when an operator picks a CGNAT
  CIDR for a normal LAN ("this is RFC 6598 carrier-grade NAT
  space — Tailscale uses it; double-check before using for
  on-prem"). Pairs with the existing Tailscale tenant
  CGNAT-block auto-create — that already lands `100.64.0.0/10`
  as the default CGNAT root for the tenant. Operator-facing
  framing only; no allocation behaviour changes.

#### Reporting & analytics

- ⬜ **Capacity forecasting** — linear-regression projection of
  subnet utilization based on the existing time-series data.
  "This /24 trends to 100% on YYYY-MM-DD at current rate."
  Surfaces as a "Days to full" column on subnet tables + an
  alert rule type `subnet_capacity_forecast` keyed on
  `< N days`. Math is trivial; the win is operator-actionable.
- ⬜ **Per-subnet utilization history** — small chart on subnet
  detail showing % used over the last 30 / 90 days. Storage
  piggy-backs on the subnet `allocated_ips` column with a daily
  snapshot (`subnet_utilization_history(subnet_id, sampled_at,
  allocated_ips, total_ips)`); 90-day retention cap.
- ⬜ **Stale-IP report** — admin page listing IP rows with
  `status = allocated` and `last_seen_at` older than N days
  (operator-tunable, default 90). One-click bulk-deprecate.
  Feed for "address space hygiene" alerts.
- ⬜ **Decom-date awareness** — first-class `decom_date` column
  on subnet + ip_address (currently only suggested as a custom
  field). Beat task generates a "subnets decom in next 30 days"
  summary that feeds the alerts framework + an admin dashboard
  widget.
- ⬜ **Top-N reports** — fixed dashboard widgets: top 10 subnets
  by utilization, top 10 owners by IP count, top 10
  most-modified resources in the last 7 days, top 10 noisiest
  DNS clients. All derivable from existing tables; just needs
  a reports router + page.
- ⬜ **Compliance / change report PDF** — scheduled or on-demand
  PDF rollup of every audit_log mutation in a date range,
  grouped by user / resource type / action. Generated
  server-side via `weasyprint` or `reportlab`.
  Auditor-friendly; pairs with the audit-log immutability work
  below.

#### Subnet planning & calculation tools

- ✅ **Built-in CIDR calculator** — utility page at `/tools/cidr`
  with sidebar nav under Tools. Pure client-side, no API. Accepts
  IPv4 or IPv6 (CIDR or bare address), renders network / netmask /
  wildcard / broadcast / first-last usable / total addresses /
  decimal + hex / binary breakdown (v4) and compressed +
  expanded forms (v6). Quick-paste preset buttons for the common
  RFC 1918 / CGNAT / ULA blocks. BigInt math throughout so v6
  prefixes work cleanly.
- ✅ **Subnet planner / "what-if" workspace** — `/ipam/plans`.
  Operator designs a multi-level CIDR hierarchy as a draggable
  tree (one root + nested children, arbitrary depth), saves it
  as a `SubnetPlan` row, validates against current state, then
  one-click applies — every block + subnet created in a single
  transaction. **Data model:** `subnet_plan(id, name,
  description, space_id, tree JSONB, applied_at,
  applied_resource_ids JSONB, created_by_user_id)`. Tree node
  shape: `{id, network, name, description, kind, existing_block_id?,
  dns_group_id?, dns_zone_id?, dhcp_server_group_id?,
  vlan_ref_id?, gateway?, children[]}`. **`kind` is explicit**
  per node (`block` or `subnet`) — root must be a block
  (subnets need a block parent), and a subnet may not have
  children (validation enforces both). **Resource bindings**
  (DNS group, DHCP group, gateway) are optional per-node;
  `null` = inherit, explicit value sets the field on the
  materialised row and flips the corresponding
  `*_inherit_settings=False`. UI exposes DNS group + DHCP
  group dropdowns + gateway field on subnets; VLAN + DNS-zone
  fields exist in the model but UI binding is deferred (VLAN
  is router-scoped — needs a flat list endpoint first). **Root
  modes:** new top-level CIDR (creates a fresh block at the
  space root) OR anchor to an existing `IPBlock` (descendants
  land as children of the existing block, root not re-created).
  **Validation** (`/plans/{id}/validate` + `/plans/validate-tree`
  for in-flight trees) checks duplicate node ids, kind rules,
  parent-containment of every child, sibling-overlap, and
  overlap against current IPAM state in the bound space.
  Auto-fires every 300 ms as the operator edits — conflicts
  surface inline as red ring on offending nodes + a banner
  above the tree. **Apply** opens a confirmation modal with
  block + subnet counts ("this will create N blocks + M
  subnets"), then re-validates inside the txn; any conflict
  → 409 with the full conflict list and nothing is written.
  Once applied, the plan flips read-only and
  `applied_resource_ids` records every created block + subnet
  for audit. **Reopen** (`/plans/{id}/reopen`) flips an
  applied plan back to draft state, but only if every
  materialised resource has been deleted from IPAM —
  otherwise 409 with the survivor list. Lets operators
  iterate on the same plan after a teardown rather than start
  fresh. **Frontend:** `@dnd-kit/core` (already a dep) for
  drag-to-reparent; drops onto descendants OR onto subnet
  targets are refused. Properties panel on the right edits
  CIDR / name / kind / DNS / DHCP / gateway for the selected
  node. Sidebar entry "Subnet Planner" alongside NAT
  Mappings. Migration `c8e1f04a932d_subnet_plan`.
  **Deferred:** sibling reordering via `@dnd-kit/sortable`
  (today reparenting only appends as last child); split-into-N
  action on a node; VLAN dropdown in the planner UI (model
  field exists; needs a flat VLAN list endpoint first); DNS
  zone selector that's gated on the chosen DNS group; per-node
  custom-fields / tags / status (those are per-IP and edited
  through normal IPAM use after apply).
- ✅ **Address planner** — `POST /api/v1/ipam/blocks/{id}/plan-
  allocation` accepts a list of `{count, prefix_len}` requests
  (e.g. `4 × /24, 2 × /26, 1 × /22`) and packs them into the
  block's free space using largest-prefix-first ordering with
  first-fit-by-address placement (so sequential same-size
  requests pack contiguously from low addresses rather than
  chasing small isolated free islands).
  Returns the planned allocations + any unfulfilled rows + the
  remaining free space after the plan. Reuses the same
  `address_exclude` walk that powers `/free-space`. UI: "Plan
  allocation…" button next to the Allocation map on the block
  detail; modal lets the operator add/remove rows and shows the
  packed result. Preview only — no writes — so the operator can
  iterate freely. **Deferred:** one-click apply that chains the
  preview into N `POST /subnets` calls inside a transaction.
- ✅ **Aggregation suggestion** — `GET /api/v1/ipam/blocks/{id}/
  aggregation-suggestions` runs `ipaddress.collapse_addresses` on
  the block's direct-child subnets; any output that subsumes more
  than one input is a clean merge opportunity (the inputs pack
  perfectly into a supernet with no gaps). Read-only banner on the
  block detail surfaces them when present (e.g. `10.0.0.0/24 +
  10.0.1.0/24 → /23`). **Deferred:** one-click merge flow — needs
  to handle the cascade across IP rows + DNS records owned by the
  deleted siblings, plus operator confirmation. Today operators
  see the suggestion and act manually (delete + recreate).
- ✅ **Free-space treemap** — Recharts squarified Treemap on the
  block detail, toggled via a Band / Treemap selector next to
  the Allocation map header (selection persisted in
  sessionStorage per block). Cells are coloured by kind (violet
  child blocks, blue subnets, hashed-zinc free) and sized by
  raw address count. Pixel-thin slices on the 1-D band become
  visible squares here, surfacing fragmentation that's easy to
  miss otherwise. Uses the existing `recharts` dep — no new
  packages added.

#### DNS-specific

- ⬜ **DNSSEC** — sign zones, manage KSK / ZSK rollover, NSEC3
  parameters, DS-record export to upstream. BIND9 supports
  inline signing (`dnssec-policy` configs in 9.16+). Storage:
  `DNSSECPolicy` + `DNSKey` tables tracking key material
  (Fernet-encrypted) and rollover state. Massive compliance ask
  in regulated verticals; table-stakes for any "enterprise DDI"
  comparison.
- ⬜ **TSIG key management UI** — list / rotate / revoke TSIG
  keys used for RFC 2136 dynamic updates and AXFR auth. Today
  they're agent-side files only; no central inventory or
  rotation flow.
- ✅ **Conditional forwarders** — per-zone forwarding for mixed-AD
  environments. `DNSZone` carries `forwarders` (JSONB list of IPs)
  + `forward_only` (true → `forward only;`, false → `forward
  first;`). When `zone_type = "forward"` the BIND9 driver renders
  `zone "X" { type forward; forward only; forwarders { ... }; }`
  in `zone.stanza.j2` and the agent's wire-format renderer
  (no zone file written, no allow-update); the form gates the
  forwarders/policy fields on `zone_type === "forward"` and
  refuses submit when no upstreams are listed. Migration
  `a07f6c12e5d3_dns_zone_forwarders`. **Deferred:** Windows DNS
  via `Add-DnsServerConditionalForwarderZone` (the field
  shape is identical; just needs the WinRM dispatch).
- ⬜ **BIND9 catalog zones (RFC 9432)** — distribute zone
  definitions across multiple BIND9 servers in a group via a
  single catalog zone rather than per-server config push.
  BIND 9.18+ supports this natively. Cuts the agent's per-server
  zone-add / zone-delete bookkeeping; massive operational win
  for >2 BIND server groups.
- ✅ **Response Policy Zones (RPZ)** — DNS-level malware / phishing
  / ad blocking via BIND9 `response-policy { zone … };`. Full
  `DNSBlockList` + `DNSBlockListEntry` + `DNSBlockListException`
  model with categories / source types (manual / url /
  file_upload) / block modes (nxdomain / sinkhole / refused) /
  per-list scheduled refresh. Service-side aggregation produces
  effective entries per server-group or view; agent renders one
  RPZ master zone per blocklist with CNAME-based actions
  (`. = NXDOMAIN`, `rpz-drop. = sinkhole`, target = redirect,
  `rpz-passthru. = exception). CRUD lives at
  `app/api/v1/dns/blocklist_router.py`. BIND9-only — Windows
  DNS has no RPZ equivalent (closest is Query Resolution
  Policies which lack the wire format).
- ✅ **Curated RPZ blocklist source catalog** — ships a static
  JSON catalog at `backend/app/data/dns_blocklist_catalog.json`
  with 14 well-known public blocklists drawn from AdGuard's
  HostlistsRegistry + Pi-hole defaults + Hagezi / OISD: AdGuard
  DNS Filter, StevenBlack Unified, OISD Small/Big, Hagezi Pro
  / Pro+, 1Hosts Lite, Phishing Army Extended, URLhaus,
  DigitalSide Threat-Intel, EasyPrivacy, plus StevenBlack
  fakenews / gambling / adult add-ons. Each entry carries
  `{id, name, description, category, feed_url, feed_format,
  license, homepage, recommended}`. `GET /dns/blocklists/catalog`
  returns the snapshot (cached in-process). `POST
  /dns/blocklists/from-catalog` creates a normal `DNSBlockList`
  row with `source_type="url"` prefilled — leverages the
  existing `parse_feed` + `_refresh_blocklist_feed_async` task
  for fetch / parse / ingest with no new beat task. Frontend
  has a "Browse Catalog" button on the Blocklists tab opening a
  filterable picker (category + free-text), with already-
  subscribed entries flagged. Catalog snapshot moves in
  lockstep with releases; operators can still add custom
  sources via the existing "New Blocking List" flow.
  **Deferred:** "Refresh catalog from upstream" button that
  re-fetches `filters.json` from HostlistsRegistry between
  releases.
- ⬜ **DoT / DoH listener** — BIND 9.18+ supports DNS-over-TLS
  (`tls`) and DNS-over-HTTPS (`https`) natively. Driver renders
  the listener config; cert lifecycle ties into the ACME
  embedded-client item already on the roadmap.
- ⬜ **DNS query analytics aggregation** — today the BIND9 query
  log surfaces individual lines via the Logs page. Missing:
  aggregation (top qnames, top clients, qtype distribution,
  NXDOMAIN ratio) rolled into the dashboard. Storage:
  per-bucket counters on a `dns_query_aggregate` table fed by
  the same agent ship pipeline.
- ⬜ **Zone delegation wizard** — when creating a sub-zone of an
  existing zone, auto-create the parent's NS records + glue and
  propagate. Today operators have to remember to do this
  manually and zones often end up un-delegated.
- ⬜ **DNS template wizards** — pre-canned zone templates:
  "AD-integrated forward zone with all required SRV records";
  "email zone with MX + SPF + DKIM + DMARC starter records";
  "kubernetes external-dns target zone". One-click materialise
  → operator edits / removes records as needed.
- ✅ **Multi-resolver propagation check** — `POST
  /dns/tools/propagation-check` fires the same query against
  Cloudflare / Google / Quad9 / OpenDNS in parallel using
  `dnspython`'s `AsyncResolver` (each query carries its own
  timeout so a slow resolver can't poison the others) and
  returns per-resolver `{resolver, status, rtt_ms, answers,
  error}`. UI surfaces as a Radar button on each record row in
  the records table; modal lets the operator switch record
  type and re-check. Driver-agnostic — queries are made from
  the API process, doesn't touch the BIND9 / Windows drivers.
  **Deferred:** operator-customisable resolver list (today the
  curated set is hard-coded server-side; the API accepts an
  override but the UI doesn't yet expose it).

#### DHCP-specific

- ⬜ **DHCP option library / templates** — named profiles ("VoIP
  devices", "PXE boot", "VPN pool") that bundle option-code →
  value sets and can be applied to scopes in one click. Today
  every scope's options are set individually. Templates are a
  `DHCPOptionTemplate` row + many-to-many to scopes.
- ⬜ **Option-code library lookup** — searchable list of
  well-known DHCP option codes (RFC 2132 + IANA registry) with
  descriptions. Helps operators dig up the meaning of "option
  121" without context-switching to the RFC. Static JSON
  shipped with the app.
- ⬜ **DHCP fingerprinting** — identify device type from
  option-55 parameter request list + vendor-class (option 60) +
  class-id. Fingerprint database from the `fingerbank` project
  (CC-BY-SA). Surfaces as a `device_type` column on leases
  ("HP iLO", "Aruba AP", "Cisco IP Phone 8841", "iOS device").
  Big differentiator vs. vanilla DHCP servers.
- ⬜ **PXE / iPXE provisioning** — first-class fields for
  `next-server`, `boot-filename`, with per-arch matching (BIOS
  vs UEFI vs ARM). Today it's manual option-stuffing. Renders
  to Kea client classes + scope-level overrides; surfaces in
  the scope edit modal as a "PXE / iPXE" tab.
- ⬜ **DHCPv6 stateful + SLAAC config UI** — Kea Dhcp6 backend
  exists; the missing piece is operator-friendly UI for
  choosing between stateless / stateful / SLAAC + RA mode.
  Today the address-family toggle exists but the v6-specific
  modes don't.
- ⬜ **Lease histogram by hour** — per-scope chart showing lease
  grants by hour-of-day over the last 7 / 30 days. Pinpoints
  office-arrival surges + capacity planning. Storage: bucketed
  counters on `dhcp_lease_hourly` written by the existing lease
  ingestion path.
- ⬜ **Option 82 (relay agent info) class matching** — Kea
  client classes that match on `relay-agent-info` sub-options
  (circuit-id, remote-id). Lets carriers / large-enterprise
  drive scope selection off switch port info inserted by the
  relay. UI: per-class predicate builder.
- ⬜ **DHCP test client** — synthetic DISCOVER → OFFER →
  REQUEST → ACK from inside SpatiumDDI to validate a scope is
  operational. Implemented as a Celery task using `scapy` or
  pure-socket DHCP. Useful for change-window verification +
  post-deploy smoke tests.

#### Operational tooling

- ⬜ **Time-travel queries** — "what did this subnet look like a
  month ago?" UI that replays the audit_log forward from a
  snapshot to a target timestamp. Read-only — no rollback.
  Powered entirely by the existing audit data; just needs a
  replay engine + UI.
- ⬜ **Maintenance mode** — global toggle that puts the entire
  system in read-only state during change windows. UI shows a
  top banner; every write endpoint returns 503 with
  `Retry-After`. Bypass for superadmins so they can still make
  the changes themselves.
- ⬜ **Built-in network tools page** — `/tools` with widgets for
  ping, traceroute, dig, whois, port-test, MTR,
  DNS-propagation-check, TLS cert checker, MAC vendor lookup.
  Each runs from the SpatiumDDI server perspective (or a chosen
  DHCP / DNS agent's perspective). Saves operators bouncing to
  a jump-box. Bound by the existing permission gates;
  rate-limited to avoid abuse.
- ⬜ **PCAP capture trigger** — start a tcpdump on a chosen
  agent pod / host with a BPF filter, return a downloadable
  pcap when done. Niche but loved; uses the agent's existing
  JWT for auth.
- ⬜ **ACL / prefix-list generator** — given a subnet or list of
  subnets, render Cisco IOS / Juniper JUNOS / Arista EOS /
  Linux iptables / nftables ACL syntax. Pairs with router-zone
  metadata for "all subnets in zone X as a Cisco prefix-list"
  exports.
- ⬜ **Config-drift report (full record diff)** — extends the
  existing zone-serial drift surface with a full record-level
  diff: AXFR the zone from each member, diff against the
  SpatiumDDI source of truth, surface adds / changes / deletes
  per server. Lets operators spot manual changes made directly
  on a BIND9 host.

#### Workflow & RBAC

- ⬜ **Approval workflows for risky ops** — two-person rule on
  subnet / zone / scope delete + bulk operations above a
  threshold. Request lands as a `PendingChange` row; second
  eligible approver clicks Approve → the change executes under
  their identity. Audit log carries both user IDs.
- ⬜ **Resource locking** — operator can lock a resource (subnet,
  zone, scope) for the duration of a change window. While
  locked, even superadmins get a confirmation prompt. Lock has
  TTL + owner; "force-unlock" requires a permission gate.
- ⬜ **Per-resource ACLs** — augment the role-based system with
  resource-scoped grants ("group X can allocate IPs in subnet
  Y but not delete"). Uses the existing `{action,
  resource_type, resource_id?}` permission grammar; just needs
  a per-resource grant editor in the UI.
- ⬜ **Time-bound permissions** — grant group X access to subnet
  Y until a specific timestamp. Beat task revokes expired
  grants automatically. Useful for vendor / contractor access
  windows.
- ⬜ **Comments / activity feed per resource** — Slack-style
  discussion thread on subnets / IPs / zones / scopes.
  Markdown-rendered comments + system-generated activity
  entries (deletes, edits, status changes). Powerful "who
  broke this" forensics aid; pairs with @-mention notifications.

#### Notifications & external integrations

- ⬜ **Email (SMTP) notifications** — first-class delivery
  channel for the alerts framework. `SMTPTarget` config row
  (host, port, TLS, auth, from-address). Templates for each
  alert rule type rendered from Jinja2. Today operators only
  get syslog / webhook. Already noted as v2 work in the alerts
  framework entry above; calling out separately so it doesn't
  stay buried.
- ⬜ **Slack / Teams / Discord webhooks** — chat-channel delivery
  for alerts + audit forward. One-click setup using the
  standard incoming-webhook URL for each platform. Templates
  render platform-specific blocks (Slack mrkdwn / Teams
  adaptive cards / Discord embeds).
- ⬜ **Generic outbound webhooks on resource changes** —
  operators subscribe to typed events (`subnet.created`,
  `ip.allocated`, `zone.modified`, etc.) at `/api/v1/webhooks`
  with a target URL + HMAC secret. Delivery worker replays
  from an outbox table with retry + DLQ. Distinct from
  audit-forward webhooks (which fire on every audit row);
  these are typed events for downstream automation.
- ⬜ **Ansible dynamic-inventory endpoint** —
  `/api/v1/ansible/inventory` returns Ansible-formatted JSON:
  groups by IP space / block / subnet / tag / custom field.
  Drop-in replacement for static inventory files. Token-auth
  scoped to read-only.
- ⬜ **ServiceNow CMDB integration** — bidirectional sync with a
  ServiceNow instance. Per-`ServiceNowInstance` row: instance
  URL + auth (basic / OAuth client-credentials / API key,
  Fernet-encrypted at rest). Phase 4 — pairs naturally with the
  alerts framework SMTP / webhook work above. **Mirror scope:**
  - **Push** IPAM subnets + IP rows to CMDB as
    `cmdb_ci_ip_network` + `cmdb_ci_ip_address` records via the
    Table API. Optional `cmdb_ci_network_adapter` for rows with
    a MAC. Provenance via `service_now_sys_id` column on each
    mirrored row so updates are PATCH not POST. Beat-driven
    reconciliation, same shape as the K8s / Docker mirrors but
    write-direction.
  - **Pull** asset ownership from CMDB → populate IPAM
    `owner_user_id` / `owner_group_id` / `managed_by` from the
    matching CI's `assigned_to` / `support_group` / `owned_by`
    fields. Operator-edits stay sticky via `user_modified_at`
    lock (same pattern as Proxmox / Tailscale rows).
  - **Ticket linkage** — operator pastes an INC / CHG / REQ
    number on an IP / subnet, SpatiumDDI resolves it to
    `sys_id` + `short_description` + `state` via the Table API
    and renders a clickable badge with deep-link back into
    ServiceNow. New `service_now_ticket_link(resource_type,
    resource_id, table, sys_id, number, short_description,
    state, link_url, created_at)` table.
  - **Auto-create CHG on risky ops** — subnet delete / resize /
    bulk-edit modals get an opt-in "Open ServiceNow change?"
    checkbox. SpatiumDDI POSTs a CHG with templated short
    description / planned start / category, captures the
    resulting number, and stamps it on the audit_log row.
  - **Self-service catalog item** — published catalog form
    "Request IP allocation" calls the SpatiumDDI API on
    fulfilment via a SNOW Flow. Decoupled — SNOW just hits our
    REST surface like any other client.
  - **Phasing.** Phase 1 = ticket-linkage badges + read-only
    pull (lowest risk, biggest immediate UX win). Phase 2 =
    CMDB push. Phase 3 = auto-CHG + catalog item. Each phase
    is separately enable-able from Settings → Integrations
    once it lands.
  - **Permission gate** `manage_servicenow` (admin-only). No
    new roles created — fits inside the existing RBAC grammar.
  - **Why this matters:** in enterprises that run SNOW as the
    asset source of truth, an IPAM tool that doesn't sync with
    CMDB ends up with stale owner / contact data within a
    quarter. The ticket-linkage piece alone closes the
    "where's the change request for this subnet?" forensics
    gap that operators currently solve by greping email.

#### Security & compliance

- ⬜ **2FA / MFA for local users** — TOTP enrolment via `pyotp`
  with recovery codes. `User.totp_secret` (Fernet-encrypted) +
  `User.mfa_enabled`. Login flow: password → TOTP prompt →
  JWT. Optional WebAuthn / FIDO2 in a follow-up.
- ⬜ **Password policy enforcement** — configurable
  `PlatformSettings.password_*`: min length, character classes,
  history (last N hashes), max age (force change after N days),
  lockout threshold. Today auth is bcrypt + force-change flag
  only.
- ⬜ **Account lockout after N failed logins** — windowed
  counter on `User.failed_login_count` +
  `failed_login_locked_until`. Resets on successful login.
  Operator-tunable threshold; superadmin bypass via "force
  unlock" admin action.
- ⬜ **Active session viewer + force-logout** — admin UI listing
  every live JWT (by `jti` cached in Redis), with last-IP /
  user-agent / login-method / age. "Revoke" adds the JTI to a
  revocation set checked at every auth-decode.
- ⬜ **Audit-log tamper detection** — chain hash on each
  audit_log row: `row_hash = sha256(prev_row_hash ||
  canonical_json(row))`. Stored in a new column; nightly
  verifier flags any chain break. Big tick for SOC2 / HIPAA
  audits; near-zero runtime cost on writes.
- ⬜ **API-token scopes** — per-token grants (read-only /
  IPAM-only / DNS-only / DHCP-only / agent-only). Today tokens
  are full-access JWT-equivalents. Storage: `APIToken.scopes`
  JSONB list checked at the auth layer alongside the existing
  permission gates.
- ⬜ **Subnet classification tags** — first-class `pci_scope` /
  `hipaa_scope` / `internet_facing` boolean flags on subnet
  (versus free-form custom fields) + a Compliance dashboard
  filtered by them. Common ask in regulated verticals —
  auditors love being able to ask "show me every PCI subnet,
  who owns it, when was it last changed."
- ⬜ **Internal cert + secret expiry monitoring** — alert rule
  type `secret_expiring` keyed off internal TLS certs
  (control-plane, agent comms), TSIG keys, API tokens, ACME
  accounts, and any Fernet-encrypted credential with an
  `expires_at`. Catches the "we forgot to rotate" failure mode
  before it pages someone at 3am.

#### UX polish

- ⬜ **Saved searches / saved views** — store filter + column +
  sort state per user as a named `SavedView(user_id, page,
  name, payload)`. Pinned to the page header dropdown.
  "All subnets in DC1 over 80% utilization, sorted by name"
  becomes a one-click view. Massive QoL.
- ⬜ **Personal pinned dashboard** — pin specific subnets /
  zones / scopes / IPs to a per-user home page. Stored as
  `UserPin(user_id, resource_type, resource_id, pinned_at)`.
  Operators get their habitual workspace as the default
  landing.
- ⬜ **Field-level history** — click any field on a resource
  detail page, see every past value with who / when. Powered
  by the existing audit_log + a JSON-diff renderer.
- ⬜ **Recent items / favourites sidebar** — last-N visited
  resources per user (browser-local) + an explicit star button
  for sticky favourites (server-side `UserFavourite`).
- ⬜ **Keyboard shortcut help overlay** — `?` opens a modal
  listing every binding (Cmd+K is great; growing the surface).
  One source of truth in `frontend/src/lib/shortcuts.ts`.
- ⬜ **Print / PDF export for IPAM tree + subnet detail** —
  server-rendered PDF (weasyprint) with stable page breaks +
  headers. Auditors and ops handovers love static deliverables;
  pairs with the compliance report PDF item above.

#### CLI tool

- ⬜ **`spddi` CLI** — stand-alone Python CLI published to PyPI
  as `spatiumddi-cli`. Auth via `~/.config/spddi/config`
  (token + URL). Commands mirror the REST surface:
  `spddi ip alloc`, `spddi subnet ls / show / split / merge`,
  `spddi zone export / import`, `spddi dhcp scope ls`, etc.
  Output supports `--format table|json|yaml`. Useful in scripts
  + ops handover when the UI is inconvenient. Built on `httpx`
  + `typer` (fast iteration).

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
