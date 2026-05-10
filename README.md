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

## Contents

- [Why SpatiumDDI](#why-spatiumddi) — the elevator pitch
- [What's in the box](#whats-in-the-box) — quick capability tour
- [Full feature detail](#full-feature-detail) — deep dive on every subsystem
- [Screenshots](#screenshots)
- [Architecture](#architecture)
- [Getting Started](#getting-started) — Docker Compose quick start, demo seed, upgrade flow, admin reset
- [Deployment Options](#deployment-options)
- [Documentation](#documentation)
- [Project Status](#project-status)
- [Contributing](#contributing)
- [License](#license)

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
| 🗂 | **Hierarchical IPAM** | spaces · blocks · subnets · IPv4 + full IPv6 (EUI-64 / random / sequential) · per-IP roles · MAC history · reservation TTL · bulk allocate with name templates |
| ✂️ | **Subnet operations** | Split · Merge · Find-free · subnet planner (multi-level CIDR design + transactional apply) — preview-then-commit with typed-CIDR confirm · single Tools dropdown on subnet headers |
| 🧮 | **Planning tools** | CIDR calculator · address planner (pack /N requests into free space) · aggregation suggestion · free-space treemap |
| 🌐 | **DNS** | BIND9 container, auto-registering · RFC 2136 dynamic updates · per-server zone-serial drift · TSIG keys · zone delegation wizard · zone templates · RPZ blocklists with curated catalog · BIND9 catalog zones (RFC 9432) |
| ⚖️ | **GSLB-lite** | health-checked DNS pools — tcp / http / https / icmp / none probes flip A/AAAA records in/out of the rendered rrset; manual enable per member |
| 🔄 | **DHCP** | Kea container · group-centric Kea HA (load-balanced or hot-standby) with self-healing peer drift · option templates · 95-entry option-code library |
| 🪟 | **Windows DNS + DHCP** | agentless — RFC 2136 + WinRM, no software on the DC |
| 📥 | **DNS configuration import** | one-shot migration from BIND9 (`named.conf` archive upload), Windows DNS (live WinRM pull), and PowerDNS (live REST pull) · preview-before-commit · per-zone savepoint so partial failures don't abort the batch · provenance stamps (`import_source` + `imported_at`) on every imported zone + record |
| 📡 | **Multicast group registry** | RFC 5771 catalog seeded · per-IPSpace groups + PIM rendezvous-point domains · auto-created enclosing `224.0.0.0/4` / `ff00::/8` IPBlock for tree visibility · per-IP collision conformity check · bulk-allocate from RFC 2365 admin-scoped ranges |
| 🔁 | **NAT cross-reference** | 1:1 / PAT / hide-NAT tracked in IPAM with FK links to live IP rows |
| 📜 | **DHCP lease history** | forensic trail of every expiry, MAC reassignment, absence-delete |
| 🗑 | **Soft-delete trash** | 30-day Trash with cascade restore for spaces, blocks, subnets, zones, scopes |

### 🌐 Network entities

| | Feature | Highlights |
|---|---|---|
| 🌐 | **ASN management** | first-class ASN entity · RDAP holder refresh (per-RIR routing via IANA bootstrap) · RPKI ROA pull (Cloudflare or RIPE) with expiry tracking · holder-drift detection with side-by-side diff · alert rules for drift / unreachable / ROA expiry · **BGP Footprint tab** with RIPEstat (announced prefixes / prefix-overview / routing history) + PeeringDB (peering profile / IXP presence) — REST + 5 MCP tools · in-process TTL cache (RIPEstat 6 h, PeeringDB 24 h) |
| 🤝 | **BGP peering + communities** | peer / customer / provider / sibling graph between tracked ASNs · BGP communities catalog (RFC 1997 / 7611 / 7999 well-knowns + per-AS extensions, large communities per RFC 8092) |
| 🛣 | **VRFs as first-class** | name / RD / import + export RTs / optional ASN linkage · cross-cutting RD/RT validator (warns or 422s on ASN-portion mismatch) · VRF picker on IPSpace + IPBlock modals · auto-backfill from existing freeform fields |
| 📛 | **Domain registration tracking** | distinct from DNSZone — registrar / registrant / expiry / nameservers / DNSSEC · RDAP refresh (TLD → RDAP-base via IANA bootstrap) · NS-drift, registrar-changed, DNSSEC-status-changed alerts · explicit `dns_zone.domain_id` linkage with sub-zone tree fallback |
| 🏢 | **Customer / Site / Provider** | logical ownership entities cross-cutting IPAM / DNS / DHCP / Network · `ON DELETE SET NULL` cross-references on every existing table so re-tagging is safe · shared pickers + chips wired into every modal |
| 🛤 | **WAN circuits** | carrier-supplied logical pipe (provider + transport class + bandwidth + endpoints + term + cost) · 9 transport classes including AWS DX / Azure ER / GCP Interconnect cross-connects · soft-deletable (`status='decom'` is operator-visible end-of-life) · alerts for term-expiring + status-changed |
| 📦 | **Service catalog** | bundles VRF / Subnet / IPBlock / DNSZone / DHCPScope / Circuit / Site / Overlay into a customer-deliverable · `mpls_l3vpn` + `sdwan` + `custom` kinds in v1 · kind-aware `/summary` endpoint with L3VPN canonical shape · alerts for term-expiring + resource-orphaned |
| 🌐 | **SD-WAN overlays** | vendor-neutral overlay topology + routing-policy intent · 6 kinds (sdwan / ipsec / wireguard / dmvpn / vxlan-evpn / gre) · ordered preferred-circuit chain per site · 33 well-known SaaS apps in the catalog · pure read-only `/simulate` what-if when circuits go down · SVG circular-layout topology view |

### 🔍 Discovery & visibility

| | Feature | Highlights |
|---|---|---|
| 📡 | **SNMP discovery** | v1 / v2c / v3 polling of routers + switches → ARP / FDB / interfaces / LLDP neighbours feed back into IPAM |
| 🎯 | **Nmap scanner** | per-IP / per-subnet (CIDR sweep) / `/tools/nmap` · live SSE streaming · stamp alive hosts → IPAM |
| 🛰 | **Device profiling** | passive DHCP fingerprinting (scapy + fingerbank) **and** opt-in auto-nmap on new DHCP lease — what kind of device is on every IP |
| 🏷 | **OUI vendor lookup** | MAC → vendor names in IP tables, DHCP leases, search filters |
| 🎨 | **Dashboards** | nine sub-tabs — **Overview / IPAM / DNS / DHCP / Network / Integrations / Security / Compliance / Conformity** — each backed by a single rollup endpoint under `/api/v1/dashboards/`, refreshed every 60 s · utilization heatmap · DNS query rate · DHCP traffic · ASN drift + RPKI ROA expiry · circuit alerts · service-catalog orphans · per-mirror integration counts · account lockout state + active sessions + audit-chain verification · platform health card |
| 📊 | **Platform Insights** | native Postgres diagnostics + per-container CPU / mem / IO. No extra agents |

### 🔌 Integrations (read-only mirrors)

| | Source | What's mirrored |
|---|---|---|
| 🐳 | **Docker** | networks · optional container IPs |
| ☸️ | **Kubernetes** | cluster CIDRs · nodes · LoadBalancer VIPs · Ingress → DNS |
| 🖥 | **Proxmox VE** | bridges · SDN VNets + subnets · VM / LXC NICs (qemu-guest-agent) |
| 🔐 | **Tailscale** | tailnet devices + synthetic `*.ts.net` zone |
| 📡 | **UniFi Network** | controller sites · networks (VLANs / CIDRs) · clients (with hostnames + MAC) |

### 🛡 Identity & ops

| | Feature | Highlights |
|---|---|---|
| 🔒 | **RBAC + external auth** | LDAP · OIDC · SAML · RADIUS · TACACS+ with backup-server failover · API tokens with auto-expiry · scoped API tokens (per-permission) |
| 🛡 | **TOTP MFA** | local-user 2FA — QR enrolment via `pyotp` + `qrcode` · single-use backup codes · admin force-disable per user (audit-logged) |
| 🔐 | **Local-auth hardening** | configurable **password policy** (min length · per-class complexity · history depth · max-age) · **account lockout** after N failed logins inside a rolling window (default off; opt-in in Settings) · **active session viewer + force-logout** at `/admin/sessions` — every login carries a `jti` claim that resolves to a `UserSession` row, flip `revoked=True` to 401 the in-flight token on its next call |
| 🏷 | **Subnet classification tags** | `pci_scope` · `hipaa_scope` · `internet_facing` first-class boolean columns on every subnet · indexed predicates · compliance roll-up card on Platform Insights · feeds the compliance-change alert + conformity policy filters |
| 🤖 | **Operator Copilot (AI)** | grounded chat over your live IPAM / DNS / DHCP / Network data — multi-vendor (OpenAI / Anthropic / Azure OpenAI / Gemini / OpenAI-compat for Ollama, vLLM, etc.) with automatic failover · **91 tools** spanning IPAM, DNS (records / pools / blocklists / views), DHCP (pools / statics / classes / option templates / PXE / MAC blocks), network modeling (ASNs / VRFs / circuits / services / overlays / domains), ownership (customers / sites / providers), admin (users / groups / roles), integration mirrors (K8s / Docker / Proxmox / Tailscale / UniFi), observability (DNS query / DHCP activity / metrics / global search), and Apply-gated write proposals (`propose_create_ip_address` / `propose_create_dns_record` / `propose_create_dhcp_static` / `propose_create_alert_rule` / `propose_run_nmap_scan` / `propose_archive_session`) · MCP HTTP endpoint for Claude Desktop / Cursor / Cline · "Ask AI about this" affordances on every resource · per-provider editable system prompt · per-provider tool allowlist · OUI vendor enrichment baked in · live nmap results in chat · per-message token / latency footer · Markdown + GFM tables in replies · daily digest |
| 🔔 | **Alerts + forwarding** | rule-based alerts · `compliance_change` rule type (PCI / HIPAA / internet-facing audit-log scanner with 24 h auto-resolve, three disabled seed rules) · multi-target syslog (RFC 5424 / CEF / LEEF / RFC 3164) · HTTP webhooks · SMTP email · Slack / Teams / Discord chat |
| 📑 | **Conformity evaluations** | declarative policy library scheduled against PCI-DSS / HIPAA / SOC2 frameworks · 6 starter check kinds (`has_field` · `in_separate_vrf` · `no_open_ports` · `alert_rule_covers` · `last_seen_within` · `audit_log_immutable`) · 8 disabled seed policies, opt-in toggle · pass→fail transitions emit alert events · auditor-facing PDF export with SHA-256 integrity hash · `Auditor` + `Compliance Editor` builtin roles |
| 🪝 | **Typed-event webhooks** | 96 typed events (resource × verb) · HMAC-SHA256 signed · outbox-backed retry with backoff + dead-letter |
| 🐛 | **Diagnostics — captured uncaught exceptions** | every uncaught Python exception across API + Celery lands in a queryable `internal_error` table with **fingerprint dedup** (sha256 of class + top-2 frames), occurrence counter, last-seen-at bumping, redaction of headers + secret-shaped payload fields, `context_json` blob capped at 16 KB · admin viewer at `/admin/diagnostics/errors` with Acknowledge / Suppress (1 h / 1 d / 1 w) / Delete / **Submit-bug** (pre-filled GitHub-issue template URL) actions · daily prune sweep against the configured retention window |
| 🏷 | **Platform-wide tags + filter** | `tags JSONB` columns across IPAM (spaces / blocks / subnets / IPs) · Network modeling (ASNs / VRFs / circuits / services / overlays / customers / sites / providers) · DNS (zones / records) · DHCP (scopes / pools / statics) · `?tag=` filter on every REST list endpoint with multi-tag AND/OR semantics · `/api/v1/tags/autocomplete` ranked by occurrence · tag chips on every list view + clickable pills on the IP detail modal that navigate to a filtered IPAM view |
| 🔐 | **ACME DNS-01** | `acme-dns`-compatible — certbot / lego / acme.sh issue public certs (wildcards included) |
| 📋 | **Audit log** | every mutation logged, append-only, filterable in the UI · **tamper-evident SHA-256 hash chain** — every row carries `seq` + `prev_hash` + `row_hash`; verifier walks the table, re-hashes, and pinpoints the first break |
| 🗑 | **Soft-delete + 30-day Trash** | spaces / blocks / subnets / DNS zones / DNS records / DHCP scopes are recoverable for 30 days · cascade restore via `deletion_batch_id` (one click brings a subnet's DHCP scopes back together) · global ORM filter hides soft-deleted rows by default · nightly `trash_purge` Celery task hard-deletes past the retention window |
| 💾 | **Backup + restore** | full-system backup with passphrase-wrapped `secrets.enc` + 8 destination kinds (local volume · AWS S3 / S3-compatible · SCP/SFTP · Azure Blob · SMB/CIFS · FTP/FTPS · GCS · WebDAV) · scheduled cron + retention · selective per-section restore · cross-install secret rewrap so cross-install operators don't hand-copy `SECRET_KEY` · `alembic upgrade head` on restore with drift auto-recovery · exclude-secrets diagnostic mode for shareable debug snapshots · proxy archive download · `system.backup_*` typed-event fan-out via the existing webhook outbox |
| 🧹 | **Factory reset** | per-section "wipe back to defaults" surface for superadmins — 12 sections (IPAM · DNS · DHCP · Network modeling · Integrations · AI · Compliance · Tools · Observability logs · Auth+RBAC · Settings · Everything) · password re-verification + per-section `DESTROY-*` confirm phrase + in-flight backup mutex + 6 h cooldown · audit anchor that survives `audit_log` wipes · calling superadmin + built-in roles preserved across every section |

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
  - **Bulk allocate** a contiguous range with a name template
    (`dhcp-{n}` / `host-{oct3}-{oct4}` / `web-{n:03d}`) — preview
    → commit, capped at 1024 IPs, skips dynamic DHCP pools, detects
    FQDN collisions, optionally creates A + PTR records
  - **IP table polish** — sticky column headers, shift-click range
    select, "Seen" recency dot per row (alive / stale / cold /
    never), subtle gap markers between non-contiguous IPs so a
    deleted hole doesn't go unnoticed

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

- 📥 **DNS configuration importer** — one-shot migration tool that turns existing zone data into native SpatiumDDI zones + records.
  - Three sources, one canonical IR + commit pipeline:
    - **BIND9** — upload a `.zip` / `.tar.gz` of the `named.conf` tree; the parser walks `include` directives, resolves zone files via four strategies (relative path, absolute path, `directory` option, search), tolerates `view {}` blocks, and feeds the same canonical-zone shape the other two sources do
    - **Windows DNS** — live pull over WinRM via the existing `WindowsDNSDriver`; honours system zones (TrustAnchors / `_msdcs.*`) by routing them through a dedicated branch instead of trying to migrate them
    - **PowerDNS** — live pull over the authoritative REST API (`X-API-Key` auth, hard cap of 5000 zones / 60 s socket timeout); hoists SOA from rrset content, splits MX / SRV priority into the dedicated columns, drops disabled records + DNSSEC + LUA / ALIAS with distinct warnings
  - Preview-before-commit on every source — operator sees the conflict picker (overwrite / skip / merge) before any rows are written
  - Per-zone savepoint commit so a failure on zone N rolls back N but keeps zones 1..N-1 — no all-or-nothing import abort
  - Provenance stamping — `import_source` + `imported_at` columns on `dns_zone` + `dns_record` flag everything that came in from the importer (UI surfaces a chip)
  - Three-tab admin page at `/admin/dns/import` — one tab per source, all three rendered by a shared preview panel + commit-result panel
  - Once imported, SpatiumDDI is the source of truth — there is no continuous two-way mirror

- 📡 **Multicast group registry** — IPv4 + IPv6 multicast groups as first-class entities.
  - RFC 5771 IANA registry seeded as platform-provided rows (e.g. `224.0.0.1` All-Hosts, `224.0.0.5` OSPF, …)
  - Operator catalog of business-defined groups (per-IPSpace) with description, owner, scope (link-local / admin-local / org-local / global)
  - **PIM rendezvous-point domains** — `pim_rp_domain` table tracking RP routers + group ranges they serve
  - **IPAM tree integration** — creating a group in an IPSpace auto-creates the enclosing `224.0.0.0/4` (v4) or `ff00::/8` (v6) IPBlock when none exists; a startup hook backfills blocks for pre-existing groups so the upgrade is seamless
  - **Tree rendering** — multicast IPBlocks render with a violet 📡 Radio icon in both the tree row and BlockDetailView identity row; inside a multicast block, a "Multicast Groups" panel surfaces the streams whose addresses fall within the block's CIDR (queries `multicast_group` directly — no mirror IPAddress rows)
  - **Click-through** opens the multicast page pre-scoped to the IPSpace via `?space=<uuid>`
  - **Bulk-allocate** from RFC 2365 admin-scoped ranges with name templating
  - Per-IP collision conformity check — flags addresses that overlap a known multicast registration before allocation

### Network entities

- 🌐 **ASN management** — first-class autonomous-system entity.
  - Data model: `asn` table with BigInteger `number` (full 32-bit range), auto-derived `kind` (public / private per RFC 6996 + RFC 7300), auto-derived `registry` (RIR — arin / ripe / apnic / lacnic / afrinic) from a hand-curated IANA delegation snapshot
  - **RDAP holder refresh** — per-RIR routing via IANA bootstrap; per-row Refresh button + scheduled hourly task (`asn_whois_interval_hours`, default 24 h)
  - **RPKI ROA pull** — Cloudflare or RIPE NCC source (operator-tunable via `rpki_roa_source`); cached for 5 min in-memory so a sweep of 50 ASNs makes one HTTP call; per-row Refresh RPKI button
  - **Holder-drift diff viewer** — `previous_holder` persisted on every refresh so the WHOIS tab can render a side-by-side without consulting the audit log
  - **Alert rules** — `asn_holder_drift`, `asn_whois_unreachable`, `rpki_roa_expiring`, `rpki_roa_expired`
  - Detail page tabs: WHOIS · RPKI ROAs · Linked IPAM · BGP Peering · Communities · Alerts

- 🤝 **BGP peering + communities** — operator-curated relationship graph + community catalog.
  - **Peerings** — `bgp_peering` table with `peer | customer | provider | sibling`; both endpoints FK ON DELETE CASCADE; unique on `(local, peer, relationship_type)`. Form lets the operator pick either side as "local"; modal normalises to canonical shape on submit
  - **`Router.local_asn_id` FK** — stamps which AS a router originates routes from
  - **Communities catalog** — 7 RFC 1997 / 7611 / 7999 well-knowns seeded as platform rows (no-export, no-advertise, no-export-subconfed, local-as, graceful-shutdown, blackhole, accept-own); per-AS catalog with `kind` validation (`standard` / `regular` `ASN:N` / `large` `ASN:N:M`)
  - "Use on this AS" button per standard row pre-fills the form with the well-known value

- 🛣 **VRFs as first-class entities** — replaces the freeform `vrf_name` / `route_distinguisher` / `route_targets` text fields on IPSpace.
  - Data model: `vrf` table with name, description, optional `asn_id` FK, RD (with format validation), split import / export RT lists, tags, custom_fields
  - `ip_space.vrf_id` + `ip_block.vrf_id` FKs ON DELETE SET NULL
  - **Cross-cutting RD / RT validator** — each `ASN:N` entry whose ASN portion does not match `vrf.asn.number` produces a non-blocking warning; `vrf_strict_rd_validation` toggle escalates to 422
  - `IPBlock.vrf_warning` flags when a block's pinned VRF differs from its parent space's VRF (intentional in hub-and-spoke designs but worth a heads-up)
  - **VRF picker** on the New / Edit IPSpace and Create / Edit IPBlock modals (replaces the freeform text inputs)
  - Migration backfills existing freeform values into VRF rows so nothing is lost

- 📛 **Domain registration tracking** — distinct from DNSZone (records SpatiumDDI serves vs. registry-side metadata).
  - Data model: `domain` table tracking registrar / registrant / expiry / DNSSEC status / nameservers
  - **RDAP refresh** — TLD → RDAP-base lookup driven by the IANA bootstrap registry (`data.iana.org/rdap/dns.json`), cached 6 h; routes `.com` → `rdap.verisign.com/com/v1/`, etc.
  - **Nameserver drift** — operator-pinned expected list vs. registry-advertised list, with a side-by-side diff panel
  - **Alert rules** — `domain_expiring` (severity escalation around `threshold_days`), `domain_nameserver_drift`, `domain_registrar_changed`, `domain_dnssec_status_changed`
  - Per-row expiry countdown badges (green > 90 d / amber 30–90 d / red < 30 d / dark-red expired)
  - **Explicit `dns_zone.domain_id` linkage** with sub-zone suffix-match fallback — `test.example.com` shows up under `example.com`'s linked-zones tab; `example.com.au` correctly does NOT

- 🏢 **Customer / Site / Provider** — three first-class logical ownership rows that cross-cut IPAM / DNS / DHCP / Network.
  - **`Customer`** — soft-deletable; account number / contact info / status (active / inactive / decommissioning) / tags
  - **`Site`** — hierarchical via `parent_site_id`; unique-per-parent `code` (NULLS NOT DISTINCT for top-level deduping); kinds (datacenter / branch / pop / colo / cloud_region / customer_premise) + free-form region label
  - **`Provider`** — kinds (transit / peering / carrier / cloud / registrar / sdwan_vendor) + optional `default_asn_id` FK
  - **Cross-reference FKs** added on subnet / ip_block / ip_space / vrf / dns_zone / asn / network_device / domain / circuit / network_service / overlay_network — every column is `ON DELETE SET NULL` so re-tagging is safe and operators never lose data
  - Shared `CustomerPicker` / `SitePicker` / `ProviderPicker` (with optional kind filter) + matching Chip components plug into every IPAM / DNS / circuit / overlay create + edit modal

- 🛤 **WAN circuits** — carrier-supplied logical pipe distinct from the equipment that lights it up.
  - Data model: `circuit` table with `provider_id` (RESTRICT), optional `customer_id` (SET NULL), 4 endpoint refs (a/z-end site + subnet, all SET NULL), `transport_class` enum (mpls / internet_broadband / fiber_direct / wavelength / lte / satellite / direct_connect_aws / express_route_azure / interconnect_gcp), asymmetric `bandwidth_mbps_down` / `bandwidth_mbps_up`, `term_start_date` / `term_end_date`, `monthly_cost` + 3-letter ISO 4217 currency
  - **Soft-deletable** — `status='decom'` is the operator-visible end-of-life flag; row stays restorable for "what carrier did Site-X use in 2024?" audits
  - List page at `/network/circuits` with bulk-action table + tabbed editor modal (General / Endpoints / Term + cost / Notes) + colour-coded term-end badge
  - **Alert rules** — `circuit_term_expiring` (severity escalates around `threshold_days`), `circuit_status_changed` (only fires on `suspended` / `decom` transitions; auto-resolves after 7 d)

- 📦 **Service catalog** — bundles network resources into a customer-deliverable.
  - `NetworkService` is one row per thing the operator delivers; polymorphic `NetworkServiceResource` join row binds to VRF / Subnet / IPBlock / DNSZone / DHCPScope / Circuit / Site / OverlayNetwork
  - **Kinds in v1**: `mpls_l3vpn` (with hard at-most-one-VRF rule + soft warnings for missing VRF, fewer than 2 edge sites, edge subnet's enclosing block in a different VRF) and `custom`. `sdwan` lit up alongside the SD-WAN overlay roadmap. Future kinds reserved in the column: `mpls_l2vpn` / `vpls` / `evpn` / `dia` / `hosted_dns` / `hosted_dhcp`
  - **Kind-aware `/summary` endpoint** — L3VPN view returns canonical VRF + edge sites + edge circuits + edge subnets + warnings
  - **Reverse lookup** — `GET /by-resource/{kind}/{id}` returns every service referencing a given resource
  - **Alert rules** — `service_term_expiring` (mirrors circuit shape), `service_resource_orphaned` (sweep over join rows whose target was deleted; auto-resolves on detach)
  - List page at `/network/services` (bulk-action table) + tabbed editor modal (General / Resources / Term + cost / Notes / Summary)

- 🌐 **SD-WAN overlays** — vendor-neutral source of truth for overlay topology and routing-policy intent.
  - Vendor config push (vManage / Meraki Dashboard / FortiManager / Versa Director) and real-time path telemetry are **explicitly out of scope** — those stay NCM / observability concerns
  - Data model: `overlay_network` (six kinds: sdwan / ipsec_mesh / wireguard_mesh / dmvpn / vxlan_evpn / gre_mesh), `overlay_site` (m2m binding sites with role hub / spoke / transit / gateway, edge device, loopback subnet, ordered `preferred_circuits` jsonb — first wins, fall through on outage), `routing_policy` (priority + match-kind + match-value + action + action-target + enabled), `application_category` (curated SaaS catalog seeded with 33 well-known apps — Office365 / Teams / Zoom / Slack / Salesforce / GitHub / AWS / Azure / GCP / SIP voice / OpenAI / Anthropic / …)
  - **`/topology` endpoint** — nodes (sites + roles + device + loopback + preferred-circuits) + edges (site pairs whose `preferred_circuits` lists overlap; `shared_circuits` is the intersection so the UI can colour by transport class) + policies
  - **`/simulate` endpoint** — pure read-only what-if; body specifies `down_circuits`, response shows per-site fallback resolution + per-policy effective-target with `impacted` flag and human-readable note
  - List page at `/network/overlays` + detail page with five tabs: Overview / Topology (SVG circular layout with role-coloured nodes + transport-coloured edges) / Sites / Policies (priority-ordered with per-kind editors) / Simulate

### Discovery & visibility

- 📡 **SNMP discovery** — v1 / v2c / v3 polling via standard MIBs.
  - MIBs walked: IF-MIB, IP-MIB, Q-BRIDGE-MIB, LLDP-MIB
  - Surfaces interfaces, ARP, FDB, LLDP neighbours
  - Per-IP switch-port + VLAN visibility in IPAM
  - Neighbours tab on each device

- 🎯 **Nmap scanner** — on-demand scans from the browser.
  - Per-IP "Scan with Nmap" launcher · per-subnet "Scan with nmap"
    in the IPAM Tools dropdown (pre-fills CIDR target +
    `subnet_sweep` preset) · standalone `/tools/nmap` page for
    ad-hoc targets
  - Presets: quick, service+version, **service+OS**, OS,
    default-scripts, **subnet_sweep** (-sn ping sweep capped at
    /16 worth of hosts), UDP top-100, aggressive, custom
  - Live SSE output streams while the scan runs; results render
    single-host or multi-host (CIDR) summaries
  - **Stamp alive hosts → IPAM** action on a CIDR scan claims
    responding IPs as `discovered` rows with `last_seen_at` set;
    `Copy alive IPs` for clipboard handoff
  - History page with bulk-delete (cancels in-flight scans + drops
    terminal ones) and a 3-tab right panel (Live / History / Last
    result) that auto-switches as a scan completes

- 🛰 **Device profiling** — answer "what kind of device is on every IP" without
  asking. Two layers feeding one consolidated panel in the IP detail modal.
  - **Passive — DHCP fingerprinting.** scapy `AsyncSniffer` thread on the DHCP
    agent reads option-55 / option-60 / option-77 / client-id from every
    DISCOVER + REQUEST, batches per-MAC, ships to the control plane
  - **Enrichment — fingerbank.** Optional API key in Settings → IPAM →
    Device Profiling turns raw signatures into Type / Class / Manufacturer
    (`HP iLO`, `Aruba AP`, `Cisco IP Phone 8841`, `iOS device`, …); 7-day
    cache; works offline-degraded
  - **Active — auto-nmap on new DHCP lease.** Per-subnet opt-in toggle picks a
    preset; refresh-window dedupe (default 30 days) means churning Wi-Fi
    leases don't fan out; per-subnet 4-scan concurrency cap
  - **"Re-profile now"** button on the IP detail modal for ad-hoc rescan
  - Default-off everywhere — IDS-aware (nmap is loud; passive collection
    needs `cap_add: NET_RAW`)

- 🎨 **Dashboard-at-a-glance** — nine sub-tabs: Overview / IPAM / DNS / DHCP / **Network** (ASN drift + RPKI expiry + circuit alerts + service orphans) / **Integrations** (per-mirror counts + last-sync staleness) / **Security** (lockout state + active sessions + audit-chain status + MFA enrolment) / **Compliance** (PCI / HIPAA / internet-facing flag counts) / **Conformity** (per-framework status + auditor PDF download).
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
  - **UniFi Network** — per-controller sites, networks (VLAN ID + CIDR → IPAM subnets), connected clients (hostname + MAC + IP); one row per controller
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
  - **Scoped API tokens** — `scopes` JSONB column on `api_token` lists the resource_types the token is allowed to touch (vs. inheriting all of the user's permissions). Permission-name granularity (`subnet:read`, `subnet:admin`, `*` for full inheritance). Authorization enforces scope intersection — token can do at most what the scope set allows AND what the user has permission for.

- 🛡 **TOTP MFA for local users** — second factor on top of password.
  - Enrolment flow: Settings → Security → "Enable MFA" → scan QR (`pyotp` + `qrcode` libraries) → enter 6-digit code → backup codes shown once
  - Login flow gains a second step when MFA is enabled — JWT pre-token issued on username+password, exchanged for full token after TOTP code or backup code accepted
  - Backup codes are single-use and persisted hashed
  - Admin can force-disable MFA per user (audit-logged)

- 🏷 **Subnet classification tags** — first-class compliance flags on every subnet.
  - `pci_scope` / `hipaa_scope` / `internet_facing` boolean columns, each individually indexed (partial index `WHERE col = true`) so the auditor's "show me every PCI subnet" filter hits an index without competing
  - List filters across the IPAM page + the API
  - Compliance dashboard at `/admin/compliance` shows the three buckets side-by-side
  - Feeds the compliance-change alert + conformity policy filters described below

- 🛂 **Compliance change alerts** — reactive: catch every mutation against PCI / HIPAA / internet-facing scope.
  - New `compliance_change` rule type with two params: `classification` (which Subnet flag the rule watches) and `change_scope` (`any_change` / `create` / `delete`)
  - Audit-log scanner runs on the existing 60 s alert tick. Watermark column on the rule baselines to `now()` on first run so historical audit history doesn't retro-page operators when a rule is first enabled
  - Resolves IP-address / DHCP-scope audit rows back to their parent subnet for classification lookup; deletes fall back to `audit_log.old_value.subnet_id` so a delete still resolves the originating subnet
  - One event per matching audit row, auto-resolves after 24 h, fans through the existing audit-forward syslog / webhook / SMTP targets
  - Three disabled seed rules ship at first boot: PCI scope changes, HIPAA scope changes, internet-facing scope changes — operator opts in by toggling enabled

- 📑 **Conformity evaluations** — proactive: prove steady state and produce the auditor PDF.
  - Declarative `ConformityPolicy` rows pin a `check_kind` against a target set (subnet / IP address / DNS zone / DHCP scope / platform). Beat-driven engine ticks every 60 s and runs every enabled policy on its `eval_interval_hours` cadence (default 24 h). On-demand re-eval via `POST /conformity/policies/{id}/evaluate`
  - 6 starter check kinds:
    - `has_field` — non-empty value on a named target column (e.g. PCI subnet must have `customer_id`)
    - `in_separate_vrf` — subnet's effective VRF holds only classification-matched siblings (no PCI ↔ non-PCI mixing)
    - `no_open_ports` — latest nmap scan within N days didn't expose forbidden ports (`warn` when no recent scan; never silent-pass)
    - `alert_rule_covers` — at least one enabled alert rule of the named type covers this scope (positive coverage signal — confirms the reactive #105 channel is wired)
    - `last_seen_within` — IP / subnet recency check (catches rows that should be decommissioned)
    - `audit_log_immutable` — platform-level positive-presence signal for the auditor checkbox
  - 8 disabled seed policies covering PCI-DSS / HIPAA / SOC2: PCI dedicated VRF, PCI owner_assigned, PCI no admin ports, PCI alert coverage, PCI no stale IPs, HIPAA dedicated VRF, internet-facing alert coverage, audit log immutable
  - `pass→fail` transitions emit `AlertEvent` rows against the policy's wired alert rule when set, so conformity drift surfaces in the existing alerts dashboard
  - Append-only `ConformityResult` history indexed twice (by policy and by resource) so both natural drilldowns hit an index — answers "every result for this policy" + "every policy that touched this resource" in O(log n)
  - Auditor-facing **PDF export** via `reportlab` — per-framework summary table, per-policy section with pass / warn / fail counts, enumerated failing rows with diagnostic JSON pretty-printed beneath, trailer with a SHA-256 hash over `(result_id, status)` tuples so the auditor can verify post-generation tampering. `GET /conformity/export.pdf` with optional `?framework=` filter
  - New `conformity` permission resource type plus two new built-in roles: **Auditor** (read-only) suitable for an external auditor account, **Compliance Editor** (admin) for the team that authors and tunes policies
  - Frontend `/admin/conformity` page with per-framework summary cards, policies table (toggle / re-eval / edit / delete inline), filterable results panel with diagnostic JSON drill-in. Platform Insights gains a Conformity card with deep-link

- 🗑 **Soft-delete + 30-day Trash** — accidental deletes are recoverable; the `Delete` button moves rows to a holding area, not the void.
  - Scope: `IPSpace`, `IPBlock`, `Subnet`, `DNSZone`, `DNSRecord`, `DHCPScope` rows inherit a `SoftDeleteMixin` (`deleted_at`, `deleted_by_user_id`, `deletion_batch_id`). IP addresses are intentionally NOT soft-deletable — they cascade-delete with their parent subnet, and the parent subnet is the recoverable unit
  - **Global ORM filter** — a `do_orm_execute` event listener injects `Model.deleted_at IS NULL` into every SELECT touching one of these models, so the rest of the codebase doesn't need to remember. Callers that need to see the trash opt in via `execution_options(include_deleted=True)`
  - **Cascade-aware restore** — when you delete a subnet its DHCP scopes are stamped under the same `deletion_batch_id`; one click on Restore brings the whole batch back atomically, with a pre-flight conflict check (rejects 409 when a live row would clash on a uniqueness key)
  - Admin page at `/admin/trash` lists soft-deleted rows newest-first with type / since / substring filters and "Restore" / "Delete permanently" per row. Sidebar entry under Admin
  - **Nightly purge** — `trash_purge` Celery beat task hard-deletes rows older than `PlatformSettings.soft_delete_purge_days` (default 30; set 0 to disable forever). The retention window is operator-tunable in Settings → Security
  - Endpoints: `GET /admin/trash` · `POST /admin/trash/{type}/{id}/restore` · `DELETE /admin/trash/{type}/{id}` (hard-delete a soft-deleted row before the purge sweep)

- 📋 **Audit log + tamper-evident hash chain** — append-only, SHA-256 chained, machine-verifiable.
  - Every mutation across IPAM / DNS / DHCP / Network / auth / ownership / integrations writes an `AuditLog` row before the response is returned. Filterable in the UI by user / action / resource type / time range; full row diff (`old_value` / `new_value` / `changed_fields` JSONB) so an audit shows you exactly what changed
  - **Hash chain** — each row carries `seq` (monotonically-increasing position), `prev_hash` (the previous row's hash), and `row_hash = sha256(prev_hash || canonical_json(row))`. A `before_flush` SQLAlchemy listener takes a Postgres transaction-scoped advisory lock so concurrent transactions can't interleave their "fetch previous hash, hash my row, write it" sequence — you can't fork the chain by racing
  - **Verifier** — `verify_chain` walks the table in `seq` order, recomputes the hash for each row, and returns the first break with `reason=row_hash_mismatch` (someone edited the row's content) or `reason=prev_hash_mismatch` (someone deleted or inserted a row mid-stream). One verification pass shows the offending row + the position in the chain
  - **Conformity hookup** — the `audit_log_immutable` conformity check kind runs the verifier on its scheduled tick and emits a `pass` / `fail` result, so the auditor's PDF export carries a positive-presence signal that nothing has been tampered with since the last evaluation
  - **Backfill migration** — `d92f4a18c763_audit_chain_hash` populates `seq`, `prev_hash`, and `row_hash` for every existing row in chronological order on upgrade, so the chain is unbroken from day one

- 💾 **Backup + restore** — full-system snapshot with passphrase-wrapped secrets and 8 destination kinds.
  - **Format** — single `.zip` archive carrying `manifest.json` (app version, schema head, hostname, dump format), `database.dump` (custom-format `pg_dump`), `secrets.enc` (PBKDF2-HMAC-SHA256 + AES-256-GCM envelope wrapping the install's `SECRET_KEY`), and `README.txt`. The operator passphrase NEVER lands on disk or in the API logs
  - **Eight destination kinds** — `local_volume` (filesystem path mounted as a docker / k8s volume), `s3` (AWS + S3-compatible: MinIO, Wasabi, Backblaze B2, Cloudflare R2 — via `endpoint_url`), `scp` (SFTP with password OR PEM private key, three host-key check modes), `azure_blob` (shared-key OR connection-string), `smb` (NTLM with optional domain + SMB3 encryption), `ftp` (plain ftp / ftps_explicit / ftps_implicit + passive/active + verify_tls toggle), `gcs` (service-account JSON key), `webdav` (Nextcloud / ownCloud / mod_dav / IIS WebDAV via PUT/GET/PROPFIND/DELETE — no SDK dep). Same `BackupDestination` ABC + module registry; the kind picker reflects on `GET /backup/targets/kinds` so adding a new driver requires no frontend changes
  - **Scheduled cron + retention** — 5-field UTC cron with friendly presets (hourly / 6h / 12h / daily 02:00 / 04:00 / weekly Sun 03:00 / monthly 1st 03:00) + custom; retention as `keep_last_n` OR `keep_for_days`; per-target last-run state surfaced inline; 60s beat sweep with `last_run_status='in_progress'` per-target mutex
  - **Selective restore** — operators tick which sections to restore (IPAM only / DNS only / etc.) on both upload-restore and restore-from-destination flows. 17-section catalog mapping all 110 schema tables; volatile sections (DHCP leases, DNS query log, DHCP activity log, nmap scan history, metric samples) skipped by default. TRUNCATE … RESTART IDENTITY CASCADE + `pg_restore --data-only --disable-triggers --table=…`; `platform_internal` (alembic_version + oui_vendor) always rides along
  - **Cross-install secret rewrap** — restoring onto an install with a different `SECRET_KEY` rewraps every Fernet-encrypted column (22 columns across 16 tables + the `backup_target.config` JSONB blob). Same-install restores short-circuit. Closes the manual `SECRET_KEY` copy step that Phase 1 needed
  - **Alembic upgrade-on-restore + drift recovery** — restore detects schema-version skew and runs `alembic upgrade head` automatically when the source is on an older head. Drift-recovery branch handles the case where the dump's alembic_version row is stale relative to the dump's own schema (operator stamped head later, then restored an older backup): canonical "table already exists" error pattern is detected and recovered via `alembic stamp head`
  - **Exclude-secrets diagnostic mode** — checkbox on the build form switches to plain-format dump and post-processes the SQL text in memory to scrub every Fernet-encrypted column + `__enc__:`-prefixed JSONB field. Live database is never touched. For sharing snapshots with support / consultants without leaking integration credentials
  - **Restore from any archive at any destination** — pick destination → expand drawer → click Restore icon. Driver fetches the bytes via `download(filename)`; standard restore path takes over. Plus `GET /backup/targets/{id}/archives/{filename}/download` for proxy-download (operator pulls a remote archive without ever holding the destination's credentials) and `…/archives/latest/download` for one-shot "give me the newest" automation
  - **Audit + observability** — every backup creates / fails / restore-performed lands an audit row; `system.backup_completed` / `system.backup_failed` / `system.restore_performed` typed events fire via the existing webhook event-outbox + HMAC-signed POST + retry pipeline
  - Endpoints: `POST /backup/create-and-download` · `POST /backup/restore` · `POST /backup/targets` (CRUD + run-now + test) · `POST /backup/targets/{id}/archives/restore` · `GET /backup/targets/{id}/archives/{filename}/download` · `GET /backup/targets/{id}/archives/latest/download`. UI lives at **Administration → Backup**

- 🧹 **Factory reset** — per-section "wipe back to defaults" surface for superadmins.
  - 12 sections mapped from the operator's mental model: IPAM · DNS · DHCP · Network modeling · Integrations · AI/Copilot · Compliance · Tools · Observability logs · Auth+RBAC · Settings+branding · Everything. Per-section confirm phrase (`DESTROY-IPAM` / `DESTROY-DNS` / … / `FACTORY-RESET-ALL`) typed exactly to commit
  - Three dispatch kinds: `truncate` (9 sections — straight `TRUNCATE … RESTART IDENTITY CASCADE`), `auth_rbac` (partial wipe preserving the calling user, every other superadmin, and built-in roles), `settings_reset` (DELETE platform_settings; recreated with model defaults). Tables intentionally untouchable: `alembic_version`, `oui_vendor`, `backup_target`, `feature_module`, `event_outbox`, `internal_error`, `audit_forward_target` — the schema head, OUI cache, recovery path, module toggles, and audit-forward shipping channels survive every reset
  - Hard guardrails server-side: superadmin gate · fresh bcrypt password re-check (NOT bearer-token check) · exact-match per-section confirm phrase · refuses 409 when any backup target is mid-run · Redis lock against concurrent resets · 6-hour cooldown · audit anchor written via fresh AsyncSessionLocal post-truncate so the trail of evidence survives an `audit_log` wipe · `system.factory_reset` typed event fans through the existing webhook outbox
  - **Pre-flight backup as warn-only with override** — if no enabled backup target exists, `POST /system/factory-reset/execute` returns 412 unless the operator passes `acknowledge_no_backup=true`. The Backup admin tab surfaces the warning + checkbox up front
  - UI lives as a third tab on the Backup admin page (after Manual + Destinations) — backup snapshots state, factory reset wipes it, two ends of the same lifecycle. Per-section cards in a 2-col grid + red-bordered "Reset everything" card. Modal gates the password field on a green-border phrase match

- 🤖 **Operator Copilot** — AI assistant grounded in your live IPAM / DNS / DHCP / Network data. Hosted-API or fully on-prem (Ollama). One provider config, **91 tools**, real conversations about your network.

  **Provider + model**

  - **Multi-vendor** — OpenAI, Anthropic (Claude), Azure OpenAI, Google Gemini, plus OpenAI-compat (Ollama, OpenWebUI, vLLM, LM Studio, llama.cpp server, LocalAI, Together, Groq, Fireworks). Add multiple providers in priority order; orchestrator picks the highest-priority enabled one
  - **Automatic failover chain** — on transient failure (5xx / timeout / rate-limit) the orchestrator walks remaining providers; first successful chunk wins. Permanent errors (4xx / auth) surface immediately
  - **Per-provider system prompt override** — admin-editable inside the AI Provider modal; baked-in default is also viewable inline so you can fork it. Snapshotted onto each session at creation so live edits don't break in-flight chats
  - **Per-provider tool allowlist** — new "Tools" tab on the AI Provider modal, category-grouped checkbox list with "write" badges on `propose_*` rows. NULL = "use whatever the registry has"; non-empty list pins exactly those tools. Right call for small Ollama models that struggle with 35 tools, kiosk providers limited to read-only, and per-provider compliance posture
  - **Reasoning-channel fallback** — `qwen3.5` / DeepSeek-R1 / o1 / o3 family that route their answer to `reasoning` instead of `content` are handled transparently by the driver
  - **Ollama context-window forwarding** — driver forwards `options.num_ctx` / `num_predict` / `extra_body` so Ollama respects the configured context window. Operators can also set `OLLAMA_CONTEXT_LENGTH` env var on the server side (recommended); without one or the other, Ollama silently truncates to 2048 tokens and small models hallucinate tool names from a half-cut tool list

  **Tool registry (91 tools)**

  Each tool is gated by both the `feature_module` it belongs to (`integrations.unifi` off → UniFi tool disappears from the registry) and an admin-controlled per-tool allowlist at **Admin → AI → Tools**, so operators can trim what the model can see without touching code. Every tool can also be flipped per-provider via the AI Provider modal's Tools tab — the right call for small Ollama models that struggle with 91 tool schemas.

  - **IPAM (10)** — `list_ip_spaces`, `list_ip_blocks`, `list_subnets`, `get_subnet_summary`, `find_ip` (returns MAC + **vendor**), `find_by_tag`, `count_ipam_resources`, `find_devices_by_vendor`, `count_devices_by_vendor`, `propose_create_ip_address`. Name-or-UUID resolution on `space_id` / `block_id` so the model can pass `"home"` directly without a UUID-lookup hop
  - **DNS (10)** — `list_dns_server_groups`, `list_dns_zones`, `list_dns_views`, `list_dns_records` (cross-zone substring search), `list_dns_pools` (GSLB pools + per-member health), `list_dns_blocklists` (RPZ rows + sync state), `query_dns_records`, `forward_dns`, `reverse_dns`, `propose_create_dns_record`
  - **DHCP (11)** — `list_dhcp_server_groups`, `list_dhcp_servers`, `list_dhcp_scopes`, `list_dhcp_pools` (dynamic / excluded / reserved), `list_dhcp_statics` (MAC → IP reservations), `list_dhcp_client_classes`, `list_dhcp_option_templates`, `list_pxe_profiles`, `list_dhcp_mac_blocks`, `find_dhcp_leases` (returns MAC + **vendor**), `propose_create_dhcp_static`
  - **Network modeling (17)** — `list_asns` + `get_asn` (RDAP holder, RPKI ROAs, BGP peerings), `list_domains` (registrar / expiry / DNSSEC / NS drift), `list_vrfs` (RDs + RTs + ASN linkage), `list_circuits` (transport + bandwidth + cost + endpoints), `trace_circuit_impact` (down-circuit blast radius across services + sites), `list_network_services` + `get_network_service_summary` (service-catalog deliverables — MPLS L3VPN, etc.), `list_overlay_networks` + `get_overlay_topology` (SD-WAN sites + policies), `list_application_categories` (RFC 4594 DSCP catalog), `list_network_devices`, `find_switchport`, `ping_host`, `list_nmap_scans`, `get_nmap_scan_results`, `propose_run_nmap_scan`
  - **Ownership (4)** — `list_customers`, `list_sites`, `list_providers`, `get_customer_summary` (per-customer rollup of subnets / blocks / spaces / circuits / services / ASNs / zones / domains / overlays in one call)
  - **Admin (3)** — `list_users`, `list_groups`, `list_roles` (superadmin-gated inline; the orchestrator returns an error dict for non-admins)
  - **Backup + factory-reset (3)** — `list_backup_targets` (every configured destination with last-run state, schedule, retention; `config` blob deliberately omitted so destination credentials stay out of the LLM context), `list_backup_archives_at_target` (calls the driver's `list_archives` so the result matches the Backup admin Archives drawer), `find_backup_audit_history` (windowed timeline of backup_created / target-run-success/failed / backup_restored / factory_reset_performed audit rows). All three superadmin-gated. **No `propose_*` writes by design** — restore + factory-reset are password-gated + confirm-phrase-gated, an LLM intermediary in "should I restore?" adds friction without value
  - **Integration mirrors (5)** — `list_kubernetes_targets`, `list_docker_targets`, `list_proxmox_targets`, `list_tailscale_targets`, `list_unifi_targets` (each tagged with the matching `integrations.*` module so disabling the integration removes the tool in lock-step with the sidebar entry; credentials never enter the response)
  - **Ops, observability + audit (18)** — `list_alerts`, `list_alert_rules`, `get_audit_history`, `audit_walk` (paginated chronology), `current_state` (platform health snapshot), `query_dns_query_log`, `query_dhcp_activity_log`, `query_logs`, `get_dns_query_rate` / `get_dhcp_lease_rate` (24-bucket timeseries), `global_search`, `lookup_whois_asn` / `lookup_whois_domain` / `lookup_whois_ip`, `tls_cert_check`, `help_write_permission`, `propose_create_alert_rule`, `propose_archive_session`
  - **Write proposals** (Apply-gated, default-off, double-validated in the Tool Catalog UI) — every `propose_*` returns a planned diff first; the operator clicks Apply in the chat drawer to actually write. Apply lands an audit row with `via=ai_proposal` so the trail distinguishes operator vs. AI-driven mutations

  **MCP integration**

  - **MCP HTTP endpoint** at `/api/v1/ai/mcp` exposes the full read-only tool set so external MCP clients (Claude Desktop, Cursor, Cline, Continue.dev) can drop SpatiumDDI in as a tool source — no Copilot UI required

  **Chat surface**

  - **Floating chat drawer** — slide-in panel with sessionStorage-backed state (close + reopen lands on the same conversation, draft text survives), Markdown + GFM tables + code blocks, blinking caret during streaming. Opens via the floating "Ask AI" button, the Cmd-K palette entry, or the per-row "Ask AI about this" affordances on every IPAM / DNS / DHCP / alerts row
  - **Per-message footer** — token-count + copy + info popover (sent timestamp, tokens in / out, latency, role) on every assistant reply; matches the OpenWebUI footer pattern
  - **Daily token + cost chip** — live in the drawer header; refetches automatically when you delete chats
  - **Multi-select session history** — checkbox column on every history row; "Select all" + "Delete N" + "Delete all" toolbar; bulk delete fans out per-id and updates the daily tally on success
  - **Live nmap proposal results** — when a `propose_run_nmap_scan` Apply lands, the proposal card polls `GET /nmap/scans/{id}` every 2 s and renders the full results table (alive flag, open ports + service / version, OS guess, CIDR-host list) inline once status flips to `completed`
  - **Custom prompts library** — operator-curated templates persisted per platform; built-in starter pack (Find unused IPs, Audit recent changes, Summarize subnet utilization, Triage open alerts)
  - **Cmd-K palette "Ask AI" entry** — top entry in the global palette, pre-fills with the current page's context

  **Reliability + safety**

  - **Per-turn dedup loop guard** — if the model emits the exact same tool call twice in a turn (a known failure mode of smaller open-weight models), the orchestrator skips re-execution and feeds back a synthetic warning telling the model the result is already in context
  - **Tool-not-found auto-correction** — when the model hallucinates a tool name, the error response includes the full list of real tool names + a hint, so the next iteration self-corrects rather than giving up
  - **Scope rules in the system prompt** — Copilot is explicitly *not* a general-purpose coding assistant; refuses code-generation requests outside narrow platform-config contexts
  - **Audit everything** — every tool call, every Apply, every chat turn writes through to the append-only audit log

  **Token / cost observability + per-user caps**

  - Per-request usage tracked in `ai_chat_message`; pricing table covers the major hosted models; per-user daily token + cost caps; AI usage card on Platform Insights aggregates the last 7 days by provider + model
  - **Daily digest** — optional 0900 local Operator Copilot summary fired through audit-forward / SMTP / webhook channels

  **Self-host with Ollama in five minutes**

  ```bash
  # On the Ollama host:
  docker run -d --gpus all -p 11434:11434 \
    -e OLLAMA_CONTEXT_LENGTH=32768 \
    -e OLLAMA_KEEP_ALIVE=30m \
    -v ollama:/root/.ollama --name ollama ollama/ollama:latest

  docker exec ollama ollama pull qwen3.5:latest
  ```

  Then in SpatiumDDI: **Admin → AI Providers → New** → `kind: openai_compat`, `base_url: http://<ollama-host>:11434/v1`, `default_model: qwen3.5:latest`, save → click the floating "Ask AI" button. `OLLAMA_CONTEXT_LENGTH` is **required** — Ollama defaults to 2048 tokens which silently truncates the system prompt + tool schemas; the result is a model that hallucinates tool names. We recommend `qwen3.5:latest` for tool calling on the small open-weight class.

- 🔔 **Alerts + audit forwarding** — multi-target delivery with pluggable wire formats.
  - Rule-based alerts framework (subnet utilization, server unreachable)
  - Multi-target syslog (UDP / TCP / TLS), HTTP webhook, SMTP email, Slack / Teams / Discord chat
  - Wire formats: RFC 5424 JSON, CEF, LEEF, RFC 3164, JSON lines
  - Per-target filters

- 🪝 **Typed-event webhooks** — curated automation surface for downstream consumers.
  - 96 typed events covering every resource × verb (e.g. `subnet.created`, `dns.zone.updated`, `ip.allocated`)
  - HMAC-SHA256 signed POSTs with reserved `X-SpatiumDDI-*` headers
  - Outbox-backed at-least-once delivery with exponential backoff + dead-letter
  - Per-subscription manual retry, custom headers, and one-time secret reveal

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

### Try the demo in GitHub Codespaces

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/spatiumddi/spatiumddi)

One click brings up a full SpatiumDDI stack in a fresh Codespace, builds the images from `main`, runs migrations, and seeds realistic IPAM / DNS / DHCP / network-modeling demo data so every screen has something to look at. Sign in with **`admin / admin`**.

The demo Codespace runs in **DEMO_MODE** — abusable surfaces are server-side locked: nmap, the AI Copilot, every read-only integration mirror (Kubernetes / Docker / Proxmox / Tailscale / UniFi), webhook subscriptions, audit-forward / SMTP, backup target creation, factory reset, and password change all return 403. IPAM / DNS / DHCP CRUD on the seeded data stays open so you can play with it.

Cold start is ~5–8 minutes (image build) on a 4-core machine; the Codespace's free-tier hours come from your own GitHub account, and trashing the data only affects your own copy. To start fresh, delete the Codespace and click the badge again.

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

### Seeding demo data

To populate a fresh install with a representative dataset (DNS group + zones + records, DHCP scope + pool, ASNs + BGP peerings, VRFs, IP space + blocks + subnets + ~30 IPs, SNMP-stubbed network devices, VLANs, domains, custom fields, IPAM templates, alert rules, and a few shared AI prompts) — useful for screenshots, demos, or kicking the tyres on the AI Copilot:

```bash
python3 scripts/seed_demo.py http://localhost:8000 admin <your-password>
```

Idempotent — re-running the seed swallows 409s and PATCHes existing rows so foreign-key pointers converge as new entities are added in later releases. Out of scope: AI providers (secrets), webhooks (per-deployment URLs), audit-forward targets, API tokens — those need real credentials from you.

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
SPATIUMDDI_VERSION=2026.05.03-1

# Then:
docker compose pull
docker compose run --rm migrate
docker compose up -d
```

Bump the pinned version when you're ready to upgrade and re-run the same three commands.

**Notes:**
- **Take a backup before upgrading.** Click `Administration → Backup → Build + download` and save the archive somewhere off the host (or pick a configured remote destination's `Run now`). If the upgrade goes sideways the archive is your one-click rollback. Eight destination kinds ship today — local volume / S3 / SCP / Azure Blob / SMB / FTP / GCS / WebDAV — plus selective per-section restore + cross-install secret rewrap + alembic upgrade-on-restore. See the in-app page for the security-model details.
- `docker compose run --rm migrate` runs alembic against your current schema — safe to run every upgrade. It exits as a no-op if there are no new migrations.
- Downgrades are **not** supported. Database migrations are forward-only; if a release introduces a schema change you can't roll back to a tag that predates it without restoring a database backup. Always snapshot Postgres before a major-version upgrade you're not sure about.
- Watch the **CHANGELOG.md** entry for your target version for any release-specific upgrade notes (e.g. "operators on Kea HA must read this before upgrading").
- The sidebar shows the running version in the bottom-left corner and surfaces an `update available` badge when a newer GitHub release exists — the version probe runs hourly.

### Running the built-in BIND9 / PowerDNS / Kea containers

The managed-service containers ship under Compose profiles — opt in when you want them:

```bash
docker compose --profile dns-bind9 up -d                 # BIND9 (default)
docker compose --profile dns-powerdns up -d              # PowerDNS (issue #127)
docker compose --profile dns-bind9 --profile dhcp up -d  # BIND9 + DHCP
```

`--profile dns` still works as a back-compat alias for `dns-bind9`. Pick whichever DNS driver matches the server group on the control plane — every PowerDNS-only feature (DNSSEC sign/unsign, ALIAS, LUA, catalog zones) gates on every server in the group running the powerdns driver.

Or set `COMPOSE_PROFILES=dns-bind9,dhcp` in your `.env` so plain `docker compose up -d` enables both automatically.

That starts `dns-bind9` bound to host port `1053` (udp + tcp), or `dns-powerdns` on `5453`. The agent registers with the control plane automatically using `DNS_AGENT_KEY` from your `.env` and appears in the UI under **DNS → Server Groups**.

> **Upgrading from a release that used port 5353?** The DNS host port default changed from `5353` to `1053` in release `2026.05.08-1` because 5353 is the well-known mDNS port and collides with avahi (default-on in Ubuntu desktop / Fedora / most lab distros). Either point your clients at the new port (`dig -p 1053 …`), or pin the old behaviour with `DNS_HOST_PORT=5353` in your `.env` and recreate the container.

Create a zone + record in the UI, then verify with `dig`:

```bash
dig @127.0.0.1 -p 1053 <your-record>.<your-zone> A +short
dig @127.0.0.1 -p 1053 -x <your-ip> +short    # reverse (PTR)
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
| [Deployment Topologies](docs/deployment/TOPOLOGIES.md) | Six reference topologies — single VM through HA cloud + on-prem hybrid — with diagrams |
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
| Phase 4 | OS appliance, Terraform provider, SAML, backup/restore, ACME | 🔄 SAML + full backup/restore + factory-reset all landed (8 destination kinds, selective restore, cross-install secret rewrap, alembic-upgrade-on-restore); appliance + providers + ACME embedded client pending |
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
