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
| `service` | `api`, `worker`, `beat`, `agent`, `dhcp`, `dns` |
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
Each service (API, Worker, Agents, DHCP, DNS)
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
- Service filter (multi-select: api, worker, agent, dhcp, dns)
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

## 4. Windows Server Log Viewer

Separate from the SpatiumDDI Log Explorer (section 3), there is a **Windows Server Log Viewer** at `/logs` that pulls logs directly off any Windows server SpatiumDDI has WinRM credentials for. Zero agent, no log-shipping — it's a read-through on demand, server-side filtered.

Two tabs today, plumbed through the driver abstraction so future sources (agent logs streamed over the bus, control-plane service logs) can plug in via `driver.available_log_names()` + `driver.get_events()` without touching the router.

### 4.1 Event Log tab

Filtered read of Windows Event Log via `Get-WinEvent -FilterHashtable`. Every filter runs server-side so the full log never crosses the wire.

**Inventory per driver:**

| Driver | Logs exposed |
|---|---|
| `WindowsDNSDriver` | `DNS Server` (classic) + `Microsoft-Windows-DNSServer/Audit` |
| `WindowsDHCPReadOnlyDriver` | `Microsoft-Windows-Dhcp-Server/Operational` + `Microsoft-Windows-Dhcp-Server/FilterNotifications` |

The DNS *Analytical* log is deliberately omitted — it's per-query, noisy, and better viewed in MMC's Event Viewer.

**Filters** (all optional, combined server-side):

- `log_name` — one of the entries above
- `level` — 1-5 per Windows severity codes (Critical / Error / Warning / Information / Verbose)
- `max_events` — 1-500 (truncation notice in the UI when the cap is hit)
- `since` — ISO datetime; frontend uses `<input type="datetime-local">` so we don't drag in a picker library
- `event_id` — filter to a single event code

**Endpoints:**

```
GET  /api/v1/logs/sources
  → [{server_id, name, driver, available_logs: [...]}, ...]

POST /api/v1/logs/query
  body: {server_id, log_name, level?, max_events?, since?, event_id?}
  → [{time_created, level, event_id, provider_name, message, ...}, ...]
```

**Error handling.** `Get-WinEvent` raises `System.Diagnostics.Eventing.Reader.EventLogException` when the log doesn't exist, zero events match, or a FilterHashtable key doesn't apply — it bypasses `-ErrorAction SilentlyContinue` because the exception is terminating at the .NET provider level. `fetch_events()` catches that explicitly and falls through to a generic "no data" matcher so those cases return `[]` cleanly instead of surfacing `"The parameter is incorrect"` to the UI as a 502.

### 4.2 DHCP Audit tab

Windows DHCP writes its **per-lease trail** to CSV-style files at `C:\Windows\System32\dhcp\DhcpSrvLog-<Day>.log` — one file per weekday (`Mon` / `Tue` / … / `Sun`), rotating, on by default on every DHCP role. These are the grants / renewals / releases / NACKs the Event Log itself does not cover.

**Helper** — `backend/app/drivers/windows_dhcp_audit.py`. Reads the day's file over WinRM via `Get-Content -Raw`, skips past the header block, feeds the remainder to `ConvertFrom-Csv`, returns the last N rows. Handles both UTF-16 (Unicode BOM) and ASCII — some Windows builds write one, some the other. Event-code → human label map covers the documented codes; unknown codes come through as `Code <n>` so new Windows releases don't drop silently. Access denied / missing file / locked-by-rotation all return `[]` instead of 500.

**Driver surface** — `WindowsDHCPReadOnlyDriver.get_dhcp_audit_events(day, max_events)` — thin wrapper routed through the standard `_load_credentials` + `_run_ps` path.

**Endpoint:**

```
POST /api/v1/logs/dhcp-audit
  body: {server_id, day?, max_events?}   # day: Mon..Sun, null = today
  → [{time, event_id, event_label, ip, hostname, mac, description}, ...]
```

**UI columns** — Time / Event (code + label) / IP / Hostname / MAC / Description. Event-dot colour mirrors Windows severity families:

| Colour | Event family |
|---|---|
| green | Grant / renew / successful ack |
| red | Failure / conflict / NACK |
| muted | Release / expire / scavenge |
| blue | Auth lifecycle (server started, authorised, etc.) |

The **event-code picker** shows distribution for the current day (e.g. `10 — Lease granted (238)`) so an operator can filter down to a specific outcome without knowing codes by heart.

### 4.3 Dispatch & auto-fetch

The frontend uses `useQuery` keyed on every filter (server / log / level / max / since), so:

