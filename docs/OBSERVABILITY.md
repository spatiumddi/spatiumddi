# Observability Specification

## Overview

SpatiumDDI provides a fully integrated observability stack: structured logging shipped to a centralized store, a **built-in log viewer in the admin UI**, Prometheus metrics, health endpoints, and a Grafana dashboard bundle. No external observability tooling is required for basic operations, but all outputs are compatible with standard enterprise stacks (ELK, Loki, Datadog, Splunk).

---

## 1. Structured Logging

### Library and Format

All services use `structlog` configured to emit **newline-delimited JSON** (NDJSON):

```json
{
  "timestamp": "2024-01-15T14:30:00.123Z",
  "level": "info",
  "service": "api",
  "instance": "api-pod-3f8a",
  "request_id": "req-uuid-here",
  "user_id": "usr-abc123",
  "user_ip": "10.0.0.45",
  "method": "POST",
  "path": "/api/v1/ipam/subnets",
  "status": 201,
  "duration_ms": 42,
  "event": "subnet_created",
  "subnet_id": "sub-xyz789",
  "subnet_network": "10.1.5.0/24"
}
```

### Standard Fields (always present)

| Field | Description |
|---|---|
| `timestamp` | ISO 8601 UTC with milliseconds |
| `level` | `debug`, `info`, `warning`, `error`, `critical` |
| `service` | `api`, `worker`, `beat`, `agent`, `dhcp`, `dns`, `ntp` |
| `instance` | Hostname or pod name |
| `request_id` | UUID, passed through as `X-Request-ID` header |

### Sensitive Data Rules (enforced by linting)
- **Never log**: passwords, tokens, API keys, full credentials
- **Always mask**: MAC addresses in logs are shown as `aa:bb:cc:**:**:**`
- **Always mask**: Encryption keys, TSIG secrets
- Validator runs in CI (`grep -r 'password' app/ --include="*.py"` audit)

---

## 2. Centralized Log Management

All service logs are shipped to a central log store. The log store is configurable.

### Log Pipeline

```
Each service (API, Worker, Agents, DHCP, DNS, NTP)
        ↓ stdout (NDJSON)
Log Collector (Vector / Promtail / Fluentd — one sidecar or DaemonSet)
        ↓
Central Log Store (choose one):
  - Grafana Loki (bundled option — recommended for self-hosted)
  - Elasticsearch (for existing ELK stacks)
  - External (Datadog, Splunk, Cloudwatch — via syslog forwarding)
        ↓
SpatiumDDI Log Viewer API (reads from log store)
        ↓
Built-in Log Viewer UI
```

### Bundled Log Store: Grafana Loki

The default Docker Compose and Helm chart include:
- **Loki** (log aggregation)
- **Grafana** (dashboards + log exploration — optional, for power users)
- **Vector** (log collector / shipper — lightweight, replaces Fluentd/Logstash)

The built-in UI log viewer queries Loki directly via its HTTP API — no Grafana required for basic use.

### Log Retention

Default: 30 days (configurable in Loki config or log store settings).
For compliance environments: ship to long-term S3/GCS/Azure Blob via Loki's object storage backend.

---

## 3. Built-in Log Viewer (Admin UI)

The admin UI includes a **Log Explorer** page at `/admin/logs`.

### Features

**Filtering:**
- Service filter (multi-select: api, worker, agent, dhcp, dns, ntp)
- Level filter (debug/info/warning/error/critical)
- Time range picker (preset: last 15m, 1h, 6h, 24h, 7d — or custom range)
- Free-text search (searches `event` and all string fields)
- Filter by `user_id`, `request_id`, `subnet_id`, `server_id`, `ip_address`

**Display:**
- Reverse chronological order (newest first)
- Expandable log lines (click to see full JSON)
- Color-coded log levels
- Highlight matching search terms
- "Copy as JSON" per log line
- "Follow" mode (live tail, polls every 2 seconds)

**Export:**
- Download filtered log range as NDJSON or CSV

### Log Viewer API

The backend exposes a log query API that the UI consumes:

```
GET /api/v1/logs?
  service=api,worker
  &level=warning,error
  &from=2024-01-15T00:00:00Z
  &to=2024-01-15T23:59:59Z
  &search=subnet_created
  &limit=200
  &cursor=<pagination_cursor>
```

This API translates to the appropriate query language for the configured log store (LogQL for Loki, KQL for Elasticsearch). If no centralized store is configured, it reads from local container stdout via Docker log API (limited functionality).

---

## 4. Audit Log

The audit log is a separate, append-only, structured log of all **data mutations** in SpatiumDDI. It is stored in the **PostgreSQL database** (not the log store) for queryability, immutability guarantees, and compliance.

```
AuditLog
  id (uuid)
  timestamp (timestamptz, indexed)
  user_id (FK → User)
  user_display_name: str    -- denormalized for historical record
  auth_source: str          -- local / ldap / oidc
  source_ip: inet
  user_agent: str
  action: enum(create, update, delete, login, logout, sync, permission_change, ...)
  resource_type: str        -- e.g., "subnet", "ip_address", "dhcp_scope"
  resource_id: str
  resource_display: str     -- human-readable, e.g., "10.1.2.0/24 (Corp LAN)"
  old_value: JSONB          -- full previous state (null for create)
  new_value: JSONB          -- full new state (null for delete)
  changed_fields: str[]     -- list of field names that changed (for updates)
  request_id: str           -- correlates to application log
  result: enum(success, denied, error)
  error_detail: str (nullable)
```

### Audit Log UI

Available at `/admin/audit`:
- Same filtering and live-tail as the log viewer
- Additional filters: `action`, `resource_type`, `result`
- Diff view for updates: shows old vs. new value side-by-side
- Non-deletable — no delete button, enforced at DB level (trigger prevents DELETE)

