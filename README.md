<p align="center">
  <img src="docs/assets/logo.svg" alt="SpatiumDDI Logo" />
</p>

<h1 align="center">SpatiumDDI</h1>

<p align="center">
  <strong>Self-hosted DNS, DHCP, and IPAM — one control plane, real servers underneath.</strong><br/>
  A modern, open-source alternative to commercial DDI platforms.
</p>

<p align="center">
  <a href="https://github.com/spatiumddi/spatiumddi/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/spatiumddi/spatiumddi/ci.yml?branch=main&label=CI" alt="CI"/></a>
  <a href="https://github.com/spatiumddi/spatiumddi/security/code-scanning"><img src="https://img.shields.io/badge/security-CodeQL-1f6feb" alt="CodeQL"/></a>
  <a href="https://github.com/spatiumddi/spatiumddi/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License"/></a>
  <a href="https://spatiumddi.github.io"><img src="https://img.shields.io/badge/docs-github.io-informational" alt="Docs"/></a>
  <img src="https://img.shields.io/badge/status-alpha-orange" alt="Status"/>
</p>

<p align="center">
  <a href="https://github.com/spatiumddi/spatiumddi/releases/latest"><img src="https://img.shields.io/github/v/release/spatiumddi/spatiumddi?label=release" alt="Latest release"/></a>
  <a href="https://github.com/spatiumddi/spatiumddi/commits/main"><img src="https://img.shields.io/github/last-commit/spatiumddi/spatiumddi" alt="Last commit"/></a>
  <img src="https://img.shields.io/maintenance/yes/2026" alt="Maintained"/>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-green" alt="Python"/>
  <img src="https://img.shields.io/badge/react-18+-61DAFB" alt="React"/>
  <a href="https://github.com/psf/black"><img src="https://img.shields.io/badge/code%20style-black-000000" alt="Code style: black"/></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/lint-ruff-FCC21B" alt="Lint: ruff"/></a>
  <a href="https://mypy-lang.org/"><img src="https://img.shields.io/badge/type%20checked-mypy-blue" alt="Type checked: mypy"/></a>
</p>

<p align="center">
  <a href="https://github.com/spatiumddi/spatiumddi/stargazers"><img src="https://img.shields.io/github/stars/spatiumddi/spatiumddi?style=social" alt="Stars"/></a>
  <a href="https://github.com/spatiumddi/spatiumddi/discussions"><img src="https://img.shields.io/github/discussions/spatiumddi/spatiumddi" alt="Discussions"/></a>
  <a href="https://github.com/spatiumddi/spatiumddi/graphs/contributors"><img src="https://img.shields.io/github/contributors/spatiumddi/spatiumddi" alt="Contributors"/></a>
  <a href="https://github.com/spatiumddi/spatiumddi/issues"><img src="https://img.shields.io/github/issues/spatiumddi/spatiumddi" alt="Issues"/></a>
</p>

---

> ⚠️ **Alpha software.** SpatiumDDI is under active development and has not yet been battle-tested in production. Expect rough edges, breaking schema changes between releases (Phase 1), and features listed in the roadmap that are still in flight. Run it in a lab, file bugs, and please don't put it in front of DHCP clients you care about until Phase 2 is complete. Early adopter feedback is very welcome — open an issue or start a discussion on GitHub.

---

## Why SpatiumDDI

**It runs DNS and DHCP — not just configures them.** A modern alternative to Infoblox and EfficientIP: most open-source IPAM tools are pretty dashboards over someone else's `/etc/bind/named.conf`. SpatiumDDI bundles BIND9 and Kea as first-class service containers; the control plane owns their config, they auto-register, and they keep serving if the control plane is down.

**One platform, three surfaces.** IPAM tree, DNS zones, DHCP scopes — one UI, one REST API, one source of truth. Hostname changes in IPAM propagate to DNS; reservations propagate to DHCP. No more three-tab reconciliation.

**Bring your own servers — or ours.** Use the bundled Kea and BIND9, or point SpatiumDDI at your existing Windows DCs and DHCP servers via WinRM. Agentless in both directions — nothing installed on the Windows side.

**Built for delegation.** Group-based RBAC with LDAP, OIDC, SAML, RADIUS, and TACACS+ (with backup-server failover). Hand a subnet or a zone to a department without handing over root.

