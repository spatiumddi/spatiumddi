<p align="center">
  <img src="docs/assets/logo.svg" alt="SpatiumDDI Logo" />
</p>

<h1 align="center">SpatiumDDI</h1>

<p align="center">
  <strong>Open-source DDI — DNS, DHCP, and IP Address Management</strong><br/>
  Manage your entire network address space from one unified platform.
</p>

<p align="center">
  <a href="https://github.com/spatiumddi/spatiumddi/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License"/></a>
  <a href="https://github.com/spatiumddi/spatiumddi/issues"><img src="https://img.shields.io/github/issues/spatiumddi/spatiumddi" alt="Issues"/></a>
  <a href="https://spatiumddi.org"><img src="https://img.shields.io/badge/docs-spatiumddi.org-informational" alt="Docs"/></a>
  <img src="https://img.shields.io/badge/status-pre--alpha-orange" alt="Status"/>
  <img src="https://img.shields.io/badge/python-3.12+-green" alt="Python"/>
  <img src="https://img.shields.io/badge/react-18+-61DAFB" alt="React"/>
</p>

---

## What is SpatiumDDI?

SpatiumDDI is a production-grade, open-source **DDI platform** — DNS, DHCP, and IP Address Management — built for teams that need real control over their network infrastructure without paying enterprise licensing fees.

It is designed as a modern alternative to commercial DDI platforms like EfficientIP and Infoblox, with first-class support for:

- 🗂 **Hierarchical IP management** — spaces, blocks, subnets, and addresses in a visual tree
- 🔄 **DHCP server management** — ISC Kea and ISC DHCP, with HA failover support
- 🌐 **DNS server management** — BIND9 and PowerDNS, with views, zones, and blocking lists
- ⏱ **NTP server management** — Chrony and ntpd integration
- 🔒 **Granular permissions** — delegate IP ranges to groups via LDAP, Entra ID, or OIDC
- 📋 **Full audit trail** — every change logged, append-only, viewable from the UI
- 🚀 **Flexible deployment** — Docker Compose, Kubernetes (Helm), bare metal, or OS appliance image

---

## Features

### IP Address Management
- Hierarchical IP space → block → subnet → address tree
- Visual subnet utilization with threshold alerting
- Next available IP allocation (sequential or random)
- VLAN / VXLAN assignment per subnet
- Custom fields on all resources (owner, contact, ticket number, etc.)
- Discovery scanning — reconcile live network vs. IPAM records
- Import/export: CSV, JSON, vendor formats

### DHCP
- Manage ISC Kea and ISC DHCP servers from one UI
- Multiple pools per subnet (dynamic, reserved, excluded)
- Static assignments by MAC address
- DHCP client classes and per-pool options
- HA server pairs (Kea HA hook, ISC DHCP failover)
- **Local config caching** — DHCP servers keep serving if control plane is unreachable
- Real-time lease tracking and reconciliation

### DNS
- BIND9 and PowerDNS support
- **Incremental updates** — RFC 2136 DDNS and PowerDNS API, never a full restart
- DNS views (split-horizon) for internal/external/DMZ zones
- Server groups — manage clusters of DNS servers as a unit
- Zone tree — navigate the full namespace hierarchy
- **Dynamic DNS** — DHCP lease → A + PTR record automatically
- **Blocking lists** — Pi-hole-style ad/malware blocking via BIND9 RPZ
- Zone import/export (RFC 1035 zone file format)

### Security & Access Control
- Local accounts, LDAP/Active Directory, Entra ID (Azure AD), Okta, Keycloak
- TOTP multi-factor authentication
- Group-based permissions scoped to specific IP ranges or DNS zones
- Permissions inherit downward through the hierarchy
- Global and per-user API tokens with optional scope restriction

### Operations
- Centralized log viewer built into the admin UI
- Prometheus metrics on all components
- Pre-built Grafana dashboards
- Health dashboard — all services at a glance
- Service start/stop/restart from the UI
- Backup and restore with S3/SFTP/Azure Blob support
- Email, webhook, Slack, and PagerDuty notifications

---

## Architecture

