# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **GitHub Org:** https://github.com/spatiumddi  
> **Docs:** https://spatiumddi.github.io (custom domain: https://spatiumddi.org — pending)  
> **License:** Apache 2.0  
> **Package:** `spatiumddi` on PyPI  
> **Container registry:** `ghcr.io/spatiumddi/*`  

> **Read this file first.** This is the entry point for all Claude Code sessions on the SpatiumDDI project. It defines the project scope, the document map, and the non-negotiable conventions every generated file must follow.

---

## What Is SpatiumDDI?

SpatiumDDI is a production-grade, open-source **all-in-one DDI (DNS, DHCP, IPAM)** platform. It does not merely configure external DDI servers — it manages and runs the DHCP, DNS, and NTP service containers directly. The control plane (FastAPI + PostgreSQL) is the source of truth; all managed service containers (Kea, BIND9/PowerDNS, chrony) are deployed and configured by SpatiumDDI.

It can be deployed as individual containers, a full Docker Compose stack, a Kubernetes application, or as a **self-contained OS appliance image**. Supported on `linux/amd64` and `linux/arm64` (all Docker images must be built multi-arch).

It is designed to serve both power users (network engineers) and delegated department admins via a granular, group-based permission system. Every feature available in the UI is also available via REST API.

---

## Document Map

Always read the relevant spec doc(s) before writing code for a feature area.

| Document | What It Covers |
|---|---|
| `CLAUDE.md` | Index, conventions, non-negotiables |
| `docs/ARCHITECTURE.md` | System topology, component relationships, HA design |
| `docs/DATA_MODEL.md` | All database models, relationships, field definitions |
| `docs/API.md` | REST API conventions, pagination, error format, versioning |
| `docs/DEVELOPMENT.md` | Coding standards, test requirements, CI pipeline |
| `docs/OBSERVABILITY.md` | Logging (centralized + UI viewer), metrics, health dashboard, alerting |
| `docs/features/IPAM.md` | IP Space/Block/Subnet/Address management, VLAN/VXLAN, custom fields, import/export, tree UI |
| `docs/features/DHCP.md` | DHCP servers, scopes, pools, static assignments, DDNS, caching |
| `docs/features/DNS.md` | DNS servers, zones, records, views, server groups, blocking lists, DDNS, zone tree |
| `docs/features/NTP.md` | NTP server management and client configuration |
| `docs/features/AUTH.md` | Authentication, LDAP/OIDC/SAML, roles, group-scoped permissions, API tokens |
| `docs/features/SYSTEM_ADMIN.md` | System config, health dashboard, notifications, backup/restore, service control |
| `docs/deployment/APPLIANCE.md` | OS appliance build, base OS selection, licensing |
| `docs/deployment/DNS_AGENT.md` | DNS agent/container architecture — image layout, auto-registration, config sync, K8s shape |
| `docs/deployment/DOCKER.md` | Docker Compose setup, ports, first-time setup, TLS, HA, password reset |
| `docs/deployment/KUBERNETES.md` | Helm chart, operators, HPA, Ingress |
| `docs/deployment/BAREMETAL.md` | Ansible playbooks, systemd services, Patroni |
| `k8s/README.md` | Kubernetes manifest usage, HA PostgreSQL (CloudNativePG), Redis Sentinel |
| `k8s/base/` | Core K8s manifests (namespace, API, worker, frontend, migrate job) |
| `k8s/ha/` | HA add-ons: CloudNativePG cluster, Redis Sentinel, Patroni Compose |
| `docs/drivers/DHCP_DRIVERS.md` | Kea, ISC DHCP driver implementation specs |
| `docs/drivers/DNS_DRIVERS.md` | BIND9, PowerDNS driver specs, incremental update strategy |

---

## Technology Stack (Summary)

| Layer | Technology |
|---|---|
| Backend API | Python 3.12+, FastAPI, SQLAlchemy 2.x (async), Alembic |
| Task Queue | Celery + Redis |
| Frontend | React 18 + TypeScript, Vite, shadcn/ui, Tailwind, React Query |
| Database | PostgreSQL 16 (HA via Patroni or CloudNativePG) |
| Cache / Sessions | Redis 7 |
| Auth | python-jose, authlib (OIDC), python-ldap |
| Logging | structlog → JSON → centralized log store (Loki / Elasticsearch) |
| Metrics | Prometheus + Grafana; InfluxDB v1/v2 push export |
| Containerization | Docker (multi-stage, amd64+arm64), Docker Compose, Kubernetes + Helm |
| Appliance OS | Alpine Linux (containers/appliance), Debian Stable (bare-metal ISO) |
| Logo / Assets | `docs/assets/logo.svg`, `docs/assets/logo-icon.svg` — also copied to `frontend/src/assets/` |

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

