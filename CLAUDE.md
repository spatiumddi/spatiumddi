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
| `docs/deployment/TOPOLOGIES.md` | Six reference deployment topologies — single VM, separated agents, DNS+DHCP HA, HA control plane (Patroni / Redis Sentinel), hybrid cloud, K8s — with SVG diagrams + sizing notes |
| `docs/deployment/KUBERNETES.md` | Helm chart, operators, HPA, Ingress |
| `docs/deployment/BAREMETAL.md` | Ansible playbooks, systemd services, Patroni |
| `docs/deployment/WINDOWS.md` | Windows Server prerequisites — WinRM, service accounts (DnsAdmins / DHCP Users), firewall, zone dynamic-updates; shared by Windows DNS + Windows DHCP |
| `k8s/README.md` | Kubernetes manifest usage, HA PostgreSQL (CloudNativePG), Redis Sentinel |
| `k8s/base/` | Core K8s manifests (namespace, API, worker, frontend, migrate job) |
| `k8s/ha/` | HA add-ons: CloudNativePG cluster, Redis Sentinel, Patroni Compose |
| `docs/drivers/DHCP_DRIVERS.md` | Kea + Windows DHCP driver internals |
| `docs/drivers/DNS_DRIVERS.md` | BIND9 + PowerDNS + Windows DNS (Path A + B) driver internals, incremental update strategy |

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
  drivers/dns/          DNS backend abstraction + BIND9 / PowerDNS / Windows DNS impls
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
k8s/base/               Core manifests (api, worker, frontend, migrate)
k8s/{dns,dhcp}/         Per-service StatefulSets + services
k8s/ha/                 CloudNativePG, Redis Sentinel, Patroni
charts/spatiumddi/      Umbrella Helm chart (API + FE + worker + beat + migrate + Postgres/Redis subcharts + optional DNS/DHCP agents)
scripts/seed_demo.py    Demo data seeder
docs/                   Specs + Jekyll site (served at spatiumddi.github.io)
website/                Marketing site source (designed in Claude Designer; deployed to spatiumddi.github.io / future custom domain — see Marketing Website note below)
```

> **Marketing website (`/website`).** Operator-facing landing page is authored in Claude Designer and lives in `website/`. The Jekyll docs at `docs/` (served on the `gh-pages` branch as `spatiumddi.github.io/spatiumddi/`) are technical and stay where they are. The marketing site will deploy as the **root** `spatiumddi.github.io` domain (or a registered custom domain via Cloudflare Pages). When editing the marketing site, leave the Jekyll docs alone; when editing the Jekyll docs, leave the marketing site alone. Both can ship in the same PR but never as the same artifact. Open questions: which static-site generator to lock in (raw HTML / Astro / Next.js static), whether to mirror the README screenshots / feature table here, and the CI pipeline (separate workflow that builds + publishes to a `marketing` branch / Cloudflare Pages on every `website/**` change). See `website/README.md` (once it lands) for the deployment recipe.

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

SpatiumDDI cut its alpha release `2026.04.16-1` on 2026-04-16 with IPAM, DNS (BIND9), and DHCP (Kea) all shipping. Subsequent releases landed Windows Server integration (`2026.04.18-1`, 2026-04-18), the **performance + polish + visibility** release (`2026.04.19-1`, 2026-04-19) — batched WinRM dispatch, DDNS pipeline, the Logs surface, subnet/block resize, subnet-scoped IP import, DHCP pool awareness, collision warnings, sync modals, dashboard heatmap, draggable modals, standardised header buttons — the 2026.04.20 IPv6 + DDNS closure work, the **Kea HA + group-centric DHCP** release (`2026.04.21-2`, 2026-04-21) which shipped the full three-wave Kea HA story: end-to-end HA shake-out (peer URL resolution, port split, `status-get`, bootstrap reload), group-centric DHCP data model (scopes / pools / statics / classes live on `DHCPServerGroup`; HA is implicit with ≥ 2 Kea members), agent rendering fix (every prior Kea install was silently rendering `subnet4: []` due to a wire-shape bug), `PeerResolveWatcher` self-healing for peer IP drift, supervised Kea daemons, and standalone agent-only compose files for distributed deployments, the **integrations + observability** release (`2026.04.22-1`, 2026-04-22) that shipped Kubernetes + Docker read-only mirrors, the ACME DNS-01 provider, DHCP MAC blocklist, dashboard timeseries charts + platform-health card + collapsible sidebar, and the **Proxmox VE + polish** release (`2026.04.24-1`, 2026-04-24) which shipped the Proxmox endpoint mirror (bridges + SDN VNets + opt-in VNet-CIDR inference + per-guest Discovery modal), the shared `IPSpacePicker` quick-create component across all three integration modals, plus four UX polish fixes (real source IP behind nginx, alphabetised Integrations nav, wider Custom Fields page, search-row amber highlight), and the **network discovery + nmap** release (`2026.04.28-1`, 2026-04-28) which shipped SNMP polling of routers + switches with ARP/FDB cross-reference into IPAM (per-IP switch-port + VLAN visibility), the on-demand nmap scanner with live SSE output streaming (per-IP + `/tools/nmap` standalone page), the read-only `IPDetailModal` opened on row-click in IPAM with action buttons for Scan/Edit/Delete, sidebar regroup (core flattened, new Tools section, Administration items separated by dividers), removal of the dead Settings → Discovery section, and a linear-time rework of the BIND9 query log parser (CodeQL alert #16 closed), and the **notifications + automation** release (`2026.04.30-1`, 2026-04-30) which closed the Notifications-and-external-integrations bucket: SMTP email delivery for alerts + audit forward (stdlib `smtplib` driven through `asyncio.to_thread`, supports starttls/ssl/none, Fernet-encrypted password at rest), Slack / Teams / Discord chat-flavored webhooks via a new `webhook_flavor` column that selects between generic JSON / Slack `mrkdwn` / Teams `MessageCard` / Discord `embed` body renderers (operators paste the platform's incoming-webhook URL into a webhook target), and a new typed-event webhook surface at `/admin/webhooks` — 96 typed events derived from a `resource_namespace × verb` cross-product (`space.created`, `subnet.bulk_allocate`, `dns.zone.updated`, `dhcp.scope.deleted`, `auth.user.created`, `integration.kubernetes.created`, …) delivered through an `EventOutbox` table with HMAC-SHA256 signing (`hmac(secret, ts + "." + body, sha256)` → `X-SpatiumDDI-Signature: sha256=<hex>`), exponential backoff (2 / 4 / 8 … 600 s capped), `state="dead"` on permanent failure, manual retry from the per-row deliveries panel, and one-time secret reveal on subscription create. Same release also shipped DNS GSLB pools (priority + weight + health-checked record sets that auto-reconcile to rendered A/AAAA records via `apply_pool_state`, with the new orphan sweep that catches records whose member just got deleted by a JOIN through `DNSPoolMember.pool_id`), a DNS server detail modal with Logs / Stats / Config tabs, **device profiling** as both phases at once: subnet-level opt-in auto-nmap on fresh DHCP leases (Phase 1, refresh-window dedupe + per-subnet 4-scan cap + `POST /ipam/addresses/{id}/profile` re-profile-now button) and passive DHCP fingerprinting via a scapy `AsyncSniffer` thread on the DHCP agent feeding a fingerbank lookup task (Phase 2, default-off, needs `cap_add: NET_RAW`), both surfaces converging in a unified "Device profile" panel inside the IP detail modal that shows passive Type/Class/Manufacturer + active OS guess + top open services. Same-day follow-ups: `setcap cap_net_raw+eip` on `/usr/bin/nmap` plus `NMAP_PRIVILEGED=1` in the api/worker image (so non-root operator OS scans actually work — Debian's nmap does an early `getuid()==0` check that ignores file caps), `securityContext.capabilities.add: [NET_RAW]` on the K8s worker + `worker.netRawCapability` Helm gate for restricted PSA / OpenShift-SCC / GKE Autopilot, and the Settings → IPAM → Device Profiling form for the fingerbank API key (Fernet-encrypted at rest; the response payload only exposes a boolean `fingerbank_api_key_set`). The post-device-profiling polish wave added two new nmap presets (`subnet_sweep` -sn for CIDR ping-sweeps capped at /16 worth of hosts, `service_and_os` -sV -O --version-light as the device-profiling default), CIDR-aware target validation + multi-host XML parsing (the runner now walks every `<host>` element and emits a `hosts[]` summary when >1), `POST /nmap/scans/bulk-delete` (cap 500, mixes cancel + delete based on per-row state) + `POST /nmap/scans/{id}/stamp-discovered` (claim alive hosts as `discovered` IPAM rows + stamp `last_seen_at` via nmap; integration-owned rows just bump the timestamp), `NmapToolsPage` rewritten as a 3-tab right panel (Live / History / Last result) with a checkbox column + bulk-delete toolbar on history, the new `discovered` status added to `IP_STATUSES_INTEGRATION_OWNED`, the IPAM subnet header collapsed from 9 buttons to 6 via a Tools dropdown (alphabetised: Bulk allocate…, Clean Orphans, Merge…, Resize…, Scan with nmap, Split…), a new "Seen" column in the IP table backed by a 4-state `SeenDot` (alive <24h green / stale 24h-7d amber / cold >7d red / never grey, source method in the tooltip — orthogonal to lifecycle status), and **bulk allocate** — `POST /ipam/subnets/{id}/bulk-allocate/{preview,commit}` stamps a contiguous IP range plus a name template (`{n}` / `{n:03d}` / `{n:x}` / `{oct1}`–`{oct4}`) in one shot with per-row conflict detection (already-allocated, dynamic-pool, FQDN collision) and `on_collision: skip|abort` policy, capped at 1024 IPs per call; `BulkAllocateModal` lives in the Tools menu with a three-phase form → preview → committed flow and live client-side template rendering as the operator types. Same wave also fixed three IPAM table polish items: the sticky `<thead>` finally holds in Chrome (the inner `<div className="overflow-x-auto">` wrapper was establishing a Y-scroll context per CSS spec — `overflow-x: auto` with `overflow-y: visible` computes to `overflow-y: auto` automatically, defeating sticky positioning by anchoring the head to a non-scrolling intermediate parent; removed the wrapper so sticky resolves to the outer `flex-1 overflow-auto`), shift-click range select on IP checkboxes (capture `e.shiftKey` in `onClick` which fires before `onChange`, walk the IP-only `tableRows` order between the previous click and the new one, toggle every selectable row to the new state), and subtle dashed-emerald gap-marker rows between non-adjacent IPAM entries (e.g. `.11 · 1 free` or `.11 – .13 · 3 free` — heads-up for "you deleted something and might have missed the hole"; suppressed inside dynamic DHCP pools where slots are owned by the DHCP server). Also a DNS pool reconciliation fix landed in the same release: `PoolMemberUpdate` schema gained the missing `address` field (an IP-edit was silently dropped because Pydantic filtered the unknown field), the diff loop in `PoolsView` was extended to detect address changes, address-change resets the member's health stats (`last_check_state="unknown"`, counters → 0) so the new IP re-proves health, the reconciliation gate widened from `enabled_changed` to `member_changed` (any of address/enabled/weight), and the zone Refresh button now invalidates `["dns-records"]`, `["dns-pools"]`, and `["dns-zone-server-state"]` so the Pools tab + per-server zone state pill stay current. and the **network-layer** release (`2026.05.03-1`, 2026-05-03) which closed the Network sidebar reorg + ASN/VRF/Domain umbrella roadmap (issues #84–#89): new "Network" sidebar section grouping Devices / VLANs / VRFs / ASNs, **ASN management** as a first-class entity (BigInteger `number` for full 32-bit range, auto-derived `kind` per RFC 6996 + RFC 7300, auto-derived `registry` from a hand-curated IANA delegation snapshot, RDAP holder refresh that derives the RIR via `derive_registry()` and queries the RIR's RDAP base directly because `rdap.iana.org/autnum/<n>` is a bootstrap registry not a query proxy and returns HTTP 501 for every real query, RPKI ROA pull from Cloudflare or RIPE with per-source 5 min in-memory cache, holder-drift diff viewer driven by `previous_holder` persisted in `whois_data` on every refresh, four ASN/RPKI alert rule types, BGP peering relationships table with `peer | customer | provider | sibling` and `Router.local_asn_id` FK, BGP communities catalog seeding the 7 RFC 1997 / 7611 / 7999 well-knowns as platform rows shared across all ASes plus per-AS catalog with `kind` validation `standard | regular ASN:N | large ASN:N:M`), **VRFs as first-class entities** replacing the freeform `vrf_name` / `route_distinguisher` / `route_targets` text fields on IPSpace with a relational `vrf` table carrying optional `asn_id` FK, RD format validation, split import / export RT lists, and a cross-cutting validator that warns (or 422s under `vrf_strict_rd_validation`) when the ASN portion of an `ASN:N` RD/RT doesn't match the linked ASN — migration backfills existing freeform values into VRF rows so nothing is lost, **Domain registration tracking** distinct from DNSZone for the registry side of a name (registrar / expiry / nameservers / DNSSEC) with RDAP refresh through a TLD → RDAP-base lookup driven by `data.iana.org/rdap/dns.json` cached in-process for 6 h with thundering-herd protection (because `rdap.iana.org/domain/<n>` returns 404 for any non-test domain), four `domain_*` alert rule types (expiring with severity escalation around `threshold_days` / NS drift / registrar changed / DNSSEC status changed) with the latch-once rules persisting `last_observed_value` JSONB so a single flip fires exactly one event auto-resolved after 7 d, explicit `dns_zone.domain_id` linkage with sub-zone suffix-match fallback (`zone === domain || zone.endsWith("." + domain)` so `test.example.com` shows up under `example.com` but `example.com.au` correctly does NOT match `example.com`), plus a wave of UX easy-wins: shared `RdapPanel` flattening RDAP wire shape into operator-friendly UI on both ASN and Domain WHOIS tabs (handle / name / DNSSEC / port43 headlines, status flags, nameserver chip list, event timeline, vCard entities flattened into per-role org / email / phone / address blocks), ASN + VRF pickers on IPSpace / IPBlock modals (replacing the freeform RD / RT inputs), Platform Health card moved from below the heatmap up to immediately under the KPI ribbon, alphabetised `/api/v1` router includes (Swagger UI lists sections A → Z), DNS zone "Sub-zone" header button that pre-fills the New Zone modal with `.<parent>`, and IPAM gap rows (`.11 – .13 · 3 free`) clickable to launch AddAddressModal pre-filled with First / Last / Random quick-pick buttons over the gap. Same release also fixed RDAP routing being silently broken end-to-end (both ASN and Domain — see above), the RPKI gate that was permanently locked out for fresh ASNs (the WHOIS task bumps `next_check_at` ~24 h forward on first refresh; the RPKI task was *also* gating on that column for the first-time SELECT, so zero ROAs ever landed for any public ASN), RPKI validity windows being hardcoded `None` per an outdated docstring (Cloudflare ships `expires` Unix epoch + RIPE ships `notBefore` / `notAfter` ISO 8601, both now parsed via `_parse_validity`), `GET /asns/{id}/rpki-roas` route never existing despite the frontend client calling it (silent 404 + empty React Query result), `GET /asns/peerings` 422'ing because earlier route registration order put `GET /{asn_id}` before the literal `/peerings` (fixed by Starlette `:uuid` converter on every `{asn_id}` path), `prefix` coming back as `IPv4Network` not `str` (asyncpg behaviour; Pydantic 500'd every list call — fixed with `mode="before"` field_validator), and the VRF migration crashing on existing rows whose `route_targets` JSONB happened to be a string rather than an array, and the **Operator Copilot + network modeling** release (`2026.05.05-1`, 2026-05-05) which shipped two big themes back-to-back: **Operator Copilot (#90)** in two phases — Phase 1 lays the LLM provider foundation (config + Fernet-encrypted api_key + ordered priority for failover, MCP HTTP endpoint, tool registry with 18 read-only tools, chat orchestrator + SSE chat endpoint with `X-Accel-Buffering: no` so nginx doesn't batch chunks, floating chat drawer with optimistic message render + clickable empty-state prompts, token / cost observability with per-user daily caps, AI usage card on Platform Insights), Phase 2 widens to a multi-vendor copilot with Anthropic (Claude) / Azure OpenAI / Google Gemini drivers alongside the existing OpenAI-compat driver (one neutral request shape, per-driver translate-at-the-SDK-boundary), automatic failover chain across enabled providers on transient 5xx/timeout/rate-limit, "Ask AI about this" affordances on subnets / IPs / DNS zones / records / alerts / audit / DHCP / network devices, custom prompts library with built-in starter pack, Cmd-K palette "Ask AI" entry, daily Operator Copilot digest fired through audit-forward / SMTP / webhook channels, write tools with preview / apply flow (model proposes via `propose_*` returning a planned diff, operator clicks Apply in the chat drawer to hit the real CRUD endpoint), and richer dynamic context interpolating platform stats + scoped permissions + "today's interesting things" into the system prompt; **network modeling (#91 / #93 / #94 / #95)** as a four-issue umbrella — **Customer / Site / Provider** logical ownership entities with cross-reference FKs (`ON DELETE SET NULL`) on every existing IPAM/DNS/DHCP/Network table plus shared pickers + chips wired into every modal, **WAN circuits** (carrier-supplied logical pipe — provider RESTRICT / customer + endpoints SET NULL, nine transport classes including AWS DX / Azure ER / GCP Interconnect cross-connects, asymmetric bandwidth, term + cost, soft-deletable so `status='decom'` is the operator-visible end-of-life flag while history stays restorable, plus `circuit_term_expiring` + `circuit_status_changed` alert rules with the latter only firing on `suspended`/`decom` transitions and auto-resolving after 7 d), **service catalog** (`network_service` + polymorphic `network_service_resource` join row binding to VRF / Subnet / IPBlock / DNSZone / DHCPScope / Circuit / Site / OverlayNetwork; first concrete kind is `mpls_l3vpn` with hard at-most-one-VRF rule + soft warnings for missing VRF, fewer than 2 edge sites, edge subnet's enclosing block in a different VRF; kind-aware `/summary` returns canonical L3VPN shape; `service_term_expiring` + `service_resource_orphaned` alert rules, with `subject_type` widened from VARCHAR(20) → VARCHAR(40) to fit `network_service_resource`; reverse-lookup `/by-resource/{kind}/{id}` for the upcoming "show services using this resource" entry points (#99)), and **SD-WAN overlay topology** (`overlay_network` + `overlay_site` m2m with role + edge device + ordered preferred-circuit chain + `routing_policy` with priority + match-kind + match-value + action + action-target + `application_category` curated SaaS catalog seeded with 33 well-known apps following the BGP-communities pattern; `/topology` returns nodes + edges-by-shared-circuits + policies; `/simulate` is pure read-only what-if accepting `down_circuits` and returning per-site fallback resolution + per-policy `impacted` flag with human-readable note; SVG circular-layout topology visualization with role-coloured nodes + transport-coloured edges; service-catalog integration unlocked — `sdwan` added to `SERVICE_KINDS_V1` and `overlay_network` lit up as a real attach target on `NetworkServiceResource`); plus the **security wave** (#69 TOTP MFA for local users with QR enrolment + backup codes + admin force-disable, #74 API-token scopes via `scopes` JSONB column with permission-name granularity, #75 subnet classification tags `pci_scope` / `hipaa_scope` / `internet_facing` / `contains_pii` inheritable through the IP block tree), **#26 IPAM template classes** (reusable stamp templates with optional sub-subnet `child_layout` and `applies_to` carrier-locked apply policy, `force=False` fills only empty target columns + `force=True` overwrites for a `/reapply-all` drift-refresh sweep capped at 200 instances), **#27 block move across IP spaces** (typed-name confirm + dependent-row pre-flight validation), **#25 split-horizon DNS publishing at the IPAM layer** (`IPBlock.dns_split_horizon` boolean + per-record `IPAddress.dns_zone_overrides` JSONB list), **#51 DHCP — PXE / iPXE provisioning profiles** (per-arch `pxe_profile` table + `DHCPScope.pxe_profile_id` + Kea driver renders one client-class per arch-match guarded by `dhcp.user-class` matching the iPXE signature so legacy PXE clients see the BIOS bootfile and iPXE clients see the iPXE script), **#96 API docs link** in sidebar + header, plus a wave of UX polish: tabbed IP Space modals (shared `ModalTabs` helper at `frontend/src/components/ui/modal.tsx`), pinned identity header above tabs on every IPAM modal, the Network sidebar section sub-grouped (Logical: Customers / Providers / Services / Sites; Infrastructure: ASNs / Circuits / Devices / Overlays / VLANs / VRFs) with `SubNavLabel` rows, the Administration sidebar split into Identity / Platform / Auditing / Tools dividers, and `scripts/seed_demo.py` extended to cover every shipped entity for a realistic demo dataset, and the **Operator Copilot polish + compliance loop** release (`2026.05.05-2`, 2026-05-05) which lands three threads on top of 2026.05.05-1: **Operator Copilot polish** makes the chat usable end-to-end against self-hosted Ollama (driver forwards `options.num_ctx` / `num_predict` / `extra_body` so Ollama respects the configured context — the silent 2048-token default was truncating the 8K-token prompt + tool schemas and caused every small model to hallucinate tool names from a half-cut list; reasoning-channel fallback captures `delta.reasoning` from qwen3.5 / DeepSeek-R1 / o1-style models so post-tool answers don't disappear; trailing-usage-chunk handler picks up Ollama's separate empty-choices `usage` chunk so per-message token counts are accurate). Tool registry expands from 22 to 35 read-only tools with new modules covering ASNs / domains / VRFs / circuits / services / overlays / applications plus `find_switchport` (joins IP→MAC→FDB→interface) + `ping_host` + nmap inspection + `propose_run_nmap_scan` write proposal + OUI vendor enrichment inline on `find_ip` / `find_dhcp_leases` / `find_switchport` + name-or-UUID resolution on `space_id` / `block_id`. Per-provider system prompt override (migration `d6a39e84c512`) + per-provider tool allowlist (migration `c4e8b71f0d23`, NULL = "use whatever the registry has", non-empty list pins exactly those tools — narrows Ollama down to the 8 tools you actually use, restricts kiosk providers to read-only, gates `propose_*` writes per provider) ship as new tabs on the AI Provider modal; chat drawer renders markdown via react-markdown / remark-gfm, persists active session + composer draft to sessionStorage, surfaces per-message tokens / copy / info popover footer, and grows multi-select bulk delete in History. **Compliance change alerts (#105)** add a `compliance_change` rule type (migration `e3f1c92a4d68` adds `classification` + `change_scope` + `last_scanned_audit_at` to `alert_rule`) plus three disabled seed rules (PCI / HIPAA / internet-facing) — the alert evaluator scans the audit log on the existing 60 s tick, opens one event per mutation against a classification-flagged subnet (or descendant IP / DHCP scope), watermarks to `now()` on first run so historical rows don't retro-page operators, and auto-resolves after 24 h. **Conformity evaluations (#106)** are the proactive companion (migration `b5d8a3f12c91` adds `conformity_policy` + `conformity_result` tables, the latter indexed twice on `(policy_id, evaluated_at)` and `(resource_kind, resource_id, evaluated_at)` so both natural drilldowns hit an index) — declarative `ConformityPolicy` rows pin a `check_kind` against a target set; beat-driven engine runs every enabled policy on its `eval_interval_hours` cadence (default 24 h); six starter check kinds cover the common shapes (`has_field` / `in_separate_vrf` / `no_open_ports` / `alert_rule_covers` / `last_seen_within` / `audit_log_immutable`); eight seed policies span PCI-DSS / HIPAA / SOC2 (all `is_builtin=True` + `enabled=False`); pass→fail transitions emit `AlertEvent` rows against the policy's wired alert rule when set; synchronous `reportlab`-based PDF export at `/conformity/export.pdf` with per-framework section, failing-row enumeration with diagnostic JSON, SHA-256 integrity hash over `(result_id, status)` tuples in the trailer for tamper detection (PDF is gated behind the new `conformity` permission). Two new built-in roles seed at startup: **Auditor** (read-only on conformity + audit + the underlying resources) and **Compliance Editor** (admin on conformity + read on the underlying resources) — wired through `main.py`'s `_BUILTIN_ROLES` dict. #105 + #106 form the complete compliance loop: alerts catch the change in real time, evaluations prove steady state and produce the document auditors actually file. Plus ancillaries: nmap `quick` preset bumped from top-100 to top-1000 (`udp_top100` preset renamed to `udp_top1000` with migration `a8d6e10f3b59` backfilling existing rows), a `PXE Profiles` button on the DHCP server-group view (was previously only reachable from inside the scope edit modal), README gains a top-level table of contents, and a mypy fix on `network_modeling.py` (`dict(rows.all())` → `{row[0]: row[1] for row in rows.all()}` on three count-rollup queries — same fix unblocks the dependabot axios PR #102), and the **backup + factory-reset** release (`2026.05.07-1`, 2026-05-07) which closes issue #117 (full system backup with remote destinations) end-to-end through Phase 3 + ships issue #116 (factory reset back to defaults) in a single commit. Backup ships eight destination kinds — local volume / AWS S3 (and every S3-compatible endpoint) / SCP/SFTP / Azure Blob / SMB-CIFS / FTP-FTPS / Google Cloud Storage / WebDAV — plus selective per-section restore (17-section catalog mapping all 110 schema tables; TRUNCATE…CASCADE + `pg_restore --data-only --table=…`), cross-install secret rewrap so cross-install operators no longer hand-copy `SECRET_KEY` (22 columns across 16 tables + the `backup_target.config` JSONB blob, idempotent re-runs counted as `skipped_idempotent`, same-install short-circuit), automatic `alembic upgrade head` on restore (with `auto_recovered` state catching the real-world drift case where a backup taken when alembic_version was stale relative to its schema would otherwise fail with "table already exists" — recovery via `alembic stamp head`), exclude-secrets diagnostic mode (plain-format dump + in-memory text scrub of every Fernet column + `__enc__:` JSONB field, live DB never touched), scheduled cron + retention with friendly presets, restore-from-destination, archive proxy-download, "download latest" per-target endpoint, friendly cron presets, and `system.backup_*` / `system.restore_performed` / `system.factory_reset` typed events through the existing webhook event-outbox. Three read-only MCP tools (`list_backup_targets` / `list_backup_archives_at_target` / `find_backup_audit_history`, all superadmin-gated, no `propose_*` writes by design) surface the backup state to the Operator Copilot. Factory reset (`9975027`) runs per-section across 12 sections (IPAM / DNS / DHCP / Network modeling / Integrations / AI / Compliance / Tools / Observability logs / Auth+RBAC / Settings / Everything) plus an "Everything" target, every guardrail enforced server-side: superadmin gate + fresh password re-check + per-section confirm phrase (`DESTROY-IPAM` / … / `FACTORY-RESET-ALL`) + in-flight-backup mutex + 6h cooldown + audit anchor that survives `audit_log` wipes. Pre-flight backup as warn-only with `acknowledge_no_backup` override (412 unless explicitly acknowledged). Three dispatch kinds: `truncate` (9 sections), `auth_rbac` (preserves calling user + every other superadmin + built-in roles), `settings_reset` (DELETE platform_settings; recreated with model defaults). Tables intentionally untouchable: `alembic_version`, `oui_vendor`, `backup_target`, `feature_module`, `event_outbox`, `internal_error`, `audit_forward_target`. The page lives as a third tab on the Backup admin surface — backup snapshots state, factory reset wipes it. Also in this release: a new `docs/deployment/TOPOLOGIES.md` with six reference production topologies (single VM through HA cloud + on-prem hybrid) and an Operator Copilot tool count of **91 tools** total (78 → 91 with the operator-toggleable platform / Tool Catalog / Tier 2-5 wave that landed before backup work began plus the 3 new backup tools). For the full list see `CHANGELOG.md`. The forward-looking work is below.

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

- ⬜ [**Windows DNS — Path B (WinRM + PowerShell, full CRUD)**](https://github.com/spatiumddi/spatiumddi/issues/21)
- ⬜ [**Windows DHCP — Path B (WinRM + PowerShell, full CRUD)**](https://github.com/spatiumddi/spatiumddi/issues/22)
- ⬜ [**IP discovery**](https://github.com/spatiumddi/spatiumddi/issues/23)
- ⬜ [**DNS Views — end-to-end split-horizon wiring**](https://github.com/spatiumddi/spatiumddi/issues/24)
- ⬜ [**ACME embedded client — certs for SpatiumDDI's own services**](https://github.com/spatiumddi/spatiumddi/issues/28)
- ⬜ [**Cloud DNS driver family — Route 53 / Azure DNS / Cisco DNA**](https://github.com/spatiumddi/spatiumddi/issues/29)
- 🟡 [**PowerDNS authoritative driver**](https://github.com/spatiumddi/spatiumddi/issues/127) — Phases 1 + 2 + 3a shipped (driver class + agent + LMDB image + multi-arch build + frontend driver picker + ALIAS records); Phases 3b-5 (LUA records, online DNSSEC, catalog zones, Helm wiring, kind-cluster smoke test, docs pass) pending.

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

- ⬜ [**UniFi Network Application**](https://github.com/spatiumddi/spatiumddi/issues/30)
- ⬜ [**OPNsense (tier 1 — firewall-of-choice for labs)**](https://github.com/spatiumddi/spatiumddi/issues/31)
- ⬜ [**pfSense (tier 1 — paired with OPNsense)**](https://github.com/spatiumddi/spatiumddi/issues/32)
- ⬜ [**MikroTik RouterOS 7 (tier 2)**](https://github.com/spatiumddi/spatiumddi/issues/33)
- ⬜ [**Incus / LXD (tier 2 — Docker-adjacent)**](https://github.com/spatiumddi/spatiumddi/issues/34)
- ⬜ [**HashiCorp Nomad (tier 2 — Kubernetes alt)**](https://github.com/spatiumddi/spatiumddi/issues/35)
- ⬜ [**NetBox read-only import (one-shot)**](https://github.com/spatiumddi/spatiumddi/issues/36)
- ⬜ [**Cloud connectors — unified "Cloud" integration with per-provider picker (Azure / AWS / GCP)**](https://github.com/spatiumddi/spatiumddi/issues/37)
- ⬜ [**Load balancer family (F5 BIG-IP, HAProxy, nginx, KEMP, A10, Citrix ADC)**](https://github.com/spatiumddi/spatiumddi/issues/38)
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

- ⬜ [**NetFlow / sFlow ingestion**](https://github.com/spatiumddi/spatiumddi/issues/39)
- ⬜ [**mDNS / Bonjour / WSD passive discovery**](https://github.com/spatiumddi/spatiumddi/issues/40)
- ⬜ [**Reverse-DNS auto-population**](https://github.com/spatiumddi/spatiumddi/issues/41)
- ⬜ [**CGNAT (RFC 6598) awareness**](https://github.com/spatiumddi/spatiumddi/issues/42)

#### Reporting & analytics

- ⬜ [**Capacity forecasting**](https://github.com/spatiumddi/spatiumddi/issues/43)
- ⬜ [**Per-subnet utilization history**](https://github.com/spatiumddi/spatiumddi/issues/44)
- ⬜ [**Stale-IP report**](https://github.com/spatiumddi/spatiumddi/issues/45)
- ⬜ [**Decom-date awareness**](https://github.com/spatiumddi/spatiumddi/issues/46)
- ⬜ [**Top-N reports**](https://github.com/spatiumddi/spatiumddi/issues/47)
- ⬜ [**Compliance / change report PDF**](https://github.com/spatiumddi/spatiumddi/issues/48)

#### Subnet planning & calculation tools

All shipped — see `Subnet planning & calculation tools` in
[`docs/SHIPPED.md`](docs/SHIPPED.md): CIDR calculator,
Subnet planner workspace, address planner, aggregation
suggestion, free-space treemap.

#### DNS-specific

- ⬜ [**DNSSEC**](https://github.com/spatiumddi/spatiumddi/issues/49)
- ⬜ [**DoT / DoH listener**](https://github.com/spatiumddi/spatiumddi/issues/50)

#### DHCP-specific


- ⬜ [**DHCPv6 stateful + SLAAC config UI**](https://github.com/spatiumddi/spatiumddi/issues/52)
- ⬜ [**Lease histogram by hour**](https://github.com/spatiumddi/spatiumddi/issues/53)
- ⬜ [**Option 82 (relay agent info) class matching**](https://github.com/spatiumddi/spatiumddi/issues/54)
- ⬜ [**DHCP test client**](https://github.com/spatiumddi/spatiumddi/issues/55)

#### Operational tooling

- ⬜ [**Time-travel queries**](https://github.com/spatiumddi/spatiumddi/issues/56)
- ⬜ [**Maintenance mode**](https://github.com/spatiumddi/spatiumddi/issues/57)
- ⬜ [**Built-in network tools page**](https://github.com/spatiumddi/spatiumddi/issues/58)
- ⬜ [**PCAP capture trigger**](https://github.com/spatiumddi/spatiumddi/issues/59)
- ⬜ [**ACL / prefix-list generator**](https://github.com/spatiumddi/spatiumddi/issues/60)
- ⬜ [**Config-drift report (full record diff)**](https://github.com/spatiumddi/spatiumddi/issues/61)

#### Workflow & RBAC

- ⬜ [**Approval workflows for risky ops**](https://github.com/spatiumddi/spatiumddi/issues/62)
- ⬜ [**Resource locking**](https://github.com/spatiumddi/spatiumddi/issues/63)
- ⬜ [**Per-resource ACLs**](https://github.com/spatiumddi/spatiumddi/issues/64)
- ⬜ [**Time-bound permissions**](https://github.com/spatiumddi/spatiumddi/issues/65)
- ⬜ [**Comments / activity feed per resource**](https://github.com/spatiumddi/spatiumddi/issues/66)

#### Notifications & external integrations

- ⬜ [**Ansible dynamic-inventory endpoint**](https://github.com/spatiumddi/spatiumddi/issues/67)
- ⬜ [**ServiceNow CMDB integration**](https://github.com/spatiumddi/spatiumddi/issues/68)

#### Security & compliance

- ⬜ [**Password policy enforcement**](https://github.com/spatiumddi/spatiumddi/issues/70)
- ⬜ [**Account lockout after N failed logins**](https://github.com/spatiumddi/spatiumddi/issues/71)
- ⬜ [**Active session viewer + force-logout**](https://github.com/spatiumddi/spatiumddi/issues/72)
- ⬜ [**Audit-log tamper detection**](https://github.com/spatiumddi/spatiumddi/issues/73)
- ⬜ [**Internal cert + secret expiry monitoring**](https://github.com/spatiumddi/spatiumddi/issues/76)

#### UX polish

- ⬜ [**Saved searches / saved views**](https://github.com/spatiumddi/spatiumddi/issues/77)
- ⬜ [**Personal pinned dashboard**](https://github.com/spatiumddi/spatiumddi/issues/78)
- ⬜ [**Field-level history**](https://github.com/spatiumddi/spatiumddi/issues/79)
- ⬜ [**Recent items / favourites sidebar**](https://github.com/spatiumddi/spatiumddi/issues/80)
- ⬜ [**Keyboard shortcut help overlay**](https://github.com/spatiumddi/spatiumddi/issues/81)
- ⬜ [**Print / PDF export for IPAM tree + subnet detail**](https://github.com/spatiumddi/spatiumddi/issues/82)

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
