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
| `CLAUDE.md` | Index, conventions, non-negotiables, **pending** roadmap |
| `docs/SHIPPED.md` | Full design context for shipped roadmap items (migration ids, file paths, deferred follow-ups) — moved out of CLAUDE.md to keep the working list scannable |
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

SpatiumDDI cut its alpha release `2026.04.16-1` on 2026-04-16 with IPAM, DNS (BIND9), and DHCP (Kea) all shipping. Subsequent releases landed Windows Server integration (`2026.04.18-1`, 2026-04-18), the **performance + polish + visibility** release (`2026.04.19-1`, 2026-04-19) — batched WinRM dispatch, DDNS pipeline, the Logs surface, subnet/block resize, subnet-scoped IP import, DHCP pool awareness, collision warnings, sync modals, dashboard heatmap, draggable modals, standardised header buttons — the 2026.04.20 IPv6 + DDNS closure work, the **Kea HA + group-centric DHCP** release (`2026.04.21-2`, 2026-04-21) which shipped the full three-wave Kea HA story: end-to-end HA shake-out (peer URL resolution, port split, `status-get`, bootstrap reload), group-centric DHCP data model (scopes / pools / statics / classes live on `DHCPServerGroup`; HA is implicit with ≥ 2 Kea members), agent rendering fix (every prior Kea install was silently rendering `subnet4: []` due to a wire-shape bug), `PeerResolveWatcher` self-healing for peer IP drift, supervised Kea daemons, and standalone agent-only compose files for distributed deployments, the **integrations + observability** release (`2026.04.22-1`, 2026-04-22) that shipped Kubernetes + Docker read-only mirrors, the ACME DNS-01 provider, DHCP MAC blocklist, dashboard timeseries charts + platform-health card + collapsible sidebar, and the **Proxmox VE + polish** release (`2026.04.24-1`, 2026-04-24) which shipped the Proxmox endpoint mirror (bridges + SDN VNets + opt-in VNet-CIDR inference + per-guest Discovery modal), the shared `IPSpacePicker` quick-create component across all three integration modals, plus four UX polish fixes (real source IP behind nginx, alphabetised Integrations nav, wider Custom Fields page, search-row amber highlight), and the **network discovery + nmap** release (`2026.04.28-1`, 2026-04-28) which shipped SNMP polling of routers + switches with ARP/FDB cross-reference into IPAM (per-IP switch-port + VLAN visibility), the on-demand nmap scanner with live SSE output streaming (per-IP + `/tools/nmap` standalone page), the read-only `IPDetailModal` opened on row-click in IPAM with action buttons for Scan/Edit/Delete, sidebar regroup (core flattened, new Tools section, Administration items separated by dividers), removal of the dead Settings → Discovery section, and a linear-time rework of the BIND9 query log parser (CodeQL alert #16 closed), and the **device profiling** work landed 2026-04-30 as both phases at once: subnet-level opt-in auto-nmap on fresh DHCP leases (Phase 1, refresh-window dedupe + per-subnet 4-scan cap + `POST /ipam/addresses/{id}/profile` re-profile-now button) and passive DHCP fingerprinting via a scapy `AsyncSniffer` thread on the DHCP agent feeding a fingerbank lookup task (Phase 2, default-off, needs `cap_add: NET_RAW`), both surfaces converging in a unified "Device profile" panel inside the IP detail modal that shows passive Type/Class/Manufacturer + active OS guess + top open services. Same-day follow-ups: `setcap cap_net_raw+eip` on `/usr/bin/nmap` plus `NMAP_PRIVILEGED=1` in the api/worker image (so non-root operator OS scans actually work — Debian's nmap does an early `getuid()==0` check that ignores file caps), `securityContext.capabilities.add: [NET_RAW]` on the K8s worker + `worker.netRawCapability` Helm gate for restricted PSA / OpenShift-SCC / GKE Autopilot, and the Settings → IPAM → Device Profiling form for the fingerbank API key (Fernet-encrypted at rest; the response payload only exposes a boolean `fingerbank_api_key_set`). The post-device-profiling polish wave (also 2026-04-30) added two new nmap presets (`subnet_sweep` -sn for CIDR ping-sweeps capped at /16 worth of hosts, `service_and_os` -sV -O --version-light as the device-profiling default), CIDR-aware target validation + multi-host XML parsing (the runner now walks every `<host>` element and emits a `hosts[]` summary when >1), `POST /nmap/scans/bulk-delete` (cap 500, mixes cancel + delete based on per-row state) + `POST /nmap/scans/{id}/stamp-discovered` (claim alive hosts as `discovered` IPAM rows + stamp `last_seen_at` via nmap; integration-owned rows just bump the timestamp), `NmapToolsPage` rewritten as a 3-tab right panel (Live / History / Last result) with a checkbox column + bulk-delete toolbar on history, the new `discovered` status added to `IP_STATUSES_INTEGRATION_OWNED`, the IPAM subnet header collapsed from 9 buttons to 6 via a Tools dropdown (alphabetised: Bulk allocate…, Clean Orphans, Merge…, Resize…, Scan with nmap, Split…), a new "Seen" column in the IP table backed by a 4-state `SeenDot` (alive <24h green / stale 24h-7d amber / cold >7d red / never grey, source method in the tooltip — orthogonal to lifecycle status), and **bulk allocate** — `POST /ipam/subnets/{id}/bulk-allocate/{preview,commit}` stamps a contiguous IP range plus a name template (`{n}` / `{n:03d}` / `{n:x}` / `{oct1}`–`{oct4}`) in one shot with per-row conflict detection (already-allocated, dynamic-pool, FQDN collision) and `on_collision: skip|abort` policy, capped at 1024 IPs per call; `BulkAllocateModal` lives in the Tools menu with a three-phase form → preview → committed flow and live client-side template rendering as the operator types. Same wave also fixed three IPAM table polish items: the sticky `<thead>` finally holds in Chrome (the inner `<div className="overflow-x-auto">` wrapper was establishing a Y-scroll context per CSS spec — `overflow-x: auto` with `overflow-y: visible` computes to `overflow-y: auto` automatically, defeating sticky positioning by anchoring the head to a non-scrolling intermediate parent; removed the wrapper so sticky resolves to the outer `flex-1 overflow-auto`), shift-click range select on IP checkboxes (capture `e.shiftKey` in `onClick` which fires before `onChange`, walk the IP-only `tableRows` order between the previous click and the new one, toggle every selectable row to the new state), and subtle dashed-emerald gap-marker rows between non-adjacent IPAM entries (e.g. `.11 · 1 free` or `.11 – .13 · 3 free` — heads-up for "you deleted something and might have missed the hole"; suppressed inside dynamic DHCP pools where slots are owned by the DHCP server). For the full list see `CHANGELOG.md`. The forward-looking work is below.

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

### Major roadmap items (⬜ pending)

Forward-looking list of feature-level work that hasn't shipped yet —
each entry is the design context to start from when picking the item
up. Shipped (✅) items live in [`docs/SHIPPED.md`](docs/SHIPPED.md);
their "Deferred follow-ups" blocks (pending sub-items still attached
to a shipped parent) stay alongside the parent in that file rather
than getting hoisted here. Pure-greenfield ideas from the
2026.04.26 brainstorm pass live in their own categorised section
further down.

- ⬜ **Windows DNS — Path B (WinRM + PowerShell, full CRUD)** — zone
  creation / edit / delete, view config, server-level options. Uses
  `pypsrp`/`pywinrm` to invoke the `DnsServer` PowerShell module on the
  DC. Requires WinRM-over-HTTPS, a service account in `DnsAdmins`, and a
  credential-handling UI in the server form. Secure-only DDNS zones
  become manageable via GSS-TSIG once Kerberos ticket acquisition lands.
- ⬜ **Windows DHCP — Path B (WinRM + PowerShell, full CRUD)** — scope
  / reservation / client-class / option CRUD via `Add-DhcpServerv4Scope`,
  `Add-DhcpServerv4Reservation`, etc. Layered on top of Path A in the
  same driver class. Service account must be in `DHCP Administrators`
  rather than `DHCP Users`. Much bigger scope than DNS Path B since
  there's no wire-level admin protocol; every scope field becomes a
  cmdlet call.
- ⬜ IP discovery — ping sweep + ARP scan Celery task; flags `discovered` status; reconciliation report (see `docs/features/IPAM.md §8`)
- ⬜ **DNS Views — end-to-end split-horizon wiring** — `DNSView`
  model + CRUD ship today, but the BIND9 driver doesn't wrap zones
  in `view { match-clients …; zone { … }; }` blocks and record
  CRUD has no `view_id` assignment UI. The storage side is ready;
  what's missing is driver rendering + record-level view selection
  + UI binding on the record form. Phase 3.
- ⬜ **Multi-group DNS publishing (split-horizon at the IPAM
  layer)** — distinct from DNS Views above. Scenario: operator
  has one public-IP subnet hosting an internet-facing service +
  two DNS server groups (one internal, one external) and wants
  the same A record published into a zone in EACH group so
  internal resolvers and external resolvers both answer for that
  hostname. Today the IPAM → DNS pipeline is 1:1 — `IPAddress`
  carries a single `forward_zone_id`, `_resolve_effective_zone`
  walks subnet → block → space and returns one zone, and
  `_sync_dns_record` publishes one A/AAAA. Need 1:N: one IPAM
  row publishes N records, one per `(group, zone)` pin.

  **Smallest correct shape** (additive, two-day landing): keep
  `forward_zone_id` as the singular primary for backward compat,
  add `IPAddress.extra_zone_ids: list[uuid]` for the multi-zone
  case. Each zone naturally belongs to exactly one group, so
  multi-group fanout is implicit — no `dns_zone_ids_by_group`
  map needed. `_sync_dns_record` enumerates both fields and
  emits one A/AAAA per zone; the agent push pipeline already
  routes by zone-owning-group, so agent code is untouched.
  Same shape applies at Subnet / Block / Space inheritance —
  the existing `dns_additional_zone_ids` columns already store
  group-agnostic zone lists; the constraint is in the picker UI.
  **Cleaner long-term shape**: many-to-many join table
  `ip_address_zone_publish(ip_id, zone_id)` retiring
  `forward_zone_id`; bigger migration, touches every record
  publish path. Start additive for v1 — promote to the join
  table only if dual-storage gets in the way.

  **Safety gates** (the part the operator actually asked
  about):
  1. **Per-subnet opt-in** `Subnet.dns_split_horizon: bool`
     (default false), inheritable from Block. When off, the
     zone picker stays single-group like today — current
     behaviour, no surprise. When on, picker becomes a
     multi-select grouped by DNS group. Don't auto-enable for
     public CIDRs; `ipaddress.is_private` is fragile (doesn't
     cleanly cover ULA / CGNAT) and operators legitimately
     run split-horizon on RFC 1918 too (e.g. two internal
     views).
  2. **`DNSServerGroup.is_public_facing: bool`** + a server-
     side guard. When an operator pins a private subnet's IP
     into a public-facing group, return 422 with
     `requires_confirmation` (mirrors the existing
     `_check_ip_collisions` shape) so the modal can render
     "This is RFC 1918 — publishing to `{group}` exposes
     internal IPs to a publicly-facing resolver. Type the
     CIDR to confirm." This is the actual safety net —
     catches the misconfiguration whether or not split-
     horizon is on.

  Pairs naturally with DNS Views (entry above) — Views are
  per-zone match-clients filtering (same SOA, different
  answers per source); this entry is per-IP fanout across
  zones in different groups (different SOAs, two
  authoritative resolvers each serving the right one). Real
  operators want both. Phase 3 — fits the same wave as the
  DNS Views finish-line.
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

### Integration roadmap (⬜ pending)

Same read-only-pull reconciler shape as Kubernetes/Docker — each
one gets a `*Target` row type, Settings → Integrations toggle,
sidebar entry, and 30 s beat sweep with per-target interval
gating. Ranked by homelab/SMB test accessibility + IPAM value so
operators can exercise them in their own lab without standing up
cloud accounts. Shipped integrations (Kubernetes, Docker,
Proxmox, Tailscale Phase 1+2) live in
[`docs/SHIPPED.md`](docs/SHIPPED.md). The ServiceNow CMDB item in
the brainstorm section follows a different shape — bidirectional
write surface, not a read-only pull mirror.

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

- ⬜ **Cloud connectors — unified "Cloud" integration with
  per-provider picker (Azure / AWS / GCP)** *(tier 3 —
  lab-inaccessible, enterprise-driven).* One Settings →
  Integrations card labelled **Cloud**; the create modal asks
  for provider first, then renders the provider-specific
  credential form with an embedded setup guide (CLI commands
  + console click-through). Every materialised row provenances
  via `cloud_endpoint_id` FK with `ON DELETE CASCADE` so
  removing the endpoint sweeps mirror rows atomically.

  **Data model.** Single `cloud_endpoint` row with
  `provider` enum (`aws | azure | gcp | hetzner | digitalocean
  | linode | vultr` — only the first three actually
  implemented at first) plus a JSONB
  `credentials_encrypted` blob (Fernet-encrypted, schema
  varies per provider). Standard fields:
  `name`, `description`, `space_id` (required — discovered
  IPs / blocks land there), `dns_group_id` (optional, mirrors
  the K8s shape), `sync_interval_seconds` (default 300, min
  60), `regions` (string list — empty = all), `last_synced_at`,
  `last_sync_error`, `mirror_load_balancers` (default true),
  `mirror_stopped_instances` (default false).

  **Per-provider credential schema:**
  - **Azure**: `tenant_id`, `client_id`, `client_secret`,
    `subscription_ids[]`. Setup: `az ad sp create-for-rbac
    --name spatiumddi --role Reader --scopes /subscriptions/
    <sub-id>` outputs all four fields. Reader is the
    least-privileged role with read access to the four
    resource types we mirror (VNets, subnets, NICs, LBs).
  - **AWS**: `access_key_id`, `secret_access_key`. Setup:
    create IAM user with `AmazonVPCReadOnlyAccess` +
    `AmazonEC2ReadOnlyAccess` +
    `ElasticLoadBalancingReadOnly`; attach those three
    managed policies, generate access key, paste into the
    form.
  - **GCP**: `service_account_json` (entire JSON key file
    as a single string, Fernet-encrypted), `project_ids[]`.
    Setup: `gcloud iam service-accounts create spatiumddi
    --display-name "SpatiumDDI"` + `gcloud projects
    add-iam-policy-binding <proj> --member
    serviceAccount:... --role roles/compute.viewer` +
    `gcloud iam service-accounts keys create key.json
    --iam-account ...`.

  **Mirror scope (structurally identical across providers):**
  - VNet (Az) / VPC (AWS) / VPC Network (GCP) → `IPSpace`
    (auto-create with vendor-prefixed name); the address
    space CIDR(s) → `IPBlock` rows under it.
  - subnet (Az) / subnet (AWS) / subnetwork (GCP) →
    `Subnet` (gateway derived from per-vendor convention —
    Az x.x.x.1, AWS x.x.x.1, GCP x.x.x.1).
  - VM NIC private IP (Az) / EC2 ENI (AWS) / GCE NIC (GCP)
    → `IPAddress` with `status="cloud-vm"`, hostname =
    `<resource>.<resource_group>` (Az) / `<instance>.<region>`
    (AWS) / `<instance>.<zone>` (GCP).
  - Public IPs / EIPs / external addresses → `IPAddress`
    with `status="cloud-public"` in a separate per-endpoint
    "public" IPSpace (or operator-chosen).
  - LB frontend IP (Az LB / AWS ELB|ALB|NLB / GCP forwarding
    rule) → `IPAddress` with `status="cloud-lb"`. Once
    `LBMapping` lands these flow into it with full backend
    pool membership.

  **Connectors:** Azure via `azure-mgmt-network` +
  `azure-mgmt-compute` + `azure-identity` (ClientSecret
  credential), AWS via `boto3` (per-region client fan-out),
  GCP via `google-cloud-compute` + `google-cloud-network`
  + `google-auth`. Each connector is a thin reconciler under
  `services/cloud/<provider>.py` that returns a normalised
  `CloudInventory` dataclass; the shared
  `services/cloud/reconcile.py` does the IPAM upsert. Same
  `user_modified_at` lock pattern as Proxmox / Docker / K8s
  rows so operator edits stay sticky.

  **Phasing (recommended).** Phase 1 = Azure (simplest auth
  flow, user-familiar). Phase 2 = AWS. Phase 3 = GCP. Each
  phase is independently shippable since the data model is
  the same; `provider` enum gates the connector dispatch.

  **Permission gate** `manage_cloud_endpoints` (admin-only).
  Optional new "Cloud Operator" builtin role — usually fits
  inside Superadmin in practice.

  **Explicit non-goals.** Writing back to the cloud (no
  resource creation, no NSG / Security Group edits, no
  instance start/stop). Cloud DNS (Route 53 / Azure DNS /
  Cloud DNS) is a **separate write-side driver family**
  already on the roadmap — it does NOT live in the cloud
  endpoint row. Tags / NSGs / route tables surface as
  `custom_fields` passthrough at most; Phase 4+ if operators
  ask. Per-provider quirks (Azure availability sets, AWS
  placement groups, GCP shared VPC) deferred — start with
  the four resource types above.

- ⬜ **Load balancer family (F5 BIG-IP, HAProxy, nginx,
  KEMP, A10, Citrix ADC)** *(tier 2 — F5 first, others
  follow).* Read-only mirror of VIPs + pools + members from
  external load balancers into a new first-class
  `LBMapping` table that parallels the existing
  `NATMapping` shape (operator-curated 1:1 / PAT / hide-NAT
  rules surface in the per-IP modal + per-subnet "NAT"
  tab; load-balancer mappings would surface the same way
  with "VIP" / "backend" role badges). The data-model
  payoff: per-IP modal answers "is this a VIP? what
  backends serve it?" / "is this a pool member behind
  what VIP?", per-subnet view shows every VIP in range,
  and operators can finally tell load-balancer IPs apart
  from regular host IPs in the IP table.

  **Decision points still open** — confirm before building:
  1. **Manual-only first, or integration-driven only?**
     NAT mappings work as manual-entry today and we
     accept the staleness; LB mappings change much more
     often (pool membership shifts on every deploy /
     auto-scale event). Recommendation: gate on the
     integration, no manual-entry shipping. The data is
     only useful when fresh.
  2. **`LBMapping` shape**: `vip_ip` (FK to `IPAddress`,
     nullable), `vip_port`, `protocol` (tcp/udp/icmp),
     `pool_name`, `description`, `lb_endpoint_id` FK to
     the integration row that materialised it (nullable
     for any future manual-entry path), `members` JSONB
     `[{ip, port, weight, state}]` with optional FKs to
     live `IPAddress` rows on each member.
     Provenance via `lb_endpoint_id` with
     `ON DELETE CASCADE` so removing the integration
     sweeps mirror rows atomically (mirrors how
     `kubernetes_cluster_id` / `proxmox_node_id` work).
  3. **Phasing.** Phase 1 = `LBMapping` table +
     `F5Endpoint` row + reconciler over iControl REST
     (token auth, Fernet-encrypted at rest, partition-
     aware). Phase 2 = HAProxy stats socket / Runtime
     API + nginx Plus API readers. Phase 3 = KEMP / A10
     / Citrix ADC. Cloud LBs (AWS ELB / Azure LB / GCP
     LB) ride along with the Cloud VPC integration
     family above and write into the same `LBMapping`
     table — the schema is shared; the connector is
     vendor-specific.
  4. **F5 specifics**: per-`F5Endpoint` row covers a
     single BIG-IP (or cluster — iControl normalises
     standalone vs HA pairs). Mirror VIPs from
     `/mgmt/tm/ltm/virtual` and pools from
     `/mgmt/tm/ltm/pool`. Member health is in
     `/mgmt/tm/ltm/pool/~<partition>~<pool>/members`.
     iRule / iApp / partition surface deferred — start
     with the basic VIP + pool + members triple.
  5. **Permission gate** `manage_load_balancers`
     (admin-only). New "Load Balancer Editor" builtin
     role for ops teams that need write access to the
     manual-entry path (if it ever lands) without full
     superadmin.

  **Explicit non-goals**: writing config back to the
  load balancer (no `iControl REST PUT`, no HAProxy
  reload), managing certificates on F5 (separate
  surface; pairs with the embedded ACME client item
  on the roadmap), iRule / iApp authoring. The mirror
  is for IPAM context, not LB administration.

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
Everything below is ⬜ pending; brainstorm items that have
since shipped (Switch-port mapping, OUI lookup, SNMP polling,
LLDP collection, nmap, CIDR calculator + Subnet planner +
Address planner, DNS templates / propagation check / catalog
zones / RPZ, DHCP option library, ACME provider, alerts
framework, dashboard time-series, …) live in
[`docs/SHIPPED.md`](docs/SHIPPED.md) under the matching
sub-headings.

#### Discovery & network awareness

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

All shipped — see `Subnet planning & calculation tools` in
[`docs/SHIPPED.md`](docs/SHIPPED.md): CIDR calculator,
Subnet planner workspace, address planner, aggregation
suggestion, free-space treemap.

#### DNS-specific

- ⬜ **DNSSEC** — sign zones, manage KSK / ZSK rollover, NSEC3
  parameters, DS-record export to upstream. BIND9 supports
  inline signing (`dnssec-policy` configs in 9.16+). Storage:
  `DNSSECPolicy` + `DNSKey` tables tracking key material
  (Fernet-encrypted) and rollover state. Massive compliance ask
  in regulated verticals; table-stakes for any "enterprise DDI"
  comparison.
- ⬜ **DoT / DoH listener** — BIND 9.18+ supports DNS-over-TLS
  (`tls`) and DNS-over-HTTPS (`https`) natively. Driver renders
  the listener config; cert lifecycle ties into the ACME
  embedded-client item already on the roadmap.
#### DHCP-specific

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