**API-first.** Every UI action is a REST call. Terraform, Ansible, and ad-hoc scripts all speak the same surface. If you can click it, you can automate it.

## What's in the box

> **One control plane for IPAM, DNS, DHCP — plus the discovery and integrations to keep it honest.** No vendor lock-in, no per-IP licence, no agents on Windows.

### 🏠 Core DDI

| | Feature | Highlights |
|---|---|---|
| 🗂 | **Hierarchical IPAM** | spaces · blocks · subnets · IPv4 + full IPv6 (EUI-64 / random / sequential) · per-IP roles · MAC history · reservation TTL |
| ✂️ | **Subnet operations** | Split · Merge · Find-free · subnet planner (multi-level CIDR design + transactional apply) — preview-then-commit with typed-CIDR confirm |
| 🧮 | **Planning tools** | CIDR calculator · address planner (pack /N requests into free space) · aggregation suggestion · free-space treemap |
| 🌐 | **DNS** | BIND9 container, auto-registering · RFC 2136 dynamic updates · per-server zone-serial drift · TSIG keys · zone delegation wizard · zone templates · RPZ blocklists with curated catalog · BIND9 catalog zones (RFC 9432) |
| ⚖️ | **GSLB-lite** | health-checked DNS pools — tcp / http / https / icmp / none probes flip A/AAAA records in/out of the rendered rrset; manual enable per member |
| 🔄 | **DHCP** | Kea container · group-centric Kea HA (load-balanced or hot-standby) with self-healing peer drift · option templates · 95-entry option-code library |
| 🪟 | **Windows DNS + DHCP** | agentless — RFC 2136 + WinRM, no software on the DC |
| 🔁 | **NAT cross-reference** | 1:1 / PAT / hide-NAT tracked in IPAM with FK links to live IP rows |
| 📜 | **DHCP lease history** | forensic trail of every expiry, MAC reassignment, absence-delete |
| 🗑 | **Soft-delete trash** | 30-day Trash with cascade restore for spaces, blocks, subnets, zones, scopes |

### 🔍 Discovery & visibility

| | Feature | Highlights |
|---|---|---|
| 📡 | **SNMP discovery** | v1 / v2c / v3 polling of routers + switches → ARP / FDB / interfaces / LLDP neighbours feed back into IPAM |
| 🎯 | **Nmap scanner** | per-IP "Scan with Nmap" + `/tools/nmap` + live SSE output streaming |
| 🏷 | **OUI vendor lookup** | MAC → vendor names in IP tables, DHCP leases, search filters |
| 🎨 | **Dashboards** | utilization heatmap · DNS query rate · DHCP traffic · platform health card |
| 📊 | **Platform Insights** | native Postgres diagnostics + per-container CPU / mem / IO. No extra agents |

### 🔌 Integrations (read-only mirrors)

| | Source | What's mirrored |
|---|---|---|
| ☸️ | **Kubernetes** | cluster CIDRs · nodes · LoadBalancer VIPs · Ingress → DNS |
| 🐳 | **Docker** | networks · optional container IPs |
| 🖥 | **Proxmox VE** | bridges · SDN VNets + subnets · VM / LXC NICs (qemu-guest-agent) |
| 🔐 | **Tailscale** | tailnet devices + synthetic `*.ts.net` zone |

### 🛡 Identity & ops

| | Feature | Highlights |
|---|---|---|
| 🔒 | **RBAC + external auth** | LDAP · OIDC · SAML · RADIUS · TACACS+ with backup-server failover · API tokens with auto-expiry |
| 🔔 | **Alerts + forwarding** | rule-based alerts · multi-target syslog (RFC 5424 / CEF / LEEF / RFC 3164) + HTTP webhooks |
| 🔐 | **ACME DNS-01** | `acme-dns`-compatible — certbot / lego / acme.sh issue public certs (wildcards included) |
| 📋 | **Audit log** | every mutation logged, append-only, filterable in the UI |

### 🚀 Deployment

| | Path | |
|---|---|---|
| 🐳 | **Docker Compose** | `docker compose up -d` |
| ☸️ | **Kubernetes** | Helm umbrella chart, OCI-published |
| 🖥 | **Bare metal / OS appliance** | bare metal today · self-contained appliance image (roadmap) |

---

## Full feature detail

The tables above are the elevator pitch. The bullets here are the same surface with the operational detail — what's stored, how it behaves, where the seams are.