## Project Phase Roadmap

| Phase | Focus | Status |
|---|---|---|
| 1 | Core IPAM, local auth, user management, audit log, Docker Compose | **In Progress** |
| 2 | DHCP (Kea + ISC), DNS (PowerDNS + BIND9), DDNS, NTP, zone/subnet tree UI | **In Progress** (DNS core + driver + agent runtime landed; DHCP/DDNS/NTP pending) |
| 3 | DNS views, server groups, blocking lists, VLAN/VXLAN, system admin panel, health dashboard | **In Progress** (DNS views, groups, blocklists, health checks landed) |
| 4 | OS appliance image, Terraform/Ansible providers, SAML, notifications, backup/restore | Not started |
| 5 | Multi-tenancy, IP request workflows, import/export, advanced reporting | Not started |

### Phase 1 — Implemented So Far

**IPAM — data model & API**
- ✅ Full CRUD: IP spaces, blocks, subnets (with network/broadcast auto-creation), IP addresses
- ✅ IP address allocation: next-available (sequential/random) and manual
- ✅ Blocks required for subnets — `block_id` non-nullable
- ✅ Nested blocks — full recursive tree; `parent_block_id` supported at API and UI
- ✅ Block utilization rollup — `_update_block_utilization()` uses a recursive CTE to sum allocated IPs from all descendant subnets; walks ancestor chain after every subnet/address mutation
- ✅ Subnet CIDR strict validation — rejects host-bits-set input with "Did you mean X?" hint
- ✅ Subnet creation `skip_auto_addresses` flag — skips network/broadcast/gateway records (loopbacks, P2P)
- ✅ Soft-delete IP addresses — DELETE marks as `orphan`; purge permanently removes; RefreshCw restores to `allocated`
- ✅ DNS assignment on blocks/subnets — `dns_group_ids`, `dns_zone_id`, `dns_additional_zone_ids`, `dns_inherit_settings` (migration `a1b2c3d4e5f6`); effective DNS resolved by walking ancestor chain

**IPAM — UI**
- ✅ Tree view (Space → Block → Subnet → IP, collapsible sidebar)
- ✅ Full CRUD UI for spaces/blocks/subnets/IPs with edit/delete modals
- ✅ Block click opens `BlockDetailView` (child blocks table + direct subnets table)
- ✅ Space tree-table — hierarchical flat table with indentation, icons, size/utilization columns; blocks violet, subnets blue
- ✅ Block detail tree-table — scoped to that block's subtree
- ✅ Breadcrumbs as colored pills (Space=blue, Block=violet, Subnet=emerald); compresses to `first > … > last two` when > 4 deep; labels show `network (name)`
- ✅ Tree toggles as boxed `[+]`/`[-]`; vertical `border-l` connecting lines
- ✅ Utilization dots on subnet tree rows (green/amber/red)
- ✅ Copy-to-clipboard on IP address column
- ✅ IP allocation form: hostname required, MAC field, status/type selector
- ✅ Delete IP confirmation modal (soft-delete → orphan, separate purge confirmation)
- ✅ `EditBlockModal` — name, description, custom fields editable post-creation
- ✅ Block delete with double confirmation (two-step modal, checkbox required)
- ✅ `BlockDetailView` "New Subnet" button — pre-fills `space_id` + `block_id`
- ✅ Subnet-by-size search — "Find by size" toggle in `CreateSubnetModal`; `GET /ipam/blocks/{id}/available-subnets?prefix_len=N`
- ✅ Per-column subnet address filters (address, hostname, MAC, status, description)
- ✅ Network/broadcast records toggle in `EditSubnetModal` — `manage_auto_addresses` flag adds or permanently removes

