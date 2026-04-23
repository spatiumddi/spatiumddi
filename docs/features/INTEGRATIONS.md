---
layout: default
title: Integrations
nav_order: 8
---

# Integrations

SpatiumDDI mirrors network state from external systems into IPAM (and optionally DNS) so operators don't have to double-enter inventory. Every integration is **read-only** — SpatiumDDI never writes back to the source system. This doc covers the shared pattern and the specific integrations available today.

---

## The shared shape

Every integration follows the same reconciler pattern:

1. **Per-integration enable toggle** on Settings → Integrations. Turning the toggle on:
   - Lights up a sidebar nav item.
   - Starts a 30 s Celery beat sweep that iterates every registered target.
2. **Per-target rows** (one `KubernetesCluster` / `DockerHost` / future `*Target` per external system) bound to:
   - Exactly one **IPAM space** (required — mirrored CIDRs and addresses land here).
   - Optionally one **DNS server group** (for any DNS records the integration emits).
3. **Per-target `sync_interval_seconds`** gates the actual reconcile pass (minimum 30 s). A target configured for 5 minutes sees 10 beat ticks between passes.
4. **Sync Now** button per target fires the reconciler on demand, bypassing the interval gate.
5. **Provenance FK** on every mirrored row (`kubernetes_cluster_id`, `docker_host_id`, …) with `ON DELETE CASCADE` so removing a target sweeps every row it created.
6. **Status surfacing** — each target row carries `last_synced_at` + `last_sync_error`. Dashboard folds these into a green / amber / red dot on the Integrations panel.
7. **Smart parent-block detection** — when a mirrored CIDR is an RFC 1918 or CGNAT range (`10/8`, `172.16/12`, `192.168/16`, `100.64/10`) and no enclosing operator block exists in the target space, the reconciler auto-creates the canonical private supernet as an **unowned** top-level block. Unowned = no integration FK, so it survives removal of the integration and can be shared with manual allocations.

---

## Kubernetes

**Phase status**: 1a + 1b shipped. Phase 2 (external-dns webhook) deferred.

Per-cluster config (`KubernetesCluster` rows):

| Field | Notes |
|---|---|
| `api_server_url` | e.g. `https://1.2.3.4:6443` |
| `ca_bundle_pem` | Optional — system CA store used when empty (cloud-managed clusters) |
| `token` | Bearer token, Fernet-encrypted at rest |
| `pod_cidr` + `service_cidr` | Auto-detected from cluster state if omitted; operator-entered otherwise |
| `ipam_space_id` | Required |
| `dns_group_id` | Optional — zone matching for Ingress → DNS is disabled when null |
| `mirror_pods` | Default off — pod IPs churn, CIDR-as-IPBlock is the value |

### Mirror semantics

- **Pod CIDR + Service CIDR** → one `IPBlock` each under the bound space.
- **Nodes** → `IPAddress` with `status="kubernetes-node"`, hostname = node name, IP = `InternalIP`.
- **Services** of type `LoadBalancer` with a populated VIP → `IPAddress` with `status="kubernetes-lb"`, hostname = `<svc>.<ns>`.
- **Service ClusterIPs** → `IPAddress` with `status="kubernetes-service"`, hostname = `<svc>.<ns>.svc.cluster.local`.
- **Ingresses** with `status.loadBalancer.ingress[0].ip` → A record per `rules[].host` in the longest-suffix-matching zone in the bound DNS group. `ingress[0].hostname` (cloud LBs) → CNAME. `auto_generated=True` + fixed 300 s TTL.
- **Pods** (opt-in) → `IPAddress` per pod with `status="kubernetes-pod"`.

**Delete semantics**: removed cluster objects immediately drop their mirror rows (not orphaned). Deleting the cluster itself cascades every mirror via the FK.

### Setup

Admin page at `/kubernetes` ships a complete setup guide (expand the **Setup Guide** dropdown when creating or editing a cluster) with copy-paste YAML for:

- Namespace
- ServiceAccount
- ClusterRole (read-only — `nodes`, `services`, `ingresses`, `pods`, `endpoints`, `namespaces`)
- ClusterRoleBinding
- Secret (token-typed ServiceAccount token)

Plus the exact `kubectl` commands to extract the bearer token + CA bundle. **Test Connection** probes `/version` + `/api/v1/nodes` and distinguishes 401 / 403 / TLS / network errors with human-readable messages.

---

## Docker

**Phase status**: 1a + 1b shipped. Phase 2 (rich per-host management surface — container actions, logs, shell, compose up/down) placeholder.

Per-host config (`DockerHost` rows):

| Field | Notes |
|---|---|
| `connection_type` | `unix` or `tcp` (SSH deferred) |
| `endpoint` | `/var/run/docker.sock` (unix) or `tcp://host:2376` (tcp+TLS) |
| `ca_bundle_pem` + `client_cert_pem` + `client_key_pem` | TCP+TLS only. Client key Fernet-encrypted at rest |
| `ipam_space_id` | Required |
| `dns_group_id` | Optional |
| `mirror_containers` | Default off |
| `include_default_networks` | Default false — skip `bridge` / `host` / `none` / `docker_gwbridge` / `ingress` |
| `include_stopped_containers` | Default false |

Swarm overlay networks are **always skipped** regardless of the toggle — they're cluster-wide and mirroring them from every node would duplicate rows.

### Mirror semantics

- **Networks** with a CIDR → `Subnet` (nested under the enclosing block, or the auto-created RFC 1918 supernet if none exists).
- **Network gateway** → one `reserved`-status `IPAddress` per subnet, so the subnet looks like a normal LAN.
- **Containers** (opt-in) → `IPAddress` per `(container × connected network)` with `status="docker-container"`. Hostname comes from either:
  - `com.docker.compose.project` + `com.docker.compose.service` labels → `<project>.<service>`, or
  - Container name (when Compose labels are absent).

### Unix socket permissions

When using `connection_type=unix`, the api + worker containers need read access to the host's `/var/run/docker.sock`. Both `docker-compose.yml` and `docker-compose.dev.yml` ship commented-out `volumes` + `group_add` blocks on the api and worker services — uncomment them and set `DOCKER_GID` in your `.env` to match the host socket's group (`stat -c '%g' /var/run/docker.sock`).

---

## Dashboard surface

When **either** integration toggle is on, the dashboard renders an **Integrations panel** below the service health strip with:

- One column per enabled integration (Kubernetes / Docker), header linking to the full admin page.
- Per-row: status dot (green = synced recently, amber = stalled > 3× sync interval, red = `last_sync_error`, gray = disabled or never synced), name, endpoint, node / container count, humanized last-synced age.
- `last_sync_error` is exposed as the row tooltip so operators can triage without leaving the dashboard.

---

## Roadmap — additional integrations

Tier 1 candidates tracked in [CLAUDE.md §Future Phases § Additional integration candidates](../../CLAUDE.md): **Proxmox VE**, **UniFi Network Application**, **Tailscale**, **OPNsense**, **pfSense**. All four target the same homelab / SMB audience and fit the Kubernetes/Docker reconciler shape. See CLAUDE.md for per-integration scope notes.

Tier 2 (narrower): MikroTik RouterOS 7, Incus / LXD, HashiCorp Nomad, NetBox one-shot import.

Tier 3 (cloud — AWS / Azure / GCP / Hetzner / DO / Linode / Vultr VPC family): roadmap-coherent but not a lab-first priority.

**Explicit non-goals**: VMware vCenter / ESXi (SOAP-heavy, enterprise effort), SNMP polling (tracked as a separate IPAM discovery line item), WireGuard raw config (no API).
