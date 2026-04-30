# Observability Specification

## Overview

SpatiumDDI provides a fully integrated observability stack: structured logging shipped to a centralized store, a **built-in log viewer in the admin UI**, Prometheus metrics, health endpoints, and a Grafana dashboard bundle. No external observability tooling is required for basic operations, but all outputs are compatible with standard enterprise stacks (ELK, Loki, Datadog, Splunk).

### Native admin surfaces (since `2026.04.26-1`)

Two admin surfaces give operators platform visibility without
standing up Prometheus / Grafana:

- **Dashboard sub-tabs** (`/dashboard`) — Overview / IPAM / DNS /
  DHCP. The DNS and DHCP tabs show full-width Recharts
  time-series of query rate (BIND9 statistics-channels) and
  traffic (Kea `statistic-get-all`), driven by the per-server
  `dns_metric_sample` / `dhcp_metric_sample` tables.
- **Platform Insights** (`/admin/platform-insights`) — Postgres
  diagnostics (DB size, cache hit, WAL position, slow queries,
  table sizes, connection state, longest-running transaction)
  and per-container CPU / memory / network / IO from the local
  Docker socket. See
  [SYSTEM_ADMIN § 0](features/SYSTEM_ADMIN.md#0-operator-surfaces-shipped-after-20260416-1).

The native surfaces are intentionally tactical — for long-term
metrics retention and cross-instance dashboards, fall back to
Prometheus scraping `/metrics` (see § 6).

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

## 4½. SpatiumDDI Agent Log Viewer (BIND9 + Kea)

Two additional tabs on the same Logs page, sourced from in-container agents instead of WinRM.

### 4½.1 DNS Queries tab

**Source.** BIND9's `query-log` channel. The control-plane template (`backend/app/drivers/dns/templates/bind9/named.conf.j2`) renders a `logging { channel queries_channel { file "/var/log/named/queries.log" versions 5 size 50m; ... }; category queries { queries_channel; }; };` block when `DNSServerOptions.query_log_enabled` is on.

**Pipeline.**

1. BIND9 writes lines to `/var/log/named/queries.log` (path is templated; agents respect `DNS_QUERY_LOG_PATH` env override).
2. The DNS agent's `QueryLogShipper` thread (`agent/dns/spatium_dns_agent/query_log_shipper.py`) tails the file like `tail -F`, batches up to 200 lines or 5 s of activity (whichever first), and POSTs to `POST /api/v1/dns/agents/query-log-entries`.
3. The control plane parses each line into `DNSQueryLogEntry` rows (`backend/app/services/logs/bind9_parser.py`) — timestamp, client IP+port, qname, qclass, qtype, flags, view. The original raw line is preserved alongside.
4. UI calls `POST /logs/dns-queries` with filters (`q` substring, `qtype`, `client_ip`, `since`, `max_events`); newest first.

**Filters.** Server picker, qtype (A / AAAA / MX / …), client IP, datetime-`since`, max events, raw-substring search.

**Resilience.** File-not-yet-present (operator hasn't enabled `query_log_enabled`) is silent — the shipper sleeps + re-checks. Inode-change rotation is detected and the file re-opened. Control-plane errors drop the batch (logs are triage data, not durable). Buffer is capped at 5000 lines; older half is trimmed if the control plane stays unreachable.

### 4½.2 DHCP Activity tab

**Source.** Kea's `kea-dhcp4` logger. The agent's `render_kea` adds two `output_options` to the rendered config — `stdout` (existing `docker logs` workflow stays intact) **and** `/var/log/kea/kea-dhcp4.log` with in-process rotation (`maxsize: 50_000_000`, `maxver: 5`, `flush: true`).

**Pipeline.** Same shape as the DNS path — `LogShipper` thread (`agent/dhcp/spatium_dhcp_agent/log_shipper.py`) → `POST /api/v1/dhcp/agents/log-entries` → `kea_parser.py` → `DHCPLogEntry` rows.

**Parser fields.** Severity (`DEBUG` / `INFO` / `WARN` / `ERROR` / `FATAL`), Kea log code (`DHCP4_LEASE_ALLOC`, `DHCP4_LEASE_DECLINE`, `DHCP4_PACKET_PROCESS_STARTED`, etc), MAC address (after `hwtype=N`), lease IP, transaction id. Lines that don't match the regex still get stored with the raw text — Kea has hundreds of log codes and we only structure-parse the most common shape.

**Filters.** Server picker, severity, log code (free-form text — operators paste e.g. `DHCP4_LEASE_ALLOC`), MAC, IP, datetime-since, max events, raw substring.

### 4½.3 Storage + retention

Two narrow tables (`dns_query_log_entry`, `dhcp_log_entry`) with composite indexes on `(server_id, ts)`. Migration `d8c5f12a47b9_query_log_entries`. FK cascade drops a server's rows when the server is removed.

Retention is a nightly Celery task (`app.tasks.prune_logs.prune_log_entries`) that deletes rows older than 24 h. This is *operator triage*, not analytics — for longer history, ship to Loki / a SIEM.

**Why so short?** Query logs are a firehose. A busy resolver doing 100 qps writes 8.6M rows per day; even 24 h of that is significant. Anyone needing days of history should run Loki alongside and the agent push can coexist with stdout shipping.

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

Every committed `AuditLog` row fans out to every enabled
`AuditForwardTarget`. Each target picks its own transport, wire
format, and optional severity / resource-type filter, so a single
SpatiumDDI instance can feed (for example) a Splunk HTTP collector,
a QRadar LEEF syslog relay, and a compliance-only JSON-lines sink at
the same time. Configure under **Settings → Audit Event Forwarding**.

**Delivery guarantee.** Best-effort. A dedicated `asyncio` task
runs outside the commit path, so the audit write always succeeds
before forwarding is attempted. A failing collector never rolls back
a DDI mutation. Each target's failure is isolated — one dead SIEM
doesn't affect the others. Operators who need at-least-once replay
should point a webhook at a queue (NATS / Kafka / Redis) and let
that be the durable layer.

**Implementation.** SQLAlchemy `after_commit` listener in
`app/services/audit_forward.py`. Snapshots new `AuditLog` rows
inside `after_flush`, then in `after_commit` schedules one
`asyncio.create_task` per (row, target) pair so slow collectors
don't serialize the queue.

**Transports.**
- **Syslog UDP** — `socket.sendto`, fire-and-forget, one syscall.
- **Syslog TCP** — `asyncio.open_connection`, 5 s connect timeout.
- **Syslog TLS** — same as TCP plus `ssl.create_default_context()`;
  an optional PEM-encoded CA bundle per target lets operators pin
  a private CA without touching the system store.
- **HTTP webhook** — `httpx.AsyncClient`, 5 s timeout,
  `Content-Type: application/json`, optional `Authorization`
  header sent verbatim. The `webhook_flavor` column picks between
  generic JSON, **Slack** (`mrkdwn` block), **Teams**
  (`MessageCard`), and **Discord** (`embed`) so chat-channel
  delivery doesn't need a separate adapter.
- **SMTP email** — stdlib `smtplib` driven through
  `asyncio.to_thread` (no extra dep). Supports `starttls` / `ssl` /
  plaintext, optional auth (Fernet-encrypted password at rest).
  Subject + body rendered from the audit row.

**Syslog output formats** (per-target). Webhook targets always
deliver JSON and ignore this setting.

| Format | Use case |
|---|---|
| `rfc5424_json` | Default. Modern SIEMs (Splunk, Elastic, Graylog) auto-parse the JSON body after the RFC 5424 header. |
| `rfc5424_cef` | ArcSight + many commercial SIEMs. `CEF:0\|SpatiumDDI\|SpatiumDDI\|1.0\|<rtype:action>\|<display>\|<sev>` header + `act=`/`suser=`/`cs1=…` extensions. CEF severity mapped as info=3, error=6, denied=9. |
| `rfc5424_leef` | IBM QRadar native format. LEEF 2.0 with `^` field delimiter (safer over UDP than the default tab). |
| `rfc3164` | Legacy BSD syslog: `<PRI>Mmm dd HH:MM:SS host tag: <JSON>`. For collectors that don't speak RFC 5424. |
| `json_lines` | Bare JSON per line — no syslog framing. For raw Logstash / Fluentd / Vector inputs. |

**Severity mapping** (RFC 5424): `result="success"` → 6 info,
`result="denied"` → 4 warn, `result="failed"` / `"error"` → 3 err.
Facility is per-target and configurable 0–23 (default 16 = local0).

**Per-target filters.**
- `min_severity` — drop events below this bucket (info / warn /
  error / denied). Null = forward everything.
- `resource_types` — optional allowlist of `AuditLog.resource_type`.
  Null / empty = forward everything. Useful for compliance-scoped
  targets that should only see, say, auth + IPAM events.

**JSON payload shape** (the body used by `rfc5424_json`, `json_lines`,
and the webhook):
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

**Backward compatibility.** The pre-multi-target flat columns on
`platform_settings` (`audit_forward_syslog_*`, `audit_forward_webhook_*`)
are preserved and migrated into one `audit_forward_target` row apiece
on upgrade. When the targets table is empty the service falls back to
those flat columns so existing installs keep forwarding without
operator intervention. They are slated for removal in a future release.

**Known gap.** Celery-scheduled audits (e.g. the lease-pull
housekeeping row) may not forward — Celery wraps the task body in
`asyncio.run`, which closes its loop before the scheduled dispatch
task runs. Operator-triggered audit events (the 99% case) forward
reliably from the API worker's long-running loop. If scheduled-task
forwarding turns out to matter, the fix is an awaitable drain in
each task wrapper.

---

## 5b. Typed-event webhooks

Distinct from audit-forward: this is a curated **typed-event
surface** for downstream automation, not raw audit rows. Operators
configure subscriptions at `Admin → Webhooks` (`/admin/webhooks`).

**Vocabulary.** 96 typed events generated from a resource × verb
cross-product — e.g. `space.created`, `subnet.bulk_allocate`,
`dns.zone.updated`, `dhcp.scope.deleted`, `auth.user.created`,
`integration.kubernetes.created`. Subscriptions with no event-type
filter match everything.

**Pipeline.** Audit-row commit triggers an `EventOutbox` write per
matching `EventSubscription`. Celery beat (`event-outbox-drain`,
every 10 s) claims rows with `SELECT … FOR UPDATE SKIP LOCKED`,
signs each POST with `hmac(secret, ts + "." + body, sha256)`, and
delivers. Failures retry with exponential backoff
(2 / 4 / 8 … 600 s capped) up to `max_attempts` (default 8 ≈
8.5 min cumulative). Permanent failures flip to `state="dead"` for
operator review.

**Wire format.**
- Body: full audit-row JSON snapshot.
- Headers: `Content-Type: application/json`,
  `User-Agent: SpatiumDDI/<ver>`,
  `X-SpatiumDDI-Event: <event_type>`,
  `X-SpatiumDDI-Delivery: <outbox_id>`,
  `X-SpatiumDDI-Timestamp: <unix-seconds>`,
  `X-SpatiumDDI-Signature: sha256=<hex>`.
- Receivers verify by recomputing the HMAC over `<ts>.<body>` and
  rejecting requests whose timestamp is outside their tolerance
  window (default 5 minutes is sensible).
- Operator-supplied custom headers are applied last, but the
  `X-SpatiumDDI-*` namespace is reserved and can't be overridden.

**Delivery semantics.** At-least-once modulo the audit-row commit
window. The outbox write happens after the audit row commits, so a
process crash between the two drops the event. Receivers should
de-duplicate on `event_id` (= `AuditLog.id`).

**Operator controls.**
- One-time secret reveal on subscription create (also rotatable on
  edit). Stored Fernet-encrypted; the response only reveals
  `secret_set: bool` afterwards.
- Test button synthesizes a `test.ping` through the same pipeline
  with an inline pass/fail flash.
- Per-subscription deliveries panel with live state + manual
  **Retry now** on failed/dead rows.

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

### Built-in Dashboard Time-Series (agent-driven)

For operators who don't run Prometheus, SpatiumDDI ships a minimal
self-contained time-series path used by the built-in dashboard
charts. Agents poll their local daemons every 60 s and POST
per-bucket counter *deltas* to the control plane:

| Surface | Source | Endpoint |
|---|---|---|
| **BIND9** | `statistics-channels` XMLv3 (`/xml/v3/server`, bound to `127.0.0.1:8053`, injected at render time) | `POST /api/v1/dns/agents/metrics` |
| **Kea** | `statistic-get-all` over the existing control socket | `POST /api/v1/dhcp/agents/metrics` |

The control plane stores deltas in two small tables —
`dns_metric_sample(server_id, bucket_at, queries_total, noerror,
nxdomain, servfail, recursion)` and `dhcp_metric_sample(server_id,
bucket_at, discover, offer, request, ack, nak, decline, release,
inform)`. Retention is enforced by the `prune_metric_samples`
Celery task (daily, default 7 days).

The dashboard queries two read endpoints:

```
GET /api/v1/metrics/dns/timeseries?window={1h|6h|24h|7d}&server_id={uuid?}
GET /api/v1/metrics/dhcp/timeseries?window={1h|6h|24h|7d}&server_id={uuid?}
```

Server-side `date_bin` downsamples transparently — 60 s buckets for
windows ≤ 24 h, 5 min buckets for 7 d. That keeps the response under
~2 k points regardless of retention.

Windows DNS / DHCP drivers don't report through this path yet (they'd
need `Get-DnsServerStatistics` / `Get-DhcpServerv4Statistics` calls
over WinRM). The dashboard card shows "no data yet" rather than an
error in that case.

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

**`GET /health/platform`** — Per-component rollup for the dashboard
- Unlike `/health/ready` (which returns a single binary verdict for
  orchestrator probes), this endpoint enumerates each control-plane
  piece so the UI can show a per-component status dot.
- Response: `200 {"status": "ok"|"degraded", "components": [...]}`.
  Individual component failures **never** make the endpoint itself
  fail — the caller always gets a 200 with the rollup so a single
  flaky component doesn't blank the dashboard card.
- Components checked:
  - `api` — always `ok` when the endpoint responds
  - `postgres` — `SELECT 1` with round-trip latency in the detail
  - `redis` — `PING` with round-trip latency
  - `celery-workers` — `celery_app.control.inspect(timeout=2).ping()`
    run in a threadpool with a 3 s overall timeout (so a dead broker
    can't hang the endpoint). Detail carries the worker count; full
    list surfaces on UI hover.
  - `celery-beat` — reads the `spatium:beat:heartbeat` key from
    Redis (written by `app.tasks.heartbeat.beat_tick` every 30 s with
    a 5-min TTL). Age folded into `ok` (≤ 90 s), `warn` (> 90 s — two
    beat intervals missed), or `error` (missing — beat stopped or
    down > 5 min).
- Consumed by the Dashboard → Platform Health card. Authenticated
  dashboard users only — the endpoint itself is unauthenticated for
  parity with the other `/health/*` probes, and exposes nothing a
  normal monitoring probe doesn't already see.

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
