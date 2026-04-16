---
layout: default
title: Home
---

# SpatiumDDI Documentation

Open-source DDI platform — unified DNS, DHCP, and IP Address Management.

## Architecture & Design

- [Architecture](ARCHITECTURE.md) — system topology, component relationships, HA design
- [Data Model](DATA_MODEL.md) — database models, relationships, field definitions
- [API Conventions](API.md) — REST API conventions, pagination, error format
- [Development Guide](DEVELOPMENT.md) — coding standards, test requirements, CI
- [Observability](OBSERVABILITY.md) — logging, metrics, health dashboard

## Feature Specs

- [IPAM](features/IPAM.md) — IP spaces, blocks, subnets, addresses, custom fields
- [DNS](features/DNS.md) — zones, records, views, server groups, blocking lists
- [DHCP](features/DHCP.md) — servers, scopes, pools, static assignments, leases
- [Auth & Permissions](features/AUTH.md) — LDAP, OIDC, SAML, roles, API tokens
- [System Admin](features/SYSTEM_ADMIN.md) — config, health dashboard, backup/restore

## Deployment

- [Docker Compose](deployment/DOCKER.md) — quick start, profiles, TLS, HA
- [Kubernetes](deployment/KUBERNETES.md) — Helm chart, operators, HPA
- [Bare Metal](deployment/BAREMETAL.md) — Ansible playbooks, systemd
- [OS Appliance](deployment/APPLIANCE.md) — appliance image build
- [DNS Agent](deployment/DNS_AGENT.md) — agent protocol, auto-registration, config sync

## Driver Internals

- [DNS Drivers](drivers/DNS_DRIVERS.md) — BIND9 driver spec
- [DHCP Drivers](drivers/DHCP_DRIVERS.md) — Kea driver spec
