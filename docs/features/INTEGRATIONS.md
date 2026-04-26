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

## Proxmox VE

**Phase status**: 1a + 1b shipped. Phase 2 (per-VM management actions — start / stop / console / snapshot / migrate) deferred.

Per-endpoint config (`ProxmoxNode` rows):

| Field | Notes |
|---|---|
| `host` | Hostname or IP of **any** node in the cluster. PVE's REST API is homogeneous across members, so a single row covers a cluster. |
| `port` | Default `8006`. |
| `verify_tls` | Default `true`. Disable for self-signed labs, or paste the PVE CA bundle in the field below. |
| `ca_bundle_pem` | Optional. Trusted in addition to the system store when `verify_tls=true`. |
| `token_id` | Format `user@realm!tokenid` (e.g. `spatiumddi@pve!spatiumddi`). |
| `token_secret` | UUID printed by `pveum user token add`. Fernet-encrypted at rest. |
| `ipam_space_id` | Required. |
| `dns_group_id` | Optional. |
| `mirror_vms` | Default `true`. Turn off to mirror bridges only. |
| `mirror_lxc` | Default `true`. |
| `include_stopped` | Default `false`. |
| `infer_vnet_subnets` | Default `false`. When on, infer SDN VNet CIDRs from guest NICs for VNets that have no declared subnet — exact from `static_cidr`, speculative /24 from runtime IPs. |

### Mirror semantics