```
                        ┌─────────────────────┐
                        │   React Frontend     │
                        │   (SPA via nginx)    │
                        └──────────┬──────────┘
                                   │ HTTPS
                        ┌──────────▼──────────┐
                        │   FastAPI Backend    │
                        │   (Python 3.12+)     │
                        └──┬──────────────┬───┘
                           │              │
              ┌────────────▼───┐    ┌─────▼──────────┐
              │  PostgreSQL    │    │  Redis          │
              │  (HA via       │    │  (Cache +       │
              │   Patroni)     │    │   Celery broker)│
              └────────────────┘    └─────────────────┘
                           │
         ┌─────────────────┼──────────────────┐
         │                 │                  │
┌────────▼──────┐  ┌───────▼───────┐  ┌──────▼───────┐
│ DHCP Servers  │  │  DNS Servers  │  │  NTP Servers │
│ Kea / ISC     │  │  BIND9 /      │  │  Chrony /    │
│ (with agent)  │  │  PowerDNS     │  │  ntpd        │
└───────────────┘  └───────────────┘  └──────────────┘
```

**Tech stack:** Python 3.12 · FastAPI · SQLAlchemy · PostgreSQL · Redis · Celery · React 18 · TypeScript · Tailwind CSS · Docker · Kubernetes

---

## Deployment Options

| Method | Use Case |
|---|---|
| **Docker Compose** | Development, small production |
| **Kubernetes (Helm)** | Production, scalable |
| **Bare metal / VM (Ansible)** | On-premises without containers |
| **OS Appliance (ISO / qcow2)** | Air-gapped, zero-dependency deployments |

---

## Getting Started

> ⚠️ SpatiumDDI is in early development. The instructions below will be updated as the project matures.

### Quick start with Docker Compose

```bash
git clone https://github.com/spatiumddi/spatiumddi.git
cd spatiumddi
cp .env.example .env
docker compose up -d
```

Then open `http://localhost` and complete the first-run setup wizard.

### Requirements

- Docker 24+ and Docker Compose v2, **or**
- Kubernetes 1.27+ with Helm 3, **or**
- Ubuntu 22.04 / Debian 12 / Alpine 3.18+ for bare metal

---

## Documentation

Full documentation is at **[spatiumddi.org](https://spatiumddi.org)** (coming soon — hosted on GitHub Pages).

| Document | Description |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | System design and component overview |
| [IPAM Features](docs/features/IPAM.md) | IP space, block, subnet, and address management |
| [DHCP Features](docs/features/DHCP.md) | DHCP server management, pools, static assignments |
| [DNS Features](docs/features/DNS.md) | DNS zones, views, server groups, blocking lists |
| [NTP Features](docs/features/NTP.md) | NTP server management |
| [Auth & Permissions](docs/features/AUTH.md) | LDAP, OIDC, roles, and scoped permissions |
| [System Admin](docs/features/SYSTEM_ADMIN.md) | Health dashboard, backup, notifications |
| [Observability](docs/OBSERVABILITY.md) | Logging, metrics, alerting |
| [Appliance Deployment](docs/deployment/APPLIANCE.md) | OS image build and licensing |
| [DNS Drivers](docs/drivers/DNS_DRIVERS.md) | BIND9 and PowerDNS driver specs |

---

## Project Status

SpatiumDDI is currently in the **design and scaffolding phase**. We are actively working toward a first usable release.

| Phase | Status | Focus |
|---|---|---|
| Phase 1 | 🔄 In progress | Core IPAM, auth, permissions, Docker Compose |
| Phase 2 | 📋 Planned | DHCP + DNS integration, DDNS, NTP |
| Phase 3 | 📋 Planned | DNS views, blocking lists, system admin panel |
| Phase 4 | 📋 Planned | OS appliance, Terraform provider, SAML |
| Phase 5 | 📋 Planned | Import/export, multi-tenancy, advanced reporting |

---

## Contributing

Contributions are welcome! SpatiumDDI is an open project and we want to build it in the open.

- Read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a PR
- Check the [open issues](https://github.com/spatiumddi/spatiumddi/issues) for good first tasks
- Join the discussion in [GitHub Discussions](https://github.com/spatiumddi/spatiumddi/discussions)

---

## License

SpatiumDDI is released under the [Apache 2.0 License](LICENSE).

Bundled components (BIND9, ISC Kea, PowerDNS, Chrony) are distributed under their own licenses. See [NOTICE](NOTICE) for the full list.

---

<p align="center">
  Built with ❤️ by the SpatiumDDI community · <a href="https://spatiumddi.org">spatiumddi.org</a>
</p>
