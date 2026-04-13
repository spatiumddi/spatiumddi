# System Administration Feature Specification

## Overview

SpatiumDDI includes a comprehensive **System Administration** panel accessible to superadmins. This covers platform-level configuration, service management, health monitoring, notifications, and backup/restore. The goal is that a full platform can be configured entirely from the UI — no manual file editing required in normal operations.

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
│ DHCP Servers │ DNS Servers  │  NTP Servers │  Log Store    │
│  2/2 online  │  3/4 online  │  2/2 online  │  ● ONLINE     │
│  ⚠ kea-02   │  ⚠ bind-03  │              │  12.4 GB used │
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
- **Start / Stop / Restart** any managed service (DHCP daemon, DNS daemon, NTP daemon, Celery workers, API)
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

### 2.3 Time and Date Configuration

```
TimeConfig
  node_id (nullable — null = global default)
  timezone: str          -- IANA timezone, e.g., "America/New_York"
  ntp_servers: str[]     -- upstream NTP servers for the node itself
  ntp_sync_enabled: bool
```

Also shows current time on each node and NTP sync status (stratum, offset, jitter).

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
  applies_to: [enum(api, agent, dhcp, dns, ntp, audit)]
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

**What is backed up:**
- PostgreSQL database (full dump or WAL-based continuous backup)
- System configuration (node network, firewall, time configs)
- Syslog / notification configurations
- API token definitions (hashes only — tokens themselves are not recoverable)
- Custom field definitions

**What is NOT backed up:**
- DHCP daemon state (leases are ephemeral; static assignments are in DB)
- DNS daemon state (all records are in DB; servers are rebuilt from DB)

**Backup Targets:**
```
BackupTarget
  id, name
  type: enum(local, s3, sftp, azure_blob, gcs)
  connection_config: JSONB (encrypted)
  schedule: cron expression
  retention_days: int
  encryption_enabled: bool
  encryption_key_reference: str   -- e.g., Vault path or env var name
  last_backup_at: timestamp
  last_backup_size_bytes: int
  last_backup_status: enum(success, failed, running)
```

**Backup Operations:**
- Manual backup trigger from UI
- Restore from backup: lists available backups with timestamp and size
- Restore preview: shows what would change before committing
- Restore is always a full restore (partial restore not supported in Phase 1)

**Restore Process:**
1. Puts system in maintenance mode (API returns 503 with maintenance message)
2. Takes a safety backup of current state
3. Restores database from selected backup
4. Restarts all services
5. Validates system health before clearing maintenance mode

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
  roles: [enum(api, worker, dhcp, dns, ntp, agent)]
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

This allows a small deployment where one VM runs DHCP + DNS + NTP agent simultaneously, and the UI correctly represents that.

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