**Auth & users**
- ✅ Local auth: login, logout, JWT tokens, forced password change
- ✅ JWT refresh (`POST /api/v1/auth/refresh`) with token rotation; frontend auto-retries on 401
- ✅ User management API (`/api/v1/users/`): create, edit, reset password, delete (superadmin only)
- ✅ Users admin page
- ✅ `/auth/me` UUID serialization fix — `UserResponse.id` is `uuid.UUID` (Pydantic v2 doesn't auto-coerce with `from_attributes=True`)

**Audit log**
- ✅ Audit log table — written on every mutation
- ✅ Audit log viewer UI (`/admin/audit`) — paginated table, action/result badges, filters

**Platform**
- ✅ Settings page (`/settings`) — `PlatformSettings` singleton; branding, allocation strategy, session timeout, utilization thresholds, discovery, release check
- ✅ Session timeout = 0 allowed — `session_timeout_minutes` uses `validate_session_timeout` (≥ 0)
- ✅ Global search (Cmd+K / Ctrl+K) — debounced search across IPs, hostnames, MACs, subnets, blocks, spaces; keyboard navigation; deep-links into IPAM tree
- ✅ Custom field definitions UI (`/admin/custom-fields`) — `CustomFieldDefinition` records per resource type; fields shown on create/edit forms + rendered as columns in `BlockDetailView` and `SpaceTableView`
- ✅ Dashboard — utilization stats, top-subnets table, DNS stat cards (server groups, zones)
- ✅ UI density pass — base font 14 px

**DNS (pre-Wave)**
- ✅ DNS server groups, servers, zones, records — full CRUD UI and API; server group sidebar with expandable zone tree
- ✅ Zone tree — nested sub-zone display (`com → example.com → sub.example.com`); recursive `DnsTreeNode` built by `buildDnsTree`
- ✅ DNS Server Options tab — forwarders, DNSSEC validation, recursion, trust anchors
- ✅ DNS ACLs and Views tabs — full CRUD
- ✅ DNS Settings section — default zone TTL, zone type, DNSSEC validation mode, recursive-by-default; DNS agent key info field
- ✅ Settings page DNS defaults — `dns_default_ttl`, `dns_default_zone_type`, `dns_default_dnssec_validation`, `dns_recursive_by_default` (migration `5d2a8f91c4e6`)

### Wave 1 + Wave 2 additions (2026-04-14/15)

**IPAM**
- ✅ Tree UX — drag-drop reparenting (blocks & subnets, with CIDR containment check + cycle guard), right-click context menus, free-space band on block detail with click-to-create-subnet; `@dnd-kit/core` + `@radix-ui/react-context-menu`
- ✅ Import/export — CSV/JSON/XLSX preview + commit (`POST /ipam/import/preview|commit`, `GET /ipam/export`); service layer at `backend/app/services/ipam_io/`; auto parent-block creation when no containing block exists; one-audit-per-import with per-row detail
- ✅ IP↔DNS/DHCP linkage fields on `IPAddress` (`forward_zone_id`, `reverse_zone_id`, `dns_record_id`, `dhcp_lease_id`, `static_assignment_id`) — migration `c5f2a9b18e34`
- ✅ SubnetDomain junction — multiple DNS domains per subnet (`GET/POST/DELETE /ipam/subnets/{id}/domains`); primary-domain pointer kept in sync
- ✅ Bulk-edit subnets — `POST /ipam/subnets/bulk-edit`; batch audit entries share `batch_id`; UI checkbox column + modal on tree-table
- ✅ Inheritance merge (read-time) — `GET /ipam/subnets/{id}/effective-fields` returns merged tags + custom_fields walking Space → Block chain → Subnet
- ✅ Custom-field search — `is_searchable` definitions flow into `/search`; results carry `matched_field` hint, shown as pill in Cmd+K

**DNS**
- ✅ Driver abstraction — `backend/app/drivers/dns/base.py` `DNSDriver` ABC + neutral dataclasses; `BIND9Driver` with Jinja templates (named.conf, zone files, RPZ), TSIG-signed RFC 2136 via `dnspython`; PowerDNS stub
- ✅ ConfigBundle service — `backend/app/services/dns/config_bundle.py` assembles server options/zones/records/views/ACLs/blocklists/trust-anchors/TSIG; SHA-256 ETag for agent long-poll
- ✅ Serial bumping — RFC 1912 `YYYYMMDDNN` convention wired into record CUD; `POST /dns/servers/{id}/apply-record` records intent + bumps serial
- ✅ Agent runtime — `agent/dns/spatium_dns_agent/` Python package; bootstrap (pre-shared key → rotating JWT), long-poll config sync with ETag, on-disk cache at `/var/lib/spatium-dns-agent/`, loopback nsupdate over TSIG, heartbeat, supervisor (tini); BIND9 driver live, PowerDNS stub
- ✅ Agent backend endpoints — `backend/app/api/v1/dns/agents.py` (`/register`, `/heartbeat`, `/config` long-poll, `/record-ops`, `/ops/{id}/ack`); `DNSRecordOp` model + `DNSServer` agent fields (migration `b7e3a1f4c8d2`); stale-sweep Celery task
- ✅ Container images — `agent/dns/images/{bind9,powerdns}/Dockerfile` Alpine 3.20 multi-arch; GH Actions workflow `.github/workflows/build-dns-images.yml` → `ghcr.io/spatiumddi/dns-{bind9,powerdns}`
- ✅ Kubernetes — `k8s/dns/{bind9,powerdns}-statefulset.yaml` (one STS per server), `service-dns.yaml`; Helm chart scaffolding at `charts/spatium-dns/`
- ✅ Docker Compose `dns` profile with `dns-bind9-dev` service; `.env.example` adds `DNS_AGENT_KEY`, `DNS_AGENT_TOKEN_TTL_HOURS`, `DNS_AGENT_LONGPOLL_TIMEOUT`, `DNS_REQUIRE_AGENT_APPROVAL`
- ✅ Blocking lists (RPZ) — `DNSBlockList`/`Entry`/`Exception` models with group + view assignment junctions (migration `c3f1e7b92a5d`); bulk-add with dedupe; feed refresh Celery task (hosts/domains/adblock parsers); "Blocklists" tab on DNS group detail; backend-neutral `EffectiveBlocklist` service consumed by BIND9 driver for RPZ rendering
- ✅ Zone import/export (RFC 1035) — dnspython parser/differ/writer at `backend/app/services/dns_io/`; endpoints for preview/commit/export plus multi-zone zip export; `ImportZoneModal` UI with color-coded diff
- ✅ View-level + zone-level query-control overrides (`allow_query`, `allow_query_cache`, `recursion`) exposed in API (migration `c3f7e5a9b2d1`); record `view_id` now surfaced in create/update for view-scoped records
- ✅ Reverse-zone auto-create on subnet — `backend/app/services/dns/reverse_zone.py` computes `in-addr.arpa`/`ip6.arpa` and creates the zone when subnet has DNS assignment; `skip_reverse_zone` opt-out
- ✅ Server health checks — `check_dns_server_health` Celery task (agent heartbeat staleness → SOA probe fallback); `check_all_dns_servers_health` fan-out scheduled every 60s via beat; status dots + health widget on DNS group detail

**Design doc:** `docs/deployment/DNS_AGENT.md` — agent topology, auto-reg protocol, config sync model, K8s shape, deliverables.

### Phase 1 — Remaining

- ⬜ LDAP / OIDC authentication
- ⬜ Group-based RBAC enforcement on API routes
- ⬜ Full IPv6 support in IPAM (address storage, CIDR validation, UI rendering)
- ⬜ Mobile-responsive UI
- ⬜ Bulk-edit UI for `tags` + `custom_fields` (API supports it; only scalar fields in modal today)
- ⬜ Wire inherited-field placeholders into `EditSubnetModal` / `EditBlockModal` (API `/effective-fields` is ready)

### Phase 2/3 — Remaining

- ⬜ DHCP (Kea + ISC) drivers, models, UI
- ⬜ DDNS pipeline (needs DHCP first) — subnet `ddns_enabled`/`ddns_hostname_policy`/`ddns_domain_override`/`ddns_ttl`; DHCP-lease → DNS A/PTR Celery task
- ⬜ PowerDNS driver (BIND9 is live; PowerDNS stub raises `NotImplementedError`)
- ⬜ NTP server/client management
- ⬜ Per-server zone serial reporting (currently all servers in a group share `DNSZone.serial`; once agents report back, surface per-server drift)
- ⬜ Trivy-clean + kind-AXFR acceptance tests for the agent images (stubs marked `@pytest.mark.e2e` in `agent/dns/tests/`)

### Future Phases — Tracked Items

- ⬜ Windows DNS / DHCP server integration — read-only visibility and basic management of existing Windows Server DNS/DHCP via WinRM or REST (see `docs/features/DNS.md`, `docs/features/DHCP.md`)
- ⬜ IP discovery — ping sweep + ARP scan Celery task; flags `discovered` status; reconciliation report (see `docs/features/IPAM.md §8`)
- ⬜ OUI/vendor lookup — IEEE OUI database loaded into `oui_vendor` table; shown next to MAC addresses (see `docs/features/IPAM.md §12`)
- ⬜ SNMP polling / network device management — ARP table polling for IP discovery (see `docs/features/IPAM.md §13`)

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
# Initial setup
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD and SECRET_KEY (openssl rand -hex 32)
docker compose build
docker compose run --rm migrate      # run Alembic migrations
docker compose up -d                 # start all services

# Default admin: username=admin, password=admin (force_password_change=True)

# Generate a new migration after model changes
make migration MSG="describe the change"

# Linting (Python: ruff + black + mypy; TypeScript: eslint + prettier)
make lint

# Run all tests
make test

# Run a single test
make test-one T=tests/test_health.py::test_liveness

# Reset admin password (if locked out)
docker compose exec api python - <<'EOF'
from app.core.security import hash_password
import asyncio
from sqlalchemy import update
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

For Python backend work:
- Formatter/linter: `ruff`, `black`, `mypy` (all enforced in CI)
- Migrations: `make migration MSG="..."` generates; `make migrate` applies

For frontend work:
- Linter/formatter: `eslint`, `prettier` (all enforced in CI)
- Dev server: `vite` (inside the frontend container or locally with Node 20+)
- Theme: dark/light/system toggle; CSS vars in `src/index.css`; toggle in Header component

---
*See individual docs for full specifications.*