### Core DDI

- 🗂 **Hierarchical IP management** — spaces, blocks, subnets, addresses in a visual tree.
  - IPv4 + full IPv6 auto-allocation (EUI-64 / random /128 / sequential)
  - Per-IP role: host / loopback / anycast / vip / vrrp / secondary / gateway
  - Reservation TTL with auto-expiry
  - Per-IP MAC observation history

- ✂️ **Subnet operations** — preview-then-commit with typed-CIDR confirm.
  - Split, Merge, Find-Free workflows
  - Surfaced on the subnet detail header *and* via bulk-action toolbars on the block + space tables
  - Bulk-select 1 row to split, 2+ to merge
  - Block-detail tables reach parity with the space view — child blocks are bulk-selectable too, so leaf-empty blocks cascade-delete alongside subnets in one shot

- 🧮 **Subnet planner + planning tools** — design CIDR hierarchies before applying them.
  - `/ipam/plans` — draggable multi-level CIDR design surface (root + nested children, arbitrary depth)
  - Saved as `SubnetPlan` rows, validated live as the operator edits, applied in a single transaction
  - Per-node DNS-group / DHCP-group / gateway bindings (null = inherit, explicit = set + flip inherit off)
  - CIDR calculator at `/tools/cidr` — pure client-side IPv4 + IPv6 breakdown
  - Address planner — packs `{count, prefix_len}` requests into free space using largest-prefix-first ordering
  - Aggregation suggestion banner — surfaces clean-merge opportunities (10.0.0.0/24 + 10.0.1.0/24 → /23)
  - Free-space treemap toggleable from the Allocation map header — surfaces fragmentation hidden in the 1-D band

- 🗑 **Soft-delete + Trash recovery** — 30-day Trash with cascade restore.
  - Covers IP spaces, blocks, subnets, DNS zones / records, DHCP scopes
  - Cascade-stamped batch IDs — one Restore click brings back every dependent row atomically
  - Conflict detection on restore guards against clashing with live rows
  - Operator-configurable purge sweep (`soft_delete_purge_days`, default 30; `0` = keep forever)

- 🔁 **NAT mapping cross-reference** — operator-curated rules with FK links to IPAM.
  - 1:1 / PAT / hide-NAT supported
  - Per-IP modal lists every mapping that touches the address
  - Per-subnet "NAT" tab uses Postgres CIDR containment to find every mapping crossing into the subnet's range

- 📜 **DHCP lease history** — forensic trail of every lease lifecycle event.
  - Captures expiry, MAC reassignment, absence-delete
  - Operator retention window (default 90 days), daily prune task

- 🌐 **Built-in DNS server** — BIND9 container, auto-registers, syncs via RFC 2136.
  - Per-server zone-serial drift reporting
  - **Zone authoring**:
    - Delegation wizard — auto-stamps NS + glue in the parent zone
    - Four starter templates: Email (MX / SPF / DMARC), Active Directory (LDAP / Kerberos / GC SRV), Web (apex + www), k8s external-dns target
    - Conditional forwarders as a first-class zone type
  - **TSIG keys** — full CRUD with Fernet-encrypted secrets
    - One-shot "copy this secret now" reveal modal
    - Rows distribute through the existing `tsig_keys` ConfigBundle block
  - **RPZ blocklists** — 14-source curated catalog with one-click subscribe + immediate refresh
    - Sources: AdGuard, StevenBlack, OISD, Hagezi, 1Hosts, Phishing Army, URLhaus, EasyPrivacy, …
  - **Catalog zones (RFC 9432)** — producer / consumer roles auto-derived from the group's primary
    - RFC-compliant SHA-1 hashing of zone names
  - **Operator tools**:
    - Multi-resolver propagation check (Cloudflare / Google / Quad9 / OpenDNS in parallel) on every record row
    - Clickable analytics strip on the Logs page (top qnames + top clients + qtype distribution)
    - Per-server detail modal — Overview / Zones / Sync / Events / Logs / Stats / Config tabs + a live `rndc status` panel — answers "is this server actually running the config we sent?" without SSHing in

