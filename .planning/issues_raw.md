════════════════════════════════════════════════════════════════
ISSUE #31
════════════════════════════════════════════════════════════════
TITLE: OPNsense (tier 1 — firewall-of-choice for labs)
LABELS: roadmap, integration, theme:integrations

BODY:
- ⬜ **OPNsense** *(tier 1 — firewall-of-choice for labs).*
  Per-`OPNsenseRouter` row, API key + secret auth over HTTPS.
  REST endpoints: `/api/dhcpv4/leases/searchLease`,
  `/api/dhcpv4/settings/getReservation`,
  `/api/diagnostics/interface/getInterfaceConfig`,
  `/api/interfaces/vlan_settings/get`. Mirror LAN / VLAN /
  OPT* interfaces → Subnets, DHCP leases → IPAddress
  (`status="dhcp"`, same shape as Kea mirror), static mappings →
  `status="reserved"`. ARP table endpoint as a secondary
  population source for devices with static-IP-outside-DHCP.

════════════════════════════════════════════════════════════════
ISSUE #46
════════════════════════════════════════════════════════════════
TITLE: Decom-date awareness
LABELS: roadmap, idea, area:reporting, theme:reporting

BODY:
- ⬜ **Decom-date awareness** — first-class `decom_date` column
  on subnet + ip_address (currently only suggested as a custom
  field). Beat task generates a "subnets decom in next 30 days"
  summary that feeds the alerts framework + an admin dashboard
  widget.

════════════════════════════════════════════════════════════════
ISSUE #47
════════════════════════════════════════════════════════════════
TITLE: Top-N reports
LABELS: roadmap, idea, area:reporting, theme:reporting

BODY:
- ⬜ **Top-N reports** — fixed dashboard widgets: top 10 subnets
  by utilization, top 10 owners by IP count, top 10
  most-modified resources in the last 7 days, top 10 noisiest
  DNS clients. All derivable from existing tables; just needs
  a reports router + page.

════════════════════════════════════════════════════════════════
ISSUE #57
════════════════════════════════════════════════════════════════
TITLE: Maintenance mode
LABELS: roadmap, idea, area:ops, theme:ops-tooling

BODY:
- ⬜ **Maintenance mode** — global toggle that puts the entire
  system in read-only state during change windows. UI shows a
  top banner; every write endpoint returns 503 with
  `Retry-After`. Bypass for superadmins so they can still make
  the changes themselves.

════════════════════════════════════════════════════════════════
ISSUE #58
════════════════════════════════════════════════════════════════
TITLE: Built-in network tools page
LABELS: roadmap, idea, area:ops, theme:ops-tooling

