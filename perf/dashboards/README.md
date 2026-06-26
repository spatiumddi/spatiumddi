# SpatiumDDI perf war-room — monitoring stack

The real-time monitoring stack for the 24h load + soak test
(`docs/PERFORMANCE_TESTING.md` §6). **Everything here runs OFF the appliance**
(§6.0). The appliance ships **no Prometheus, no Grafana, no metrics-server, no
postgres/redis exporter** (§2.4) — this directory adds them: an off-box
Prometheus + Grafana on the monitoring VM (`lg-0`), plus a small set of
**tightly-capped on-node exporters** that the off-box Prometheus pulls.

> **Why off-box (non-negotiable).** Co-locating Prometheus/Grafana on the SUT
> would steal the exact CPU/mem/IO/connection budget criterion (a)/(c) is
> measuring. Prometheus + Grafana **pull**; they never run on the appliance
> (§6.0). The only thing on the node is the exporters — and they are sized to
> **evict before the SUT** under pressure (Burstable QoS, §6.4).

```
perf/dashboards/
├── prometheus/prometheus.yml          off-box scrape config (15s steady / 10s ceiling ceiling)
├── exporters/
│   ├── postgres_exporter.yaml         minimal Deployment + Service + DSN Secret template
│   ├── redis_exporter.yaml            minimal Deployment + Service + conn Secret template
│   └── queries.yaml                   postgres_exporter custom queries (locks/deadlocks/per-table/pgss)
├── grafana/
│   ├── dashboards/warroom.json        THE war-room board — §6.2 rows 0..5
│   └── provisioning/
│       ├── datasources.yaml           auto-provisions the Prometheus datasource
│       └── dashboards.yaml            auto-loads warroom.json
└── README.md                          (this file)
```

---

## The two pillars (§6.1)

The war-room is **two data paths**, kept deliberately separate so the observer
doesn't contaminate the thing it measures (§5.4 / §6.3):

1. **The exporters** (this directory) — node-exporter, kube-state-metrics,
   postgres_exporter, redis_exporter, cAdvisor. These carry the **deep** series:
   the criterion-(a) DB panel (locks, deadlocks, per-table tuples, WAL,
   pg_stat_statements), Celery queue LLENs, host CPU/disk/mem, per-pod CPU/mem,
   pod restarts/OOM, and — via node-exporter's **textfile collector** — the
   per-second client-side truth from the load generators.

2. **The JSON poller** (`perf/warroom/poller.py`, a *separate* component) — one
   off-box process, one superadmin token, re-exposing the product's **native
   rollups** (`/health/platform`, `/admin/redis/{overview,wake-bus}`,
   `/metrics/{dns,dhcp}/timeseries`) as Prometheus gauges. This board scrapes the
   poller for the platform dots, wake-bus, Kea/BIND 60s funnels, and propagation
   lag.

> **Observer discipline (§6.3 / §5.4 critique H1).** The deep DB series come
> from **postgres_exporter + the direct psql poller ONLY** — NOT from the JSON
> poller's `/admin/postgres/*` path (which would route through the api pool +
> CNPG and double-load the SUT). One source per metric. The `warroom.json`
> board honours this: every `spddi_*` series is exporter-sourced; only the
> `spatium_*` series (platform dots, wake-bus, dns/dhcp funnels) are JSON-poller
> sourced.

---

## On-node footprint (§6.4 — keep it tiny)

| Exporter | Kind | requests | limits | Port | Source |
|---|---|---|---|---|---|
| node-exporter | DaemonSet (hostNetwork) | 10m / 16Mi | — / 64Mi | :9100 | appliance chart `values.yaml:167` |
| kube-state-metrics | Deployment | 10m / 32Mi | — / 128Mi | :8080 | appliance chart `values.yaml:149` |
| postgres_exporter | Deployment | 10m / 32Mi | 30m / 64Mi | :9187 | `exporters/postgres_exporter.yaml` |
| redis_exporter | Deployment | 5m / 16Mi | 20m / 64Mi | :9121 | `exporters/redis_exporter.yaml` |
| cAdvisor | (kubelet built-in — no new pod) | — | — | :10250 | — |

**Typical added on-node footprint ≈ 35m CPU requests / ~96Mi RSS** — well
inside the §6.4 "≤ ~110m CPU / ~256Mi" budget (node-exporter + KSM idle around
~10–30MB RSS each per their chart comments). The memory *limits* sum higher
(64+128+64+64 = 320Mi) only as eviction headroom — every exporter is
**Burstable QoS** (requests < limits) so the kubelet evicts it before the SUT
pods under memory pressure. They are scrape *targets* only — cheap counter
reads, no on-box aggregation (§6.0).