- ⚖️ **DNS pools (GSLB-lite)** — health-checked DNS round-robin.
  - One DNS name returns one record per healthy + enabled member; members flip in / out of the rrset as state changes
  - **Health checks**:
    - `tcp` — open-connection probe
    - `http` / `https` — status-code match with optional TLS verification
    - `icmp` — echo-request via `iputils-ping`
    - `none` — always healthy (for pools that just want manual-enable + multi-RR semantics)
  - Per-pool interval (default 30 s), timeout, and consecutive-failure / consecutive-success thresholds so single flapping checks don't churn records
  - **Operator UX**:
    - Top-level `/dns/pools` page — every pool across every zone with live health summary
    - Per-zone Pools tab on the zone detail page
    - Manual enable / disable per member, like a load-balancer pool
  - **Driver-agnostic** — members render as regular A/AAAA records via the normal record pipeline, so BIND9 + Windows DNS serve them unchanged
  - **Tradeoff (UI-warned)** — TTL races. DNS is cached client-side; a member dropping out doesn't take effect until TTL expires. This is not a real L4/L7 load balancer. Default TTL is 30 s with an inline pointer to the LB-mapping roadmap item.

- 🔄 **DHCP server management** — Kea container + agent with lease tracking.
  - Group-centric HA (hot-standby + load-balancing) with live state reporting
  - Self-healing peer-IP drift
  - Supervised daemons for crash-loop-safe restarts
  - **Scope authoring**:
    - 95-entry RFC 2132 + IANA option-code library with autocomplete on the custom-options row (search by code or name, description shown inline)
    - Named option templates (group-scoped, e.g. "VoIP phones", "PXE BIOS clients") — apply to a scope in one click; apply is a stamp not a binding, so later template edits don't propagate

- 🪟 **Windows Server DNS + DHCP** — agentless management of existing Windows DCs.
  - RFC 2136 + WinRM for DNS
  - Near-real-time WinRM lease-mirroring for DHCP
  - No software installed on the Windows side

### Discovery & visibility

- 📡 **SNMP discovery** — v1 / v2c / v3 polling via standard MIBs.
  - MIBs walked: IF-MIB, IP-MIB, Q-BRIDGE-MIB, LLDP-MIB
  - Surfaces interfaces, ARP, FDB, LLDP neighbours
  - Per-IP switch-port + VLAN visibility in IPAM
  - Neighbours tab on each device

- 🎯 **Nmap scanner** — on-demand scans from the browser.
  - Per-IP "Scan with Nmap" launcher
  - Presets: quick, service-version, OS, default-scripts, UDP top-100, aggressive
  - Live SSE output streams while the scan runs
  - Structured XML parsed into a results panel
  - Standalone `/tools/nmap` page for ad-hoc targets

- 🎨 **Dashboard-at-a-glance** — sub-tabs for Overview / IPAM / DNS / DHCP.
  - Platform health card (API / Postgres / Redis / workers / beat)
  - Live DNS query rate + DHCP traffic charts — self-contained, no Prometheus needed
    - Sources: BIND9 statistics-channels + Kea `statistic-get-all`
  - Subnet utilization heatmap
  - Live activity feed

- 📊 **Platform Insights admin page** — native diagnostics, no extra agents.
  - Postgres: DB size, cache hit ratio, WAL position, slow queries via `pg_stat_statements`, table sizes, idle-in-transaction watch
  - Containers: per-container CPU / memory / network / IO from the local Docker socket

- 🏷 **IEEE OUI vendor lookup** — opt-in MAC vendor display.
  - Surfaces in IP tables and DHCP leases
  - Filter-by-vendor support

### Integrations

- 🧩 **Read-only integrations** — auto-mirror cluster / hypervisor / overlay state into IPAM.
  - **Kubernetes** — CIDRs, nodes, LoadBalancer VIPs, Ingress → DNS
  - **Docker** — networks, optional container IPs
  - **Proxmox VE** — bridges, SDN VNets + subnets, VMs, LXC guests (runtime IPs via QEMU guest-agent); one row per cluster
  - **Tailscale** — device mirror + synthetic `*.ts.net` DNS zone
  - One-click setup guides per integration
  - Opt-in VNet-CIDR inference from guest NICs (for SDN deployments where PVE is L2-only)
  - Per-endpoint "Discovery" modal — which VMs aren't reporting IPs + copy-ready fix hints
  - Settings toggle gates each; per-target sync interval + on-demand Sync Now
  - Supernet auto-creation for RFC 1918 / CGNAT ranges keeps the tree tidy

### Identity & ops

