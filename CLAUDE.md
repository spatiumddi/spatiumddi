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
| 2 | DHCP (Kea + ISC), DNS (PowerDNS + BIND9), DDNS, NTP, zone/subnet tree UI | Not started |
| 3 | DNS views, server groups, blocking lists, VLAN/VXLAN, system admin panel, health dashboard | Not started |
| 4 | OS appliance image, Terraform/Ansible providers, SAML, notifications, backup/restore | Not started |
| 5 | Multi-tenancy, IP request workflows, import/export, advanced reporting | Not started |

### Phase 1 — Implemented So Far

- ✅ Full IPAM CRUD: IP spaces, blocks, subnets (with network/broadcast auto-creation), IP addresses
- ✅ IP address allocation: next-available (sequential/random) and manual
- ✅ Local auth: login, logout, JWT tokens, forced password change
- ✅ User management API (`/api/v1/users/`): create, edit, reset password, delete (superadmin only)
- ✅ Audit log table (written on every mutation)
- ✅ IPAM tree view UI (Space → Block → Subnet → IP address, collapsible sidebar)
- ✅ Full CRUD UI for spaces, blocks, subnets, IP addresses with edit/delete modals
- ✅ Dashboard with utilization stats and top-subnets table
- ✅ Users admin page
- ✅ Audit log viewer UI (`/admin/audit`) — paginated table, action/result badges, filters
- ✅ Utilization dots on subnet tree rows (green/amber/red)
- ✅ Copy-to-clipboard on IP address column
- ✅ Subnet CIDR strict validation — rejects host-bits-set input with "Did you mean X?" hint
- ✅ Subnet creation `skip_auto_addresses` flag — skips network/broadcast/gateway records (loopbacks, P2P)
- ✅ IP allocation form: hostname required, MAC address field, status/type selector for all modes
- ✅ Soft-delete IP addresses — DELETE marks as `orphan`; purge button permanently removes
- ✅ JWT token refresh endpoint (`POST /api/v1/auth/refresh`) with token rotation; frontend auto-retries on 401
- ✅ UI density pass — base font 14 px
- ✅ Delete IP address confirmation modal (soft-delete → orphan, with separate purge confirmation)
- ✅ Restore orphaned IPs — RefreshCw button sets status back to `allocated`
- ✅ Blocks required for subnets — `block_id` is now non-nullable; subnets must belong to a block
- ✅ Nested blocks (blocks within blocks) — full recursive tree; `parent_block_id` supported at API and UI
- ✅ Block click navigation — clicking a block in the tree opens `BlockDetailView` (child blocks table + direct subnets table)
- ✅ Space tree-table view — clicking an IP Space shows all blocks and subnets in a hierarchical flat table with indentation, icons, size/utilization columns; blocks are violet, subnets are blue
- ✅ Breadcrumbs as colored pills — Space = blue, Block = violet, Subnet = emerald; all levels clickable; compresses to `first > … > last two` when > 4 levels deep
- ✅ Tree toggles as boxed `[+]`/`[-]` buttons — replaces chevron arrows; vertical `border-l` connecting lines show tree structure
- ✅ Block detail tree-table — clicking a block shows the same hierarchical tree-table as the space view, scoped to that block's subtree; uses the same columns and rendering
- ✅ Breadcrumb pill labels show `network (name)` format — e.g. `10.0.0.0/8 (rfc1918)` when a name is set; subnet pill also includes name
- ✅ Block utilization rollup — `_update_block_utilization()` in `ipam/router.py` uses a recursive CTE to sum allocated IPs from all descendant subnets; walks ancestor chain after every subnet/address mutation
- ✅ Settings page (`/settings`) — `PlatformSettings` singleton; branding, allocation strategy, session timeout, utilization thresholds, discovery, release check
- ✅ Global search (Cmd+K / Ctrl+K) — modal with debounced search across IPs, hostnames, MACs, subnets, blocks, spaces; keyboard navigation; deep-links into IPAM tree
- ✅ Custom field definitions UI (`/admin/custom-fields`) — admin defines `CustomFieldDefinition` records per resource type; custom fields shown on create/edit forms for blocks, subnets, IP addresses; custom field columns rendered in BlockDetailView and SpaceTableView tables
- ✅ EditBlockModal — name, description, custom fields editable post-creation
- ✅ Per-column subnet address filters — address, hostname, MAC, status, description columns each have independent filter inputs; replaces old single global filter bar
- ✅ Network/broadcast records toggle in EditSubnetModal — detects current state from loaded addresses; sends `manage_auto_addresses` flag to add or permanently remove network/broadcast records post-creation
- ✅ `/auth/me` UUID serialization fix — `UserResponse.id` changed from `str` to `uuid.UUID` (Pydantic v2 `from_attributes=True` does not auto-coerce)

### Phase 1 — Remaining

- ⬜ LDAP / OIDC authentication
- ⬜ Group-based RBAC enforcement on API routes
- ⬜ Full IPv6 support in IPAM (address storage, CIDR validation, UI rendering)
- ⬜ Mobile-responsive UI

### Future Phases — Tracked Items

- ⬜ Windows DNS / DHCP server integration — read-only visibility and basic management of existing Windows Server DNS/DHCP via WinRM or REST (see `docs/features/DNS.md`, `docs/features/DHCP.md`)
- ⬜ IP discovery — ping sweep + ARP scan Celery task; flags `discovered` status; reconciliation report (see `docs/features/IPAM.md §8`)
- ⬜ Import/export — CSV/JSON/Excel subnet import with dry-run preview; export a block/space (see `docs/features/IPAM.md §7`)
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