- **SDN VNets** (`/cluster/sdn/vnets/{vnet}/subnets`) → `Subnet` named `vnet:<vnet>`, gateway from the VNet subnet declaration. Operator-declared intent, authoritative over bridges that happen to carry the same CIDR. PVE without SDN installed returns 404 — the reconciler treats that as "no SDN" and keeps going; no error on the endpoint row.
- **VNet subnet inference** (opt-in via `infer_vnet_subnets`) — for VNets that exist but have no declared subnet, derive the CIDR from guests attached to the VNet. Priority: (1) exact `static_cidr` from `ipconfigN` / LXC `ip=`, gateway from `gw=`; (2) /24 guess around guest-agent runtime IPs, no gateway. The /24 path is speculative and logs a `proxmox_vnet_cidr_guessed` warning — declare subnets properly with `pvesh create /cluster/sdn/vnets/<vnet>/subnets --subnet <cidr> --gateway <ip> --type subnet` to replace guesses with exact values.
- **Bridges + VLAN interfaces with a CIDR** → `Subnet` under the enclosing operator block (or the auto-created RFC 1918 / CGNAT supernet). Bridges without a CIDR (common L2-only VM bridges, *and* every VLAN the PVE host doesn't terminate) are skipped to avoid empty-subnet noise — use SDN for those, or add the subnet in IPAM manually.
- **Bridge gateway IP** → one `reserved` `IPAddress` per subnet.
- **VM NICs** → `IPAddress` with `status="proxmox-vm"`, MAC from `netN` config. Runtime IP from the QEMU guest-agent (`/nodes/{n}/qemu/{vmid}/agent/network-get-interfaces`) when the agent is enabled + running; falls back to `ipconfigN`; NIC dropped when nothing resolves. Link-local + loopback filtered.
- **LXC NICs** → `IPAddress` with `status="proxmox-lxc"`, hostname = container hostname, MAC from `netN`. Runtime IP from `/nodes/{n}/lxc/{vmid}/interfaces`; falls back to the inline `ip=` on `netN`.

### Setup — minimum-privilege token

The admin page ships a copy-paste guide. TL;DR on any PVE node:

```sh
pveum useradd spatiumddi@pve --comment "SpatiumDDI read-only"
pveum aclmod / -user spatiumddi@pve -role PVEAuditor
pveum user token add spatiumddi@pve spatiumddi --privsep=0
```

The last command prints the `full-tokenid` (`spatiumddi@pve!spatiumddi`) + `value` (UUID). Paste those into the admin form's **Token ID** and **Token Secret** fields; hit **Test Connection** to verify.

`PVEAuditor` grants read-only ACLs across datacentre + SDN resources — enough for the endpoints this integration calls (`/version`, `/cluster/status`, `/cluster/sdn/vnets*`, `/nodes/*/{qemu,lxc,network}`) plus the per-guest agent / interfaces calls.

### Discovery modal

The reconciler writes a `last_discovery` JSONB snapshot on every successful sync containing category counters and a per-guest diagnostic list. The admin page exposes a magnifier-icon button on each endpoint row that opens the **Discovery** modal:

- **Counter pills** at the top: VM totals vs. agent reporting / not responding / off, LXC reporting / no IP, SDN VNets resolved vs. unresolved, subnets mirrored, addresses skipped because no subnet encloses the IP.
- **Filter tabs** — `Issues (N)` (default), `All (N)`, and one tab per issue code (`Agent not responding`, `Agent off`, `No IP`, `No NIC`, `Static only`).
- **Search box** — name / vmid / node / bridge substring match.
- **Per-row table** with agent-state pills, mirrored IP count split (`Na/Ms` = `N` from agent, `M` from static), and an inline operator-facing **hint** like "install qemu-guest-agent inside the VM: `apt install qemu-guest-agent && systemctl enable --now qemu-guest-agent`" or "Enable the QEMU agent on this VM in Options → QEMU Guest Agent".

The button is disabled until the endpoint has been synced at least once (nothing to show). Snapshot freshness matches `last_synced_at`; click **Sync Now** on the row to refresh.

---

## Tailscale

**Phase status**: 1 shipped (read-only device mirror). Phase 2 (synthetic `<tailnet>.ts.net` DNS surface backed by the same poll, optionally rendering a BIND9 forwarder zone for `100.100.100.100`) is on the roadmap.

Per-tenant config (`TailscaleTenant` rows):

| Field | Notes |
|---|---|
| `tailnet` | Tailnet slug from <https://login.tailscale.com/admin/settings/general>, or `-` for the API key's default tailnet (works for solo accounts and any PAT issued without an explicit tailnet override). |
| `api_key` | Personal-access token (`tskey-api-…`). Generate at <https://login.tailscale.com/admin/settings/keys>. Fernet-encrypted at rest; tokens carry the issuing user's permissions but SpatiumDDI only ever reads. |
| `ipam_space_id` | Required. The CGNAT IPv4 + IPv6 ULA blocks land here. |
| `dns_group_id` | Optional. Held for Phase 2 — unused today. |
| `cgnat_cidr` | Default `100.64.0.0/10` (Tailscale's standard CGNAT slice). Override only if your tailnet was provisioned with a custom slice. |
| `ipv6_cidr` | Default `fd7a:115c:a1e0::/48` (Tailscale's standard ULA prefix). |
| `skip_expired` | Default `true`. Skip devices whose Tailscale node-key has expired. The `0001-01-01T00:00:00Z` "never expires" sentinel is correctly treated as not-expired. |
| `sync_interval_seconds` | Default `60`. Floor `30`. Tailscale's documented rate limit is 100 req/min — 60 s default leaves plenty of headroom. |

### Mirror semantics

- **CGNAT IPv4 block + IPv6 ULA block** are auto-created under the bound space on first sync (one tenant-owned `IPBlock` each), with a single `Subnet` per block covering the whole slice. The tailnet is a flat overlay — no subdivision.
- **Devices** (`GET /api/v2/tailnet/{tn}/devices?fields=all`) → one `IPAddress` per `(device, address)` tuple under the matching subnet, with:
  - `status="tailscale-node"`, `hostname` = device FQDN (`<host>.<tailnet>.ts.net`).
  - `description` = `<os> <client_version> — <user>`.
  - `custom_fields` carry: `tailscale_id`, `tailscale_node_id`, `os`, `client_version`, `user`, `tags`, `authorized`, `last_seen`, `expires`, `key_expiry_disabled`, `update_available`, `advertised_routes`, `enabled_routes`. Empty / falsy fields are dropped to keep the JSON column compact.
- **MAC** is always null — Tailscale is an L3 overlay, no L2 addresses.
- **Tailnet domain** (`tailnet_domain` on the row) is auto-derived from the first device FQDN — no separate config field.

### Lock semantics

Same shape as Proxmox / Docker / Kubernetes:

- **Claim-on-existing**: pre-existing operator rows in the CGNAT block at desired Tailscale addresses get adopted (FK stamped) with `user_modified_at` set, locking the operator's hostname / description / status / mac from reconciler overwrites.
- **Skip-on-locked**: rows the operator has edited (`user_modified_at IS NOT NULL`) preserve their soft fields on every subsequent reconcile. The reconciler still updates `subnet_id` and the `custom_fields` JSON (since tailnet metadata like `last_seen` is most useful when fresh).
- **Un-claim-on-disappear**: locked rows whose upstream device is gone keep the operator's edits — the FK is released so the row appears as "manually managed", rather than being silently deleted.
- **Cross-integration safety**: rows already owned by Proxmox / Docker / Kubernetes are skipped with a warning rather than claimed.

### Setup

The admin page ships a copy-paste guide:

1. Open <https://login.tailscale.com/admin/settings/keys>, click **Generate API key…**.
2. Pick an expiry window (90 days minimum). Tailscale doesn't support non-expiring API keys.
3. Copy the printed value (`tskey-api-…`).
4. Open <https://login.tailscale.com/admin/settings/general> for the **Organization** slug — paste it into **Tailnet**, or leave the default `-` for solo / single-tailnet accounts.
5. Hit **Test Connection** to verify before save.

### Phase 2 — synthetic tailnet DNS surface

Shipped as Option 2 from the original plan (synthetic `DNSZone` materialised by the reconciler, not a `TailscaleDNSReadOnlyDriver`). Activates automatically when the tenant has `dns_group_id` bound; skipped silently otherwise.

**What it does.** Every reconcile pass:

1. Derives the tailnet domain from the first device FQDN (already cached on `TailscaleTenant.tailnet_domain`).
2. Upserts a `DNSZone` named `<tailnet>.ts.net.` in the bound DNS group, FK-stamped with `tailscale_tenant_id` + `is_auto_generated=True`.
3. Diffs the desired record set (one A or AAAA per device address) against the existing synthesised records — keyed on `(label, record_type, value)`. Adds new, deletes orphaned. No-ops when the device list is unchanged.
4. Stamps each record with `auto_generated=True` + `tailscale_tenant_id`. TTL = 300 s.

**Read-only enforcement.** API write paths (`PUT /zones/{id}`, `DELETE /zones/{id}`, record CRUD) return **422** with an explanatory message when `tailscale_tenant_id IS NOT NULL` on the target row. UI shows a cyan **Tailscale (read-only)** badge near the zone title and disables the Edit / Delete / Add Record buttons. The per-record lock badge differentiates `Tailscale` vs `IPAM` based on which integration owns the row.

**Conflict with operator zones.** If an operator has manually created `<tailnet>.ts.net` in the same group, the reconciler refuses to claim it (which would silently overwrite operator records every sync). The conflict is recorded as a `summary.warnings` entry visible in the audit log; the operator-managed zone is left untouched, and the IPAM mirror still runs to completion. To unblock Phase 2, the operator deletes their manual zone (or rebinds the tenant to a different DNS group).

**Filtering.** Devices skipped by the IPAM mirror (expired keys, no FQDN, foreign-tailnet FQDN) are also skipped here, so the DNS view stays consistent with the IPAM view.

**Bonus.** Records land in real `DNSRecord` rows — the existing BIND9 render path picks them up automatically. Non-Tailscale LAN clients can resolve `<host>.<tailnet>.ts.net` through SpatiumDDI's BIND9 with no forwarder plumbing.

### What's not done

- **Per-tenant zone-name override.** Auto-derived from the device FQDN today. Adding a `synthetic_zone_name` column would cover operators with custom split-DNS arrangements (e.g. publishing under `tailnet.internal`).
- **Subnet-router routes (`enabled_routes`) as first-class IPBlock rows** — currently surface only in `custom_fields`. Promoting them is straightforward but waits for an operator request.
- **Per-device management surface** (rename / expire / authorize / delete via the admin API write side) — outside scope of the read-only mirror; Phase 3 territory if it ever lands.

---

## Dashboard surface

When **any** integration toggle is on, the dashboard renders an **Integrations panel** below the service health strip with:

- One column per enabled integration (Kubernetes / Docker / Proxmox / Tailscale), header linking to the full admin page.
- Per-row: status dot (green = synced recently, amber = stalled > 3× sync interval, red = `last_sync_error`, gray = disabled or never synced), name, endpoint, node / container count, humanized last-synced age.
- `last_sync_error` is exposed as the row tooltip so operators can triage without leaving the dashboard.

---

## Roadmap — additional integrations

Tier 1 remaining (tracked in [CLAUDE.md §Future Phases § Additional integration candidates](../../CLAUDE.md)): **UniFi Network Application**, **OPNsense**, **pfSense**. All target the same homelab / SMB audience and fit the Kubernetes/Docker/Proxmox/Tailscale reconciler shape. See CLAUDE.md for per-integration scope notes.

Tier 2 (narrower): MikroTik RouterOS 7, Incus / LXD, HashiCorp Nomad, NetBox one-shot import.

Tier 3 (cloud — AWS / Azure / GCP / Hetzner / DO / Linode / Vultr VPC family): roadmap-coherent but not a lab-first priority.

**Explicit non-goals**: VMware vCenter / ESXi (SOAP-heavy, enterprise effort), SNMP polling (tracked as a separate IPAM discovery line item), WireGuard raw config (no API).