- 🔒 **Group-based RBAC + external identity** — multi-protocol auth.
  - LDAP, OIDC, SAML, RADIUS, TACACS+
  - Backup-server failover for every protocol
  - Delegate IP ranges and zones by role
  - API tokens with auto-expiry

- 🔔 **Alerts + audit forwarding** — multi-target delivery with pluggable wire formats.
  - Rule-based alerts framework (subnet utilization, server unreachable)
  - Multi-target syslog (UDP / TCP / TLS) + HTTP webhook
  - Wire formats: RFC 5424 JSON, CEF, LEEF, RFC 3164, JSON lines
  - Per-target filters

- 🔐 **ACME DNS-01 provider** — `acme-dns`-compatible HTTP surface.
  - certbot / lego / acme.sh issue public certs (wildcards included)
  - For any FQDN delegated to a SpatiumDDI-managed zone

- 📋 **Full audit trail** — every mutation logged, append-only.
  - Viewable in the UI with per-column filters

### Deployment

- 🚀 **Flexible deployment** — same control plane, multiple paths.
  - Docker Compose
  - Kubernetes — Helm umbrella chart, OCI-published
  - Bare metal
  - OS appliance (roadmap)

---

## Screenshots

_Click any image to open the full-size version._

| [Dashboard](docs/assets/screenshots/dashboard.png) | [IPAM](docs/assets/screenshots/ipam.png) |
| :---: | :---: |
| [<img src="docs/assets/screenshots/dashboard.png" alt="Dashboard" width="450"/>](docs/assets/screenshots/dashboard.png) | [<img src="docs/assets/screenshots/ipam.png" alt="IPAM" width="450"/>](docs/assets/screenshots/ipam.png) |
| Utilisation, VLAN, DNS &amp; DHCP status at a glance | Hierarchical space / block / subnet tree with per-IP DNS sync |

| [DNS](docs/assets/screenshots/dns.png) | [DHCP](docs/assets/screenshots/dhcp.png) | [VLANs](docs/assets/screenshots/vlans.png) |
| :---: | :---: | :---: |
| [<img src="docs/assets/screenshots/dns.png" alt="DNS" width="300"/>](docs/assets/screenshots/dns.png) | [<img src="docs/assets/screenshots/dhcp.png" alt="DHCP" width="300"/>](docs/assets/screenshots/dhcp.png) | [<img src="docs/assets/screenshots/vlans.png" alt="VLANs" width="300"/>](docs/assets/screenshots/vlans.png) |
| Zones, records, server groups | Scopes, pools, static reservations | Routers &amp; VLANs linked to subnets |

---

## Architecture

<p align="center">
  <img src="docs/assets/architecture.svg" alt="SpatiumDDI architecture" width="900"/>
</p>

**Control plane** — FastAPI + PostgreSQL + Redis + Celery. Single source of truth for everything (IPAM tree, DNS records, auth, audit log). Exposes a REST API; the web UI and any Terraform / Ansible / CLI integration all speak the same API.

**Data plane — two shapes:**

- **Agented** (BIND9, Kea) — one container per service. Each bakes in a sidecar agent (`spatium-dns-agent` / `spatium-dhcp-agent`) that (1) bootstraps with a PSK → rotating JWT, (2) long-polls `/config` with an ETag, (3) caches the last-known-good bundle on disk so the service keeps serving if the control plane is unreachable, (4) drains record / config ops over loopback (nsupdate + TSIG for BIND9; Kea Control Agent API for Kea). Structural changes reload named / kea-dhcp4; record changes do not.

- **Agentless** (Windows DNS, Windows DHCP) — no software on the Windows side. The control plane speaks directly: RFC 2136 over UDP/TCP 53 (DNS record writes + AXFR), WinRM + PowerShell over 5985/5986 (DNS zone CRUD, DHCP lease / scope reads). Credentials are Fernet-encrypted on the server row.

The driver abstraction is backend-neutral — services speak to `DNSDriver` / `DHCPDriver`, never to BIND9 / Kea / PowerShell specifics.

**Tech stack**: Python 3.12 · FastAPI · SQLAlchemy 2.x (async) · PostgreSQL 16 · Redis 7 · Celery · React 18 · TypeScript · Tailwind · shadcn/ui · pywinrm · dnspython · Docker · Kubernetes + Helm

---

## Getting Started

> ⚠️ SpatiumDDI is **alpha** (first release: `2026.04.16-1`). Commands and APIs may still shift between releases.

