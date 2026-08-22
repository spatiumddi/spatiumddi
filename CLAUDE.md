# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **GitHub Org:** https://github.com/spatiumddi  
> **Docs:** https://www.spatiumddi.com  
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
| `docs/THIRD_PARTY.md` | Catalogue of every bundled/shipped third-party component — engine, library, OS package — with license, the artifact it ships in, and the rationale. Operator-facing companion to the root `NOTICE` (which stays authoritative for license text). **When you add a shipped component, update BOTH** |
| `docs/features/IPAM.md` | IP Space/Block/Subnet/Address management, VLAN/VXLAN, custom fields, import/export, tree UI |
| `docs/features/DHCP.md` | DHCP servers, scopes, pools, static assignments, DDNS, caching, Windows DHCP (Path A) |
| `docs/features/DNS.md` | DNS servers, zones, records, views, server groups, blocking lists, DDNS, zone tree, Windows DNS (Path A + B), Technitium, encrypted transports (DoT / DoH / DoQ), DNS threat analytics, sync-with-servers reconciliation |
| `docs/features/AUTH.md` | Authentication, LDAP/OIDC/SAML, roles, group-scoped permissions, API tokens |
| `docs/features/ACME.md` | ACME DNS-01 provider — acme-dns-compatible HTTP surface for LE / public-CA cert issuance |
| `docs/features/INTEGRATIONS.md` | Read-only Kubernetes + Docker mirror integrations; setup, semantics, dashboard surface |
| `docs/features/MIGRATION.md` | One-shot importers — DNS (BIND9 / Windows DNS / PowerDNS / Technitium) + DHCP (Kea / Windows DHCP / ISC dhcpd.conf) + NetBox → IPAM, into native rows; preview → commit, provenance, IPAM linkage. Also the **Windows → SpatiumDDI cutover** (#756), which is *not* an importer — parity → parallel run → per-item switch + rollback → decommission |
| `docs/features/LOOKING_GLASS.md` | BGP Looking Glass — receive-only GoBGP collector peering with operator routers; Sessions + Routes grid, IPAM/ASN/VRF linkage at ingest, `bgp_lg_*` alerts, as-path Query tab + collector-vantage tools; distinct from the #527 public-table hijack monitor. The MetalLB BGP-mode VIP advertiser (#566 D1) ships alongside it, opt-in (`bgp.enabled=false`, `frrk8s.enabled=false` by default; enabling pulls in FRRouting / GPL-2.0) |
| `docs/features/VERTICALS.md` | Vertical network awareness — AV-over-IP (Dante / AES67 / SMPTE 2110) flow descriptors + reserved multicast ranges, BACnet/IP device-instance registry + BBMD conformity, Industrial-OT device inventory + Purdue zoning, DICOM AE Title registry + peer-association map. Four default-on Network feature modules (`network.av` / `network.bacnet` / `network.ot` / `network.dicom`); registry + conformity only, no network probing (and why each discovery phase is deferred). Also the un-gated fragile-device `do_not_probe` flag (#722) that suppresses SpatiumDDI's own active probes |
| `docs/PERMISSIONS.md` | RBAC permission grammar (`{action, resource_type, resource_id?}`), builtin roles, wildcards |
| `docs/features/SYSTEM_ADMIN.md` | System config, health dashboard, notifications, backup/restore, service control |
| `docs/deployment/APPLIANCE.md` | OS appliance build, base OS selection, licensing |
| `docs/deployment/DNS_AGENT.md` | DNS agent/container architecture — image layout, auto-registration, config sync, K8s shape |
| `docs/deployment/DOCKER.md` | Docker Compose setup, ports, first-time setup, TLS, HA, password reset |
| `docs/deployment/TOPOLOGIES.md` | Six reference deployment topologies — single VM, separated agents, DNS+DHCP HA, HA control plane (Patroni / Redis Sentinel), hybrid cloud, K8s — with SVG diagrams + sizing notes |
| `docs/deployment/KUBERNETES.md` | Umbrella Helm chart walkthrough — HPA, Ingress / LoadBalancer, CloudNativePG + Redis Sentinel HA (see also `k8s/README.md` + `charts/spatiumddi/README.md`) |
| `docs/deployment/BAREMETAL.md` | Bare-metal/VM paths — Docker Compose on a host, Patroni HA Postgres overlay, OS appliance (no Ansible playbooks; that path is planned, not implemented) |
| `docs/deployment/WINDOWS.md` | Windows Server prerequisites — WinRM, service accounts (DnsAdmins / DHCP Users), firewall, zone dynamic-updates; shared by Windows DNS + Windows DHCP |
| `k8s/README.md` | Kubernetes manifest usage, HA PostgreSQL (CloudNativePG), Redis Sentinel |
| `k8s/base/` | Core K8s manifests (namespace, API, worker, frontend, migrate job) |
| `k8s/ha/` | HA add-ons: CloudNativePG cluster, Redis Sentinel, Patroni Compose |
| `docs/drivers/DHCP_DRIVERS.md` | Kea + Windows DHCP driver internals |
| `docs/drivers/DNS_DRIVERS.md` | BIND9 + PowerDNS + Technitium (agent-managed + agentless `technitium_api`) + Windows DNS (Path A + B) driver internals, incremental update strategy |

---

## Technology Stack (Summary)

| Layer | Technology |
|---|---|
| Backend API | Python 3.12+, FastAPI, SQLAlchemy 2.x (async), Alembic |
| Task Queue | Celery + Redis |
| Frontend | React 18 + TypeScript, Vite, shadcn/ui, Tailwind, React Query |
| Database | PostgreSQL 16 (HA via Patroni or CloudNativePG) |
| Cache / Sessions | Redis 7 |
| Auth | python-jose + bcrypt (local), ldap3 (LDAP), joserfc (OIDC ID-token / JWKS), python3-saml (SAML), pyrad (RADIUS), tacacs_plus (TACACS+); Fernet for secrets at rest |
| Logging | structlog → JSON → centralized log store (Loki / Elasticsearch) |
| Metrics | Prometheus + Grafana (InfluxDB push export is **planned, not built** — [#889](https://github.com/spatiumddi/spatiumddi/issues/889)) |
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
  drivers/dns/          DNS backend abstraction + BIND9 / PowerDNS / Technitium (agent + API) / Windows DNS impls
  drivers/dhcp/         DHCP backend abstraction + Kea impl
  tasks/                Celery tasks (dns_health, dhcp_health, sweep_expired_leases, …)
  core/, db.py, config.py, celery_app.py
backend/alembic/        Migrations (tracked in git — do not re-add to .gitignore)
frontend/src/
  pages/                Top-level routes (ipam/, dns/, dhcp/, admin/, settings/)
  components/           Shared UI; shadcn/ui primitives under components/ui/
  lib/api.ts            All API clients (ipamApi, dnsApi, dhcpApi, …)
  hooks/                Incl. useSessionState (sessionStorage-backed useState)
agent/dns/              Standalone DNS agent (Python) + BIND9 / PowerDNS container images
agent/dhcp/             Standalone DHCP agent (Python) + Kea container image
agent/supervisor/       Spatium supervisor (Python) + container image — host-side controller
                        for the upcoming Application install role (#170 Wave A — scaffolding
                        only, dormant on every existing install)
k8s/base/               Core manifests (api, worker, frontend, migrate)
k8s/{dns,dhcp}/         Per-service StatefulSets + services
k8s/ha/                 CloudNativePG, Redis Sentinel, Patroni
charts/spatiumddi/      Umbrella Helm chart (API + FE + worker + beat + migrate + Postgres/Redis subcharts + optional DNS/DHCP agents)
scripts/seed_demo.py    Demo data seeder
docs/                   Specs + Jekyll site (published to BOTH Pages sites — see Documentation sites note below)
website/                Marketing site source (designed in Claude Designer; needs a custom domain — the github.io root now serves docs; see Marketing Website note below)
```

> **Documentation sites (`/docs`).** The Jekyll docs in `docs/` are served from **two** GitHub Pages sites, and it matters which one you are looking at:
>
> | URL | Pages site type | Source | Tracks |
> |---|---|---|---|
> | `www.spatiumddi.com` | organization site | repo `spatiumddi/spatiumddi.github.io` | **`main`** (published by CI) |
> | `www.spatiumddi.com/spatiumddi/` | project site | this repo, `main` branch, `/docs` path | **`main`** |
>
> The custom domain `www.spatiumddi.com` is configured on the **org-site repo** (its `CNAME` file), which is why *both* URLs live under it — setting a custom domain on an organization site moves its project sites too. The repo keeps the `spatiumddi.github.io` name because that name is what makes GitHub serve it as the org site at all; the domain sits on top. `spatiumddi.github.io` 301s to the custom domain. **Never add a `CNAME` to `docs/`** — it would be mirrored to the site repo *and* applied to the project site, pointing the domain at the wrong one; `docs-publish.yml` excludes `CNAME` from its `rsync --delete` so the real one survives releases. There is **no `gh-pages` branch** — both sites build from a `/docs` path, and the project site needs no workflow at all. The org-root site exists because GitHub only ever serves an org's root Pages site from a repo named `<org>.github.io`; that repo holds **no sources of its own** and is mirrored from `docs/` by `.github/workflows/docs-publish.yml` on every `main` push and release tag (auth: the `DOCS_DEPLOY_KEY` secret, an SSH deploy key with write access to the site repo — `GITHUB_TOKEN` cannot push cross-repo). Never hand-edit the site repo; the next release overwrites it. Both sites therefore serve the same content; the root is the canonical one, and the one the README badge, `docs/sitemap.xml` and `docs/robots.txt` advertise. The root was originally release-pinned, but that let a docs fix sit unpublished behind the release cadence — the wrong trade for documentation, so it now publishes on every `main` push (and on release tags, which is a redundant-but-deterministic republish). `docs/_config.yml` deliberately sets **no `baseurl`**, and none should be added: GitHub Pages injects the correct one per build (empty for the org site, `/spatiumddi` for the project site), and hardcoding either value overrides that and breaks the other site. The layouts in `docs/_layouts/` address assets by a **relative prefix** computed from page depth rather than by `baseurl`, because baseurl is empty for a local `jekyll build` — that is what makes the site previewable before publishing. See [#751](https://github.com/spatiumddi/spatiumddi/issues/751).

> **Marketing website (`/website`).** Operator-facing landing page is authored in Claude Designer and lives in `website/`. It is **not yet built**, and it no longer has a home: `www.spatiumddi.com` — the domain this note originally earmarked for it — now serves the **docs** (above). So the marketing site needs either a different hostname or a decision to move the docs to `docs.spatiumddi.com` and give it the apex. Cloudflare Pages can build it straight from `website/` in this monorepo, needing no second repo and no cross-repo sync. Tracked in [#754](https://github.com/spatiumddi/spatiumddi/issues/754). When editing the marketing site, leave the Jekyll docs alone; when editing the Jekyll docs, leave the marketing site alone. Both can ship in the same PR but never as the same artifact. Open questions: which static-site generator to lock in (raw HTML / Astro / Next.js static), whether to mirror the README screenshots / feature table here, and the CI pipeline (separate workflow that builds + publishes to a `marketing` branch / Cloudflare Pages on every `website/**` change). See `website/README.md` (once it lands) for the deployment recipe.

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
13. **MCP coverage for new features**: When adding a resource or feature with REST endpoints, also expose matching MCP tools for the operator copilot (`find_*` / `count_*` reads, plus `propose_*` writes where mutation makes sense). Each tool's default-enabled state must be an explicit decision — default to enabled so admins discover what exists, *unless* the surface exposes secrets, has broad-blast-radius writes, or makes off-prem calls (those default to disabled and the operator opts in)
14. **Feature-module gating for new top-level surfaces**: When adding a new top-level resource family (sidebar section, REST router prefix, MCP tool cluster), evaluate whether it should be a togglable feature module. If yes: (a) add a `ModuleSpec` to `app.services.feature_modules.MODULES`, (b) seed a row in a migration alongside the model migration, (c) apply `dependencies=[Depends(require_module("…"))]` to the router include in `app/api/v1/router.py`, (d) tag MCP tools with `module="…"` in their `register_tool(...)` call, (e) carry `module: "…"` on the matching sidebar `NavItem` definition. Default-enabled, per #13's discovery argument — operators turn off what they don't use
15. **New integrations show up on the Dashboard — both surfaces**: When adding an integration mirror (Kubernetes / Docker / Proxmox / Tailscale / UniFi shape — read-only pull reconciler with per-target rows), wire it into BOTH dashboard surfaces: (1) the `IntegrationsPanel` inside the IPAM tab on `frontend/src/pages/DashboardPage.tsx` — add the `useQuery` gated on the `integration_*_enabled` flag, thread `enabled` + row list through props, extend column-count + grid cn() case, add a panel block following the existing icon + name + count + view-all + per-row `IntegrationRow` pattern; (2) the dedicated **Integrations dashboard tab** at `backend/app/api/v1/dashboards/integrations.py` — append a target query, add a `_build_panel(...)` entry to the `panels` list, register the new resource_type string in `_INTEGRATION_RESOURCE_TYPES` so reconciler error-audit rows surface in the recent-errors list, and extend the frontend `IntegrationDashboardKind` union in `lib/api.ts`. Both surfaces are operator-facing health rollups; missing either one means a new integration is invisible somewhere it should be obvious
16. **Per-role node-label gating for every new workload**: Every new top-level workload (Deployment / StatefulSet / DaemonSet) added to `charts/spatiumddi/` or `charts/spatiumddi-appliance/` gates scheduling on a per-role node label (`spatium.io/role-<service>=true`), not on chart-render `values.<svc>.enabled` toggles. The `nodeSelector` block merges `global.nodeSelector` (the umbrella `spatium.io/role=appliance` gate) AND a per-role label. Labels are stamped by two paths that already exist: install-time bake in `appliance/mkosi.extra/usr/local/bin/spatium-install`'s `config.yaml.d/spatium-roles.yaml` drop-in for `full-stack` / `control-only` variants; dynamic apply via the supervisor's `kubectl label` (`agent/supervisor/spatium_supervisor/k8s_api.py`) for `application` variants on role-assignment changes. `enabled: false` values stay as a global suppression knob; the per-role label is the source of truth for *which* node a workload lands on. Reference pattern: `charts/spatiumddi-appliance/templates/{dns-bind9,dhcp-kea}.yaml`. Without this, multi-node HA (#272) silently schedules control-plane workloads on DNS-only nodes — invisible misplacement that won't surface until the first node loss.

---

## Cross-cutting Patterns

Three patterns recur across the DNS and DHCP subsystems. Know these before adding a backend feature.

1. **Driver abstraction.** `backend/app/drivers/{dns,dhcp}/base.py` defines an ABC + neutral dataclasses (`ScopeDef`, `ZoneDef`, `ConfigBundle`, etc). Concrete drivers (`bind9.py`, `kea.py`) render backend-specific config from those dataclasses. The services layer only speaks to the ABC via the driver registry — never import a concrete driver from a service.

2. **ConfigBundle + ETag long-poll.** The control plane assembles a `ConfigBundle` from DB state and hashes it to a sha256 ETag (`backend/app/services/{dns,dhcp}/config_bundle.py`). The agent long-polls `/config` with its last-seen ETag; the server blocks until the ETag changes (or timeout) and only then returns a new bundle. When you add a field that affects rendered config, verify it flows into the bundle so the ETag shifts — otherwise agents will not pick up the change. **Redis wake (#358):** the long-poll no longer blind-polls the DB every 2 s — it waits on a Redis pub/sub channel (`backend/app/core/agent_wake.py`) that config-mutating handlers publish to *after commit* (DNS records via the `enqueue_record_op` chokepoint + the `wake_publishing` router dependency that flushes `collect_wake`; DHCP/structural handlers call `collect_wake` directly; Celery workers call `publish_wake` over `settings.redis_url`). So when you add a new config-mutating endpoint, also `collect_wake(...)` the affected `dns_group`/`dhcp_group`/`dhcp_server` channel (or it converges only on the 12 s `WAKE_TICK_SECONDS` safety tick). The ETag compare stays the source of truth — the wake is advisory; if Redis is down the loop falls back to the 2 s poll, so the wake is never the sole delivery path (non-negotiable #5). **Supervisor heartbeat (#358 Phase 1):** the same bus also wakes the supervisor heartbeat long-poll — per-appliance desired-state changes (fleet upgrade / reboot / role-assign via `update_appliance_roles`, per-appliance firewall, plus the shared `HOSTCONFIG_ALL` broadcast) `publish_wake(appliance_channel(id))` after commit, and the heartbeat holds on `appliance_wake_channels(row) + [HOSTCONFIG_ALL]` when the supervisor opts in via `wait_seconds` (HTTP-only on the agent side — remote supervisors that can't reach `sentinel://` just fall back to the heartbeat interval). So a new per-appliance desired-state endpoint should also `publish_wake(appliance_channel(id))` after its commit. Phases 2–3 (the fleet-scale broker threshold + the Mosquitto escalation seam, both deferred) are written up in `docs/OBSERVABILITY.md` — Redis stays the transport until the documented threshold is crossed.

3. **Agent bootstrap + reconnection.** The agent joins with a pre-shared key (the DNS agent reads `DNS_AGENT_KEY`; the DHCP agent reads `SPATIUM_AGENT_KEY` — `DHCP_AGENT_KEY` is the control-plane-side env that gets interpolated into the agent's `SPATIUM_AGENT_KEY` at deploy time), exchanges it for a rotating JWT, and caches the JWT on disk. On **401 or 404** the agent re-bootstraps from the PSK (the 404 case covers stale server rows after a control-plane reset). The local config cache under `/var/lib/spatium-{dns,dhcp}-agent/` lets the service keep running if the control plane is unreachable (non-negotiable #5).

---

## Project Phase Roadmap

| Phase | Focus | Status |
|---|---|---|
| 1 | Core IPAM, local auth, user management, audit log, Docker Compose | **Done** — LDAP/OIDC/SAML + RADIUS/TACACS+ auth, group-based RBAC enforcement, bulk-edit tags/CF, inherited-field placeholders, mobile-responsive UI, and full IPv6 allocation all landed |
| 2 | DHCP (Kea), DNS (BIND9), DDNS, zone/subnet tree UI | **Done** — DNS core, Kea DHCPv4, subnet-level DDNS, agent-side Kea DDNS, block/space DDNS inheritance, and per-server zone serial reporting all landed |
| 3 | DNS views, server groups, blocking lists, VLAN/VXLAN, system admin panel, health dashboard | **Done** — DNS views storage, groups, blocklists, health checks, Trivy-clean + kind-AXFR acceptance tests landed; end-to-end split-horizon view rendering ([#24](https://github.com/spatiumddi/spatiumddi/issues/24)) closed the last gap in `2026.06.04-1` |
| 4 | OS appliance image, Terraform/Ansible providers, SAML, notifications, backup/restore, ACME (DNS-01 provider + embedded client) | **In Progress** (SAML SP landed in Wave A.4; alerts framework, OS appliance image, backup/restore, and ACME — both the DNS-01 provider and the embedded Let's Encrypt client (#438) — all landed; Terraform/Ansible providers still pending) |
| 5 | Multi-tenancy, IP request workflows, import/export, advanced reporting | **In Progress** — import/export (DNS + DHCP + NetBox importers, IPAM CSV/JSON/XLSX, plus the guided Windows cutover [#756](https://github.com/spatiumddi/spatiumddi/issues/756)), advanced reporting (Top-N #47, utilization history #44, compliance PDF #48) and the self-service request portal ([#696](https://github.com/spatiumddi/spatiumddi/issues/696)) landed; multi-tenancy still pending |

### Current state

SpatiumDDI has been shipping CalVer releases since its alpha `2026.04.16-1`, and the working set is large: IPAM, DNS (BIND9 / PowerDNS / Windows / four cloud providers), DHCP (Kea / Windows), the OS appliance with atomic A/B upgrades and multi-node control-plane HA, ~20 read-only integration mirrors, the Operator Copilot, and the compliance loop.

**Where to look up what shipped, and when:**

| Question | Source |
|---|---|
| What landed in release X? | [`CHANGELOG.md`](CHANGELOG.md) — one section per release, authoritative |
| How does shipped feature Y work, and why was it built that way? | [`docs/SHIPPED.md`](docs/SHIPPED.md) — design context, migration ids, file paths, deferred follow-ups |
| What is still pending? | The ⬜ / 🟡 roadmap sections below, which ARE kept current |

> **Why there is no prose summary of shipped work here.** There used to be: a single ~73,000-character paragraph, 52% of this entire file. It was not maintained per-release (release prep updates `CHANGELOG.md` and flips the 🟡→✅ markers below, not that paragraph), so it drifted; and because it mentioned nearly every identifier in the project, any `grep` against `CLAUDE.md` returned it and buried the real hit. Both problems are solved by pointing at the files that are actually kept current. Keep it that way: **record new work in `CHANGELOG.md` and flip the marker below — do not start a new narrative here.**

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

### Appliance architecture pivot (#170, 2026-05-14)

The Application appliance role + `spatium-supervisor` + approval workflow shipped in [#170](https://github.com/spatiumddi/spatiumddi/issues/170) waves A–D on 2026-05-14:

- **Wave A** — scaffolding. New `spatium-supervisor` container in `agent/supervisor/spatium_supervisor/`; supervisor identity (Ed25519 keypair on `/var/persist/spatium-supervisor/`, appliance row in `pending_approval` state); pairing codes reshape (drops `deployment_kind`, adds `persistent` + `enabled` + `max_claims` + Fernet-encrypted reveal); container images baked into the appliance OS rootfs (`/usr/lib/spatiumddi/images/*.tar.zst`) for air-gap-ready installs; A/B slots bumped from 4 GiB to 8 GiB each.
- **Wave B** — provisioning. Internal CA + cert lifecycle (RSA-2048 self-signed root, lazy-bootstrap on first approve, 90-day supervisor cert signed against the supervisor's Ed25519 pubkey); admin approve / reject / delete / re-key endpoints; supervisor `/poll` + `/heartbeat` endpoints with session-token interim auth; installer wizard collapsed from 5 roles to 3 (`full-stack` / `frontend-core` / `application`); the Application install prompt asks only control-plane URL + 8-digit pairing code; Approvals frontend tab with pending queue + drilldown.
- **Wave C** — service vs supervisor split. All four host bind mounts (`/etc/spatiumddi-host`, `/boot/efi-host`, `/var/lib/spatiumddi-host/release-state`, `/run/udev`) move off the DNS / DHCP service containers onto the supervisor; `slot_state.py` ported to `appliance_state.py` (one impl instead of three); supervisor heartbeat persists slot telemetry + reads back `desired_appliance_version` / `desired_slot_image_url` / `reboot_requested` for trigger-file firing; role assignment endpoint with capability gate (supervisor must advertise `can_run_dns_bind9=true` etc.) + multi-role + DHCP `network_mode: host` vs `bridged` for relay deployments; per-role nftables drop-in renderer (`/etc/nftables.d/spatium-role.nft`) with always-open mgmt rules + per-role service ports + operator-pasted override fragment, `nft -c -f` dry-run before live-swap.
- **Wave D** — fleet UI + MCP + docs. `Approvals` tab renamed to `Fleet`; drilldown grows an OS & lifecycle block (per-slot version chips, schedule OS upgrade, cancel pending, reboot host with double-confirm checkbox) and the existing firewall preview + role-assignment + capabilities sections; new admin endpoints `/upgrade` + `/clear-upgrade` + `/reboot` stamp desired state on the appliance row for the supervisor to act on; four MCP tools land for the Operator Copilot (`find_pending_appliances`, `find_appliance_fleet`, `propose_approve_appliance`, `propose_assign_role`) — all superadmin-gated; APPLIANCE.md + DNS_AGENT.md gain post-#170 architecture sections.

**Landed after 2026.05.14-1 — fleet shake-out + Wave E watchdog layer:**

- **DNS record propagation across all agents in a group** — `enqueue_record_op` previously queued one op against `is_primary=True`, and the bundle's pending-op shipper gated on the same flag. Under #170 every agent renders the zone as `type master` (independent authoritative copy), so secondaries stayed frozen at the bundle they received on initial register. Now fans out per-server: one `DNSRecordOp` row per enabled agent-based server in the group; `agent_config.py` ships them regardless of `is_primary`. `is_primary` only matters for the agentless / Windows-DNS path.
- **Supervisor → service-container auth-key delivery** — `SupervisorRoleAssignment` carries `dns_agent_key` / `dhcp_agent_key` (only when the matching role is assigned). The supervisor writes them into `role-compose.env`; service containers interpolate `${DNS_AGENT_KEY}` / `${DHCP_AGENT_KEY}` on first boot with zero operator action. Closes the "the bind9 / kea container can't register itself" gap that surfaced post-Wave-A3 when the `/api/v1/appliance/pair` endpoint was removed.
- **Supplementary-group fix on docker.sock** — `su-exec spatium:spatium` (explicit `:group`) cleared supplementary groups, so the unprivileged supervisor couldn't read `/var/run/docker.sock` (owned `root:103` on Debian). Every `_docker_image_present` call returned False → `can_run_*` flags all False → role checkboxes grayed out in the Fleet UI. Entrypoint now detects the host docker.sock gid, adds `spatium` to a matching `docker` group, and drops the `:spatium` suffix so `initgroups()` pulls the new group. Supervisor image also gained `docker-cli-compose` — without it every `apply_role_assignment` failed with `docker: unknown command: docker compose`.
- **Profile → service mapping for DHCP** — `apply_role_assignment` intersected compose *profile* names (`dhcp`) against `SUPERVISED_SERVICES` (`dhcp-kea`), so DHCP role assignments silently no-op'd. New `_PROFILE_TO_SERVICE` table (identity for BIND9 + PowerDNS, `dhcp → dhcp-kea`) shared with the new watchdog.
- **Docker poll storm reduction (~5× CPU cut on a 1-CPU VM)** — new `agent/supervisor/spatium_supervisor/docker_api.py` talks to `/var/run/docker.sock` directly via `http.client.HTTPConnection` over a unix-socket subclass (~10 ms per call instead of ~300 ms for a CLI shell). 5-min cache on `_docker_image_present`. `apply_role_assignment` skips the `docker compose ps` + `up -d` pair when the rendered env-file content hash is unchanged from the last successful apply (sidecar `role-compose.env.hash`). Same direct-socket pattern adopted by the console's `docker_ps`. The dashboard's previous 3 s subprocess timeout was killing dockerd mid-response, generating `superfluous response.WriteHeader call from go.opentelemetry.io/contrib/...` log spam in a self-feeding loop; that's gone.
- **Wave E in-process watchdog (`agent/supervisor/spatium_supervisor/watchdog.py`)** — runs inside the heartbeat loop every 5 min. Reads `role-compose.env` for desired profiles, maps profile → compose service, snapshots running containers via `docker_api`, derives `healthy` / `missing` / `unhealthy` / `starting` per service with a `since` first-observed timestamp. Auto-heal: `missing` services trigger an idempotent `apply_role_assignment` re-fire. Cached verdict rides on every heartbeat as `role_health`; persisted to new `appliance.role_health` JSONB column (migration `c4e2b7f81a39`); rendered as a per-service health table in the Fleet drilldown (status chip + `since X ago`). Cache invalidates whenever `apply_role_assignment` runs so the next heartbeat re-probes immediately instead of waiting 5 min.
- **Wave E external watchdog** — host-side `bash` script + systemd `.service` + `.timer` units that catch the case where the supervisor process is alive (pgrep passes, `restart: unless-stopped` doesn't fire) but the heartbeat loop has wedged. The supervisor `touch()`es `/var/persist/spatium-supervisor/last-loop-at` at the top of every iteration; `/usr/local/bin/spatiumddi-supervisor-watchdog` stats the file every 2 min and `docker restart spatium-supervisor` when mtime > 5 min stale. Rate-limited to 3 restarts per 30 min, beyond which it writes an alert trigger file the in-process watchdog reads and surfaces as a red `Watchdog: Restart cap hit` chip on the console dashboard. Intentionally `bash`-only (no Python, no docker SDK) so it survives anything that breaks the supervisor's own runtime stack. Enabled at install time by `mkosi.postinst`.
- **Firewall drift detection** — every 5 min the supervisor reads the kernel-active ruleset via `nft -j list chain inet filter input`, confirms each expected per-role service port is present, and forces a re-apply if anything's missing. Catches the "drop-in file is right but `nft -f /etc/nftables.conf` silently failed" and "operator `nft flush ruleset`'d in a debugging session" cases. Logs `supervisor.firewall.drift_detected`. `FirewallProfile` gains `expected_tcp_ports` + `expected_udp_ports` frozensets so the comparison is straightforward.
- **Fleet UI** — file rename `ApprovalsTab.tsx` → `FleetTab.tsx` (component + React-Query keys + URL hash all migrated from `approvals` → `fleet`); sidebar regrouped under **Infrastructure** (Appliances / Pairing codes / Slot images) + **Services** (NTP / SNMP) sub-headings so future Wave-E host-config surfaces drop in cleanly; new **Services** column on the Appliances list with per-role chips coloured by `role_switch_state` (green `ready` / amber `pending` / rose `failed` / neutral observer); new **Service health** section in the per-appliance drilldown rendering the watchdog's `role_health` table; **Approve + sign cert** now refreshes the drilldown row on success; **Role assignment Save** shows a transient `✓ Saved` indicator and re-baselines the `dirty` check; **Slot image Delete** gated behind a `ConfirmModal` that shows version + notes + SHA-256 prefix.
- **Console dashboard polish** — F9 / Diag chip removed (handler was a no-op); live-log noise filter drops Python traceback frames + systemd restart-counter spam; `--since` 10 min → 2 min so crash spam from a previous instance clears 5× faster; CPU usage 92 % → 1.4 % via `auto_refresh=False` on Rich Live + tick 0.25 s → 0.5 s; Build line collapses when `APPLIANCE_VERSION == SPATIUMDDI_VERSION`; `slot_a` → `A` in the slot indicator; IPv6 SLAAC addresses fold into a `+N IPv6` chip; Agent panel deleted (Control plane URL + Identity fold into one header row); Vitals + Disks merged into one row; Services row gains a ports / network-mode column (`53/tcp 53/udp` for published-port containers, `host net` in bold cyan for DHCP-kea); Disk dedupe — `/home` / `/root` bind mounts collapse to the underlying `/var` device, `/var/lib/spatiumddi/docker-overlay/lower` hidden; new `Watchdog` header line surfacing the external watchdog state (green `Loop ticking · Ns ago` / yellow `Loop stale · Ns ago` / red `Restart cap hit`); Services panel unions whichever supervisor-managed service is either in `docker ps` or listed in `role-compose.env`'s `COMPOSE_PROFILES` so a crashed container surfaces as `(not running)` instead of disappearing.
- **Misc** — `spatiumddi-firstboot` writes `/etc/spatiumddi/.env` mode 644 (was 600) so the supervisor's unprivileged user can read it through the `/etc/spatiumddi:/etc/spatiumddi-host:ro` bind mount; `service_lifecycle.py` passes the host `.env` as an additional `--env-file` to `docker compose` so `${SPATIUMDDI_VERSION}` / `${DOCKER_GID}` interpolation resolves without re-emitting every var into the role env.

Open Wave E follow-ups: container-watchdog auto-heal cap (currently re-fires `apply_role_assignment` on every probe — could backoff after N consecutive `missing` cycles); nftables base-config strip (`/etc/nftables.conf` currently has hardcoded DNS / DHCP / HTTP "belt-and-braces" rules from the pre-#170 5-role world); per-appliance scoped agent keys (current implementation passes the platform-wide global PSK — a per-appliance scoped key would limit blast radius if a supervisor cert ever leaked); host-OS config plane (#155–#166 — APT sources / proxy, syslog forwarder, SSH `authorized_keys`, static routes, etc., riding the same ConfigBundle → trigger-file → host runner pattern as shipped SNMP / NTP; **APT #155 implemented on `issue-77-99-155`** — opt-in `platform_settings.apt_*` (managed sources / proxy / Fernet-encrypted GPG keys + private-mirror auth / unattended-upgrades toggle) → `apt_bundle` in the supervisor heartbeat → `spatiumddi-apt-reload` host runner that **validates a staged config with `apt-get update` before swapping the live files** (classifies failures into `proxy-failed` / `mirror-unreachable` / `signature-mismatch` / `no-sources`), `POST /settings/apt/validate` structural pre-check, `find_apt_settings` MCP tool, APT Services-sidebar form + per-row `apt_state` Fleet chip).

Superseded by #170 (still functional for in-field installs, deprecated for new ones): legacy `dns-agent-bind9` / `dns-agent-powerdns` / `dhcp-agent` installer roles, the per-service slot-state collectors on DNS + DHCP agents, the PSK-based `DNS_AGENT_KEY` / `SPATIUM_AGENT_KEY` registration path, and pairing codes' `deployment_kind` field from #169.

### Major roadmap items

Feature-level tracker for the IPAM / DNS / DHCP core — each entry is
the design context to start from when picking the item up, and each
carries a status marker (below). Older shipped items had their full
bodies moved to
[`docs/SHIPPED.md`](docs/SHIPPED.md), and their "Deferred follow-ups"
blocks (pending sub-items still attached to a shipped parent) stay
alongside the parent in that file rather than getting hoisted here.
Pure-greenfield ideas from the 2026.04.26 brainstorm pass live in
their own categorised section further down.

**Markers:** ⬜ pending · 🟡 partially shipped (what's left is stated
inline) · ✅ shipped, with the closing release · ❌ closed as not
planned. When an item ships, flip its marker here and add the release
in the same edit — a wrong marker misdirects the next session, which
is what [#534](https://github.com/spatiumddi/spatiumddi/issues/534)
was filed for. Last swept against live issue state **2026-07-28**.

- ✅ [**Windows DNS — Path B (WinRM + PowerShell)**](https://github.com/spatiumddi/spatiumddi/issues/21) — shipped: agentless WinRM + PowerShell path in `backend/app/drivers/dns/windows.py` (enabled per-server when `DNSServer.credentials_encrypted` is set) drives zone CRUD (`Add-DnsServerPrimaryZone` / `Remove-...`), an AXFR-free record pull (`Get-DnsServerResourceRecord`), and server-level probes over the `DnsServer` module. Record-level writes still ride RFC 2136 to avoid the PowerShell-per-record cost. Remaining literal-scope items — zone *edit* via `Set-DnsServerZone`, server-level option writes, DNS view config, GSS-TSIG for "Secure only" AD zones — were re-homed to [#444](https://github.com/spatiumddi/spatiumddi/issues/444) (open). See [`docs/features/DNS.md`](docs/features/DNS.md) §13.
- ✅ [**Windows DHCP — Path B (WinRM + PowerShell, full CRUD)**](https://github.com/spatiumddi/spatiumddi/issues/22) — shipped: despite the legacy `WindowsDHCPReadOnlyDriver` class name, `capabilities()` reports `read_only=False` and the driver does scope / reservation / exclusion write CRUD + scope-option reconcile + MAC deny-filter over WinRM, wired through `backend/app/services/dhcp/windows_writethrough.py` (batched, transactional, multi-server). Remaining gaps — client-class CRUD, a broader option-code map, and the stale "read-only" naming/docs — were re-homed to [#444](https://github.com/spatiumddi/spatiumddi/issues/444) (open).
- ✅ [**IP discovery**](https://github.com/spatiumddi/spatiumddi/issues/23) — shipped `2026.06.04-1`: opt-in per-subnet scheduled ping / ARP sweep + reconciliation (unprivileged `SOCK_DGRAM` ICMP with TCP-connect fallback, `/proc/net/arp` scan for ICMP-silent hosts, `status="discovered"` rows for live IPs with no row). Producer of the #45 / #41 hygiene loop. Migration `a7e3c1f49d20`.
- ✅ [**DNS Views — end-to-end split-horizon wiring**](https://github.com/spatiumddi/spatiumddi/issues/24) — shipped `2026.06.04-1`: the BIND9 agent now emits one `view "<name>" { match-clients …; }` block per view with per-view zone files (`view_id IS NULL` records render into every view). Storage + CRUD + record-form picker had shipped earlier. Full body in [`docs/SHIPPED.md`](docs/SHIPPED.md).
- ✅ [**ACME embedded client — certs for SpatiumDDI's own services**](https://github.com/spatiumddi/spatiumddi/issues/28) — [#438](https://github.com/spatiumddi/spatiumddi/issues/438) **shipped 2026.06.19-1**, **Phases 1–5 implemented + Phase 6 resolved N/A**: a hand-rolled RFC 8555 ACME client (`backend/app/services/acme_client/` — `engine.py` manual JWS over `cryptography` + `httpx`, `dns01.py` self-solve over SpatiumDDI's own managed zones via the `record_ops` pipeline, `orchestrator.py` end-to-end driver) that issues a CA-trusted Web UI TLS cert from Let's Encrypt, landing the chain in the existing `ApplianceCertificate` storage + deploy path with `source="letsencrypt"`. Surface at `/api/v1/appliance/acme` (account upsert + `POST /preview` + `POST /issue` → `app.tasks.acme.run_acme_order` + orders list/get/cancel) behind the default-enabled `security.certificates` feature module (group Security), plus the unauthenticated root route `GET /.well-known/acme-challenge/{token}` for HTTP-01 (nginx-proxied). Account key + EAB HMAC are Fernet-encrypted + never returned (`eab_hmac_set` boolean only). **Phase 1** DNS-01 over managed zones; **Phase 2** 12h beat task `app.tasks.acme.renew_due_certificates` (re-issues active LE certs within 30d of `valid_to`, idempotent + advisory-locked, gated on `acme_enabled`+`acme_auto_renew`) + the `secret_expiring` alert now covers the LE Web-UI cert (`appliance_cert_tls:<id>`); **Phase 3** cloud-hosted DNS-01 auto-solve via the Cloudflare/Route53/Azure/Google agentless drivers (creds configured under DNS, not the ACME screen) + `POST /preview` per-domain managed/manual report + `allow_manual` manual-TXT fallback (`manual_challenges[]` + public-DNS polling converges the order); **Phase 4** http-01 (`challenge_type:"http-01"`, CA fetches the well-known route, appliance must be reachable on :80/:443 at the FQDN, no wildcards); **Phase 5** `tls-alpn-01` → 422 (not supported on the nginx/k3s topology, UI shows it disabled); **Phase 6** per-appliance certs resolved N/A (Web UI is control-plane-only behind one shared VIP cert = fleet-singleton). MCP: `find_certificates` / `count_certificates_expiring` (default on) + `get_acme_account` (default off). See `docs/features/ACME.md`. Distinct from the shipped ACME *provider* (`/api/v1/acme/`).
- 🟡 [**Cloud DNS driver family — Route 53 / Azure DNS / Cisco DNA**](https://github.com/spatiumddi/spatiumddi/issues/29) — Route 53 + Azure DNS + Cloudflare + Google Cloud DNS landed as agentless first-class drivers via #37 Part B, **shipped `2026.06.04-1`**; `2026.06.11-1` then dropped the `dnssec_online` / `alias_records` capability advertisements so the UI stops offering cloud DNSSEC sign / ALIAS authoring that the server-side gates 422 anyway. **Still open:** real cloud DNSSEC + ALIAS support. Cisco DNA stays out of scope (SD-Access controller, not a hosted-DNS service).
- ✅ [**DHCP configuration importer — ISC DHCP, Kea, Windows DHCP**](https://github.com/spatiumddi/spatiumddi/issues/129) — shipped `2026.06.04-1`: one-shot import-to-evaluate (sister to the DNS importer #128) behind one canonical IR + preview → commit pipeline — Kea JSON-with-comments upload, Windows live-pull (reuses the Path A driver), ISC `dhcpd.conf` parse. Provenance columns via migration `c7f1a3e58b94`. See [`docs/features/MIGRATION.md`](docs/features/MIGRATION.md).
- ✅ [**Windows → SpatiumDDI cutover (guided migration)**](https://github.com/spatiumddi/spatiumddi/issues/756) — shipped `2026.08.12-1`: the half the #128 / #129 importers stop short of. **Not a fifth importer** — it creates no zones, scopes, pools or records; its only writes are TTL reductions on a zone SpatiumDDI already owns, reservations synthesised from live Windows leases, and `is_active` on a managed scope. Unit of work is a `cutover_plan` holding independent `cutover_item`s (one zone or one scope), each cut over and rolled back on its own — no big-bang step. Four phases: **parity** (diff vs live Windows, classified by *why* the sides differ — `value_mismatch` / `drifted_since_import` / `never_imported` / `intentionally_diverged`; CAA/TLSA/SSHFP are `not_compared` because a Path-B pull can't emit them, and an unparseable response is `unverified`, never "everything diverged"), **parallel run** (replay query-log traffic at both sides, RD=0, IP literal not hostname), **the switch** (TTL pre-flight snapshot/restore, DHCP lease→reservation handover, deactivate-Windows-before-activate-managed with compensating rollback sealed inside the transaction), **decommission checklist** (15 items, 3 advisory-evaluated, none auto-ticked). Plus a markdown runbook carrying the Windows-side PowerShell we deliberately don't run. **Load-bearing refusal:** an AD-integrated zone with "Secure only" dynamic updates is a hard block `force` cannot bypass (GSS-TSIG unimplemented — #444), failing closed. Behind the default-on `migration.cutover` module (group Tools), **superadmin on every endpoint**; router `/api/v1/migration/cutover`; 4 MCP tools (`find_cutover_parity_check` default-off — live WinRM pull). Migration `a4f1c93d7e28`. See [`docs/features/MIGRATION.md`](docs/features/MIGRATION.md). **Deferred:** auto-approve of parity warnings, and a non-Windows (BIND9 / ISC) cutover source.
- ✅ [**Technitium — agentless driver for an install the operator already runs**](https://github.com/spatiumddi/spatiumddi/issues/810) — shipped `2026.08.12-1`: new `technitium_api` driver (`backend/app/drivers/dns/technitium_api.py`), agentless like Windows Path B: operator pastes an API URL + permanent bearer token, control plane drives Technitium's HTTP API directly, nothing deployed. Coexists with the agent-managed `technitium` (a group is single-driver). **No migration** — credentials ride the existing `DNSServer.credentials_encrypted`. Zone + record CRUD and topology pull only; DNSSEC / forwarders / blocklists stay agent-managed and `technitium_api` is deliberately absent from `_DRIVER_GATED_OPERATIONS["dnssec_sign"]`. Also extracted the rdata translation both drivers and the #744 importer need into `app/services/technitium/rdata.py` (the agent keeps a third copy it can't share — separate package), and replaced two inline `windows_dns or CLOUD_DNS_DRIVERS` topology gates with `TOPOLOGY_PULL_DRIVERS` / `supports_topology_pull()`. See [`docs/drivers/DNS_DRIVERS.md` §4C](docs/drivers/DNS_DRIVERS.md). **Deferred:** query-log polling (#742's shape, easier here than on the agent path), Technitium's DHCP API as a DHCP driver, and clustering awareness.

### Integration roadmap

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

- ✅ [**UniFi Network Application**](https://github.com/spatiumddi/spatiumddi/issues/30) — read-only mirror of UniFi networks + clients into IPAM (local + cloud-hosted controllers) behind the `integrations.unifi` feature module (`backend/app/services/unifi/`, model `models/unifi.py`, task `tasks/unifi_sync.py`, router `api/v1/unifi/`).
- ✅ [**OPNsense (tier 1 — firewall-of-choice for labs)**](https://github.com/spatiumddi/spatiumddi/issues/31) — read-only mirror of OPNsense interfaces + DHCP leases + reservations into IPAM behind the `integrations.opnsense` feature module (`backend/app/services/opnsense/`, model `models/opnsense.py`, task `tasks/opnsense_sync.py`, router `api/v1/opnsense/`).
- ⬜ [**pfSense (tier 1 — paired with OPNsense)**](https://github.com/spatiumddi/spatiumddi/issues/32)
- ✅ [**Palo Alto PAN-OS / Panorama (enterprise-firewall family reference vendor)**](https://github.com/spatiumddi/spatiumddi/issues/605) — shipped `2026.07.11-1`: read-only mirror of address objects/groups → a new `firewall_endpoint_object` "shadow IPAM" store (with IPAM drift report), NAT rules → `nat_mapping` provenance rows, + opt-in zones/interfaces + DHCP leases, behind the `integrations.paloalto` feature module (`backend/app/services/panos/`, model `models/panos.py`, task `tasks/panos_sync.py`, router `api/v1/panos/` at the `/paloalto` prefix). Also a commit-free **Dynamic Address Group** enforcement tier extending Active block sync (#601) — `paloalto` target kind registers `IP → tag` via the User-ID API, gated by the new `manage_firewall_enforcement` permission. Fortinet / Check Point / Cisco FTD / Meraki follow this pattern.
- 🟡 [**Enterprise firewall family — Fortinet / Check Point / Cisco FTD / Meraki + policy-aware conformity**](https://github.com/spatiumddi/spatiumddi/issues/606) — **Phase 1 (Fortinet + Meraki) shipped `2026.07.11-1`.** Both follow the #605 shape via a new shared mirror engine `backend/app/services/firewall_mirror.py` (the #605 PAN-OS reconciler was migrated onto it; `firewall_endpoint_object` generalized to one-of-three vendor owners with a `num_nonnulls=1` CHECK). **Fortinet FortiGate** — read-only FortiOS-REST mirror (address objects/groups → shadow IPAM, VIPs → `nat_mapping`, opt-in interfaces + DHCP leases) behind `integrations.fortinet` (`services/fortinet/`, `models/fortinet.py`, `tasks/fortinet_sync.py`, `api/v1/fortinet/`); enforcement is the credential-free **Threat-Feed inversion** — new `FirewallFeed` (`models/firewall_feed.py`, `services/firewall_feeds/`, `api/v1/firewall_feeds/`) serves a token-scoped `blocklist.txt` the FortiGate polls (module `security.firewall_feeds`, default-on). **Cisco Meraki MX** — read-only Dashboard-API mirror (VLANs → subnets, DHCP fixed-IP reservations → IPAM, org policy objects → shadow IPAM, 1:1-NAT/port-forward → `nat_mapping`, opt-in clients) behind `integrations.meraki` (`services/meraki/`, `models/meraki.py`, `tasks/meraki_sync.py`, `api/v1/meraki/`); enforcement is a `meraki` block-sync target (kind=`mac`, per-client `Blocked` device policy via the Dashboard API, gated by `manage_firewall_enforcement`). 3 MCP tools (`list_fortinet_targets` / `list_meraki_targets` / `list_firewall_feeds`); `find/count_firewall_objects` made vendor-neutral. **Deferred:** FortiManager JSON-RPC centralization; Phase 2 (Check Point + Cisco FTD/FMC, both leading with feed-based enforcement); and the cross-cutting **policy-aware conformity checks** (`pci_scope`/`internet_facing` subnets asserted against live firewall policy, plugging into #106).
- ✅ [**NetBird (managed WireGuard mesh)**](https://github.com/spatiumddi/spatiumddi/issues/603) — shipped `2026.07.11-1`: read-only mirror of NetBird peers into IPAM behind the `integrations.netbird` feature module (`backend/app/services/netbird/`, model `models/netbird.py`, task `tasks/netbird_sync.py`, router `api/v1/netbird/`), cloned from the Tailscale shape — NetBird's real management API is what makes it a legitimate pull mirror where raw WireGuard isn't. Phase 1 mirrors each peer's overlay IP (OS / version / groups / connection state in custom fields) under an auto-created overlay block + subnet; Phase 2 adds an optional synthetic read-only DNS zone for the mesh domain. Per-instance operator-supplied management URL + `verify_tls` toggle (SSRF-guarded at the test-connection boundary), Token auth, and a cross-integration ownership guard — NetBird and Tailscale both default to `100.64.0.0/10`, so neither reconciler will claim the other's rows. 1 MCP tool (`list_netbird_targets`).
- ⬜ [**MikroTik RouterOS 7 (tier 2)**](https://github.com/spatiumddi/spatiumddi/issues/33)
- ⬜ [**Incus / LXD (tier 2 — Docker-adjacent)**](https://github.com/spatiumddi/spatiumddi/issues/34)
- ⬜ [**HashiCorp Nomad (tier 2 — Kubernetes alt)**](https://github.com/spatiumddi/spatiumddi/issues/35)
- ✅ [**NetBox read-only import (one-shot)**](https://github.com/spatiumddi/spatiumddi/issues/36) — **shipped 2026.06.28-1**. One-shot migration importer (not a continuous reconciler): live-pulls prefixes / addresses / VRFs / tenants→Customers / sites / VLANs out of a NetBox install and stamps them into native IPAM rows via a stateless preview → commit flow (`backend/app/services/netbox_import/`, router at `/api/v1/ipam/import/netbox/{test-connection,preview,commit}`). Provenance `import_source="netbox"` + `netbox_id` makes re-runs idempotent (default-skip-on-conflict); `per_vrf` (one IPSpace per VRF + Global) vs `single` (collapse into a chosen space) strategy; connection + token supplied per-request, never persisted. Behind the default-on `ipam.import.netbox` feature module; 2 MCP tools (`find_netbox_import_preview` + `propose_commit_netbox_import`). See `docs/features/MIGRATION.md`.
- ✅ [**Cloud connectors — unified "Cloud" integration with per-provider picker (Azure / AWS / GCP)**](https://github.com/spatiumddi/spatiumddi/issues/37) — shipped `2026.06.04-1`. Part A: read-only infra mirror (`cloud_endpoint` + AWS/Azure/GCP connectors → IPBlock/Subnet/IPAddress, `services/cloud/`, feature module `integrations.cloud`). Part B: Cloudflare / Route 53 / Azure DNS / Google Cloud DNS as agentless first-class DNS drivers (`drivers/dns/{cloudflare,route53,azuredns,googledns}.py`) with import-existing-zones (`services/dns_import/cloud.py`). See `docs/features/INTEGRATIONS.md` + `docs/drivers/DNS_DRIVERS.md`. Stretch token-only DNS providers (DigitalOcean / Hetzner / Linode / Vultr) deferred.
- ⬜ [**Load balancer family (F5 BIG-IP, HAProxy, nginx, KEMP, A10, Citrix ADC)**](https://github.com/spatiumddi/spatiumddi/issues/38)
- **VMware vCenter / ESXi.** Bigger enterprise audience, but
  vCenter's SOAP-heavy + licensed API makes it a significantly
  bigger dev effort than the tier 1 candidates. Revisit only if
  a deployment specifically needs it.
- **SNMP device polling** as an integration. Already tracked as
  its own line item above (IPAM ARP discovery); belongs with
  ping-sweep / ARP-scan, not the read-only integration shelf.
- **WireGuard raw config.** No API — config files only. Belongs
  in a manual-import flow if at all. Managed WireGuard meshes that
  *do* expose an API are covered: Tailscale (shipped) and NetBird
  (#603, above).

### Future ideas — categorised (added 2026.04.26)

Brainstorm pass that catalogues standard IPAM / DDI features
operators of comparable tools (Infoblox, EfficientIP, NetBox,
phpIPAM, SolarWinds IPAM) expect but SpatiumDDI doesn't yet
ship. Sketched at enough depth to start work without
re-deriving the design — pick by impact, not by section order.
Markers below follow the same key as the Major-roadmap section
above. Brainstorm items whose full design body was moved out
(Switch-port mapping, OUI lookup, SNMP polling,
LLDP collection, nmap, CIDR calculator + Subnet planner +
Address planner, DNS templates / propagation check / catalog
zones / RPZ, DHCP option library, ACME provider, alerts
framework, dashboard time-series, …) live in
[`docs/SHIPPED.md`](docs/SHIPPED.md) under the matching
sub-headings.

#### Discovery & network awareness

- ⬜ [**NetFlow / sFlow ingestion**](https://github.com/spatiumddi/spatiumddi/issues/39)
- ❌ [**mDNS / Bonjour / WSD passive discovery**](https://github.com/spatiumddi/spatiumddi/issues/40)
  — **closed as not planned** (feasibility + merit review). Link-local
  multicast is only audible to a host-networked, on-segment agent, and
  the agent↔subnet binding that needs isn't modelled. Kept listed
  because #540/#541/#542 named it as their shared discovery primitive;
  its closure is why every one of their discovery phases is deferred.
- ✅ [**Reverse-DNS auto-population**](https://github.com/spatiumddi/spatiumddi/issues/41) — shipped `2026.06.04-1`: scheduled, platform-opt-in sweep that PTR-resolves `hostname IS NULL` rows against configured resolvers (bounded concurrency, per-run cap), filling the short label into `hostname` and the FQDN into `description` only when blank. Migration `d7a3f2b9c1e4`.
- ✅ [**CGNAT (RFC 6598) awareness**](https://github.com/spatiumddi/spatiumddi/issues/42) — shipped `2026.06.04-1`: amber "CGNAT" badge on subnet detail + a New-Subnet advisory hint when the typed network falls in `100.64.0.0/10` — the one reserved IPv4 range overlays actively carve, so reaching for it as an on-prem LAN silently overlaps overlay space.

#### Vertical network awareness

Umbrella [#543](https://github.com/spatiumddi/spatiumddi/issues/543) —
four IP-native domains a generic IPAM doesn't speak, built on the same
DDI primitives (uniqueness registry + segmentation documentation +
conformity). See [`docs/features/VERTICALS.md`](docs/features/VERTICALS.md).
The first three children closed 2026-07-28; the umbrella stays open for
the deferred discovery phases listed per-item below. The healthcare pass
concluded there is **no** `network.healthcare` catch-all — it splits into
separable pieces, of which DICOM (#723) is the anchor and the
probe-safety fix (#722) is not a vertical at all.

- ✅ [**AV / Audio-Video-over-IP — Dante · AES67 · SMPTE 2110 · NDI**](https://github.com/spatiumddi/spatiumddi/issues/540)
  — shipped `2026.07.30-1` (#714): `network.av` module,
  `av_flow_profile` 1:1 AV descriptor on `multicast_group` + operator-declared `av_reserved_range` per
  protocol, allocation-conflict preview, 2 conformity checks, 3 MCP
  tools. **Phase 2 (Dante mDNS) blocked** — #40 closed not-planned.
  **Phase 3 (NMOS IS-04 mirror) deferred** — a full pull integration
  with both dashboard surfaces, separable into its own change.
- ✅ [**BACnet/IP building automation**](https://github.com/spatiumddi/spatiumddi/issues/541)
  — shipped `2026.07.30-1` (#714): `network.bacnet` module,
  `bacnet_device` with the internetwork-wide
  `uq_bacnet_device_instance` constraint (the differentiating hook),
  BBMD flag + BDT/FDT snapshots, 3 conformity checks incl.
  `bbmd_one_per_subnet` failing in both directions, 3 MCP tools.
  **Phase 2 (`Who-Is` sweep) deferred** — needs a UDP broadcast
  carrying a real payload; the only generic prober sends an empty
  datagram.
- ✅ [**Industrial / OT — PROFINET · EtherNet/IP · Modbus TCP · OPC UA**](https://github.com/spatiumddi/spatiumddi/issues/542)
  — shipped `2026.07.30-1` (#714): `network.ot` module, `ot_device`
  1:1 descriptor + `ot_zone` Purdue zoning (`Numeric(2,1)` so level 3.5 / the DMZ is representable), CSV import
  of engineering-tool exports, 2 conformity checks, 3 MCP tools.
  Read-only identification only — control-protocol writes are
  permanently out of scope. **Phase 2 (routable probes) deferred** —
  nmap runs NSE but nothing parses `<script>` output. **Phase 3
  (PROFINET DCP) deferred** — raw L2, needs a container capability grant.
- ✅ [**DICOM AE Title registry + peer-association map**](https://github.com/spatiumddi/spatiumddi/issues/723)
  — Phase 1 shipped `2026.07.30-1` (#731): `network.dicom` module,
  `dicom_ae` with the institution-wide `uq_dicom_ae_title` constraint (the differentiating hook — PS3.15 Annex H specifies a
  registry for exactly this and nobody deploys one), `dicom_peer`
  directed AE→AE edges + a renumber-impact view, CSV import of the
  estate's AE table, 4 conformity checks, 3 MCP tools. `ip_address_id`
  is **nullable / SET NULL**, deliberately unlike BACnet's CASCADE: an
  AE Title outlives its host, so decommissioning demotes it to a
  reservation rather than freeing a name peers still send to. AE-title
  validation follows PS3.5 exactly — 16 **bytes**, spaces legal as
  padding, all-space forbidden, no backslash / control chars. **No PHI,
  ever** — network identity only, or SpatiumDDI becomes a HIPAA
  Business Associate. **C-ECHO verification probe deferred** to its own
  issue: it is the one probe in the family that does *not* inherit the
  agent↔subnet blocker (routable unicast TCP), but it must respect #722.
- ✅ [**Fragile-device "do not probe" flag**](https://github.com/spatiumddi/spatiumddi/issues/722)
  — shipped `2026.07.30-1` (#731). **Not a vertical and not
  behind a feature module**: a constraint on our own behaviour and a
  correctness fix to three shipped features (#23 sweeps, `tools.nmap`,
  `tools.network`), so hiding it behind a default-off module would leave
  the sites that need it unprotected. `do_not_probe` +
  `do_not_probe_reason` on `IPSpace` / `IPBlock` / `Subnet` **OR down**
  the chain with no per-level inherit toggle — deliberately unlike the
  DDNS fields they mirror, because a descendant must not be able to opt
  a clinical space back into being swept. One resolver
  (`services/ipam/probe_policy.py`) that every prober consults; audited
  superadmin-only per-request override; `fragile_subnet_probed`
  conformity check working backwards from the `ot_device` / `dicom_ae` /
  `role="bmc"` registries; `bmc` added to `IP_ROLES`. Migration
  `b1e7c04a93df`.

#### Reporting & analytics

- ⬜ [**Capacity forecasting**](https://github.com/spatiumddi/spatiumddi/issues/43)
- ✅ [**Per-subnet utilization history**](https://github.com/spatiumddi/spatiumddi/issues/44) — shipped `2026.06.11-1`: daily beat task snapshots each subnet's allocated / total IP counts (pruned > 90 d); Trend tab on subnet detail renders a 30 / 90-day % used line chart; `get_subnet_utilization_trend` MCP tool. Migration `c7a3e1f90d24`.
- ✅ [**Stale-IP report**](https://github.com/spatiumddi/spatiumddi/issues/45) — shipped `2026.06.04-1`: over the #23 discovery `last_seen_at` signal — which allocated IPs has nothing answered for in N days. Paginated report (optional space / block / subnet scope) + one-click bulk-deprecate of selected or all-matching (capped, reversible).
- ✅ [**Decom-date awareness**](https://github.com/spatiumddi/spatiumddi/issues/46) — shipped `2026.06.11-1`: first-class `decom_date` on subnet + IP, a `decom_expiring` alert rule (severity escalation reused from the other `*_expiring` rules), a dashboard widget, and a `find_subnets_decommissioning` MCP tool. Migration `a3f7c1e92b48`.
- ✅ [**Top-N reports**](https://github.com/spatiumddi/spatiumddi/issues/47) — shipped `2026.06.11-1`: a `/reports` surface (top subnets by utilization, owners by IP count, most-modified resources via `audit_log`, noisiest DNS clients), feature-module-gated with 4 MCP read tools.
- ✅ [**Compliance / change report PDF**](https://github.com/spatiumddi/spatiumddi/issues/48) — shipped `2026.06.11-1`: `GET /api/v1/audit/export.pdf` renders an auditor-facing PDF of every `audit_log` mutation in a date range, grouped by user / resource / action, with a per-row SHA-256 tamper-evidence trailer.
- ⬜ [**InfluxDB push export**](https://github.com/spatiumddi/spatiumddi/issues/889) — build the writer the tech-stack
  table claimed for months but nothing implemented: periodic push of the
  existing `metric_sample` rows to InfluxDB v1 / v2 / v3. Spec shape is
  already written up in `docs/SHIPPED.md`; the code is greenfield.

#### Subnet planning & calculation tools

All shipped — see `Subnet planning & calculation tools` in
[`docs/SHIPPED.md`](docs/SHIPPED.md): CIDR calculator,
Subnet planner workspace, address planner, aggregation
suggestion, free-space treemap.

#### DNS-specific

- ✅ [**DNSSEC**](https://github.com/spatiumddi/spatiumddi/issues/49) — shipped `2026.06.04-1`: BIND9 inline-signing, policies, DS export, rollover. `DNSSECPolicy` (reusable `dnssec-policy`) + `DNSKey` (public per-zone key state — no private-key custody; BIND owns + auto-rotates keys), config-driven `dnssec-policy { … }` + per-zone `inline-signing yes;`. Migration `f2b6d4a91c37`. PowerDNS online-signing landed separately in `2026.05.11-1`.
- ✅ [**DoT / DoH — inbound listener + encrypted upstream forwarding**](https://github.com/spatiumddi/spatiumddi/issues/50) —
  shipped `2026.07.30-1` (#692). Serves DoT (853) / DoH (`/dns-query`)
  to local clients *and* forwards to upstream resolvers
  over TLS instead of plaintext 53. Both halves are per-group, default-off
  (existing installs render a byte-identical `named.conf`), and additive —
  the Do53 listener is unaffected. **BIND9** renders `tls` / `http`
  statements + extra `listen-on` clauses natively and forwards over DoT
  with strict `remote-hostname` validation that fails closed;
  **PowerDNS** gets inbound-only via the dnsdist sidecar (#146 Phase 2,
  docker-compose-only) since pdns auth speaks neither protocol and doesn't
  forward at all. Certs come from the existing `ApplianceCertificate`
  store + the shipped ACME client (#438), ride the hashed config bundle so
  a renewal shifts the ETag, and degrade to Do53 (never a dead daemon) if
  the cert is deleted out from under a live listener. Operator-chosen
  ports flow to the supervisor firewall via a new `dns_encrypted_tcp_ports`
  field on the role assignment. Deferred: DoH-upstream (BIND has no
  client-side HTTP transport — needs the dnsdist path), per-forwarder TLS
  hostnames (one per group today, so mixed providers need one group each),
  DoQ, and a k8s dnsdist front to unblock PowerDNS DoT/DoH off compose.
  Not to be confused with `PlatformSettings.resolver_dns_over_tls`, which
  is the appliance host's own systemd-resolved stub resolver.

- ✅ [**Upstream resolver presets**](https://github.com/spatiumddi/spatiumddi/issues/877) — shipped in PR
  [#893](https://github.com/spatiumddi/spatiumddi/pull/893): 16 verified
  presets across 7 providers in `backend/app/data/dns_resolver_presets.json`,
  served by `GET /dns/forwarder-presets` and picked from the Forwarders
  card. Each carries the **certificate name its addresses actually
  present**, which is the point: since DoT upstream forwarding (#50),
  BIND validates against ONE group-level `remote-hostname` and a
  mismatch fails closed (SERVFAIL), so "Quad9 is 9.9.9.9" without
  "…and its DoT name is dns.quad9.net" yields a group that resolves
  nothing. Two hard 422s for configurations that cannot work (a
  forwarder set spanning two certificate names under verification;
  Mullvad on plaintext 53) and a UI advisory — deliberately not a
  refusal — for a non-canonical hostname, because providers list
  several names per certificate. Addresses are matched by **value, not
  spelling**: `2606:4700:4700::1111` and its expanded form are one
  host, and a string compare would fail OPEN. Manual entry is
  unconstrained — the catalogue is a convenience, never a whitelist,
  with a test pinning that. 1 MCP tool (`list_resolver_presets`).
- ✅ [**Family filter — adult-content blocking bundle + SafeSearch enforcement**](https://github.com/spatiumddi/spatiumddi/issues/878)
  — the catalog gains **templates** (entry sets shipped inline, for rules
  with no upstream feed) and **profiles** (compositions applied in one
  action), alongside the existing feeds:
  `backend/app/services/dns/blocklist_templates.py` +
  `POST /dns/blocklists/{from-template,apply-profile}`. Ships a
  **SafeSearch enforcement** template — RPZ *rewrites*, not blocks,
  riding the `entry_type="redirect"` path that already existed — and a
  **Family filter** profile pairing adult + gambling feeds with the
  **DoH / VPN / proxy bypass** lists, because a filter one browser
  setting routes around is not one. Five new sources; the two shipped
  Hagezi entries were **dead** (`hosts/` retired upstream, and one was
  `recommended: true`) and are repointed. Applying a profile assigns to
  nothing — auto-scoping would filter the server VLAN too.
  **Six latent bugs fixed on the way**, each of which made the feature
  wrong rather than merely absent. Three about **RPZ zone validity** —
  and note the shared blast radius: BIND rejects a malformed zone
  *whole*, so any one of these silently stopped every other entry being
  enforced, and nothing caught it because `validate()` runs
  `named-checkconf`, which never reads zone files. (1) the two renderers
  disagreed about `redirect` — the agent emitted `CNAME <target>`, the
  control-plane driver `IN A <target>` — so a hostname target produced
  rdata BIND rejects; both now branch on IP-vs-hostname. (2) the agent
  renderer never filtered entries against `exceptions` (the
  control-plane one did), so excepting a domain a feed lists — *the
  entire point of an exception* — put a block CNAME and a passthru CNAME
  on one owner name. (3) nothing deduped owner names, so one domain in
  two assigned lists with different `block_mode`s did the same; the
  Family filter ships four overlapping Hagezi feeds (146 shared domains
  measured) so this went from hand-assembled to one setting away. Both
  renderers now emit each owner once, first writer wins, and log the
  collision. Verified against `named-checkzone`: identical duplicates
  load, differing ones do not. Plus (4) Technitium routed
  `action="redirect"` into its **allow** set, silently inverting a
  SafeSearch rule into an exemption — now skipped with a warning, since
  its native blocking has no per-domain rewrite; (5) feed-sourced
  entries never set `is_wildcard`, so every subscribed list blocked
  apexes only and `www.<blocked>` resolved fine — now on, matching the
  manual add-entry default, with migration `b7e4a1c56d93` backfilling
  existing rows (`parse_feed` also strips the `*.` prefix OISD and
  Hagezi publish); (6) `entry_count` double-counted on a first sync,
  because the recount query autoflushes the pending inserts and the old
  code added `len(to_add)` on top — a 16k-domain feed reported 33k.
  Sizing consequence of (5), documented in DNS.md: two RPZ records per
  feed entry, so the Family filter's ~596k entries render ~1.2M records.
  1 MCP tool (`list_blocklist_templates`). BIND9-only,
  and [`docs/features/DNS.md` §8.1](docs/features/DNS.md) is explicit
  that DNS filtering is bypassable at all.
- ✅ [**Per-subnet DNS blocklist scoping — surface it in the UI**](https://github.com/spatiumddi/spatiumddi/issues/876)
  — the backend has scoped a blocklist to a view *or* a server group
  since #24 (`dns_blocklist_view_assoc`, rendered per-view by the agent);
  the UI wrote only the group half, so the #878 family filter's whole
  point — filtering one network and not another — was unreachable from
  the product. Now: the **Views tab is full CRUD** (it was read-only, so
  split-horizon was API-only to configure at all), with an *Add subnets…*
  picker that turns IPAM prefixes into `match_clients`; a **scope modal**
  on each blocking list writing group and view assignment in one PUT; and
  per-view chips on both tabs. **The load-bearing addition is server-side
  validation** (`app/services/dns/named_conf_validation.py`): `match_clients`,
  `match_destinations` and the view name are interpolated *verbatim* into
  `named.conf`, and the name additionally becomes a directory on the
  agent — so a malformed prefix, an undefined ACL name, a `;`-injection or
  a `../` traversal are now 422s naming the offending element. That gate
  matters because the agent runs `named-checkconf` before swapping config
  in: an accepted-but-invalid value doesn't break one view, it stops the
  whole group's config converging, silently. Also fixed: the Blocklists
  tab classified a view-scoped list as "Available (not applied)" — i.e.
  reported a list actively filtering a VLAN as doing nothing — and its
  Apply/Detach toggle keyed off that section rather than the actual group
  relationship, so "Detach" on a view-scoped list was a no-op that looked
  broken. BIND9-only, and both surfaces say so when the group runs another
  driver. **Found on the way:** the agent never renders `acl {}`
  definitions at all — `DNSAcl` rows are stored and editable but the bundle
  ships only `{id, name}` and the agent renderer ignores it, so naming an
  ACL in a view would leave an undefined symbol and stop the group
  converging. Named ACLs were therefore rejected in a view's match-list
  with a 422 saying why, until
  [#899](https://github.com/spatiumddi/spatiumddi/issues/899) made them real.
- ✅ [**Blocklist feed wildcard semantics are per-list**](https://github.com/spatiumddi/spatiumddi/issues/894)
  — #878 made every feed row `is_wildcard=True`, right for all 19
  catalog sources but a global constant, and wrong for a host-specific
  threat-intel feed. Now `DNSBlockList.feed_entries_are_wildcard`
  (migration `c8a3f207e51b`, defaults true so nothing changes for
  existing lists), a checkbox on the list form, and an optional
  `entries_are_wildcard` key on a catalog source. **Flipping it
  restamps the rows already imported** — the refresh task diffs by
  domain and never revisits an unchanged one, so without that the
  toggle would appear to do nothing until the feed's contents happened
  to churn. Feed rows only: a manual entry's `is_wildcard` is that
  row's own choice. `parse_feed_detailed` also reports how many lines
  arrived `*.`-prefixed, so an apex-only list fed a wildcard-syntax
  feed logs that it is overriding the feed's stated intent instead of
  doing it silently.

- ✅ [**DNS agent never renders `acl {}` definitions**](https://github.com/spatiumddi/spatiumddi/issues/899)
  — `DNSAcl` rows were stored, listed and editable on the ACLs tab and
  applied to **nothing**: the bundle carried `{id, name}` with no entries
  and the agent's BIND9 renderer emitted no `acl {}` stanza at all (the
  control-plane template that does render one has no production caller).
  Citing an ACL anywhere that reached `named.conf` therefore left an
  undefined symbol — `named-checkconf` fails, the agent declines the
  *whole* bundle, and the group stops converging rather than just that
  statement, which is why #876 had to reject ACL names outright. Now the
  bundle ships entries and the agent renders `acl "<name>" { … };` **above
  `options`** — placement is the correctness property, since BIND resolves
  an `acl` where it is written and a definition below its first use is an
  error, not a forward declaration. The list is emitted
  dependency-ordered (`order_acls_for_render`, a DFS topological sort) so
  a nested reference resolves, and **cycles are refused at the commit**
  with a graph check: `a → b → a` has two individually-legal edges, so
  per-field validation cannot see it. Entry values now go through the same
  gate as a view's `match_clients`, and an entry-less ACL is skipped at
  render because BIND rejects `acl "x" { };`. Global ACLs (`group_id IS
  NULL`) are documented unsupported — nothing creates one and the bundle
  is per-group. **The audit the issue asked for found a second instance of
  the same bug class:** `DNSServerOptions.forward_policy` was settable,
  persisted and shipped, and no `forward` statement was ever rendered — so
  `only` silently behaved as BIND's default `first`, letting queries leak
  past a filtering upstream that an operator had deliberately forced
  everything through. Third field in this class after `allow_transfer`
  (#734); the lesson recorded in `DNS.md` §8.2 is to assert on the
  *rendered config*, not the stored row.

#### DHCP-specific

- ✅ [**DHCPv6 stateful + SLAAC config UI**](https://github.com/spatiumddi/spatiumddi/issues/52) — shipped `2026.06.04-1`: `DHCPScope.v6_address_mode` + `ra_managed_flag` / `ra_other_flag`; the Kea driver renders `subnet6` by mode (stateful → pools + options; stateless / SLAAC → options only). Migration `e4c1a8f63b29`.
- ⬜ [**Lease histogram by hour**](https://github.com/spatiumddi/spatiumddi/issues/53)
- ⬜ [**Option 82 (relay agent info) class matching**](https://github.com/spatiumddi/spatiumddi/issues/54)
- ⬜ [**DHCP test client**](https://github.com/spatiumddi/spatiumddi/issues/55)

#### Operational tooling

- ⬜ [**Time-travel queries**](https://github.com/spatiumddi/spatiumddi/issues/56)
- ✅ [**Maintenance mode**](https://github.com/spatiumddi/spatiumddi/issues/57) — shipped `2026.06.11-1`: middleware 503s mutating requests during a change window (`Retry-After`, superadmin bypass, agent / auth / health exempt per non-negotiable #5); PlatformSettings-driven, audited, with a global banner + Settings surface. Migration `d1b8f4a92c30`.
- ✅ [**Built-in network tools page**](https://github.com/spatiumddi/spatiumddi/issues/58) — shipped `2026.06.11-1`: a `/tools` page (ping / traceroute / mtr / dig / whois over sandboxed argv, port-test / TLS-cert over sockets, DNS-propagation, MAC-vendor), permission-gated + Redis rate-limited, with 7 MCP tools.
- ✅ [**PCAP capture trigger**](https://github.com/spatiumddi/spatiumddi/issues/59) — shipped `2026.06.15-1`: on-demand tcpdump as an RBAC-gated/audited Tools page, both server-container and appliance-host (real-NIC) vantages, keep-partial-on-Stop, `.pcap` download, 4 Operator Copilot tools.
- ❌ [**ACL / prefix-list generator**](https://github.com/spatiumddi/spatiumddi/issues/60) — **closed as not planned** 2026-06-17, in the same triage pass that closed #40. No rationale was recorded on the issue; re-open it rather than re-filing if the need comes back.
- ✅ [**Config-drift report (full record diff)**](https://github.com/spatiumddi/spatiumddi/issues/61) — **backend shipped `2026.06.11-1`**: `GET …/zones/{id}/drift` AXFRs the live zone from every server in the group and diffs it against the DB — extra-on-server (manual host change) / missing-on-server / in-sync, per server, read-only — plus a `find_dns_zone_drift` MCP tool. **UI shipped `2026.07.30-1` (#735)** — a Drift tab on the zone detail, fetched on demand because one call fans out an AXFR to every server in the group. That PR also fixed the reason nothing had ever consumed the endpoint: `dns.query.xfr` takes an IP *literal* and raises a bare, message-less `ValueError` for a hostname before sending a packet, so every hostname-addressed server failed 100% of the time reporting `""` as the error. **[#734](https://github.com/spatiumddi/spatiumddi/issues/734) closed the last gap** — agent-managed BIND9 / Technitium reached the server but got `REFUSED`, because the control plane transferred unsigned while the agent granted `allow-transfer` only to the group key and only on *dynamic* zones; separately, `DNSServerOptions.allow_transfer` and `DNSZone.allow_transfer` were both settable, persisted and **never rendered at all** (a silent no-op). Now: the shared AXFR helper takes an optional `TsigKey`, `resolve_group_transfer_key` picks the same key the agent granted (legacy group key first, then operator `DNSTSIGKey` rows by name — matching the bundle's ordering, which is why `op_keys` is now `order_by(name)`), the BIND9 agent renders the grant in the **options** block so it covers every zone type, Technitium falls back to `Allow` + `zoneTransferTsigKeyNames` (verified against upstream `DnsServer.cs`: the two gates AND, so naming keys makes a signature *required*), and a keyless group reports `unsupported` naming the missing key instead of a misleading ACL error. Windows Path A is deliberately excluded from `AXFR_TSIG_DRIVERS` — it authorises by address, so signing would break a working pull. **Drift now works on every driver except PowerDNS**, which implements no record pull.
- ✅ [**Support bundle**](https://github.com/spatiumddi/spatiumddi/issues/875)
  — platform-wide scrubbed diagnostics export at
  `POST /system/support-bundle{,/preview,/decode-map}`
  (`backend/app/services/support_bundle/`), superadmin + audited, and
  working on **all three deployment shapes** — the appliance-only
  `/appliance/diagnostics/bundle` it supersedes 503s on compose and
  plain k8s because its pod-log and self-test halves go through kubeapi.
  **Research finding that shaped the design:** GitHub has no private
  channel for this. Attachment URLs on a public repo follow *repository*
  visibility, and deleting the comment does not purge the file — so the
  answer is scrubbing, not secrecy. **Two tiers:** secrets (Fernet,
  bcrypt/argon2, PEM, JWT, PSK, TSIG) are **hard-excluded in every
  mode** including the unscrubbed one, matched by field name *and* value
  shape; identifiers (IPs / hostnames / MACs / usernames) are
  pseudonymised HMAC-deterministically off `SECRET_KEY` so mappings are
  stable per install and unguessable outside it. Topology survives —
  same /24 → same synthetic /24 with the host octet kept; zone and
  subdomain grouping preserved. Synthetic v4 lands in **240.0.0.0/6,
  not sos's CGNAT range**, because #42 makes CGNAT a real modelled thing
  here and obfuscating into it would read as genuine. The IPv6 interface
  ID is **discarded rather than mapped** — SLAAC embeds the MAC (RFC
  4291), so preserving it would route hardware identity past the MAC
  scrubber. Decode map is a separate endpoint, **never in the archive**.
  A last-chance `safety_net` sweeps the assembled text and *reports*
  what it caught, because a net firing means a collector has a bug.
  **Two bugs found building it, both about failure isolation:** a
  collector that swallows its own DB error leaves PostgreSQL in
  "transaction is aborted" so every *later* section fails and the bundle
  blames the wrong ones — every guarded query now runs in its own
  SAVEPOINT, as does every section. 1 MCP tool
  (`get_support_bundle_preview`, default **off**). No feature-module
  gate: an ops primitive like backup. **Deferred:** true streaming (a
  zip's central directory is written last, so it needs a third-party
  writer — bounded by per-section + 48 MB caps instead) and a CLI
  fallback for a host that cannot serve HTTP.
- ✅ [**Agents never read the `previous.json` they write**](https://github.com/spatiumddi/spatiumddi/issues/882)
  — non-negotiable #5's cached config protects against a *dead* control
  plane; this closes the other half, a *wrong* one. All three agents
  (DNS / DHCP / looking-glass) gain `config_apply.py`: an `ApplyStatus`
  reported on the heartbeat and a persisted `Quarantine` so a failed etag
  is not re-applied every poll (the long-poll's 12 s wake tick + 2 s
  fallback made a bad bundle a re-render *loop*, not one failure). The
  agent parks on the failing etag — the long-poll then blocks on a 304,
  costing nothing — and retries on a 60 s → 5 min → 15 min ladder so a
  *transient* failure still self-heals.
  **The rotation was the actual bug.** `previous.json` was written on
  every *fetch*, so it meant "the bundle before this one", not "the last
  one that worked" — identical only while every apply succeeds, which is
  the case it does not exist for. Worse, it destroyed the fallback in two
  poll cycles: a failing bundle leaves the etag unadvanced, so the next
  poll re-fetches the *same* bundle and rotates it, now known-bad, over
  the only good config on disk. `previous` is now written by an explicit
  `commit_config` that **refuses to run if `current` is not the bundle
  that applied**.
  Apply is phased (render → validate → swap/reload) and the phase picks
  the recovery: BIND validates into `rendered.new`, so a
  `named-checkconf` failure never reached `named` and re-rendering would
  bounce a healthy daemon to reach the state it is already in. Kea is the
  mirror image — `config-test` rejects without touching the running
  server, but the refused document is already at `kea_config_path`, which
  is what Kea reads on its next start, so that one always rewrites the
  files. Only Kea's *rejected* is a verdict about the config;
  *socket-unreachable* is not, and reverting there would discard a good
  bundle because Kea happened to be restarting.
  **Four latent bugs found on the way, all of the "written, never read"
  class the #899 audit named.** (1) `daemon` and `config` were declared
  on the DNS + DHCP heartbeat request models and read by **neither
  handler** — the degraded verdict agents already computed was discarded
  at the door; the LG agent's `daemon_status` was set by its sync loop and
  never even put on the wire. (2) A config **Kea refused** was reported as
  a success: the loop advanced its etag, called `_record_success()`,
  logged `dhcp_config_applied` and stamped the K8s readiness marker.
  (3) `named-checkconf` writes its diagnostics — with the line number —
  to **stdout**, and `validate()` read only stderr, so the error was
  always the empty string `"named-checkconf failed: "`. Harmless while it
  went nowhere; now it is the operator's only explanation. (4) The
  supervisor audit the issue asked for: `maybe_fire_console_mode`
  bypassed `_fire_host_config`, so the console-mode plane had none of
  #387's protections — a failing `spatiumddi-verbose-boot-reload` left
  the applied sidecar unchanged, the next heartbeat rewrote the trigger,
  its rename re-fired the `.path` unit, and it repeated every ~30 s
  forever with nothing said upward; that runner's three failure paths
  also left the trigger in place (the #550 pattern, unfixed there).
  Reporting matters more than it sounds: a reverted agent keeps serving
  and keeps heartbeating, so `status`, the health check and `last_seen_at`
  all read normal while the saved zone or scope is live nowhere. Verdict
  → `{dns_server,dhcp_server,looking_glass_collector}.config_apply_*`
  (migration `e9c2d47b1a63`, partial index on the failing states only),
  a chip + detail banner, the default-**on** `agent_config_rejected` alert
  rule (severity from the agent's own verdict — `reverted` is a warning,
  `revert_failed` / `no_previous` critical), and 1 MCP tool
  (`find_agents_with_config_failures`). NULL means UNKNOWN, never `ok` —
  an agent too old to report is exactly where a silent revert would hide.
  **Deferred:** the `tz-status` / `verbose-boot-status` sidecars carry a
  failure *reason* nothing reads (`host_config_health` reports only that a
  plane is unapplied); and `DNSServerOptions.allow_query` is interpolated
  into `named.conf` unvalidated — a third instance of the #876/#899
  class, used deliberately here as the E2E fault-injection lever.
- ⬜ [**Config snapshots + rollback**](https://github.com/spatiumddi/spatiumddi/issues/883) — audit-log-driven revert
  of a single change plus scoped named snapshots. Distinct from #882,
  which is an agent-side safety net rather than an operator action.
- ⬜ [**Service restart from the GUI**](https://github.com/spatiumddi/spatiumddi/issues/890) — closes the #111 gap on
  docker-compose and Helm deployments, where the appliance's supervisor
  path has no equivalent.

#### Workflow & RBAC

- ✅ [**Approval workflows for risky ops — P1**](https://github.com/spatiumddi/spatiumddi/issues/62) — shipped `2026.06.25-1`: two-person rule over the 6 delete handlers behind the default-off `governance.approvals` module + a self-governance lock; lifecycle API + Change Requests admin page + 4 MCP tools + Change Approver builtin role. The issue closed on P1, so its P2 scope was re-filed ↓.
- ⬜ [**Approval workflows — P2 (bulk ops / factory reset / import gating + approval notifications)**](https://github.com/spatiumddi/spatiumddi/issues/717) — build on the shipped `governance.approvals` module + change-request lifecycle, not a parallel mechanism.
- ✅ [**Self-service request portal — IP / subnet / DNS / DHCP requests with approve-and-provision**](https://github.com/spatiumddi/spatiumddi/issues/696) — shipped `2026.07.30-1` (#721): the Phase 5 "IP request workflows" item. #62's approval engine pointed the other way: a low-privilege user asks for something they cannot do themselves, an approver reviews it with the operation's own preview, and approving **provisions** it. Deliberately **not** a second state machine — portal rows live in `change_request` under `origin="portal"` and reuse the entire #62 approve spine (FOR UPDATE guard, self-approval block, approver must hold the operation's own permission, re-preview stale guard, `apply()` under the approver, audit rows, expiry sweep), so provisioning behaves identically to a manual create. A catalog allow-list (`services/requests/catalog.py`) maps four kinds onto existing `Operation`s — that allow-list is load-bearing security, because submit intentionally skips the operation's permission check. Behind the default-off `governance.requests` module; new `provisioning_request` permission (`write` + `read`) + `Requester` builtin role; 3 MCP tools. **Auto-approve rules are designed for but not built** — the seam is in `submit_request`. Pairs with multi-tenancy and #64.
- ⬜ [**Resource locking**](https://github.com/spatiumddi/spatiumddi/issues/63)
- ⬜ [**Per-resource ACLs**](https://github.com/spatiumddi/spatiumddi/issues/64)
- ✅ [**Time-bound permissions**](https://github.com/spatiumddi/spatiumddi/issues/65) — shipped `2026.06.11-1`: a `time_bound_grant` table of auto-expiring *additive* RBAC grants (`{action, resource_type, resource_id?}` to a group until `expires_at`), consulted live by `user_has_permission` and soft-revoked by a 60 s beat sweep. Migration `d5e9b2c14a07`.
- ⬜ [**Comments / activity feed per resource**](https://github.com/spatiumddi/spatiumddi/issues/66)

#### Notifications & external integrations

- ✅ [**Ansible dynamic-inventory endpoint**](https://github.com/spatiumddi/spatiumddi/issues/67) — shipped `2026.06.11-1`: `GET /api/v1/ansible/inventory` returns standard Ansible dynamic-inventory JSON built from IPAM — hosts grouped by space / block / subnet / tag / custom-field, with `_meta.hostvars`. Read-only.
- ⬜ [**ServiceNow CMDB integration**](https://github.com/spatiumddi/spatiumddi/issues/68)

#### Security & compliance

- ✅ [**Password policy enforcement**](https://github.com/spatiumddi/spatiumddi/issues/70) — shipped `2026.05.07-1`: configurable complexity / history / max-age rules applied to every local-auth password set, over seven `platform_settings.password_*` knobs; `password_changed_at` + Fernet-wrapped `password_history_encrypted` on `user`.
- ✅ [**Account lockout after N failed logins**](https://github.com/spatiumddi/spatiumddi/issues/71) — shipped `2026.05.07-1`: windowed-counter lockout for local-auth users over `user.failed_login_count` / `failed_login_locked_until` / `last_failed_login_at`, reset on any success. Defaults to disabled (`threshold=0`). Migration `a7b3c8d92e14`.
- ✅ [**Active session viewer + force-logout**](https://github.com/spatiumddi/spatiumddi/issues/72) — shipped `2026.05.07-1`: live JWT registry the operator can browse and revoke from — access tokens carry a `jti`, and `user_session` gains `auth_source` / `last_seen_at` / `revoked` + a `(revoked, expires_at)` index. Migration `c8e4f7a91d36`.
- ✅ [**Internal cert + secret expiry monitoring**](https://github.com/spatiumddi/spatiumddi/issues/76) — shipped `2026.06.11-1`: one `secret_expiring` alert rule that fires per internal credential expiring within `threshold_days` — supervisor mTLS certs (`appliance.cert_expires_at`) + API tokens (`api_token.expires_at`). Extended in `2026.06.19-1` to cover the Let's Encrypt Web-UI cert (#438).
- ⬜ [**FIPS 140-3 posture**](https://github.com/spatiumddi/spatiumddi/issues/880) — crypto audit plus a tiered
  roadmap for a FIPS-capable build (containers / Kubernetes /
  appliance). Gate for government deployments; the audit comes first
  because the answer may be "these three libraries block it".
- ⬜ [**Appliance full-disk encryption**](https://github.com/spatiumddi/spatiumddi/issues/881) — LUKS2 at install for
  STATE + `/var`, TPM2 auto-unlock, and a recovery key the installer
  has to present exactly once without writing it anywhere it protects.

#### UX polish

- ✅ [**Saved searches / saved views**](https://github.com/spatiumddi/spatiumddi/issues/77) — **shipped 2026.06.19-1**: per-user `SavedView(user_id, page, name, payload, is_default)` table + `/api/v1/saved-views` CRUD (scoped by user, audited) behind the default-enabled `ui.saved_views` feature module, 2 read-only MCP tools (`find_saved_views` / `count_saved_views`), and a reusable `SavedViewsMenu` header dropdown (save / load / set-default / delete) wired into the Services / Circuits / Sites list pages. New pages opt in with two props (`currentPayload` + `onApply`).
- ⬜ [**Personal pinned dashboard**](https://github.com/spatiumddi/spatiumddi/issues/78)
- ⬜ [**Field-level history**](https://github.com/spatiumddi/spatiumddi/issues/79)
- ⬜ [**Recent items / favourites sidebar**](https://github.com/spatiumddi/spatiumddi/issues/80)
- ✅ [**Keyboard shortcut help overlay**](https://github.com/spatiumddi/spatiumddi/issues/81)
  — shipped `2026.07.30-1` (#737): `?` opens a modal listing every
  binding, from a new `frontend/src/lib/shortcuts.ts`
  mounted via `Header`. The map is deliberately **load-bearing rather
  than a parallel list** — `GlobalSearch`'s Cmd/Ctrl+K listener matches
  against it and its trigger keycap renders from it, so retuning a combo
  moves the handler, the keycap and the help together. That coupling
  reaches exactly two shortcuts today (Cmd/Ctrl+K, `?`); the other ten
  are flagged `describedOnly` because their handlers live in components
  that don't consult the map, and are the rows that can still drift.
  Note for anyone adding a binding: declare it here and match via
  `matchesShortcut` rather than adding another described-only row.
- ✅ [**Print / PDF export for IPAM tree + subnet detail**](https://github.com/spatiumddi/spatiumddi/issues/82) — shipped `2026.07.30-1` (#739): `GET /ipam/export.pdf` takes the same scope selector as the CSV/JSON/XLSX exporter (and reuses its `_collect`, so the two can't disagree about the subtree) and renders one of two shapes — a **tree** report for a space / block, or a **detail** report for a subnet. Surfaced as *Print / PDF* in both Export dropdowns. **reportlab, not the weasyprint the issue text proposed** — two reportlab PDFs already ship and a second engine would add Cairo / Pango to every image for no new capability. Unlike #48 and the conformity report, this one paginates: `repeatRows=1` plus a two-pass `_NumberedCanvas` for "Page N of M". Two things worth knowing if you touch it: reportlab's `Paragraph` parses mini-XML, so **all** DB-sourced text must go through `_para()` (an unescaped `a<b>c` space name 500s the export, and `<legacy> net` silently renders as "net"); and the tree indent must stay inside WinAnsiEncoding, or reportlab swaps in ZapfDingbats and nested blocks render as `■■`. Both have regression tests. *(GitHub auto-closed this issue on 2026-06-18 in error — PR [#446](https://github.com/spatiumddi/spatiumddi/pull/446) said `CodeQL #82`, meaning alert 82. Reopened 2026-07-28, genuinely shipped now.)*
- ✅ [**Global search v2**](https://github.com/spatiumddi/spatiumddi/issues/879) — all six gaps closed.
  Matching, ranking and gating moved out of the router into
  `backend/app/services/search/` (`ranking` / `providers` / `engine`),
  which the `global_search` MCP tool now calls instead of carrying its
  own copy of the fan-out. Coverage went 7 types → 20 via a
  `SearchProvider` registry that the engine, the scope chips, the MCP
  tool and `GET /search/types` all read from. **Ranking is computed in
  SQL, before each type's `LIMIT`** — the ordering bug was not really
  about order: with no `ORDER BY`, the database returned any N matching
  rows and the exact hit was routinely not among them, which sorting in
  Python afterwards cannot fix. Trigram GIN indexes (migration
  `f4b91d38a70c`) back the leading-wildcard `ILIKE` on the tables that
  actually grow; small tables are left unindexed on purpose. Frontend:
  scope chips, sessionStorage recents, and go-to-page commands sourced
  from the sidebar's own nav tree, extracted to `lib/navigation.ts` so
  the palette can't drift from the sidebar (the `lib/shortcuts.ts`
  argument from #737).
  **Four bugs found on the way, three of them pre-existing.** (1) Search
  applied **no permission filtering at all** — it was the widest read
  surface in the product and the only one that checked nothing, so an
  IPAM-only operator could read DNS zones and records straight out of a
  palette whose `GET /dns/zones` would have 403'd them; the Copilot tool
  had the same hole. (2) The query was interpolated raw into `%…%`, so
  searching `50%` matched every row in every table. (3) The MAC branch
  in the address query was unindexable and sat in an `OR` beside two
  indexed predicates, forcing the whole query to a sequential scan —
  the two working indexes bought nothing until it was normalised.
  (4) `_statement_references` in `app/db.py` called
  `statement.get_final_froms()` once per soft-delete model, ~1.9 ms
  each × 8, i.e. **~16 ms of Python on every ORM SELECT in the
  application** — invisible because it was uniform. Resolving the FROM
  graph once cut a 20-provider fan-out over 500k addresses from 734 ms
  to 93 ms. **Deferred:** an expression index for custom-field values
  (the field name is runtime-chosen, so no trigram index can serve
  `custom_fields ->> 'x' ILIKE …`), and action commands that *do*
  something rather than navigate.
- 🟡 [**Native mobile app**](https://github.com/spatiumddi/spatiumddi/issues/884) — PWA groundwork first, then iOS
  (SwiftUI) against the REST API. Non-negotiable #1 means the API is
  already complete enough to build against. The app itself now lives in its
  own repo, **[spatiumddi/spatiumddi-mobile](https://github.com/spatiumddi/spatiumddi-mobile)**;
  what remains here is the server side of that split. **Still open:** the app.
  - ✅ [**Publish `openapi.json` as a release asset**](https://github.com/spatiumddi/spatiumddi/issues/903)
    — with the app out of this repo the schema stops being a file a client
    reads off the working tree and becomes the contract *between two repos*,
    so it has to be versioned and fetchable. New `export-openapi` job in
    `release.yml` attaches it to every CalVer tag;
    `scripts/export_openapi.py` + `make openapi VERSION=…` reproduce the
    identical bytes locally and in the client repo's CI.
    **`info.version` was hardcoded `"0.1.0"`** in `create_app()` while
    `settings.version` carried the real one — so every release would have
    published a spec claiming to be 0.1.0, and a generated client would be
    stamped with a version that never changes, defeating the entire point
    of pinning. Fixed at the source, which also means a *running* server
    stops misreporting itself at `/api/docs`. The export must go through
    `app.openapi()` and never `get_openapi`: `create_app()` wraps it to
    widen `HTTPValidationError.detail` to the string form ~270 handlers
    actually return, and re-deriving the document drops that without
    touching a call site — verified present in generated TypeScript as
    `ValidationError[] | string`. `info.title` is pinned to the default
    because it follows the operator-settable `app_title` (#886/#888), so a
    branded install would otherwise publish its own name as the name of the
    public API. **Two footguns found building it:** an *empty* `VERSION`
    env var is not an absent one — pydantic-settings honours `""`, so
    `settings.version` becomes empty and FastAPI asserts on a falsy version
    (`create_app()` now passes `settings.version or "dev"` so a stray empty
    `VERSION` degrades instead of killing the container at import); an
    undefined Make
    variable and an unset `GITHUB_REF_NAME` both produce exactly that. And
    the script only imported `app` because the API image happens to set
    `PYTHONPATH=/app`; run from a plain checkout — the client repo's case —
    Python puts `scripts/` on `sys.path` rather than the cwd, so it now
    self-locates `backend/`. Output is `sort_keys`-canonical so the
    release-to-release diff stays readable; ~3.6 MB (not the "few hundred
    KB" the issue estimated). Retained on every release via the pruner's
    "unknown / future asset" default branch — **do not add a pattern for it
    to `scripts/prune-release-assets.sh`**. The version handshake the issue
    wanted is `GET /api/v1/version` (unauthenticated), **not**
    `/health/platform`, which reports no version at all.
- ✅ [**Login banner**](https://github.com/spatiumddi/spatiumddi/issues/885) · [**custom logo**](https://github.com/spatiumddi/spatiumddi/issues/886) ·
  [**environment banner**](https://github.com/spatiumddi/spatiumddi/issues/887) · [**`app_title` wired up**](https://github.com/spatiumddi/spatiumddi/issues/888)
  — shipped together in PR
  [#892](https://github.com/spatiumddi/spatiumddi/pull/892): an
  acceptable-use banner on the login screen, an operator-uploaded logo,
  a coloured DEV/TEST/PROD strip, and a real browser/product title
  (`app_title` was previously settable and read by nothing). All four
  ride nine `platform_settings` columns plus a `branding_asset` table;
  migration `d3f8b6c02a41`. The **logo lives in Postgres, not on
  disk** — a node-local file does not propagate across a multi-node
  control plane, the same reasoning as the #296 slot-image mirror.
  New unauthenticated `GET /settings/public` + `/settings/public/logo`
  (ETag + 304), because the login page needs all of this *before* a
  token exists. 1 MCP tool (`find_branding_settings`).

#### CLI tool

- ⬜ [**`spddi` CLI**](https://github.com/spatiumddi/spatiumddi/issues/83)

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
make trivy                             # container-image CVE scan — run before pushing ANY agent Dockerfile change.
make docs                              # local Jekyll preview of docs/ on :4000 (DOCS_PORT to override); docs-down stops it.
make docs-verify                       # diagram-geometry gate — same check CI's "Docs — Diagram Geometry" job runs.
                                       #   Run before pushing ANY docs/assets/**.svg change. Needs chromium on PATH.
make trivy IMAGE=kea                   #   ...one image only. Same gate CI uses (HIGH/CRITICAL, ignore-unfixed).
                                       #   CI's Trivy is path-filtered + PR-only, so touching a Dockerfile can surface a
                                       #   PRE-EXISTING CVE. Note golang:X.Y.Z pins are SECURITY pins — Go static-links its
                                       #   stdlib into the binary, so no apk upgrade can fix a stdlib CVE.
# ⚠️  DO NOT use `make test` on a small dev box. It runs `-n auto` (pytest-xdist),
#     one worker per CPU, each importing the full app and carving its own
#     `spatiumddi_test_gw<N>` database. On 8 cores / 7 GB that exhausts RAM and
#     `max_locks_per_transaction` partway through, and the failure DOES NOT look
#     like OOM — it looks like thousands of ERRORs at *fixture setup* on files
#     unrelated to your change (2655 then 2839 in one session, every affected
#     file passing serially). Budget a wasted debugging cycle if you trust it.
#     Also: only ever ONE pytest session at a time — conftest TRUNCATEs every
#     mapped table between tests, so two runs deadlock on the same locks.
#     Reach for `-n auto` only in CI or on a bigger machine.
make test                              # backend pytest, -n auto — CI / big-machine only; see warning above
make test-one T=tests/test_health.py::test_liveness   # ← PREFER THIS LOCALLY. Serial, ~5 min per ~110 tests.
                                       #   Or several files at once, still serial:
                                       #   docker compose -f docker-compose.dev.yml exec -T api \
                                       #     python -m pytest tests/test_a.py tests/test_b.py -q --no-cov

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