1. Entering the page fires the first query immediately against the first available server + log.
2. Changing any filter (including the Since datetime picker) fires a re-fetch automatically.
3. `staleTime: Infinity` means switching tabs + back doesn't hit the DC unless the user actually changes something.
4. The explicit **Refresh** button calls `refetch()` to bypass the cache on demand.

Shared helpers (`ServerPicker` / `MaxEventsPicker` / `FilterSearch` / `RefreshButton` / `QueryErrorBanner` / `QueryingIndicator` / `TruncatedNotice`) are reused across both tabs.

---

## 5. Audit Log

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

### 5.1 External forwarding

Every committed `AuditLog` row can be forwarded to external systems in
near-real-time. Two independent channels — configure either, both, or
neither under **Settings → Audit Event Forwarding**.

**Delivery guarantee.** Best-effort. A dedicated `asyncio` task runs
outside the commit path, so the audit write always succeeds before
forwarding is even attempted. A failing collector never rolls back
a DDI mutation. Operators who need at-least-once replay should point
the webhook at a queue (NATS / Kafka / Redis) and let that be the
durable layer.

**Implementation.** SQLAlchemy `after_commit` listener in
`app/services/audit_forward.py`. Registered once at import time via
`app/main.py`. Snapshots new `AuditLog` rows inside `after_flush`
(while they still carry committed values), then in `after_commit`
schedules an `asyncio.create_task` per row so slow collectors don't
serialize the queue.

**Channel 1: Syslog (RFC 5424).**
- Transport: UDP or TCP. TLS deferred (cert management complexity).
- Format: `<PRI>1 <ISO-TIME> <HOST> spatiumddi - AUDIT - <JSON-MSG>`.
  MSG body is compact JSON; Splunk / Elastic / Graylog auto-detect
  and parse it.
- Severity mapping: `result="success"` → 6 (info),
  `result="denied"` → 4 (warn), `result="failed"` → 3 (err).
- Facility configurable 0–23; default 16 (local0).
- UDP path uses `socket.sendto` (one syscall, no backpressure). TCP
  path uses `asyncio.open_connection` with a 5 s timeout so a
  tarpitted collector can't stall the event loop.

**Channel 2: HTTP webhook.**
- POST with `Content-Type: application/json`; 5 s timeout.
- Optional `Authorization` header — sent verbatim (include the
  scheme: `Bearer …` / `Basic …`).
- Non-2xx responses are logged via structlog at warning but not
  retried. Use a reverse-proxy / queueing layer for replay.
- Payload shape matches the syslog JSON MSG body:
  ```json
  {
    "id": "…", "timestamp": "2026-04-21T10:00:00+00:00",
    "action": "create", "resource_type": "dns_zone",
    "resource_id": "…", "resource_display": "example.com.",
    "result": "success", "user_id": "…",
    "user_display_name": "admin", "auth_source": "local",
    "changed_fields": [], "old_value": null, "new_value": {…}
  }
  ```

**Known gap.** Celery-scheduled audits (e.g. the lease-pull
housekeeping row) may not forward — Celery wraps the task body in
`asyncio.run`, which closes its loop before the scheduled dispatch
task runs. Operator-triggered audit events (the 99% case) forward
reliably from the API worker's long-running loop. If scheduled-task
forwarding turns out to matter, the fix is an awaitable drain in
each task wrapper.

---

## 6. Prometheus Metrics

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

## 7. Health Endpoints

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

## 8. Bundled Grafana Dashboards

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

## 9. Alerting

SpatiumDDI exposes two parallel paths for alerts. Both can run at
once — they serve different audiences.

### 9.1 In-app alerts framework (native)

Operator-authored rules stored in the `alert_rule` table. The
evaluator (`backend/app/services/alerts.py:evaluate_all`) runs on a
60 s Celery beat tick; `POST /api/v1/alerts/evaluate` forces an
immediate pass. Each match opens an `alert_event` row (partial
index on `(rule_id, subject_type, subject_id) WHERE resolved_at IS
NULL` keeps dedup O(1)); events resolve automatically when the
subject clears. Delivery reuses the **Audit Event Forwarding**
syslog + webhook targets — one SIEM destination for both audit and
alerts.

Rule types shipped today:

| Type | Fires when |
|---|---|
| `subnet_utilization` | `Subnet.utilization_percent ≥ threshold`; honours `PlatformSettings.utilization_max_prefix_{ipv4,ipv6}` so PTP / loopback subnets can't trip the alarm. |
| `server_unreachable` | Any DNS / DHCP server whose status is `unreachable` or `error`. `server_type` filters the family. |

Admin UI lives at `/admin/alerts` — rules CRUD + live events viewer
(15 s refetch) + per-event `Resolve` to manually silence a known-
noisy event.

### 9.2 Prometheus alerting rules (external)

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
