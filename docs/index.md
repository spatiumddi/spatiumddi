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

- [Architecture](ARCHITECTURE.md) — system topology, component relationships, HA design
- [Data Model](DATA_MODEL.md) — database models, relationships, field definitions
- [API Conventions](API.md) — REST API conventions, pagination, error format
- [Development Guide](DEVELOPMENT.md) — coding standards, test requirements, CI
- [Observability](OBSERVABILITY.md) — logging, metrics, health dashboard
- [Permissions](PERMISSIONS.md) — RBAC grammar, builtin roles, scope delegation

## Feature Specs

- [IPAM](features/IPAM.md) — IP spaces, blocks, subnets, addresses, custom fields
- [DNS](features/DNS.md) — zones, records, views, server groups, blocking lists, Windows DNS (Path A + B)
- [DHCP](features/DHCP.md) — servers, scopes, pools, static assignments, leases, Windows DHCP (Path A)
- [Auth & Permissions](features/AUTH.md) — LDAP, OIDC, SAML, RADIUS, TACACS+, roles, API tokens
- [System Admin](features/SYSTEM_ADMIN.md) — config, health dashboard, backup/restore

## Deployment

- [Docker Compose](deployment/DOCKER.md) — quick start, profiles, TLS, HA
- [Windows Server](deployment/WINDOWS.md) — connecting to Windows DNS / DHCP over WinRM + RFC 2136
- [Kubernetes](deployment/KUBERNETES.md) — Helm chart, operators, HPA
- [Bare Metal](deployment/BAREMETAL.md) — Ansible playbooks, systemd
- [OS Appliance](deployment/APPLIANCE.md) — appliance image build
- [DNS Agent](deployment/DNS_AGENT.md) — agent protocol, auto-registration, config sync

## Driver Internals

- [DNS Drivers](drivers/DNS_DRIVERS.md) — BIND9 + Windows DNS driver internals
- [DHCP Drivers](drivers/DHCP_DRIVERS.md) — Kea + Windows DHCP driver internals
