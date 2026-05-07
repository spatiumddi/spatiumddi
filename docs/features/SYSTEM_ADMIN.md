# System Administration Feature Specification

## Overview

SpatiumDDI includes a comprehensive **System Administration** panel accessible to superadmins. This covers platform-level configuration, service management, health monitoring, notifications, and backup/restore. The goal is that a full platform can be configured entirely from the UI — no manual file editing required in normal operations.

---

## 0. Operator surfaces shipped after `2026.04.16-1`

This document was originally written as a forward-looking spec.
Several admin surfaces have since landed; the rest of this file
remains the design reference for what hasn't shipped yet.

### Trash (`/admin/trash`) — landed in `2026.04.26-1`

Soft-delete + 30-day recovery for IP spaces, blocks, subnets,
DNS zones / records, and DHCP scopes. See
[IPAM § 15.16](IPAM.md#-1516-soft-delete--trash-recovery) for
the full data model + cascade-batch semantics. The admin page
lists deleted rows newest-first with type / since / `q` filters,
per-row Restore (with conflict-detail rendering when a live row
would clash) and Delete-permanently buttons. Nightly purge sweep
is operator-configurable via
`PlatformSettings.soft_delete_purge_days`.

### Platform Insights (`/admin/platform-insights`) — landed in `2026.04.26-1`

Read-only diagnostic surface so operators can see what their
control plane is doing without standing up a separate Prometheus
/ pgwatch / Grafana pipeline. Two tabs:

- **Postgres** — version + DB size, cache hit ratio, current WAL
  position, active vs max connections, longest-running
  transaction (PID / age / state / query / app / client),
  per-table size with autovacuum lag, connections grouped by
  state with idle-in-transaction tinted amber, slow queries from
  `pg_stat_statements` if the extension is installed.
- **Containers** — per-container CPU% (computed the same way
  `docker stats` does), memory used / limit / %, network rx /
  tx, block-IO read / write. Default-filtered to the
  `spatiumddi-*` prefix; set the filter to empty to see every
  container on the host. Reports `available=false` with a
  one-line setup hint when `/var/run/docker.sock` isn't mounted
  into the api container.

Backend at `app/api/v1/admin/postgres.py` (4 endpoints) +
`app/api/v1/admin/containers.py` (1 endpoint). Both
superadmin-gated.

---

## 1. System Health Dashboard

The main **Health Overview** page gives a single-pane-of-glass view of the entire platform.

### Component Health Grid

Each managed component shows a status card:

```
┌────────────────────────────────────────────────────────────┐
│ SYSTEM HEALTH                              Last updated: 2s │
├──────────────┬──────────────┬──────────────┬───────────────┤
│ API Backend  │  Database    │    Redis     │  Celery       │
│  ● ONLINE    │  ● PRIMARY   │  ● ONLINE    │  ● 3 workers  │
│  3 replicas  │  + 2 replicas│  Sentinel ✓  │  0 pending    │
├──────────────┼──────────────┼──────────────┼───────────────┤
│ DHCP Servers │ DNS Servers  │  Log Store    │               │
│  2/2 online  │  3/4 online  │  ● ONLINE     │               │
│  ⚠ kea-02   │  ⚠ bind-03  │  12.4 GB used │               │
└──────────────┴──────────────┴──────────────┴───────────────┘
```

### Per-Component Detail

Clicking any component opens a detail panel:

**Database:**
- Primary/replica status
- Replication lag (ms)
- Active connections / max connections
- Longest running query
- WAL archive status
- Last backup timestamp

**DHCP/DNS Servers:**
- Online/offline status per server
- Last successful sync timestamp
- Config version: IPAM DB version vs. pushed version
- Query rate (DNS), lease count (DHCP)
- Agent connectivity status

**Celery Workers:**
- Worker list with hostname, status, active tasks
- Queue depths per queue
- Failed task count in last hour

**Redis:**
- Sentinel / Cluster topology
- Memory usage
- Hit/miss ratio

### Service Start / Stop / Restart

From the Health Dashboard, superadmins can:
- **Start / Stop / Restart** any managed service (DHCP daemon, DNS daemon, Celery workers, API)
- **Force sync** any DHCP or DNS server (push current config immediately)
- **Test connectivity** to any backend server

Service control operations are executed via:
- **Docker/Compose**: Docker API calls to the container daemon
- **Kubernetes**: Rollout restart via Kubernetes API
- **Bare metal**: SSH to host, `systemctl start/stop/restart <service>`

All service control actions are **audit logged**.

---

## 2. System Configuration Sections

The System Configuration panel is organized into the following sections, each accessible via a settings menu.

### 2.1 Network Configuration (per node/container)

Configure the network settings for each SpatiumDDI node:

```
NodeNetworkConfig
  node_id
  interface: str         -- e.g., eth0, ens3
  ip_address: inet
  prefix_length: int
  gateway: inet
  dns_servers: inet[]
  search_domains: str[]
  mtu: int (default 1500)
  apply_method: enum(netplan, ifupdown, networkd, nmcli)
```

- Changes are applied via the SpatiumDDI agent on the target node
- Preview diff before applying
- Rollback available (reverts to previous config) if connectivity is lost after 60 seconds

### 2.2 Firewall Rules (per node/container)

Manage host firewall rules on SpatiumDDI nodes:

```
FirewallRule
  node_id (nullable — null = apply to all nodes)
  direction: enum(inbound, outbound)
  protocol: enum(tcp, udp, icmp, any)
  source_cidr: cidr (nullable)
  destination_cidr: cidr (nullable)
  port_range: str (nullable)   -- e.g., "80", "1024-65535"
  action: enum(allow, deny, log)
  priority: int
  description: str
```

Implemented via:
- **Linux**: `nftables` rules (preferred) or `iptables`
- **Containers**: Rules applied to Docker network or Kubernetes NetworkPolicy

Default ruleset (enforced, cannot be deleted):
- Allow: HTTPS inbound (443)
- Allow: API port inbound from configured management networks
- Allow: Prometheus scrape from monitoring network
- Allow: all established/related
- Deny: all other inbound

### 2.4 Users and Groups (Platform-Level)

Separate from IP-range-scoped permissions — this section manages:
- Local user accounts (create, edit, enable/disable, reset password, force MFA)
- Local groups (create, manage membership)
- LDAP / OIDC sync configuration:
  - Which LDAP OUs to sync groups from
  - Group attribute mapping
  - Sync interval
  - Manual "Sync Now" trigger
- Last login per user
- Active sessions (with revoke capability)

### 2.5 API Tokens

```
APIToken
  id, name, description
  token_hash: str        -- only hash stored; token shown once on creation
  scope: enum(global, user)
  user_id (FK, nullable) -- if user-scoped
  permissions: JSONB     -- optional restriction (e.g., read-only, specific resource types)
  expires_at: timestamp (nullable)
  last_used_at: timestamp
  created_by_user_id, created_at
  is_active: bool
```

- **Global tokens**: used for automation, CI/CD, Terraform providers — permissions are explicit
- **User tokens**: scoped to the creating user's permission set — cannot exceed user's own rights
- Tokens are displayed once on creation (PBKDF2 hash stored)
- Tokens can be scoped to specific API paths (e.g., `/api/v1/ipam/*` only)

### 2.6 Syslog / Log Forwarding

```
SyslogTarget
  id, name
  protocol: enum(udp, tcp, tcp_tls)
  host, port
  format: enum(rfc3164, rfc5424, json)
  facility: enum(local0..local7, daemon, syslog)
  severity_filter: enum(debug, info, warning, error, critical)
  tls_ca_cert: text (nullable)
  is_enabled: bool
  applies_to: [enum(api, agent, dhcp, dns, audit)]
```

Multiple syslog targets can be configured simultaneously (e.g., local Loki + remote SIEM).

### 2.7 Audit Log Settings

- Retention period (default: 365 days)
- Export audit log: date range → CSV or JSON download
- Archive to S3-compatible storage (optional)
- Audit log is append-only — no delete capability, even for superadmin
- Fields logged on every mutation: user, timestamp, source IP, action, resource, old value, new value

### 2.8 Statistics / Reporting

Dashboard tiles showing platform-wide stats:
- Total IP spaces / blocks / subnets / addresses
- Overall utilization heatmap
- Top 10 subnets by utilization
- DHCP lease counts by server
- DNS query rates by server
- Recent audit log summary (top actors, most-changed resources)
- Scheduled report configuration:
  - Report type (utilization, expiring leases, unused IPs)
  - Schedule (daily/weekly/monthly)
  - Output format (PDF, CSV, JSON)
  - Delivery method (email, webhook, S3)

### 2.9 Backup and Restore

The Backup admin page (`/admin/backup`) ships two tabs: **Manual** (build-and-download / restore-from-file) and **Destinations** (configured remote targets, scheduling, restore-from-destination). Everything described here is reachable from the UI; the same surface is exposed via REST under `/api/v1/backup` and `/api/v1/backup/targets` so operators can drive it from automation.

#### What's in the archive

A single `.zip` per backup, named `spatiumddi-backup-{hostname}-{YYYYMMDD-HHMMSS}.zip`:

| Member | Role |
|---|---|
| `manifest.json` | `app_version`, `schema_version` (alembic head), `hostname`, `created_at`, `dump_format` (`plain` or `custom`), `secret_passphrase_hint` |
| `database.dump` (or `database.sql` for Phase 1 archives) | Full `pg_dump --format=custom` of the SpatiumDDI database |
| `secrets.enc` | Operator-passphrase-wrapped JSON envelope carrying the source install's `SECRET_KEY` + `CREDENTIAL_ENCRYPTION_KEY` (PBKDF2-HMAC-SHA256 600k → AES-256-GCM) |
| `README.txt` | Human-readable note covering format, restore steps, version compatibility |

The archive is the unit operators move around — single-file, easy to ship over SCP / drop into S3 / download to a laptop.

#### Passphrase rules

Operators supply a passphrase at backup time (min 8 chars). The passphrase wraps the `secrets.enc` envelope so the source install's master key never lands in clear on disk anywhere. The same passphrase is required at restore. There's also a `passphrase_hint` field — a free-text label (max 200 chars) that's stored alongside the envelope so operators with multiple archives can remember which key decrypts which one.

The passphrase is **not** the destination's auth credential — every destination type has its own credential fields (S3 keys, SCP password / private key, Azure account key, etc.) which are Fernet-encrypted at rest in the `backup_target.config` JSONB.

#### Destination kinds

All seven destination kinds register in the same driver registry; the UI's destination picker reflects on `GET /backup/targets/kinds` so adding a new kind requires no frontend changes.

| Kind | Tier | Notes |
|---|---|---|
| `local_volume` | 1 | Filesystem path on the api/worker container — production deployments mount this as a docker / k8s volume so archives survive container recycle |
| `s3` | 1 | AWS S3 + S3-compatible (MinIO, Wasabi, Backblaze B2, Cloudflare R2, DigitalOcean Spaces) via the `endpoint_url` field |
| `scp` | 1 | SSH password *or* PEM private key auth (`paramiko`); SFTP write/read; per-call connection lifecycle (no pooling) |
| `azure_blob` | 1 | Azure Storage account via shared-key or full connection string |
| `smb` | 2 | Windows / Samba shares (`smbprotocol`); NTLM auth, optional SMB3 encryption toggle |
| `ftp` | 2 | Plain FTP / FTPS-explicit / FTPS-implicit; passive + active; `verify_tls` toggle for self-signed labs |
| `gcs` | 2 | Google Cloud Storage; service-account JSON key (encrypted at rest) — no ADC by design |

Every driver implements the same four operations: `write` / `list_archives` / `delete` / `download` + a `test_connection` probe (writes a 16-byte random payload, head/stats it, deletes it — same shape as the DNS / DHCP server probes).

#### Schedule + retention

Each target carries:

| Field | Behaviour |
|---|---|
| `schedule_cron` | 5-field UTC cron (`0 2 * * *` for "daily at 02:00 UTC"). Optional — leave blank for manual-only. |
| `retention_keep_last_n` | Keep the N newest archives matching the archive-name regex. |
| `retention_keep_days` | Drop archives whose mtime is older than N days. |
| `last_run_status` / `last_run_at` / `last_run_filename` / `last_run_bytes` / `last_run_duration_ms` / `last_run_error` | Surfaced inline on the target row. `last_run_status=in_progress` acts as a per-target mutex so a slow run can't double up on the next tick. |

Set exactly one of `retention_keep_last_n` / `retention_keep_days`, or neither for no auto-prune. A single Celery beat task (every 60 s) walks all enabled targets, checks each one against its `next_run_at`, and dispatches a one-off backup task per target that's due.

#### Manual triggers

| Action | Endpoint |
|---|---|
| Build + download a fresh archive | `POST /backup/create-and-download` (StreamingResponse, browser saves the zip directly) |
| Restore from a laptop-uploaded archive | `POST /backup/restore` (multipart upload) |
| Run a configured target now | `POST /backup/targets/{id}/run` |
| Test a configured target's connection | `POST /backup/targets/{id}/test` |
| List archives at a configured target | `GET /backup/targets/{id}/archives` |
| Restore from any archive at a target | `POST /backup/targets/{id}/archives/restore` |
| Download an archive from a target through the proxy | `GET /backup/targets/{id}/archives/{filename}/download` |

The proxy-download endpoint streams `driver.download(filename)` straight back to the operator's browser, so SCP / S3 / Azure / SMB / FTP / GCS archives can be pulled to a laptop without giving the operator the destination credentials.

#### Restore — what the server does

Same code path is hit whether the archive comes from an upload or a destination download.

1. **Pre-flight.** Validates archive shape, manifest `format_version` (1 or 2 currently), passphrase. The passphrase verify happens **before** any destructive step so a wrong passphrase is rejected up front.
2. **Pre-restore safety dump.** The current state of the database is snapshotted to `/var/lib/spatiumddi/backups/pre-restore-{ts}.zip` (passphrase `pre-restore-safety`). If the apply fails for any reason, the operator can roll back from this dump.
3. **Connection pool teardown.** SQLAlchemy's engine is disposed and `pg_terminate_backend` kicks every other connection so psql's `--clean` doesn't deadlock against the worker / beat / agents.
4. **Data replay.**
   - Phase 2+ archives (`dump_format: custom`) → `pg_restore --clean --if-exists --no-owner --no-acl --single-transaction --exit-on-error`.
   - Phase 1 archives (plain SQL) → `psql --single-transaction --set=ON_ERROR_STOP=1`.
   - Selective restore (operator ticked specific sections) → `TRUNCATE … RESTART IDENTITY CASCADE` for the selected sections' tables, then `pg_restore --data-only --disable-triggers --table=…` for just those tables. `platform_internal` (alembic_version + oui_vendor) always rides along.
5. **Alembic upgrade-on-restore.** If the archive's `schema_version` is older than this install's expected head, `alembic upgrade head` runs against the freshly-restored database. Same head → no-op. Source head not in this install's chain → restore succeeds, operator gets a clear `"upgrade SpatiumDDI on this destination, then re-run the restore"` warning. The restore returns a `migration` block with `state` (`up_to_date` / `upgraded` / `incompatible_newer` / `unknown` / `failed`), `source_head`, `local_head`, `migrations_applied`.
6. **Cross-install secret rewrap.** Walks every Fernet-encrypted column (22 columns across 16 tables) plus the `__enc__:`-prefixed fields inside `backup_target.config` JSONB; decrypts each with the source key recovered from `secrets.enc`, re-encrypts with the destination's local key, UPDATEs in place. Same-install restores short-circuit with `same_install=true`. The operator no longer has to copy the recovered `SECRET_KEY` into the destination's `.env` manually — it just works.
7. **Audit row.** Inserts a `backup_restored` row into the `audit_log` table on a fresh session — this row sits in the *restored* database (the trail of evidence survives the wipe), and carries the manifest, pre-restore safety path, migration counters, and rewrap counters.

The restore endpoint is superadmin-only. Operators must type the literal phrase `RESTORE-FROM-BACKUP` server-side to confirm, so accidental drag-and-drops don't nuke the install. Selective restore is opt-in via the section checklist on the restore modal.

#### Confirmation phrase + selective restore

Selecting "Selective restore" on the modal reveals a section checklist driven by `GET /backup/sections`. The checklist auto-ticks every non-volatile selectable section the first time the operator flips into selective mode; volatile sections (DHCP leases, DNS query log, DHCP activity log, nmap scan history, metric samples, Celery scratch) stay unticked by default but can be ticked individually. `platform_internal` is forced-on (alembic_version + oui_vendor are install-state, not user data, so they always ride along).

The UI surfaces a TRUNCATE-CASCADE warning on the selective panel because rows in *non-selected* sections that reference wiped data via foreign key are also removed. The reasoning is documented in the warning copy.

#### Cross-install rewrap counters

The restore response carries a `rewrap` block:

```jsonc
{
  "same_install": false,            // true when source + dest keys derive identically
  "rewrapped_rows": 7,              // column-level rewraps
  "rewrapped_jsonb_fields": 0,      // backup_target.config __enc__: rewraps
  "skipped_idempotent_rows": 0,     // already-dest-key-decryptable (re-run / post-restore-created)
  "failed_rows": 0,                 // couldn't decrypt with either key — operator re-enters by hand
  "columns_visited": 22,
  "failures": []                    // first 10 failures with table / column / pk / reason
}
```

Same-install restores return `{"same_install": true, ...counters all zero}`. The operator-facing `note` string adapts to the rewrap state ("no key rewrap was needed" / "re-encrypted N secret values" / "re-encrypted N, but K rows could not be decrypted with either key").

#### What's NOT in the archive

| Excluded | Why |
|---|---|
| DHCP daemon state (leases live in DB; binary state on the agent is regenerated from config) | Volatile — re-syncs from agents on next poll. Sectioned as `leases` (volatile) so operators can opt in for diagnostic backups. |
| DNS daemon state | All records live in DB; the BIND9 zone files are templated by the agent on apply. |
| DNS query log / DHCP activity log | Short-lived diagnostic data. Sectioned as `logs` (volatile). |
| nmap scan history | Often huge, regenerable. Sectioned as `nmap_history` (volatile). |
| Metric samples | Volatile, short retention. Sectioned as `metrics` (volatile). |
| Uploaded asset directory | Phase 2 polish — uploaded files (custom-field attachments, future logo overrides) aren't a separately-tracked path yet. |

---

## 3. Notification Settings

Notifications are sent when system events occur.

### Notification Channels

```
NotificationChannel
  id, name
  type: enum(email, webhook, slack, teams, pagerduty)
  config: JSONB {
    -- email:
    smtp_host, smtp_port, smtp_user, smtp_password_ref, from_address, to_addresses[]
    -- webhook:
    url, method, headers, auth_type, auth_config, payload_template
    -- slack:
    webhook_url, channel, username
    -- teams:
    webhook_url
    -- pagerduty:
    routing_key, severity_mapping: JSONB
  }
  is_enabled: bool
  test_trigger: bool   -- POST to this endpoint triggers a test notification
```

### Notification Rules

```
NotificationRule
  id, name
  event_type: enum(
    subnet_utilization_threshold,   -- e.g., > 80%
    ip_allocated,
    ip_released,
    dhcp_server_offline,
    dns_server_offline,
    dhcp_sync_failed,
    dns_sync_failed,
    lease_pool_exhausted,
    discovery_conflict_found,
    backup_failed,
    audit_suspicious_activity,       -- e.g., bulk delete by non-admin
    certificate_expiring,
    blocklist_update_failed
  )
  conditions: JSONB    -- e.g., { "utilization_gt": 80, "subnet_id": "*" }
  channels: [NotificationChannel]
  cooldown_minutes: int   -- don't re-notify for same event within N minutes
  is_enabled: bool
```

### Email Configuration

Global SMTP settings (used by all email channels unless overridden):

```
SMTPConfig
  host, port
  use_tls: bool
  use_starttls: bool
  username, password_ref
  from_address, from_name
  reply_to: str (nullable)
```

---

## 4. Multi-Role Servers

A physical or virtual server can run **multiple SpatiumDDI service roles simultaneously**. The platform tracks this explicitly.

```
ManagedServer
  id, name, hostname, ip_address
  roles: [enum(api, worker, dhcp, dns, agent)]
  platform: enum(bare_metal, vm, docker, kubernetes_pod)
  os_info: JSONB         -- populated by agent heartbeat
  agent_version: str
  agent_status: enum(online, offline, stale)
  last_heartbeat_at: timestamp
  resource_metrics: JSONB {   -- populated by agent
    cpu_percent, memory_percent, disk_percent, uptime_seconds
  }
```

From the UI, a managed server card shows:
- All roles it is running
- Health of each role
- Resource usage
- Link to per-role configuration
- Start/stop individual roles

This allows a small deployment where one VM runs DHCP + DNS agents simultaneously, and the UI correctly represents that.

---

## 5. Maintenance Mode

Superadmins can put the system into maintenance mode:
- API returns `503 Service Unavailable` with a configurable message
- Frontend shows a maintenance page
- Agent connections are maintained (DHCP/DNS continue from cache)
- Bypass available for superadmin users (via `?bypass_maintenance=<token>`)

---

## 6. Platform Settings (Miscellaneous)

```
PlatformSettings (singleton table)
  app_title: str (default "SpatiumDDI")
  app_logo_url: str (nullable)
  session_timeout_minutes: int (default 60)
  max_api_token_age_days: int (default 365)
  password_policy: JSONB {
    min_length, require_uppercase, require_number, require_special,
    max_age_days, history_count
  }
  ip_allocation_strategy: enum(sequential, random)
  default_lease_time: int
  discovery_scan_enabled: bool
  discovery_scan_interval_minutes: int
  utilization_warn_threshold: int (default 80)
  utilization_critical_threshold: int (default 95)
  subnet_tree_default_expanded_depth: int (default 2)
  auto_logout_minutes: int (default 60)   -- idle session timeout; 0 = disabled
  github_release_check_enabled: bool (default true)
  github_release_check_interval_hours: int (default 24)
```

---

## 7. Version and Release Management

### Current Version Display

The current application version is displayed in the UI header bar and on the System Admin → About page. The version string follows **CalVer** format: `YYYY.MM.DD-N` where N is the release number for that date (starting at 1).

Examples: `2026.04.13-1`, `2026.04.13-2` (hotfix same day)

The version is injected at build time and exposed via:
- UI header (e.g., `v2026.04.13-1`)
- `GET /api/v1/version` — returns `{ "version": "...", "commit": "..." }`

### GitHub Release Check

When `github_release_check_enabled` is true, SpatiumDDI periodically polls the GitHub Releases API for the latest release tag. If a newer version is available:
- A banner appears in the admin UI: "SpatiumDDI 2026.05.01-1 is available — view changelog"
- A notification is sent to configured notification channels if `notify_on_new_release` is enabled
- Superadmins can dismiss the banner or snooze for N days

The check is performed by the Celery beat scheduler (task: `system.check_github_release`). No personal data is sent — only a GET request to the public GitHub API.

```python
# API model
GET /api/v1/version
→ {
    "version": "2026.04.13-1",
    "commit": "abc1234",
    "update_available": true,          # null if check is disabled or failed
    "latest_version": "2026.05.01-1",  # null if up to date or check failed
    "latest_release_url": "https://github.com/spatiumddi/spatiumddi/releases/tag/2026.05.01-1"
  }
```

---

## 8. Metrics Export

### Prometheus (built-in)

Available at `/metrics` when `prometheus_metrics_enabled=true`. Scraped by any Prometheus-compatible tool including Grafana Cloud.

### InfluxDB Export

SpatiumDDI can push metrics to InfluxDB (v1.x and v2.x) for Grafana dashboards:

```
InfluxDBTarget
  id, name
  version: enum(v1, v2)
  url: str                   -- e.g., http://influxdb:8086
  -- v1 fields:
  database: str
  username, password_ref
  -- v2 fields:
  org: str
  bucket: str
  token_ref: str             -- reference to encrypted secret
  measurement_prefix: str (default "spatiumddi_")
  push_interval_seconds: int (default 60)
  is_enabled: bool
```

**Metrics pushed to InfluxDB:**
- IP space/subnet utilization (current allocation %)
- DHCP lease counts per scope
- DNS query rates per server
- API request rates and latencies
- System health status per component

Multiple InfluxDB targets can be configured simultaneously (e.g., local InfluxDB + remote InfluxCloud).