---

## Step 1 — turn on the two chart exporters

node-exporter + kube-state-metrics already ship in the **appliance** chart
(`charts/spatiumddi-appliance`), both `enabled: false` by default (§2.4 / §6.2).
Flip them on:

```bash
# Helm values override (appliance chart). Cite: values.yaml:149 (KSM) / :167 (node-exporter).
helm upgrade <release> charts/spatiumddi-appliance \
  --reuse-values \
  --set observability.kubeStateMetrics.enabled=true \
  --set observability.nodeExporter.enabled=true
```

(Phase 8 wires a Fleet-UI "Observability" toggle for the same two knobs; until
then it's a `helm upgrade`. The caps in those chart blocks already match the
§6.4 budget — node-exporter 10m/16Mi→64Mi, KSM 10m/32Mi→128Mi.)

node-exporter needs the **textfile collector** for the generator stats (§6.1
Layer 1). The chart's node-exporter does not enable it by default; add the flag
+ a hostPath the generators write into. Patch the DaemonSet (or fork the chart
template) to add:

```
args:
  - --collector.textfile.directory=/host/var/lib/node_exporter/textfile
volumeMounts:
  - { name: textfile, mountPath: /host/var/lib/node_exporter/textfile, readOnly: true }
volumes:
  - { name: textfile, hostPath: { path: /var/lib/node_exporter/textfile } }
```

The load generators (perfdhcp/dnsperf/orchestrator wrappers) write
`*.prom` files into that host dir; node-exporter merges them into its own
`/metrics`. The board reads the `spddi_gen_*` series listed in
`prometheus/prometheus.yml`.

---

## Step 2 — the postgres_exporter pg_monitor role

