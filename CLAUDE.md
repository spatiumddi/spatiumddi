# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **GitHub Org:** https://github.com/spatiumddi  
> **Docs:** https://spatiumddi.github.io  
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
| `docs/ARCHITECTURE.md` | System topology, component relationships, HA design |
| `docs/DATA_MODEL.md` | All database models, relationships, field definitions |
| `docs/API.md` | REST API conventions, pagination, error format, versioning |
| `docs/DEVELOPMENT.md` | Coding standards, test requirements, CI pipeline |
| `docs/OBSERVABILITY.md` | Logging (centralized + UI viewer), metrics, health dashboard, alerting |
| `docs/features/IPAM.md` | IP Space/Block/Subnet/Address management, VLAN/VXLAN, custom fields, import/export, tree UI |
| `docs/features/DHCP.md` | DHCP servers, scopes, pools, static assignments, DDNS, caching |
| `docs/features/DNS.md` | DNS servers, zones, records, views, server groups, blocking lists, DDNS, zone tree |
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
| `docs/drivers/DNS_DRIVERS.md` | BIND9 driver spec, incremental update strategy |

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
charts/spatium-dns/     Helm chart
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
| 1 | Core IPAM, local auth, user management, audit log, Docker Compose | **In Progress** |
| 2 | DHCP (Kea + ISC), DNS (BIND9), DDNS, zone/subnet tree UI | **In Progress** (DNS core landed; DHCP Kea driver + agent + UI landed; ISC DHCP + DDNS pending) |
| 3 | DNS views, server groups, blocking lists, VLAN/VXLAN, system admin panel, health dashboard | **In Progress** (DNS views, groups, blocklists, health checks landed) |
| 4 | OS appliance image, Terraform/Ansible providers, SAML, notifications, backup/restore | Not started |
| 5 | Multi-tenancy, IP request workflows, import/export, advanced reporting | Not started |

### Current state

SpatiumDDI cut its alpha release `2026.04.16-1` on 2026-04-16 with IPAM, DNS (BIND9), and DHCP (Kea) all shipping. For the full list of what has landed — Phase 1 core, Waves 1–5, DHCP Wave 1 — see `CHANGELOG.md`. The forward-looking work is below.

### Phase 1 — Remaining

- ⬜ LDAP / OIDC authentication
- ⬜ Group-based RBAC enforcement on API routes
- ⬜ Full IPv6 support in IPAM (address storage, CIDR validation, UI rendering)
- ⬜ Mobile-responsive UI
- ⬜ Bulk-edit UI for `tags` + `custom_fields` (API supports it; only scalar fields in modal today)
- ⬜ Wire inherited-field placeholders into `EditSubnetModal` / `EditBlockModal` (API `/effective-fields` is ready)

### Phase 2/3 — Remaining

- ⬜ ISC DHCP driver (Kea driver + agent + UI landed in DHCP Wave 1)
- ⬜ DDNS pipeline (needs DHCP lease events flowing) — subnet `ddns_enabled`/`ddns_hostname_policy`/`ddns_domain_override`/`ddns_ttl`; DHCP-lease → DNS A/PTR Celery task
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