> 📘 For the full setup order (servers → zones/scopes → subnets → addresses) see **[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)**. For Windows DC integration see **[docs/deployment/WINDOWS.md](docs/deployment/WINDOWS.md)**.

### Quick start with Docker Compose

```bash
git clone https://github.com/spatiumddi/spatiumddi.git
cd spatiumddi
cp .env.example .env
# Required env vars in .env:
#   POSTGRES_PASSWORD=<set this>
#   SECRET_KEY=$(openssl rand -hex 32)
#   DNS_AGENT_KEY=$(openssl rand -hex 32)   # needed if running the DNS container
docker compose build
docker compose run --rm migrate
docker compose up -d
```

Open `http://localhost:8077` and log in with `admin` / `admin` (you're forced to change the password on first login).

### Upgrading

SpatiumDDI uses CalVer (`YYYY.MM.DD-N`) and ships every component
(api, worker, beat, frontend, dns-bind9, dhcp-kea) at the same tag.
The image tag is controlled by `SPATIUMDDI_VERSION` in your `.env`.

**Track latest** (default — your `.env` ships with `SPATIUMDDI_VERSION=latest`):

```bash
cd spatiumddi
git pull                              # refresh docker-compose.yml + .env.example for any new fields
docker compose pull                   # fetch the newest images
docker compose run --rm migrate       # apply any new alembic migrations (idempotent — no-op if up to date)
docker compose up -d                  # recreate api/worker/beat/frontend on the new images
```

**Pin to a specific release** (recommended for production — reproducible, no surprise upgrades):

```bash
# In your .env:
SPATIUMDDI_VERSION=2026.04.30-1

# Then:
docker compose pull
docker compose run --rm migrate
docker compose up -d
```

Bump the pinned version when you're ready to upgrade and re-run the same three commands.

**Notes:**
- `docker compose run --rm migrate` runs alembic against your current schema — safe to run every upgrade. It exits as a no-op if there are no new migrations.
- Downgrades are **not** supported. Database migrations are forward-only; if a release introduces a schema change you can't roll back to a tag that predates it without restoring a database backup. Always snapshot Postgres before a major-version upgrade you're not sure about.
- Watch the **CHANGELOG.md** entry for your target version for any release-specific upgrade notes (e.g. "operators on Kea HA must read this before upgrading").
- The sidebar shows the running version in the bottom-left corner and surfaces an `update available` badge when a newer GitHub release exists — the version probe runs hourly.

### Running the built-in BIND9 / Kea containers

The managed-service containers ship under Compose profiles — opt in when you want them:

```bash
docker compose --profile dns up -d                 # DNS only
docker compose --profile dns --profile dhcp up -d  # DNS + DHCP
```

Or set `COMPOSE_PROFILES=dns,dhcp` in your `.env` so plain `docker compose up -d` enables both automatically.

That starts `dns-bind9` bound to host port `5353` (udp + tcp). The agent registers with the control plane automatically using `DNS_AGENT_KEY` from your `.env` and appears in the UI under **DNS → Server Groups → default**.

Create a zone + record in the UI, then verify with `dig`:

```bash
dig @127.0.0.1 -p 5353 <your-record>.<your-zone> A +short
dig @127.0.0.1 -p 5353 -x <your-ip> +short    # reverse (PTR)
```

Record changes propagate to BIND9 via RFC 2136 — typically sub-second, no daemon restart. Zone / ACL / view changes trigger a config reload.

**Production**: point the agent at your real control plane, expose `53/udp` + `53/tcp`, and run one container per DNS server you want in the cluster. All servers in a group share the same TSIG key for dynamic updates.

### API & interactive docs

The FastAPI backend auto-generates OpenAPI / Swagger:

| Path | What |
|---|---|
| `http://localhost:8077/api/docs` | Swagger UI — try endpoints directly from the browser |
| `http://localhost:8077/api/redoc` | ReDoc — cleaner reference layout |
| `http://localhost:8077/api/openapi.json` | Raw OpenAPI 3 spec (for code generators) |

Every UI action is a REST call, so anything you do in the UI you can do via `curl`, Terraform, or your own client. Log in to the UI first to obtain a bearer token, then use `Authorization: Bearer <token>`.

### Reset the admin password

```bash
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

### Requirements

- Docker 24+ and Docker Compose v2, **or**
- Kubernetes 1.27+ with Helm 3, **or**
- Ubuntu 22.04 / Debian 12 / Alpine 3.20+ for bare metal

---

## Deployment Options

| Method | Use case | Status |
|---|---|---|
| **Docker Compose** | Dev, small single-host production | ✅ Supported |
| **Kubernetes + Helm** | Multi-node production, scalable | ✅ Umbrella chart (`charts/spatiumddi`, published OCI to `ghcr.io/spatiumddi/charts/spatiumddi`) |
| **Bare metal / VM (Ansible)** | On-prem without containers | 📋 Planned |
| **OS Appliance (ISO / qcow2)** | Air-gapped, zero-dependency | 📋 Planned |

---

## Documentation

Full docs at **[spatiumddi.github.io](https://spatiumddi.github.io)** (coming soon).

| Document | Description |
|---|---|
| [Getting Started](docs/GETTING_STARTED.md) | Recommended setup order — from server groups down to allocating an IP |
| [IPAM Features](docs/features/IPAM.md) | IP space, block, subnet, address management |
| [DHCP Features](docs/features/DHCP.md) | DHCP server management — Kea, Windows DHCP |
| [DNS Features](docs/features/DNS.md) | DNS zones, views, server groups, blocking lists, Windows DNS |
| [Auth & Permissions](docs/features/AUTH.md) | LDAP, OIDC, SAML, RADIUS, TACACS+, roles, scoped permissions |
| [System Admin](docs/features/SYSTEM_ADMIN.md) | Health dashboard, backup, notifications |
| [Observability](docs/OBSERVABILITY.md) | Logging, metrics, alerting |
| [Windows Server Setup](docs/deployment/WINDOWS.md) | WinRM, service accounts, firewall — Windows-side checklist |
| [DNS Agent Design](docs/deployment/DNS_AGENT.md) | Agent protocol, auto-registration, config sync |
| [DNS Driver Spec](docs/drivers/DNS_DRIVERS.md) | BIND9 + Windows DNS driver internals |
| [DHCP Driver Spec](docs/drivers/DHCP_DRIVERS.md) | Kea + Windows DHCP driver internals |
| [Appliance Deployment](docs/deployment/APPLIANCE.md) | OS image build and licensing |

---

## Project Status

| Phase | Focus | Status |
|---|---|---|
| Phase 1 | Core IPAM, auth, user management, audit log, Docker Compose | ✅ Done — LDAP/OIDC/SAML + RADIUS/TACACS+, group-based RBAC, bulk-edit, inheritance, mobile-responsive UI, and full IPv6 `/next-address` (EUI-64 + random /128 + sequential) all shipped |
| Phase 2 | DHCP (Kea), DNS (BIND9), DDNS, zone/subnet tree UI | ✅ Done — DNS, Kea DHCPv4, subnet-level DDNS, agent-side Kea DDNS, block/space DDNS inheritance, per-server zone serial reporting all shipped |
| Phase 3 | DNS views, server groups, blocking lists, VLAN/VXLAN, system admin, Kea HA | 🔄 DNS features + health dashboard + alerts framework + group-centric Kea HA (self-healing peer-IP drift + supervised daemons) landed; DNS Views end-to-end + HA state-transition actions still pending |
| Phase 4 | OS appliance, Terraform provider, SAML, backup/restore, ACME | 🔄 SAML landed; appliance + providers + backup + ACME (DNS-01 provider + embedded client) pending |
| Phase 5 | Multi-tenancy, IP request workflows, advanced reporting | 📋 Planned |

See [CHANGELOG.md](CHANGELOG.md) for the per-release feature list and
[CLAUDE.md](CLAUDE.md) for the authoritative spec.

---

## Contributing

Contributions are welcome.

- Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR
- Good first tasks are tagged on the [issue tracker](https://github.com/spatiumddi/spatiumddi/issues)
- Design discussion happens in [GitHub Discussions](https://github.com/spatiumddi/spatiumddi/discussions)

---

## License

Released under the [Apache 2.0 License](LICENSE).

Bundled components (BIND9, ISC Kea) are distributed under their own licenses. See [NOTICE](NOTICE) for the full list.

---

<p align="center">
  Built with ❤️ by the SpatiumDDI community · <a href="https://spatiumddi.github.io">spatiumddi.github.io</a>
</p>
