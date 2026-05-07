# Kubernetes Manifests

## Directory Structure

```
k8s/
├── base/              # Core application manifests (namespace, API, worker, frontend, migrations)
├── dns/               # Managed DNS server StatefulSets (bind9)
├── dhcp/              # Managed DHCP server StatefulSets (kea)
└── ha/                # High-availability add-ons (PostgreSQL Patroni/CloudNativePG, Redis Sentinel)
```

## Managed DHCP servers (Kea)

One StatefulSet per DHCP server row, matching the DNS-agent topology.

```bash
# Create the agent PSK secret (must match DHCP_AGENT_KEY on the control plane)
kubectl create secret generic spatium-dhcp-agent-key \
  --from-literal=SPATIUM_AGENT_KEY=$(openssl rand -hex 32) \
  -n spatiumddi

# Duplicate kea-statefulset.yaml / service-dhcp.yaml per server (dhcp1, dhcp2, ...)
kubectl apply -f k8s/dhcp/kea-statefulset.yaml
kubectl apply -f k8s/dhcp/service-dhcp.yaml
```

DHCPv4 requires broadcast reception on the client LAN. In most clusters you
either run the pod with `hostNetwork: true` or front it with a DHCP relay
(option 82). The stock manifests expose UDP/67 via `NodePort` for lab use
only.


## Quick Start (single-node / dev)

```bash
# 1. Create namespace and secrets
kubectl apply -f k8s/base/namespace.yaml
kubectl create secret generic spatiumddi-secrets \
  --from-literal=postgres-password=CHANGEME \
  --from-literal=secret-key=$(openssl rand -hex 32) \
  -n spatiumddi

# 2. Deploy a standalone PostgreSQL (not HA — for dev/test only)
kubectl run postgres --image=postgres:16-alpine -n spatiumddi \
  --env=POSTGRES_USER=spatiumddi \
  --env=POSTGRES_PASSWORD=CHANGEME \
  --env=POSTGRES_DB=spatiumddi

# 3. Run migrations
kubectl apply -f k8s/base/migrate-job.yaml
kubectl wait --for=condition=complete job/spatiumddi-migrate -n spatiumddi --timeout=120s

# 4. Deploy application
kubectl apply -f k8s/base/configmap.yaml
kubectl apply -f k8s/base/api.yaml
kubectl apply -f k8s/base/worker.yaml
kubectl apply -f k8s/base/frontend.yaml
```

## High Availability (production)

### PostgreSQL HA — CloudNativePG (recommended for K8s)

```bash
# Install CloudNativePG operator
kubectl apply --server-side -f \
  https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.24/releases/cnpg-1.24.0.yaml

# Deploy HA cluster (3 nodes: 1 primary + 2 replicas, auto-failover)
kubectl apply -f k8s/ha/postgres-cluster.yaml
```

The operator creates two Services automatically:
- `postgres-primary` → always points to the current primary (read/write)
- `postgres-replica` → load-balances across read replicas

### PostgreSQL HA — Patroni (Docker Compose)

For Docker Compose HA deployments, use the Patroni-based setup:

```bash
docker compose -f docker-compose.yml -f k8s/ha/postgres-docker-compose.yaml up -d
```

Set `DATABASE_URL` to point at HAProxy port 5000 instead of the single `postgres` container.

### Redis HA — Sentinel (K8s)

```bash
kubectl apply -f k8s/ha/redis-sentinel.yaml
```

Three Redis nodes with Sentinel provides automatic failover with quorum of 2.

**Recommended alternative:** Use the Bitnami Redis Helm chart with `sentinel.enabled=true` for production — it handles Sentinel configuration correctly and includes proper password injection.

### Redis HA — Docker Compose

