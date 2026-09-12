---
title: Documentation
description: SpatiumDDI documentation — setup, architecture, feature specs, deployment, driver internals and the privacy statement.
---

# SpatiumDDI Documentation

SpatiumDDI is an open-source, self-hosted DNS, DHCP and IP Address Management platform. These pages are its documentation: every specification, deployment guide and driver internal, grouped by what you're trying to do. Source lives in the [`docs/` directory](https://github.com/spatiumddi/spatiumddi/tree/main/docs) of the main repository and is republished on every push to `main`.

## Start Here

- [Getting Started](GETTING_STARTED.md) — recommended setup order (servers → zones/scopes → subnets → addresses)
- [Troubleshooting](TROUBLESHOOTING.md) — recovery recipes: deleted agent rows, password reset, refused subnet deletes, and more
- [Windows Server Setup](deployment/WINDOWS.md) — WinRM, service accounts, firewall — Windows-side checklist for agentless DNS + DHCP

## Architecture & Design

- [Architecture](ARCHITECTURE.md) — system topology, component relationships, HA design
- [Deployment Topologies](deployment/TOPOLOGIES.md) — six reference production layouts with sizing notes
- [Data Model](DATA_MODEL.md) — database models, relationships, field definitions
- [API Conventions](API.md) — REST API conventions, pagination, error format
- [Permissions](PERMISSIONS.md) — RBAC grammar, builtin roles, scope delegation
- [Observability](OBSERVABILITY.md) — logging, metrics, health dashboard
- [Fleet Firewall design](design/FLEET_FIREWALL.md) — declarative per-role appliance firewall policy compiled to nftables
- [Shipped roadmap items](SHIPPED.md) — the full design context behind every feature that has landed

## Feature Specs

- [IPAM](features/IPAM.md) — IP spaces, blocks, subnets, addresses, custom fields
- [DNS](features/DNS.md) — zones, records, views, server groups, blocking lists, encrypted transports, Windows DNS (Path A + B)
- [DHCP](features/DHCP.md) — servers, scopes, pools, static assignments, leases, Windows DHCP (Path A)
- [Auth & Permissions](features/AUTH.md) — LDAP, OIDC, SAML, RADIUS, TACACS+, roles, API tokens
- [ACME DNS-01 Provider](features/ACME.md) — acme-dns-compatible surface for LE / public-CA cert issuance against SpatiumDDI-managed zones
- [Migration](features/MIGRATION.md) — one-shot importers for BIND9 / Windows / PowerDNS / Technitium DNS, Kea / Windows / ISC DHCP, and NetBox IPAM, plus the guided Windows cutover
- [Integrations](features/INTEGRATIONS.md) — read-only Kubernetes, Docker, Proxmox VE, Tailscale, cloud and firewall mirrors into IPAM; per-integration setup, mirror semantics, dashboard surface
- [BGP Looking Glass](features/LOOKING_GLASS.md) — receive-only BGP collector (GoBGP) peering with the operator's routers; Sessions + Routes grid, RPKI status at ingest
- [Vertical network awareness](features/VERTICALS.md) — AV-over-IP flow descriptors, BACnet/IP device-instance registry, Industrial-OT inventory + Purdue zoning, DICOM AE Title registry, and the fragile-device do-not-probe flag
- [E911 dispatchable location](features/E911.md) — SpatiumDDI as a Location Information Server: Emergency Response Locations as RFC 5139 civic addresses, switch-port / subnet / VLAN / device bindings, HELD and DHCP option delivery
- [System Admin](features/SYSTEM_ADMIN.md) — config, health dashboard, notifications, backup/restore, service control

## Deployment

- [Docker Compose](deployment/DOCKER.md) — quick start, profiles, TLS, HA
- [Windows Server](deployment/WINDOWS.md) — connecting to Windows DNS / DHCP over WinRM + RFC 2136
- [Kubernetes](deployment/KUBERNETES.md) — umbrella Helm chart, HPA, Ingress / LoadBalancer, CloudNativePG + Redis Sentinel HA
- [Bare Metal](deployment/BAREMETAL.md) — Docker Compose on a host, Patroni HA Postgres overlay, OS appliance path
- [OS Appliance](deployment/APPLIANCE.md) — appliance image build, installer, A/B slot upgrades
- [DNS Agent](deployment/DNS_AGENT.md) — agent protocol, auto-registration, config sync

## Driver Internals

- [DNS Drivers](drivers/DNS_DRIVERS.md) — BIND9 + PowerDNS + Technitium + Windows DNS driver internals
- [DHCP Drivers](drivers/DHCP_DRIVERS.md) — Kea + Windows DHCP driver internals

## Development & Testing

- [Development Guide](DEVELOPMENT.md) — coding standards, test requirements, CI
- [Performance Testing](PERFORMANCE_TESTING.md) — the 24-hour university-scale load and soak plan behind the `perf/` suite
- [Performance-test appliance setup](PERF_APPLIANCE_SETUP.md) — preparing a clean single-node appliance for the suite to drive

## Project

- [Privacy](PRIVACY.md) — no telemetry, no analytics, no phone-home; every outbound connection the software can make, what it sends, and how to turn it off
- [Third-Party Components](THIRD_PARTY.md) — every bundled engine, library and OS package, with licenses and which artifact each ships in
- [Source, releases and issues](https://github.com/spatiumddi/spatiumddi) — the project on GitHub
