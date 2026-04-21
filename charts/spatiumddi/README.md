# SpatiumDDI Helm chart

Umbrella chart that deploys the full SpatiumDDI control plane (API +
frontend + Celery worker + Celery beat + migrate Job) plus PostgreSQL
and Redis via Bitnami subcharts, with optional DNS and DHCP agent
StatefulSets.

- **Chart type:** application
- **Registry:** `oci://ghcr.io/spatiumddi/charts/spatiumddi`
- **Versioning:** Each SpatiumDDI release tag (CalVer `YYYY.MM.DD-N`)
  publishes a chart version with leading zeroes stripped so it's a
  valid SemVer 2 identifier — e.g. tag `2026.04.20-1` →
  chart version `2026.4.20-1`.

## TL;DR

```bash
helm install ddi oci://ghcr.io/spatiumddi/charts/spatiumddi \
  --version 2026.4.20-1 \
  --namespace spatiumddi --create-namespace
```

Default login: **`admin` / `admin`** (forced password change on first login).

## Prerequisites

- Kubernetes 1.26+
- Helm 3.8+ (needed for OCI)
- A StorageClass supporting `ReadWriteOnce` (Postgres, Redis, and
  agent state all use PVCs)
- Ingress controller or LoadBalancer for external access (optional)

## Install

```bash
# Default install — all-in-one with bundled Postgres + Redis
helm install ddi oci://ghcr.io/spatiumddi/charts/spatiumddi \
  --version <CHART_VERSION> \
  --namespace spatiumddi --create-namespace
```

### Exposing the UI

```yaml
# values.yaml
ingress:
  enabled: true
  className: nginx
  hosts:
    - host: ddi.example.com
      paths:
        - { path: /, pathType: Prefix }
  tls:
    - secretName: ddi-tls
      hosts: [ddi.example.com]
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
```

Or, without an Ingress, flip the frontend service to `LoadBalancer`:

```yaml
frontend:
  service:
    type: LoadBalancer
```

### Using an external Postgres

Point the chart at an existing database and skip the bundled subchart:

```yaml
postgresql:
  enabled: false

externalDatabase:
  host: pg.internal
  port: 5432
  username: spatiumddi
  database: spatiumddi
  existingSecret: my-db-secret       # must carry key `password`
  existingSecretPasswordKey: password
```

### Using an external Redis

```yaml
redis:
  enabled: false

externalRedis:
  host: redis.internal
  port: 6379
  existingSecret: my-redis-secret    # optional; remove for unauth'd redis
```

### Running managed DNS agents

```yaml
dnsAgents:
  enabled: true
  agentKey:
    existingSecret: spatium-dns-agent-key   # must carry key DNS_AGENT_KEY
  servers:
    - name: ns1
      role: primary
      group: internal-resolvers
      service: { type: LoadBalancer }
    - name: ns2
      role: secondary
      group: internal-resolvers
      service: { type: LoadBalancer }
```

Pre-create the PSK secret (or use `agentKey.value` inline for lab use only):

```bash
kubectl -n spatiumddi create secret generic spatium-dns-agent-key \
  --from-literal=DNS_AGENT_KEY="$(openssl rand -hex 32)"
```

### Running managed DHCP agents

Same shape as DNS but `SPATIUM_AGENT_KEY`:

```yaml
dhcpAgents:
  enabled: true
  agentKey:
    existingSecret: spatium-dhcp-agent-key  # must carry key SPATIUM_AGENT_KEY
  servers:
    - name: dhcp1
      role: primary
      # hostNetwork: true required for real DHCPv4 unless relay-only.
      hostNetwork: true
```

## Values reference

### Top-level

| Key | Default | Description |
|-----|---------|-------------|
| `image.registry` | `ghcr.io` | Image registry for control-plane images |
| `image.repository` | `spatiumddi` | Repo prefix (images are `<registry>/<repo>/<name>`) |
| `image.tag` | `""` → `Chart.appVersion` | Control-plane image tag |
| `image.pullPolicy` | `IfNotPresent` |  |
| `image.pullSecrets` | `[]` |  |
| `auth.secretKey` | `""` | Fernet key; auto-generated on first install if empty |
| `auth.existingSecret` | `""` | BYO secret with key `secret-key` |
| `fullnameOverride` | `""` |  |
| `nameOverride` | `""` |  |

