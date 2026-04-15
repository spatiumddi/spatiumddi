# Kubernetes Manifests

## Directory Structure

```
k8s/
├── base/              # Core application manifests (namespace, API, worker, frontend, migrations)
└── ha/                # High-availability add-ons (PostgreSQL Patroni/CloudNativePG, Redis Sentinel)
```

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

## TLS / Ingress

The Ingress in `k8s/base/frontend.yaml` uses `ingressClassName: nginx`. For TLS:

1. Install [cert-manager](https://cert-manager.io/)
2. Create a `ClusterIssuer` for Let's Encrypt
3. Uncomment the `tls` and `cert-manager.io/cluster-issuer` annotations in `frontend.yaml`

## DNS server deployment

SpatiumDDI ships managed DNS containers (BIND9, PowerDNS) that auto-register
with the control plane. One `StatefulSet` per `DNSServer` row — see
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

```bash
helm install spatium-dns charts/spatium-dns -n spatiumddi \
  --set controlPlaneUrl=https://api.spatiumddi.example \
  --set agentKey.existingSecret=spatium-dns-agent-key
```

Declare each server in `values.yaml` under `.servers[]`. The chart renders a
StatefulSet + LoadBalancer Service per entry.

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