---

## 5. Prometheus Metrics

Exposed on each service at `:9090/metrics` (separate from the API port).

### API Service Metrics

```
spatiumddi_api_requests_total{method, path_template, status_code}
spatiumddi_api_request_duration_seconds{method, path_template} (histogram)
spatiumddi_api_active_requests (gauge)
spatiumddi_auth_login_total{method, result}   # method=local/ldap/oidc
spatiumddi_auth_token_usage_total{scope}
```

### IPAM Metrics

```
spatiumddi_subnet_utilization_percent{subnet_id, subnet_network, space_id}
spatiumddi_ip_addresses_total{subnet_id, status}
spatiumddi_subnets_total{space_id, status}
```

### DHCP Metrics

```
spatiumddi_dhcp_leases_active{server_id, scope_id}
spatiumddi_dhcp_pool_utilization_percent{server_id, scope_id, pool_id}
spatiumddi_dhcp_sync_last_success_timestamp{server_id}
spatiumddi_dhcp_sync_duration_seconds{server_id}
spatiumddi_dhcp_server_status{server_id}   # 1=online, 0=offline
```

### DNS Metrics

```
spatiumddi_dns_sync_last_success_timestamp{server_id}
spatiumddi_dns_records_total{server_id, zone_id, record_type}
spatiumddi_dns_zones_total{server_id, view_id}
spatiumddi_dns_blocklist_entries_total{list_id}
spatiumddi_dns_server_status{server_id}
```

### Celery / Worker Metrics

```
spatiumddi_celery_tasks_total{task_name, state}   # state=success/failure/retry
spatiumddi_celery_task_duration_seconds{task_name} (histogram)
spatiumddi_celery_queue_length{queue_name}
spatiumddi_celery_workers_active (gauge)
```

### Database Metrics

```
spatiumddi_db_pool_connections{state}   # state=checked_out/idle/overflow
spatiumddi_db_query_duration_seconds{operation} (histogram)
spatiumddi_db_replication_lag_seconds{replica}
```

---

## 6. Health Endpoints

All services expose:

**`GET /health/live`** — Liveness probe
- Returns `200 {"status": "ok"}` if the process is running
- Returns `503` only if the process should be restarted

**`GET /health/ready`** — Readiness probe
- Checks: DB connectivity, Redis connectivity, required config present
- Returns `200 {"status": "ok", "checks": {...}}` if ready to serve traffic
- Returns `503` with failed check details if not ready

**`GET /health/startup`** — Startup probe (for Kubernetes slow-start containers)
- Same as readiness but used only during initial startup

---

## 7. Bundled Grafana Dashboards

Pre-built dashboards shipped with the project (in `deploy/grafana/dashboards/`):

| Dashboard | Contents |
|---|---|
| **SpatiumDDI Overview** | API request rate, error rate, p95 latency, active users |
| **IP Utilization** | Top subnets by utilization, space-wide heatmap, trending |
| **DHCP Health** | Server status, lease counts, pool utilization, sync lag |
| **DNS Health** | Server status, zone counts, sync lag, blocklist hit rate |
| **System Health** | DB connections, replication lag, Redis memory, Celery queues |
| **Audit Activity** | Actions per hour, top users, failed auth attempts |

---

## 8. Alerting Rules

Pre-built Prometheus alerting rules (in `deploy/prometheus/alerts/`):

| Alert | Condition | Severity |
|---|---|---|
| `DHCPServerOffline` | `dhcp_server_status == 0` for 2m | critical |
| `DHCPSyncStale` | `time() - dhcp_sync_last_success > 900` | warning |
| `DHCPPoolNearExhaustion` | `pool_utilization > 90` | warning |
| `DNSServerOffline` | `dns_server_status == 0` for 2m | critical |
| `SubnetNearExhaustion` | `subnet_utilization > 90` | warning |
| `SubnetExhausted` | `subnet_utilization > 99` | critical |
| `APIHighErrorRate` | `rate(requests[5m]{status=~"5.."})/rate(requests[5m]) > 0.05` | warning |
| `DBReplicationLag` | `replication_lag > 30` | warning |
| `CeleryQueueBacklog` | `queue_length > 100` for 5m | warning |
| `BackupFailed` | Checked via Celery task failure metric | critical |


## DNS Agent Telemetry

See [`docs/deployment/DNS_AGENT.md`](deployment/DNS_AGENT.md) §4 for the full protocol.

### Metrics (scraped from the control plane)

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `spatium_dns_agent_up` | gauge | `server_id`, `flavor` | 1 if last heartbeat within 90s |
| `spatium_dns_agent_config_lag_seconds` | gauge | `server_id` | Age of the applied config etag |
| `spatium_dns_zone_serial` | gauge | `server_id`, `zone` | Last reported SOA serial |
| `spatium_dns_failed_ops_total` | counter | `server_id` | RecordOps that exhausted retries |
| `spatium_dns_agent_token_rotations_total` | counter | `server_id` | JWT rotations per server |

Agents are egress-only and do **not** expose a scrape endpoint — all metrics
are derived from the heartbeat body on the control plane.

### Logs

Agent logs are structured JSON (non-negotiable #7) and include the canonical
`service=spatium-dns-agent` binding. Notable events:

- `dns_agent_registered` — bootstrap complete
- `dns_agent_token_rotated` — JWT rotation
- `dns_agent_config_applied` — new config etag applied
- `dns_agent_op_failed` — RecordOp failed (attempt <= 5)
- `dns_agent_stale_sweep` — control-plane Celery task marked servers unreachable