### Control plane

| Key | Default | Description |
|-----|---------|-------------|
| `api.replicas` | `2` |  |
| `api.service.type` | `ClusterIP` |  |
| `api.service.port` | `8000` |  |
| `api.autoscaling.enabled` | `true` | HPA on CPU + memory |
| `api.autoscaling.minReplicas` / `maxReplicas` | `2` / `10` |  |
| `frontend.replicas` | `2` |  |
| `frontend.service.type` / `.port` | `ClusterIP` / `80` |  |
| `worker.replicas` | `2` |  |
| `worker.concurrency` | `4` |  |
| `worker.queues` | `"ipam,dns,dhcp,default"` |  |
| `beat.*` | see values.yaml | Singleton scheduler |
| `migrate.enabled` | `true` | Alembic Job as pre-install/pre-upgrade hook |
| `ingress.*` | disabled |  |

### Dependencies

The `postgresql` and `redis` keys are passed through to the Bitnami
subcharts verbatim — any option those charts accept works here. See:

- https://github.com/bitnami/charts/tree/main/bitnami/postgresql
- https://github.com/bitnami/charts/tree/main/bitnami/redis

### Agents

| Key | Default | Description |
|-----|---------|-------------|
| `dnsAgents.enabled` | `false` |  |
| `dnsAgents.image.repository` | `ghcr.io/spatiumddi/dns-bind9` |  |
| `dnsAgents.agentKey.existingSecret` | `""` | Carries `DNS_AGENT_KEY` |
| `dnsAgents.servers` | `[]` | One entry → one StatefulSet + Services |
| `dhcpAgents.enabled` | `false` |  |
| `dhcpAgents.image.repository` | `ghcr.io/spatiumddi/dhcp-kea` |  |
| `dhcpAgents.agentKey.existingSecret` | `""` | Carries `SPATIUM_AGENT_KEY` |
| `dhcpAgents.servers` | `[]` |  |

Each server entry accepts `name`, `role`, `group`, `storage.agentState`,
`storage.dnsState` (or `storage.keaState`), `service.type`,
`hostNetwork` (DHCP only), and `resources`.

## Upgrade

```bash
helm upgrade ddi oci://ghcr.io/spatiumddi/charts/spatiumddi \
  --version <NEW_CHART_VERSION> \
  --namespace spatiumddi --reuse-values
```

The migrate Job runs as a `pre-upgrade` hook, so Alembic applies
before the new API pods roll out.

## Uninstall

```bash
helm uninstall ddi --namespace spatiumddi
```

PVCs for Postgres, Redis, and agent state are **not** deleted
automatically — remove them manually if you want a clean slate:

```bash
kubectl -n spatiumddi delete pvc -l app.kubernetes.io/instance=ddi
```

## Troubleshooting

- **API pods CrashLoopBackOff on first install:** the migrate Job
  probably hasn't finished. `kubectl -n spatiumddi logs job/ddi-spatiumddi-migrate`.
- **`secret-key` rotated unexpectedly:** the chart's `lookup` preserves
  it across upgrades, but `helm template` — or a fresh install under
  a new release name — generates a new one. Always use `helm install` /
  `helm upgrade` against the same release name, or pre-create the
  secret and set `auth.existingSecret`.
- **DNS agent can't reach control plane:** the chart sets
  `CONTROL_PLANE_URL` to `http://<release>-spatiumddi-api.<ns>.svc.cluster.local:8000`.
  Agents running outside the cluster need a different URL via a custom
  `values.yaml` override (not currently exposed — raise an issue).

## Development

```bash
cd charts/spatiumddi
helm dependency update           # pull bitnami/postgresql + bitnami/redis
helm lint .
helm template test . --namespace test | less
```

For local testing against a real cluster:

```bash
helm install test . \
  --namespace spatiumddi --create-namespace \
  --set image.tag=latest \
  --set postgresql.primary.persistence.size=2Gi
```
