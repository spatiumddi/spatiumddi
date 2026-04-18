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
| `docs/features/IPAM.md` | IP Space/Block/Subnet/Address management, VLAN/VXLAN, custom fields, import/export, tree UI |
| `docs/features/DHCP.md` | DHCP servers, scopes, pools, static assignments, DDNS, caching, Windows DHCP (Path A) |
| `docs/features/DNS.md` | DNS servers, zones, records, views, server groups, blocking lists, DDNS, zone tree, Windows DNS (Path A + B), sync-with-servers reconciliation |
| `docs/features/AUTH.md` | Authentication, LDAP/OIDC/SAML, roles, group-scoped permissions, API tokens |
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
| `docs/drivers/DHCP_DRIVERS.md` | Kea + Windows DHCP driver internals; ISC DHCP planned |
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
| 1 | Core IPAM, local auth, user management, audit log, Docker Compose | **Mostly done** — LDAP/OIDC/SAML + RADIUS/TACACS+ auth, group-based RBAC enforcement, bulk-edit tags/CF, inherited-field placeholders, and mobile-responsive UI all landed; IPv6 partial |
| 2 | DHCP (Kea + ISC), DNS (BIND9), DDNS, zone/subnet tree UI | **In Progress** (DNS core landed; DHCP Kea driver + agent + UI landed; ISC DHCP + DDNS pending) |
| 3 | DNS views, server groups, blocking lists, VLAN/VXLAN, system admin panel, health dashboard | **In Progress** (DNS views, groups, blocklists, health checks landed) |
| 4 | OS appliance image, Terraform/Ansible providers, SAML, notifications, backup/restore | **In Progress** (SAML SP landed in Wave A.4; appliance, providers, backup still pending) |
| 5 | Multi-tenancy, IP request workflows, import/export, advanced reporting | Not started |

### Current state

SpatiumDDI cut its alpha release `2026.04.16-1` on 2026-04-16 with IPAM, DNS (BIND9), and DHCP (Kea) all shipping. For the full list of what has landed — Phase 1 core, Waves 1–5, DHCP Wave 1 — see `CHANGELOG.md`. The forward-looking work is below.

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
- IPv6 partial — `DHCPScope.address_family` column + Kea driver `Dhcp6` branch; subnet create skips the v6 broadcast row; `_sync_dns_record` emits AAAA + PTR in `ip6.arpa`; `/next-address` returns 409 on v6 (EUI-64/hash allocation is a future enhancement). Dhcp6 option-name translation is marked TODO in `backend/app/drivers/dhcp/kea.py`.

### IPAM polish (shipped alongside the waves)

- **Block overlap validation** — `_assert_no_block_overlap` rejects same-level duplicates and CIDR overlaps in `create_block` + the reparent path in `update_block`.
- **Scheduled IPAM ↔ DNS auto-sync** — opt-in Celery beat task `app.tasks.ipam_dns_sync.auto_sync_ipam_dns`. Beat fires every 60 s; the task itself gates on `PlatformSettings.dns_auto_sync_enabled` + `dns_auto_sync_interval_minutes`, so cadence changes in the UI take effect without restarting beat. Optionally deletes stale auto-generated records.
- **Shared `ZoneOptions` dropdown** (`frontend/src/pages/ipam/IPAMPage.tsx`) — renders primary zone first, `<optgroup label="Additional zones">` below; applied in Create / Edit / Bulk-edit IP modals. Zone picker is restricted to the subnet's explicit primary + additional zones when any are pinned.
- **Bulk-edit DNS zone** — new `dns_zone_id` field on `IPAddressBulkChanges`; each selected IP routes through `_sync_dns_record` for move / create / delete.

### Phase 1 — Remaining

- ⬜ Finish full IPv6 — Dhcp6 option-name translation in `backend/app/drivers/dhcp/kea.py`, EUI-64 / hash-based /128 allocation for `/next-address`, v6-specific test coverage

### Phase 2/3 — Remaining

- ⬜ ISC DHCP driver (Kea driver + agent + UI landed in DHCP Wave 1)
- ⬜ DDNS pipeline (needs DHCP lease events flowing) — subnet `ddns_enabled`/`ddns_hostname_policy`/`ddns_domain_override`/`ddns_ttl`; DHCP-lease → DNS A/PTR Celery task
- ⬜ Per-server zone serial reporting (currently all servers in a group share `DNSZone.serial`; once agents report back, surface per-server drift)
- ⬜ Trivy-clean + kind-AXFR acceptance tests for the agent images (stubs marked `@pytest.mark.e2e` in `agent/dns/tests/`)

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
  on the server detail header for agentless drivers.
- ⬜ **Windows DHCP — Path B (WinRM + PowerShell, full CRUD)** — scope
  / reservation / client-class / option CRUD via `Add-DhcpServerv4Scope`,
  `Add-DhcpServerv4Reservation`, etc. Layered on top of Path A in the
  same driver class. Service account must be in `DHCP Administrators`
  rather than `DHCP Users`. Much bigger scope than DNS Path B since
  there's no wire-level admin protocol; every scope field becomes a
  cmdlet call.
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
