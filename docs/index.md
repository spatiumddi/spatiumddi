---
layout: default
title: Home
---

# SpatiumDDI Documentation

Open-source DDI platform — unified DNS, DHCP, and IP Address Management.

## Start Here

- [Getting Started](GETTING_STARTED.md) — recommended setup order (servers → zones/scopes → subnets → addresses)
- [Windows Server Setup](deployment/WINDOWS.md) — WinRM, service accounts, firewall — Windows-side checklist for agentless DNS + DHCP

## Architecture & Design

- [Deployment Topologies](deployment/TOPOLOGIES.md) — reference topologies, component relationships, HA design
- Data Model — database models, relationships, field definitions
- API Conventions — REST API conventions, pagination, error format
- Development Guide — coding standards, test requirements, CI
- [Observability](OBSERVABILITY.md) — logging, metrics, health dashboard
- [Permissions](PERMISSIONS.md) — RBAC grammar, builtin roles, scope delegation

## Feature Specs

- [IPAM](features/IPAM.md) — IP spaces, blocks, subnets, addresses, custom fields
- [DNS](features/DNS.md) — zones, records, views, server groups, blocking lists, Windows DNS (Path A + B)
- [DHCP](features/DHCP.md) — servers, scopes, pools, static assignments, leases, Windows DHCP (Path A)
- [Auth & Permissions](features/AUTH.md) — LDAP, OIDC, SAML, RADIUS, TACACS+, roles, API tokens
- [ACME DNS-01 Provider](features/ACME.md) — acme-dns-compatible surface for LE / public-CA cert issuance against SpatiumDDI-managed zones
- [Integrations](features/INTEGRATIONS.md) — read-only Kubernetes + Docker + Proxmox VE + Tailscale + Cloud (AWS/Azure/GCP) mirrors into IPAM; per-integration setup, mirror semantics, dashboard surface, roadmap
- [System Admin](features/SYSTEM_ADMIN.md) — config, health dashboard, backup/restore

## Deployment

- [Docker Compose](deployment/DOCKER.md) — quick start, profiles, TLS, HA
- [Windows Server](deployment/WINDOWS.md) — connecting to Windows DNS / DHCP over WinRM + RFC 2136
- [Kubernetes](../k8s/README.md) — Helm chart, manifests, HA PostgreSQL + Redis Sentinel
- Bare Metal — Ansible playbooks, systemd
- [OS Appliance](deployment/APPLIANCE.md) — appliance image build
- [DNS Agent](deployment/DNS_AGENT.md) — agent protocol, auto-registration, config sync

## Driver Internals

- [DNS Drivers](drivers/DNS_DRIVERS.md) — BIND9 + Windows DNS driver internals
- [DHCP Drivers](drivers/DHCP_DRIVERS.md) — Kea + Windows DHCP driver internals
