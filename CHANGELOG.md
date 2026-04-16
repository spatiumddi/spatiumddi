# Changelog

All notable changes to SpatiumDDI are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/); versioning uses [CalVer](https://calver.org/) (`YYYY.MM.DD-N`).

---

## Unreleased

### Added

**IPAM**
- IP aliases — Allocate/Edit IP modal supports extra CNAME/A records tied to the IP. Auto-deleted on IP purge.
- Reverse-zone backfill — dedicated button on Space / Block / Subnet headers (`POST /ipam/{scope}/{id}/reverse-zones/backfill`). Also backfills opportunistically on every IP allocation.
- DHCP Pool membership column on subnet IP table — cyan/violet/zinc badge per IP shows which pool (dynamic/reserved/excluded) it falls in.
- Bulk orphan cleanup modal on subnet header.
- `IPAddress.auto_from_lease` column distinguishes DHCP-lease-mirrored rows from manual allocations (migration `e2a6f3b8c1d4`).

**DNS**
- Real RPZ blocklist rendering in the BIND9 agent — `response-policy { } break-dnssec yes`, CNAME trigger zone files (nxdomain/sinkhole/redirect/passthru). Wildcards block both apex and subdomains.
- Blocklist entries get a `reason` column (migration `b4d1c9e2f3a7`) and per-entry `is_wildcard` toggle (defaults true).
- Inline edit for blocklist entries + exceptions (`PUT .../entries/{id}`, `PUT .../exceptions/{id}`).
- Blocklist page reorganized into red **Blocked Domains** and green **Allow-list** sections.
- DNS records table: always-visible edit/delete, clickable record name, single-step delete confirm, multi-select bulk delete (IPAM records excluded).
- DNS agent re-bootstraps on 404 (not just 401) — recovers from stale server rows.

**DHCP**
- Pool overlap validation + existing-IP warning on pool create.
- Static DHCP ↔ IPAM sync (creates `status=static_dhcp` rows, fires DNS sync on create/update/delete).
- Lease → IPAM mirror: active leases create `dhcp` rows; expired leases remove them (`auto_from_lease` flag only).
- Celery `sweep_expired_leases` task (every 5min) catches missed lease events.
- Force-sync coalesces repeated clicks into one pending op.
- Kea agent: UDP socket mode for relay-only deployments; `/run/kea` perms; lease op acks via heartbeat.
- DHCP scope options default-prefill from Settings (DNS/NTP/domain/lease-time).
- Static assignments moved from DHCP Pools tab into IPAM Allocate IP flow.

**Platform**
- Jekyll docs site config (`docs/_config.yml`, `docs/index.md`).
- CHANGELOG; alpha banner; clickable screenshot thumbnails in README.
- Seed script (`scripts/seed_demo.py`).
- Alembic migrations now tracked in git (were `.gitignore`d — CI was broken).
- `COMPOSE_PROFILES` documented.

### Fixed

- Full audit of IPAM/VLANs/DNS/DHCP frontend ↔ backend API contracts; 10+ mismatches fixed.
- `allocate_next_ip` — `FOR UPDATE` on outer join, now `of=Subnet` + `.unique()`.
- Workflow permissions hardened (CodeQL alerts resolved).

---

## 2026.04.16-1 — Alpha

First public release. **Alpha quality** — expect rough edges and breaking changes between releases.

### Added

**IPAM**
- Hierarchical IP management: spaces, blocks (nested), subnets, addresses
- Subnet CIDR validation with "Did you mean?" hints
- Next-available IP allocation (sequential / random)
- Soft-delete IP addresses (orphan → restore / purge)
- Bulk orphan cleanup modal on subnet view
- Subnet-by-size search ("Find by size" in create modal)
- Per-column filters on address & block tables
- Drag-and-drop reparenting of blocks and subnets
- Free-space band on block detail with click-to-create
- Import/export (CSV, JSON, XLSX with preview)
- Bulk-edit subnets (tags, custom fields)
- Custom field definitions per resource type
- DNS assignment at space / block / subnet level with inheritance
- IPAM ↔ DNS drift detection and reconciliation (subnet / block / space scope)
- DNS sync indicator column on IP address table
- DHCP pool membership column on IP address table

**DNS**
- Server groups, servers, zones, records — full CRUD
- BIND9 driver with Jinja templates, TSIG-signed RFC 2136 dynamic updates
- Agent runtime: bootstrap (PSK → JWT), long-poll config sync with ETag, on-disk cache
- Container image: `ghcr.io/spatiumddi/dns-bind9` (Alpine 3.22, multi-arch)
- Zone tree with nested sub-zone display
- Zone import/export (RFC 1035 parser, color-coded diff preview)
- Server health checks (heartbeat staleness → SOA fallback)
- ACLs, views, trust anchors
- Blocking lists (RPZ) with feed refresh, bulk-add, exceptions
- Query logging configuration (file / syslog / stderr)
- DNS defaults in Settings (TTL, zone type, DNSSEC, recursion)

**DHCP**
- Kea driver + agent runtime (bootstrap, long-poll, lease tail, local cache)
- Container image: `ghcr.io/spatiumddi/dhcp-kea` (Alpine 3.22, multi-arch)
- Server groups, servers, scopes, pools, static assignments, client classes
- DHCP options editor with NTP (option 42) as first-class field
- Pool overlap validation on create and resize
- Existing-IP-in-range warning on pool creation
- Scope auto-binds to sole server; gateway + settings defaults pre-filled
- Static DHCP ↔ IPAM sync (status=static_dhcp, DNS forward/reverse)
- DHCP defaults in Settings (DNS servers, domain, NTP, lease time)
- UDP socket mode for relay-only deployments (no broadcast / no NET_RAW)

**VLANs**
- Routers and VLANs with full CRUD
- Subnet ↔ VLAN association (router + VLAN columns in IPAM views)
- Delete protection when subnets still reference a VLAN/router

**Auth & Users**
- Local auth with JWT + refresh token rotation
- Forced password change on first login
- User management (create, edit, reset password, delete)

**Platform**
- Dashboard with utilisation stats, top subnets, VLAN/DNS/DHCP status sections
- Global search (Cmd+K / Ctrl+K) across IPs, hostnames, MACs, subnets
- Settings page (branding, allocation, session, DNS/DHCP defaults, utilisation thresholds)
- Audit log viewer with action/result badges and filters
- Docker Compose with `dns` and `dhcp` profiles (`COMPOSE_PROFILES=dns,dhcp`)
- Kubernetes manifests (StatefulSets, services, PVCs)
- GitHub Actions CI (lint, type-check, test) + release workflow (multi-arch images, GitHub Release)

### Security
- Workflow permissions hardened (CodeQL alerts resolved)
- All mutations audited before commit
- Agent re-bootstraps on 401/404 (no stale-token loops)

---

_For the full commit history, see the [GitHub compare view](https://github.com/spatiumddi/spatiumddi/commits/main)._