postgres_exporter connects as a **dedicated read-only role**, never the app or
superuser role (§6.2 / non-negotiable #3). Create it on the SUT database:

```sql
-- Run as a superuser (CNPG: `kubectl cnpg psql <cluster>` or psql to the -rw service).
CREATE ROLE spddi_perf_monitor LOGIN PASSWORD '<choose-a-strong-password>';
GRANT pg_monitor TO spddi_perf_monitor;           -- pg_stat_*, pg_stat_statements, pg_locks
-- pg_monitor already covers pg_stat_activity / pg_stat_database / pg_stat_user_tables /
-- pg_stat_statements / pg_locks. Our custom queries also read dns_record_op directly:
GRANT CONNECT ON DATABASE spatiumddi TO spddi_perf_monitor;
GRANT USAGE ON SCHEMA public TO spddi_perf_monitor;
GRANT SELECT ON dns_record_op TO spddi_perf_monitor;   -- spddi_record_ops query (queries.yaml)
```

`pg_stat_statements` must already be pre-loaded into CNPG `parameters`
(`shared_preload_libraries`) + `CREATE EXTENSION pg_stat_statements;` run at
**provisioning, not during the run** (§5.4 / §6.0) — the `pgss_enable` seeder
worker handles this. If it's absent, the `spddi_statements` query simply returns
zero rows (the `JOIN pg_extension` guard never errors).

Then create the DSN Secret (NEVER hardcode — non-negotiable #6) and apply the
exporter into the **SUT namespace** (the appliance uses `spatium`):

```bash
SUT_NS=spatium
PG_HOST=<release>-spatiumddi-postgresql-rw      # umbrella chart CNPG -rw service
#       (k8s/ha manifest: PG_HOST=postgres-rw)  — CNPG auto-creates -rw/-ro/-r (cnpg-cluster.yaml:14)

# DSN secret (overwrites the template Secret baked into the manifest)
kubectl -n "$SUT_NS" create secret generic perf-postgres-exporter-dsn \
  --from-literal=DATA_SOURCE_NAME="postgresql://spddi_perf_monitor:<password>@${PG_HOST}:5432/spatiumddi?sslmode=require" \
  --dry-run=client -o yaml | kubectl apply -f -

# custom queries as a ConfigMap the Deployment mounts
kubectl -n "$SUT_NS" create configmap perf-postgres-exporter-queries \
  --from-file=queries.yaml=exporters/queries.yaml \
  --dry-run=client -o yaml | kubectl apply -f -

# the exporter Deployment + Service (the embedded Secret template is a no-op once
# the real Secret above exists — apply selectively or delete the Secret block)
kubectl -n "$SUT_NS" apply -f exporters/postgres_exporter.yaml
```

---

## Step 3 — the redis_exporter

```bash
SUT_NS=spatium
REDIS_HOST=<release>-spatiumddi-redis            # umbrella chart redis service
#          (HA: the current Sentinel master Service — see "Sentinel variant" below)
REDIS_PASS=$(kubectl -n "$SUT_NS" get secret <redis-secret> -o jsonpath='{.data.redis-password}' | base64 -d)

kubectl -n "$SUT_NS" create secret generic perf-redis-exporter-conn \
  --from-literal=REDIS_ADDR="redis://${REDIS_HOST}:6379" \
  --from-literal=REDIS_PASSWORD="$REDIS_PASS" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$SUT_NS" apply -f exporters/redis_exporter.yaml
```

`--check-keys=ipam,dns,dhcp,default` makes the exporter export
`redis_key_size{key="ipam"}` = `LLEN ipam` for each Celery broker queue list
(§6.1 Layer 3 — the canonical soak-backlog signal; there is **no native
endpoint** for queue depth, §2.4). Empty queues delete the key → the series is
absent → the board's PromQL `... or vector(0)` coalesces to 0.

**Sentinel variant.** The appliance runs Redis as `sentinel kind, 1 replica`
(§2.1). The minimal exporter above points at a fixed Redis Service address. If
your install only exposes Sentinel, point `REDIS_ADDR` at the current master's
pod/Service, or run redis_exporter with `--redis.addr=redis://<sentinel>:26379
--is-cluster=false` and let it follow `+switch-master` (out of scope for the
single-node baseline where the master doesn't move).

---

## Step 4 — cAdvisor (no new pod)

cAdvisor is already inside the kubelet — Prometheus scrapes
`https://<node-ip>:10250/metrics/cadvisor` directly (§6.2/§6.4). It is
authenticated; give the off-box Prometheus a ServiceAccount token with
`nodes/metrics` access and drop it where `KUBELET_TOKEN_FILE` points:

```bash
# In the SUT cluster:
kubectl -n "$SUT_NS" create serviceaccount perf-kubelet-reader
kubectl create clusterrole perf-kubelet-metrics \
  --verb=get --non-resource-url=/metrics/cadvisor 2>/dev/null || true
# bind a role that allows nodes/metrics + nodes/proxy:
kubectl create clusterrolebinding perf-kubelet-reader \
  --clusterrole=system:node-metrics \
  --serviceaccount="$SUT_NS":perf-kubelet-reader 2>/dev/null || \
  kubectl create clusterrolebinding perf-kubelet-reader \
  --clusterrole=cluster-admin --serviceaccount="$SUT_NS":perf-kubelet-reader  # lab fallback

# mint a token onto the monitoring VM:
kubectl -n "$SUT_NS" create token perf-kubelet-reader --duration=48h > ./kubelet.token
```

Set `KUBELET_TOKEN_FILE=/etc/prometheus/kubelet.token` in the Prometheus env
(mounted read-only). Prometheus skips TLS verify against the kubelet's
self-signed serving cert (`insecure_skip_verify: true` — the data plane is
self-signed throughout, §2.2).

---

## Step 5 — reaching the ClusterIP exporters from off-box Prometheus

node-exporter + cAdvisor are reachable on the **node IP** (hostNetwork /
kubelet). KSM, postgres_exporter and redis_exporter are **ClusterIP** — expose
them to the off-box Prometheus by one of:

- **kubectl port-forward** from the monitoring VM (simplest for a single run):
  ```bash
  kubectl -n spatium port-forward --address 0.0.0.0 svc/kube-state-metrics 8080:8080 &
  kubectl -n spatium port-forward --address 0.0.0.0 svc/postgres-exporter   9187:9187 &
  kubectl -n spatium port-forward --address 0.0.0.0 svc/redis-exporter      9121:9121 &
  ```
  then set `KSM_ADDR=127.0.0.1:8080`, `PG_EXPORTER_ADDR=127.0.0.1:9187`,
  `REDIS_EXPORTER_ADDR=127.0.0.1:9121` (Prometheus env).
- **NodePort** Services (flip `type: NodePort` in the three manifests + use
  `<node-ip>:<nodePort>`) for a long-lived setup.

`NODE_IP` is the single appliance node IP (k3s AIO, §2.2). Set every `${VAR}` in
`prometheus/prometheus.yml` before launch (the docker-compose below interpolates
from the env / an `.env` file).

---

## Step 6 — stand up Prometheus + Grafana off-box

### Option A — docker compose (on the monitoring VM `lg-0`)

Create `.env` next to a `docker-compose.yml` you write on `lg-0`
(this directory ships the configs the compose mounts, not the compose file
itself — keep it on the monitoring box, off the appliance):

```yaml
# docker-compose.yml — runs on lg-0, NEVER on the appliance.
services:
  prometheus:
    image: prom/prometheus:v3.1.0
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --storage.tsdb.retention.time=30d
      - --web.enable-admin-api          # REQUIRED for the §8.2.1 end-of-run TSDB snapshot
    env_file: .env                       # NODE_IP, KSM_ADDR, PG_EXPORTER_ADDR, REDIS_EXPORTER_ADDR, JSON_POLLER_ADDR, KUBELET_TOKEN_FILE
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./kubelet.token:/etc/prometheus/kubelet.token:ro
      - prom-data:/prometheus
    ports: ["9090:9090"]
  grafana:
    image: grafana/grafana:11.4.0
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD}   # from .env — never hardcode
      PROMETHEUS_URL: http://prometheus:9090
    volumes:
      - ./grafana/provisioning/datasources.yaml:/etc/grafana/provisioning/datasources/datasources.yaml:ro
      - ./grafana/provisioning/dashboards.yaml:/etc/grafana/provisioning/dashboards/dashboards.yaml:ro
      - ./grafana/dashboards:/var/lib/grafana/dashboards:ro
    ports: ["3000:3000"]
    depends_on: [prometheus]
volumes:
  prom-data: {}
```

`.env` (no secrets in git — non-negotiable #6):

```ini
NODE_IP=10.20.0.10
KSM_ADDR=127.0.0.1:8080
PG_EXPORTER_ADDR=127.0.0.1:9187
REDIS_EXPORTER_ADDR=127.0.0.1:9121
JSON_POLLER_ADDR=127.0.0.1:9101
KUBELET_TOKEN_FILE=/etc/prometheus/kubelet.token
GRAFANA_ADMIN_PASSWORD=<set-me>
```

```bash
docker compose up -d
# Grafana → http://lg-0:3000  → folder "SpatiumDDI Perf" → "SpatiumDDI Perf — War Room"
```

Grafana auto-provisions the Prometheus datasource (`datasources.yaml`) and
auto-loads `warroom.json` (`dashboards.yaml`) — no click-import.

### Option B — k8s (off-cluster monitoring namespace)

If `lg-0` is its own k8s cluster, deploy Prometheus + Grafana there (kube-prom
stack or hand-rolled), mount `prometheus/prometheus.yml` as a ConfigMap and the
`grafana/` tree as ConfigMaps via the standard Grafana sidecar/provisioning
mechanism. **Do not deploy them into the SUT cluster** (§6.0). The scrape
targets stay the same node-IP / port-forward / NodePort addresses.

---

## The war-room board (`grafana/dashboards/warroom.json`)

One board, top-to-bottom = "where it hurts first" (§6.2). Six rows; every panel
carries the §6.2/§6.5 "bad looks like" in its description tooltip:

| Row | What | Headline thresholds |
|---|---|---|
| **0 · SUMMARY ribbon** | platform dots · DHCP/DNS achieved-vs-offered · prop p95 · PG conns/cache · pod restarts · Redis used | PG conns >70% amber / >85% red; cache <95/<90; any restart red; prop p95 >12s |
| **1 · LOAD** | DHCP offered-vs-achieved + ACK p50/95/99 + drops/NAK; DNS offered-vs-achieved + resolve p50/95/99; operator-mutation latency + 5xx | ACK p99 >50ms; resolve p99 >20ms; 5xx>0 = audit-lock contention |
| **2 · SERVICE** | Kea DORA funnel + NAK; BIND qps + SERVFAIL + RCODE mix; **REFUSED must be 0** | discover≫ack = pool empty; SERVFAIL climbing = starved; REFUSED>0 = out-of-zone bug (§4.8) |
| **3 · CONTROL PLANE** | **lease-events POST p50/95/99**; **Celery queue LLEN**; Redis ops/evict; wake-bus; sweep duration | lease-events p95 >1s = DB ceiling; any queue monotonic climb = soak fail; any eviction = back off; sweep >240s |
| **4 · DATABASE** | D1 conns-by-state · D2 active-vs-200 · D3 cache+temp · D4 dead-ratio + autovacuum age · D5 size growth · D6 locks+deadlocks · D8 WAL · D9 slow queries · D10 longest-txn | idle-in-tx climbing; any deadlock = FAIL; dns_zone lock-wait (H3); dns_record_op monotonic (no prune) |
| **5 · HOST** | node CPU+load · disk %util/await (CNPG PV) · mem+swap · per-pod CPU/working-set (cAdvisor) · OOM + disk-free | %util pegged 100% = disk bottleneck not lock; any OOM = soak fail; working-set >90% limit |

The board has a **`DS_PROMETHEUS` datasource variable** (templating) so it
imports cleanly into any Grafana with a Prometheus datasource, and a
**`namespace`** variable (defaults to `spatium`) that scopes the KSM/cAdvisor
panels. Default time range `now-15m` (ceiling windows); switch to `now-24h` for
the soak view (§6.2 default refresh 15s).

### JSON poller series → API (grounded)

The board's `spatium_*` panels read the JSON poller's re-exposed native
rollups. The poller (separate component) maps:

| Prometheus gauge | API source | Field (grounded) |
|---|---|---|
| `spatium_platform_component_up{component}` | `GET /health/platform` | `components[].status` (`backend/app/api/health.py:270`) |
| `spatium_redis_used_memory` / `_evicted_keys` / `_ops_per_sec` / `_hit_ratio` | `GET /api/v1/admin/redis/overview` | `used_memory_bytes`/`evicted_keys`(via INFO)/`instantaneous_ops_per_sec`/`keyspace_hits|misses` (`admin/redis.py:44-62,109`) |
| `spatium_wakebus_subscribers` / `_publishes_total{class}` | `GET /api/v1/admin/redis/wake-bus` | `total_subscribers` / `published_by_class` (`admin/redis.py:81-87,186`) |
| `spatium_dns_qps` / `_servfail` / `_nxdomain` | `GET /api/v1/metrics/dns/timeseries?window=1h` | `points[].queries_total|servfail|nxdomain` (`metrics/router.py:49-57,92`) |
| `spatium_dhcp_{discover,offer,request,ack,nak}_rate` | `GET /api/v1/metrics/dhcp/timeseries?window=1h` | `points[].discover|offer|request|ack|nak` (`metrics/router.py:66-76,144`) |

> The deep PG series (`spddi_*` from postgres_exporter) intentionally do **not**
> come via `/admin/postgres/*` — see "Observer discipline" above (§6.3 note).
> The native PG overview *is* available at `/api/v1/admin/postgres/overview`
> (`admin/postgres.py:108`) but the board sources active-conns/cache from the
> exporter to keep one source per metric and avoid loading the api pool.

### Generator textfile series (Layer 1)

The `spddi_gen_*` series come from the load generators' `.prom` files via the
node-exporter textfile collector. The generator components own emitting them;
the names the board expects are documented in `prometheus/prometheus.yml`
(`spddi_gen_dhcp_*`, `spddi_gen_dns_*`, `spddi_gen_propagation_lag_s`,
`spddi_gen_api_*`, `spddi_gen_scheduler_lag_s`, `spddi_gen_celery_sweep_duration_s`).

---

## Tear-down

```bash
docker compose down                 # off-box stack
kubectl -n spatium delete -f exporters/postgres_exporter.yaml -f exporters/redis_exporter.yaml
kubectl -n spatium delete configmap perf-postgres-exporter-queries
kubectl -n spatium delete secret perf-postgres-exporter-dsn perf-redis-exporter-conn
helm upgrade <release> charts/spatiumddi-appliance --reuse-values \
  --set observability.kubeStateMetrics.enabled=false \
  --set observability.nodeExporter.enabled=false
# drop the monitor role:
# DROP ROLE spddi_perf_monitor;
```

---

## Deferred / out of scope

- **Recording + alerting rules.** The board carries thresholds inline (§6.2
  "bad looks like"); Prometheus alerting rules + an Alertmanager are a follow-up.
  The §6.5 live-watch thresholds are the source if/when they're authored.
- **Sentinel-aware redis_exporter discovery** (single-node master doesn't move;
  HA is out of scope, §0 non-goals).
- **Terminal `live status` fallback** (`spatium-warroom.sh`, §6.6) — the SSH
  one-screen `watch` view — is a separate deliverable; this directory is the
  Grafana/Prometheus stack only.
