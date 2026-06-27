# Observability Specification

## Overview

SpatiumDDI's **shipped** observability consists of: structured NDJSON
logging to stdout (§ 1), a **built-in `/logs` viewer** over agent-pushed
DNS/DHCP logs and on-demand Windows reads (§ 3–4½), an append-only audit
log with external forwarding (§ 5), an API-request Prometheus family at
`/metrics` (§ 6), an agent-driven dashboard time-series path (§ 6), and
health endpoints (§ 7). All stdout output is compatible with standard
enterprise stacks (ELK, Loki, Datadog, Splunk) that you point at the
container streams.

> **What is *not* bundled.** Several pieces below are **design intent,
> not shipped code** and are flagged inline as planned: a centralized
> log store + collector (Loki / Grafana / Vector / Promtail — § 2), a
> generic control-plane log-explorer API (§ 3), the IPAM / DHCP / DNS /
> Celery / Database Prometheus metric families (§ 6), and the product
> Grafana dashboard bundle (§ 8). No Loki / Grafana / Vector / Promtail
> service ships in any `docker-compose*.yml` or in `charts/`.

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
  table sizes, connection state, longest-running transaction),
  a **Redis** tab (since `#358`), and per-container CPU / memory /
  network / IO from the local Docker socket. See
  [SYSTEM_ADMIN § 0](features/SYSTEM_ADMIN.md#0-operator-surfaces-shipped-after-20260416-1).

  The **Redis** tab is superadmin-gated and reads Redis `INFO`
  (`/admin/redis/overview` — version / role / used + peak memory /
  fragmentation / connected clients / ops-per-sec / keyspace
  hit-ratio / connected replicas) plus `/admin/redis/keyspace`. Its
  headline panel is the **agent config-wake bus**
  (`/admin/redis/wake-bus`): the Redis pub/sub channels that wake
  parked agent `/config` long-polls the instant a change commits
  (#358), showing publishes-by-resource-class and the live
  subscriber count per channel (one subscriber per parked agent
  long-poll — so it doubles as a "how many agents are connected
  right now?" read). Every endpoint degrades to
  `available: false` + a hint on a Redis error rather than 500ing,
  so a Redis blip never takes the dashboard down. The Operator
  Copilot `get_redis_stats` tool (superadmin-gated, default-off)
  exposes the same rollup to chat.

  **Heartbeat-gated signals (#358 Phase 1).** The same bus also wakes
  the **supervisor heartbeat** long-poll: fleet OS/slot upgrade,
  reboot, role-assignment, and per-appliance host-config (firewall /
  the shared SNMP/NTP/LLDP/timezone broadcast) changes publish to a
  per-appliance channel (the `appliance` resource-class), so the
  supervisor acts in ~0 s instead of waiting up to one heartbeat
  interval. The supervisor stays HTTP-only — the wake is server-side,
  behind the heartbeat's opt-in `wait_seconds` long-poll — so remote /
  Application supervisors that can't reach `sentinel://` are
  unaffected and no Redis port is exposed to agent nodes. Redis-down
  degrades the hold to the bounded heartbeat interval (no storm), and
  a concrete pending command (upgrade / reboot) skips the hold so it
  is never delayed (non-negotiable #5).

  **Fleet-scale instrumentation + broker threshold (#358 Phase 2).**
  The wake-bus panel is the capacity signal for whether this
  Redis-only design still suffices: `total_subscribers` (open
  long-poll connections ≈ live agents + supervisors) and
  publishes-by-class (DNS / DHCP / hostconfig / appliance) are the
  inputs, with Redis `ops-per-sec` on the overview panel as the
  fan-out cost. The explicit threshold that would justify graduating
  to a dedicated broker — written down so the call is data-driven,
  not vibes — is **any of**: (a) sustained **> ~150** concurrent
  agent/supervisor long-poll subscribers (well past the "dozens" #171
  sized for); (b) the first **external / WAN-agent** topology (agents
  reaching the control plane across an untrusted network, where MQTT's
  retained-message / last-will / QoS / WSS-through-:443 wins start to
  matter); or (c) a sustained wake publish rate high enough that the
  per-replica pub/sub fan-out is visible in Redis `ops-per-sec`. Until
  one is crossed, Redis is the right answer and the work is done.

  **Transport abstraction + escalation path (#358 Phase 3 —
  deferred, not built).** `publish_wake()` / `wake_subscription()` in
  `backend/app/core/agent_wake.py` are the transport seam: callers
  only speak "publish a wake on this channel" / "wait for a wake", and
  the Redis pub/sub implementation is private to that module. Swapping
  in **Eclipse Mosquitto** (the escalation documented in #171:
  single-node StatefulSet behind the control-plane VIP, JWT-as-
  username auth + Dynamic-Security ACLs rendered from DB via the
  existing ConfigBundle → trigger-file → host-runner pattern, WSS on
  :443 for remote agents, MQTT kept a wake/notify channel only with
  HTTP config-pull + cache authoritative) would therefore be an
  implementation change inside that one module, not a protocol
  redesign. It is intentionally **not built** — no broker ships until
  a Phase-2 threshold is actually crossed.

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

> **Status: planned / not yet shipped.** No log collector, log store,
> or "log store query" API ships in the repo today. There is **no**
> Loki / Grafana / Vector / Promtail / Fluentd service in any
> `docker-compose*.yml` or in `charts/`, and nothing reads from a
> central store. What ships instead is described in sections 3–4½:
> every service logs **NDJSON to stdout** (section 1), and operators
> who want centralized retention point their own collector at those
> streams (Loki / Elasticsearch / Datadog / Splunk via standard
> Docker / Kubernetes log shipping). The pipeline below is the
> design target, not a bundled deliverable.

### Log Pipeline (planned)

```
Each service (API, Worker, Agents, DHCP, DNS)
        ↓ stdout (NDJSON)        ← shipped
Log Collector (Vector / Promtail / Fluentd)   ← operator-supplied, not bundled
        ↓
Central Log Store (operator's choice):
  - Grafana Loki
  - Elasticsearch (for existing ELK stacks)
  - External (Datadog, Splunk, Cloudwatch)
```

The shipped log surface (sections 3–4½) reads from PostgreSQL tables
the agents push into (BIND9 query log, Kea activity) and from Windows
servers over WinRM — **not** from a central log store. A store-backed
log-viewer API is not implemented.

### Bundled Log Store (planned)

If a bundled store lands, the intended shape is **Loki** (aggregation) +
**Grafana** (optional dashboards / log exploration) + **Vector** (collector
/ shipper). None of these ship today; treat this as a design note.

### Log Retention

For the **shipped** surfaces, retention is per-table and short — the
agent-pushed query/activity logs are pruned at 24 h (see § 4½.3) since
they're operator triage, not analytics. The audit log (§ 5) lives in
PostgreSQL and is not auto-pruned. Long-term log retention belongs in
an external store (Loki / a SIEM) fed from the stdout streams.

---

## 3. Built-in Log Viewer (shipped)

The shipped log viewer lives at **`/logs`**
(`frontend/src/pages/LogsPage.tsx`), backed by the router at
`backend/app/api/v1/logs/router.py`. It is **not** a generic
"control-plane application log explorer" — there is no `/admin/logs`
page and no generic `GET /api/v1/logs?service=&level=` query API. The
control-plane API / worker / beat processes log NDJSON to stdout
(section 1); to browse those, read the container streams
(`docker compose logs -f api worker`) or ship them to an external
store of your choice.

What the `/logs` page actually surfaces is four data-source tabs over
two transports — agent-pushed DNS/DHCP logs and on-demand Windows
server reads:

| Tab | Source | Transport | Detail |
|---|---|---|---|
| **DNS Queries** | BIND9 / PowerDNS query log | agent push → DB | § 4½.1 |
| **DHCP Activity** | Kea DHCPv4 activity | agent push → DB | § 4½.2 |
| **Event Log** | Windows DNS / DHCP Event Log | WinRM read-through | § 4.1 |
| **DHCP Audit** | Windows DHCP per-lease CSV trail | WinRM read-through | § 4.2 |

Each tab does its own server-side filtering, auto-fetches on mount and
on filter change, and has an explicit Refresh button (§ 4.3). The
agent tabs read from narrow PostgreSQL tables (§ 4½.3); the Windows
tabs are on-demand reads with no log-shipping. The endpoints are the
`POST /api/v1/logs/{query,dhcp-audit,dns-queries,dhcp-activity}` family
plus `GET /api/v1/logs/{sources,agent-sources}` — see sections 4 and
4½ for the full surface.

> **Planned (not shipped):** a unified explorer over the control-plane
> NDJSON streams (service / level / free-text filtering, live tail,
> NDJSON/CSV export) backed by a log-store query API. Today those
> streams are stdout-only.

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

**Vocabulary.** A curated set of typed events generated from a
resource × verb cross-product (plus a handful of special-cased
names) — e.g. `space.created`, `subnet.bulk_allocate`,
`dns.zone.updated`, `dhcp.scope.deleted`, `auth.user.created`,
`integration.kubernetes.created`. The live list is served by
`GET /api/v1/webhooks/event-types`. Subscriptions with no event-type
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

The API service exposes a Prometheus scrape endpoint at `/metrics` on
the main API port (gated by the `prometheus_metrics_enabled` setting,
default on; registered in `backend/app/main.py`).

> **Implemented today:** only the **API-request family** below, defined
> and emitted in `backend/app/metrics.py` via a Starlette middleware on
> every request. The IPAM / DHCP / DNS / Celery / Database families that
> follow are **planned, not emitted** — no collector or exporter
> populates them, and scraping `/metrics` returns the request family
> (plus the default `prometheus_client` process / GC metrics) only. The
> two `spatiumddi_auth_*` counters are *declared* in `metrics.py` but
> not yet incremented by any call site, so they read zero. For the
> shipped, agent-driven dashboard metrics path, see
> **Built-in Dashboard Time-Series** below — that one is real.

### API Service Metrics (implemented)

```
spatiumddi_api_requests_total{method, path_template, status_code}
spatiumddi_api_request_duration_seconds{method, path_template} (histogram)
spatiumddi_api_active_requests (gauge)
```

Plus two counters declared but not yet wired to a call site (read zero):

```
spatiumddi_auth_login_total{method, result}   # planned — method=local/ldap/oidc
spatiumddi_auth_token_usage_total{scope}       # planned
```

### IPAM Metrics (planned — not emitted)

```
spatiumddi_subnet_utilization_percent{subnet_id, subnet_network, space_id}
spatiumddi_ip_addresses_total{subnet_id, status}
spatiumddi_subnets_total{space_id, status}
```

### DHCP Metrics (planned — not emitted)

```
spatiumddi_dhcp_leases_active{server_id, scope_id}
spatiumddi_dhcp_pool_utilization_percent{server_id, scope_id, pool_id}
spatiumddi_dhcp_sync_last_success_timestamp{server_id}
spatiumddi_dhcp_sync_duration_seconds{server_id}
spatiumddi_dhcp_server_status{server_id}   # 1=online, 0=offline
```

### DNS Metrics (planned — not emitted)

```
spatiumddi_dns_sync_last_success_timestamp{server_id}
spatiumddi_dns_records_total{server_id, zone_id, record_type}
spatiumddi_dns_zones_total{server_id, view_id}
spatiumddi_dns_blocklist_entries_total{list_id}
spatiumddi_dns_server_status{server_id}
```

### Built-in Dashboard Time-Series (agent-driven) — shipped

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

### Celery / Worker Metrics (planned — not emitted)

```
spatiumddi_celery_tasks_total{task_name, state}   # state=success/failure/retry
spatiumddi_celery_task_duration_seconds{task_name} (histogram)
spatiumddi_celery_queue_length{queue_name}
spatiumddi_celery_workers_active (gauge)
```

> Worker / beat health is currently surfaced through the
> `GET /health/platform` rollup (§ 7) and the Platform Insights admin
> surface, not through Prometheus metrics.

### Database Metrics (planned — not emitted)

```
spatiumddi_db_pool_connections{state}   # state=checked_out/idle/overflow
spatiumddi_db_query_duration_seconds{operation} (histogram)
spatiumddi_db_replication_lag_seconds{replica}
```

> Postgres diagnostics (DB size, cache hit, WAL position, slow
> queries, connection state) ship today through the **Platform
> Insights** admin surface (`/admin/platform-insights`, described in
> the Overview), which queries Postgres `pg_stat_*` views directly
> rather than exporting Prometheus metrics.

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

> **Status: planned / not shipped as a product bundle.** No
> operator-facing Grafana dashboard set ships with the product, and
> the dashboards below depend on the planned (un-emitted) metric
> families in § 6. What *does* exist is a single **perf-testing**
> "war room" dashboard at
> [`perf/dashboards/grafana/dashboards/warroom.json`](../perf/dashboards/grafana/dashboards/warroom.json)
> (with provisioning under `perf/dashboards/grafana/provisioning/` and
> a matching scrape config at `perf/dashboards/prometheus/prometheus.yml`).
> That stack is for the load/soak test harness under `perf/`, not for
> production observability, and it scrapes the perf exporters rather
> than the product `/metrics` endpoint's request family.

The intended product dashboard bundle, once the § 6 metric families
are emitted, would be:

| Dashboard | Contents |
|---|---|
| **SpatiumDDI Overview** | API request rate, error rate, p95 latency, active users |
| **IP Utilization** | Top subnets by utilization, space-wide heatmap, trending |
| **DHCP Health** | Server status, lease counts, pool utilization, sync lag |
| **DNS Health** | Server status, zone counts, sync lag, blocklist hit rate |
| **System Health** | DB connections, replication lag, Redis memory, Celery queues |
| **Audit Activity** | Actions per hour, top users, failed auth attempts |

Until then, the **shipped** in-product equivalents are the Dashboard
sub-tabs and Platform Insights surfaces described in the Overview.

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
| `asn_holder_drift` / `asn_whois_unreachable` / `rpki_roa_expiring` / `rpki_roa_expired` | ASN / RPKI signals from the WHOIS + ROA refresh tasks (#85). |
| `domain_expiring` / `domain_nameserver_drift` / `domain_registrar_changed` / `domain_dnssec_status_changed` | Registry-side domain signals (#87). The two transition-once rules latch their value into `last_observed_value` JSONB so a single flip fires exactly one event auto-resolved after 7 d. |
| `circuit_term_expiring` / `circuit_status_changed` | WAN circuit alerts (#93). Status-changed only fires on `suspended` / `decom` transitions and auto-resolves after 7 d. |
| `service_term_expiring` / `service_resource_orphaned` | Service-catalog alerts (#94). Orphan-sweep walks `network_service_resource` join rows whose target was deleted. |
| `compliance_change` | Audit-log scanner rule type (#105). One event per mutation against a classification-flagged subnet (or descendant IP / DHCP scope). Params: `classification` (one of `pci_scope` / `hipaa_scope` / `internet_facing`) and `change_scope` (one of `any_change` / `create` / `delete`). Watermark column on the rule baselines to `now()` on first run so historical audit history doesn't retro-page operators. Resource resolution falls back to `audit_log.old_value.subnet_id` for delete actions where the live row no longer exists. Three disabled seed rules ship at first boot (PCI / HIPAA / internet-facing). Auto-resolves after 24 h. |

Admin UI lives at `/admin/alerts` — rules CRUD + live events viewer
(15 s refetch) + per-event `Resolve` to manually silence a known-
noisy event.

Conformity evaluations (#106) also feed this surface: a
`ConformityPolicy` can pin `fail_alert_rule_id`, and a `pass→fail`
transition on a (policy, resource) pair opens an `AlertEvent` row
against the named alert rule with `subject_type="conformity"` so
operators see drift in the same dashboard. Configure the wiring in
the policy edit modal at `/admin/conformity`.

### 9.2 Prometheus alerting rules (external)

Reference Prometheus alerting rules for the `spatiumddi_*` metrics
(a perf-testing Prometheus config already lives under
`perf/dashboards/prometheus/`):

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