For Redis HA in Docker Compose, use the Redis Sentinel pattern (see `k8s/ha/redis-sentinel.yaml` comments) or consider [Valkey](https://valkey.io/) with cluster mode.

### Celery Workers — HA

Celery workers are stateless and support any replica count. In K8s, the `worker` Deployment runs 2+ replicas by default. The Beat scheduler runs as a `Recreate` Deployment (replicas: 1) to prevent double-scheduling.

### Worker capability — `NET_RAW`

The worker pod ships with `securityContext.capabilities.add: ["NET_RAW"]` so `nmap` can run SYN scans + `-O` OS detection from the device-profiling auto-nmap path. The image already grants the cap to the `nmap` binary via `setcap` — this line keeps it in the pod's bounding set so restricted Pod Security Admission (`restricted` profile), OpenShift SCC, and GKE Autopilot don't drop it. The cap is in containerd's default cap set on permissive clusters, so it's a no-op there. If you've turned device profiling off cluster-wide and want a tighter security posture, drop the `securityContext` block from `k8s/base/worker.yaml` (or set `worker.netRawCapability: false` in the Helm chart).

## TLS / Ingress

The Ingress in `k8s/base/frontend.yaml` uses `ingressClassName: nginx`. For TLS:

1. Install [cert-manager](https://cert-manager.io/)
2. Create a `ClusterIssuer` for Let's Encrypt
3. Uncomment the `tls` and `cert-manager.io/cluster-issuer` annotations in `frontend.yaml`

## DNS server deployment

SpatiumDDI ships a managed BIND9 DNS container that auto-registers with
the control plane. One `StatefulSet` per `DNSServer` row — see
[`docs/deployment/DNS_AGENT.md`](../docs/deployment/DNS_AGENT.md).

### Secret

```bash
# Shared bootstrap PSK (must match DNS_AGENT_KEY on the control plane)
kubectl create secret generic spatium-dns-agent-key \
  --from-literal=DNS_AGENT_KEY=$(openssl rand -hex 32) -n spatiumddi
```

### Option A: static manifests

```bash
kubectl apply -f k8s/dns/bind9-statefulset.yaml
kubectl apply -f k8s/dns/service-dns.yaml
```

Duplicate the StatefulSet/Service pair per server (rename `ns1` → `ns2`, etc.).

### Option B: Helm chart (recommended)

The umbrella chart `charts/spatiumddi` deploys the entire stack (API,
frontend, worker, beat, migrate Job, Postgres, Redis) and can optionally
stand up DNS and DHCP agent StatefulSets alongside it. Published to
`oci://ghcr.io/spatiumddi/charts/spatiumddi` — see
[charts/spatiumddi/README.md](../charts/spatiumddi/README.md) for the
full option surface.

```bash
helm install ddi oci://ghcr.io/spatiumddi/charts/spatiumddi \
  --version <CHART_VERSION> \
  --namespace spatiumddi --create-namespace \
  --set dnsAgents.enabled=true \
  --set dnsAgents.agentKey.existingSecret=spatium-dns-agent-key \
  --set-json 'dnsAgents.servers=[{"name":"ns1","role":"primary","group":"internal-resolvers"}]'
```

Declare each DNS server under `.dnsAgents.servers[]` in `values.yaml`.
The chart renders a StatefulSet + LoadBalancer Service per entry.

### How servers register

On first boot each agent:

1. Reads `CONTROL_PLANE_URL` and `DNS_AGENT_KEY` (from the secret).
2. `POST /api/v1/dns/agents/register` with a generated `agent_id` + SHA-256
   fingerprint.
3. Receives a per-server JWT (24h, rotated on heartbeat).
4. Long-polls `GET /api/v1/dns/agents/config` and applies the bundle.

If `dns_require_agent_approval=true` the server appears in the DNS Server
Group UI with `pending_approval=true` until an admin approves it.

## Image Tags

All manifests default to `:latest`. Pin to a specific version tag (e.g., `2026.04.13-1`) for production:

```bash
kubectl set image deployment/api api=ghcr.io/spatiumddi/spatiumddi-api:2026.04.13-1 -n spatiumddi
kubectl set image deployment/frontend frontend=ghcr.io/spatiumddi/spatiumddi-frontend:2026.04.13-1 -n spatiumddi
```

## Upgrading

> **Take a backup before upgrading.** Sign in as a superadmin → **System Admin → Backup → Manual → Build + download**, supply a passphrase you'll remember (or pick a configured destination's **Run now** button). The archive is the single rollback artifact if the upgrade goes sideways. See [`docs/features/SYSTEM_ADMIN.md`](../docs/features/SYSTEM_ADMIN.md#29-backup-and-restore) for the full operator reference.

```bash
# Pin the new tag on every deployment
NEW_TAG=2026.05.07-1
for d in api worker beat frontend; do
  kubectl set image deployment/$d $d=ghcr.io/spatiumddi/spatiumddi-$d:$NEW_TAG -n spatiumddi
done

# Run the migration job — Alembic is idempotent, safe to re-run
kubectl delete job/spatiumddi-migrate -n spatiumddi --ignore-not-found
kubectl apply -f base/migrate-job.yaml -n spatiumddi
kubectl wait --for=condition=complete job/spatiumddi-migrate -n spatiumddi --timeout=10m
```

Helm chart users: `helm upgrade spatiumddi charts/spatiumddi -n spatiumddi --set image.tag=$NEW_TAG`. The chart's pre-upgrade hook re-runs the migrate job; the `alembic upgrade head` invocation honours the same DATABASE_URL the api uses.

If you skipped the backup and need to roll back: every restore takes a `pre-restore-{ts}.zip` safety dump under the api pod's `/var/lib/spatiumddi/backups/` (passphrase is the literal string `pre-restore-safety`). For that path to survive pod recycle, mount it as a `PersistentVolumeClaim` on both the api and worker deployments — see Backup below.

## Backup

The full backup + restore surface (build-and-download, S3 / S3-compatible / SCP / Azure / SMB / FTP / GCS / local-volume destinations, scheduled cron, retention, selective restore, restore-from-destination, alembic upgrade-on-restore, cross-install secret rewrap) lives in **System Admin → Backup**. See [`docs/features/SYSTEM_ADMIN.md`](../docs/features/SYSTEM_ADMIN.md#29-backup-and-restore) for the full reference.

The shape that's specific to Kubernetes:

- **Operator-friendly default — no PVC.** Most installs pair SpatiumDDI with an off-cluster object store (S3 / Azure Blob / GCS) for backup. In that case neither the api nor the worker needs a PVC for backups; the in-app `Build + download` button streams archives straight to the operator's browser, and scheduled targets push to the configured remote destination. Pre-restore safety dumps land in the api pod's writable layer and disappear on recycle — that's an acceptable trade because the configured remote destination IS the rollback artifact.

- **PVC mode — local_volume target.** Operators who want a `local_volume` destination (writes to a path on the pod's filesystem) MUST mount a `ReadWriteMany` PVC at the same path on both the api and worker deployments — the worker runs the scheduled sweep, so it has to write the same files the api lists back. Add to your overlay:

  ```yaml
  # k8s/overlays/yourenv/spatium-backups.yaml
  apiVersion: v1
  kind: PersistentVolumeClaim
  metadata:
    name: spatium-backups
    namespace: spatiumddi
  spec:
    accessModes: [ReadWriteMany]
    resources:
      requests:
        storage: 50Gi
    storageClassName: <your-rwx-class>     # NFS / Ceph / Azure Files / EFS
  ---
  apiVersion: apps/v1
  kind: Deployment
  metadata: { name: api, namespace: spatiumddi }
  spec:
    template:
      spec:
        volumes:
          - name: backups
            persistentVolumeClaim:
              claimName: spatium-backups
        containers:
          - name: api
            volumeMounts:
              - { name: backups, mountPath: /var/lib/spatiumddi/backups }
  # ... same volume + volumeMount on the worker deployment
  ```

  RWX is required because the api pod (write side: `Build and download`, pre-restore safety dumps) and the worker pod (write side: scheduled sweep) both need write access concurrently. RWO will reject the second mount.

- **Helm chart support is roadmap.** The umbrella chart at `charts/spatiumddi/` doesn't ship a `backup.localVolume` value yet — operators wanting a local_volume target on K8s today either patch the chart-rendered manifests with a kustomize overlay, or skip local_volume entirely and use a remote destination (which is what we recommend on K8s anyway).