BODY:
- ⬜ **Built-in network tools page** — `/tools` with widgets for
  ping, traceroute, dig, whois, port-test, MTR,
  DNS-propagation-check, TLS cert checker, MAC vendor lookup.
  Each runs from the SpatiumDDI server perspective (or a chosen
  DHCP / DNS agent's perspective). Saves operators bouncing to
  a jump-box. Bound by the existing permission gates;
  rate-limited to avoid abuse.

════════════════════════════════════════════════════════════════
ISSUE #65
════════════════════════════════════════════════════════════════
TITLE: Time-bound permissions
LABELS: roadmap, idea, area:rbac, theme:rbac-workflow

BODY:
- ⬜ **Time-bound permissions** — grant group X access to subnet
  Y until a specific timestamp. Beat task revokes expired
  grants automatically. Useful for vendor / contractor access
  windows.

════════════════════════════════════════════════════════════════
ISSUE #156
════════════════════════════════════════════════════════════════
TITLE: appliance: syslog / journald forwarding to central SIEM (Loki / Splunk / Graylog) — GUI + fleet sync
LABELS: enhancement, theme:appliance-host-config, area:appliance

BODY:
## Problem

Network admins running SpatiumDDI alongside a SIEM (Splunk / Loki / Graylog / ELK) want every appliance shipping its `/var/log/spatiumddi/*` + journald to the central log store. Today there's no operator surface for this — operators would have to SSH in and edit `/etc/rsyslog.d/` or set up `systemd-journal-remote` by hand, which doesn't scale across a fleet and isn't visible in the control plane.

## Sibling issues — same architecture

Third leg (with SNMP support and NTP server management) of the "Settings → Host services" surface. Same trigger-file fleet-rollout pattern: control plane PUT → ConfigBundle long-poll → agent writes trigger → host-side `.path` unit reloads the forwarder.

## Design — rsyslog at the OS level

Use Debian's `rsyslog` (or stick with journald via `systemd-journal-upload`). rsyslog gives finer control over filtering / formatting (RFC 5424 vs 3164, TLS, structured JSON for Loki). Ship as a host-level service for the same reason as snmpd — `/var/log` and journald aren't sensibly accessible from a container without aggressive bind mounts.

## Schema

`platform_settings.syslog_*`:
- `syslog_enabled` (bool, default false)
- `syslog_targets` (JSONB array of `{host, port, protocol: udp/tcp/tls, format: rfc5424/rfc3164/json, ca_cert_pem}`)
- `syslog_filter` (string filter expression — start with "everything", let operators narrow)
- `syslog_buffer_disk` (bool, default true — local spool if remote unreachable, replay on reconnect)

## Acceptance criteria

- [ ] `rsyslog` package + sensible default in mkosi.conf
- [ ] Backend GET/PUT `/api/v1/settings/syslog` (admin, audited)
- [ ] ConfigBundle carries `syslog_settings` block
- [ ] Host-side `spatiumddi-syslog-reload.{path,service}` + runner with pre-apply validation (`rsyslogd -N1`)
- [ ] Fleet view chip per row showing `syslog: forwarding / unreachable / disabled`
- [ ] Acceptance: enable + point at a Loki instance → log entries appear within ~10 s; disconnect Loki → local spool keeps appliance running; reconnect → spool drains

## Related

- Sibling: SNMP #153, NTP #154, APT #155

════════════════════════════════════════════════════════════════
ISSUE #157
════════════════════════════════════════════════════════════════
TITLE: appliance: SSH authorized_keys management — push team keys + disable password auth from GUI
LABELS: enhancement, theme:appliance-host-config, area:appliance

BODY:
## Problem

The installer creates one local admin user with a password. Operators with a team want to push everyone's public keys to every appliance + disable password auth. Today: SSH to each box, edit `~admin/.ssh/authorized_keys`, edit `sshd_config`, hope you got it right on every box.

## Sibling

"Settings → Host services" group with SNMP #153, NTP #154, APT #155, syslog. Same trigger-file fleet-rollout pattern.

## Schema

`platform_settings.ssh_*`:
- `ssh_authorized_keys` (JSONB array of `{name, public_key, comment}`) — keys to push to the admin user
- `ssh_password_auth_enabled` (bool, default true initially — auto-flip to false once at least one key is in place + heartbeats prove it's reachable)
- `ssh_allow_root_login` (bool, default false — already enforced by postinst; expose toggle so air-gapped recovery scenarios can flip it)
- `ssh_port` (int, default 22)
- `ssh_allowed_source_networks` (array of CIDRs; default empty = allow all; folds into nftables drop-in)

## Pre-apply safety

**Critical:** runner validates that disabling password auth WITH zero authorized keys would lock the operator out. Refuse the apply with a clear "you'd be locked out" error. Same for setting ssh_port to a privileged port already in use.

## Acceptance criteria

- [ ] Backend GET/PUT `/api/v1/settings/ssh` (admin, audited)
- [ ] ConfigBundle carries `ssh_settings` block; agent writes trigger; host-side runner replaces `~admin/.ssh/authorized_keys` (mode 0600) + `/etc/ssh/sshd_config.d/spatiumddi.conf` + reloads sshd
- [ ] **Lockout safety check** — refuse to disable password auth if zero keys would survive
- [ ] Frontend Settings → Host services → SSH form with paste-key UI
- [ ] Fleet view chip: per-row `ssh_key_count` showing how many keys are deployed

## Related

- Sibling: #153 #154 #155 + syslog

════════════════════════════════════════════════════════════════
ISSUE #158
════════════════════════════════════════════════════════════════
TITLE: appliance: DNS resolver / search domain override (systemd-resolved global config)
LABELS: enhancement, theme:appliance-host-config, area:appliance

BODY:
## Problem

Each appliance picks up DNS resolvers from its NetworkManager connection (DHCP-supplied by default, or set in the static-IP installer step). For sites where the DHCP-advertised DNS isn't what ops wants — internal split-horizon resolvers, corporate DNS, fallback to public when internal is down — there's no fleet-wide GUI knob. Today: edit `/etc/systemd/resolved.conf.d/*.conf` over SSH on every box.

## Sibling

Same trigger-file pattern as #153 #154 #155 + syslog + SSH. Settings → Host services.

## Schema

`platform_settings.resolver_*`:
- `resolver_mode` (`automatic` / `override`) — automatic = NetworkManager-provided; override = use the explicit list below
- `resolver_servers` (array of IPs; only meaningful when mode=override)
- `resolver_fallback_servers` (array; used when primary servers unreachable)
- `resolver_search_domains` (array of search-domain suffixes)
- `resolver_dnssec` (`yes` / `no` / `allow-downgrade`)
- `resolver_dns_over_tls` (`yes` / `opportunistic` / `no`)

## Render path

`/etc/systemd/resolved.conf.d/spatiumddi.conf` — drop-in that overrides NM's per-connection DNS when `resolver_mode=override`. Runner does `systemctl reload systemd-resolved` (or `restart` since reload isn't supported for resolved on Debian 13).

## Acceptance criteria

- [ ] Backend GET/PUT `/api/v1/settings/resolver` (admin, audited)
- [ ] ConfigBundle + host-side reload runner
- [ ] Frontend form with DNS-over-TLS + DNSSEC pickers
- [ ] Acceptance: setting `resolver_servers=[10.0.0.1, 10.0.0.2]` overrides DHCP-advertised resolvers; `resolvectl status` on the appliance shows the operator's servers
- [ ] Reverting to `automatic` removes the drop-in + restarts resolved → NM-supplied servers come back

## Related

- Sibling: #153 #154 #155 + syslog + SSH keys (same Settings → Host services surface)

