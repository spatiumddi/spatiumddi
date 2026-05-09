# SpatiumDDI — Shipped roadmap items

Companion to `CLAUDE.md`. The roadmap sections in CLAUDE.md track only items that haven't shipped yet — each ✅ entry below is the full body of an item that landed, kept here so future sessions can recover the design context (migration id, file paths, deferred follow-ups, why-it-was-built) without crowding the working list.

Mirrors CLAUDE.md's three mixed sections:
- [Major roadmap items](#major-roadmap-items) — feature-level tracker for the IPAM / DNS / DHCP core.
- [Integration roadmap](#integration-roadmap) — read-only pull mirrors (Kubernetes, Docker, Proxmox, Tailscale, …).
- [Future ideas — categorised](#future-ideas--categorised) — the 2026.04.26 brainstorm pass, organised by topic.

## Major roadmap items

- ✅ **Multicast group tracking — IPAM-native registry of streams + producer/consumer relationships**
  ([#126](https://github.com/spatiumddi/spatiumddi/issues/126))
  — Full multicast registry shipping the four-phase issue body
  (registry → PIM domain → SNMP discovery → Operator Copilot tools)
  end-to-end. Eleven commits between 2026-05-08 and 2026-05-09:

  | Phase | Wave | Commit | What landed |
  |---|---|---|---|
  | 1 | 1 | `d56f3a6` | Data model — `multicast_group` + `multicast_group_port` + `multicast_membership` tables (migration `f1e7a3c92b40`), CHECK enforcing the IANA multicast classes (`224.0.0.0/4` v4 / `ff00::/8` v6) on `address`, UNIQUE `(group_id, ip_address_id, role)` on memberships, REST CRUD with `network.multicast` feature module gate (default-enabled per CLAUDE.md #14), permission grammar wired into the Network Editor builtin role |
  | 1 | 2 | `7f3ffd9` | Operator UI — `/network/multicast` list page, three-tab editor modal (General / Ports / Memberships), sidebar entry with the Radio icon under Network → Infrastructure |
  | 1 | 3 | `31749c5` | Bulk-allocate — `POST /multicast/groups/bulk-allocate/{preview,commit}` reusing the IPAM `_expand_bulk_template` grammar, `BulkAllocateModal` form→preview→commit flow, reusable `IPAddressPicker` component (debounced search via `/search?types=ip_address`) |
  | 1 | 4 | `a11b5a4` | Cross-group `GET /multicast/memberships?ip_address_id=…` returning joined group info, IPDetailModal "Multicast memberships" section (module-gated), `no_multicast_collision` conformity check + seeded built-in policy + engine `multicast_group` target-kind resolver |
  | 1 | bulk-delete | `2be4d59` | `POST /multicast/groups/bulk-delete` + checkbox column + select-all toolbar matching the Circuit pattern |
  | 2 | 1 | `cea3fda` | PIM domain registry — `multicast_domain` table (migration `c8d2f47a90b3`) with PIM mode (sparse/dense/ssm/bidir/none) + VRF binding + RP (device FK + free-text address) + SSM range, REST CRUD with sparse/bidir-needs-RP validation, FK promotion of `multicast_group.domain_id` from placeholder UUID to real ON DELETE SET NULL |
  | 2 | 2 | `f79f88b` | Domains UI + group→domain picker on the create modal |
  | 2 | 3 | `00edc06` | `Subnet.kind` discriminator (migration `d3a9c5b71e84`) auto-detected from CIDR on create, IPAM allocation endpoints refuse on `kind="multicast"`, frontend badge in IPAM tree |
  | 2 | tabs | `bfc99ef` | Sub-tab refactor — Groups + Domains live as `?tab=` sub-tabs of `/network/multicast`; legacy `/network/multicast/domains` route redirects to `?tab=domains` |
  | 3 | 1 | `77d66cd` | SNMP IGMP-snooping populator — vendor-neutral RFC 2933 walker (`walk_igmp_cache`) + cross-reference matcher writing `MulticastMembership` rows tagged `seen_via='igmp_snooping'`, promotes `manual` → `igmp_snooping` when discovery validates a hand entry, opt-in `NetworkDevice.poll_igmp_snooping` toggle (migration `a1f4d97c8e25`) |
  | 4 | 1 | `84aa308` | Operator Copilot tools — 4 read tools (`find_multicast_group` / `find_multicast_membership` / `find_multicast_domain` / `count_multicast_groups_by_vrf`, default-on, module-gated) + `propose_create_multicast_group` write proposal (default-off) + `create_multicast_group` Operation (preview/apply with multicast-class validation + collision soft-warn) |
  | 4 | 2 | `3d8be70` | `propose_allocate_multicast_group` bulk-stamp proposal + IGMP membership reaper Celery beat task (drops `seen_via='igmp_snooping'` rows older than 30 min every 5 min, manual + sap_announce rows untouched) |

  **Test coverage**: 40 cases in `backend/tests/test_multicast.py` covering CRUD + IPv4/IPv6 multicast-class validation + port range CHECK + unique-triplet membership + feature-module 404 gate + bulk-allocate flow + memberships-by-IP joined response + conformity check (pass/fail/cross-space) + PIM domain CRUD + sparse-without-RP rejection + Subnet.kind auto-detection + IPAM-allocation-refuses-on-multicast + IGMP cross-reference matcher + Operator Copilot read tools + module-disabled tool filtering + bulk-allocate operation preview/apply + IGMP reaper sweep.

  **Phase 3 deferrals (explicit non-goals)**:
  * **SAP listener (RFC 2974)** — needs raw multicast packet capture inside the agent (scapy + `NET_RAW` capability, like the DHCP fingerprint sniffer). Audience is legacy pro-AV / NDI; SNMP IGMP-snooping covers the same use case for >90% of operators.
  * **NMOS IS-04 mirror** — fits the integration shelf shape (peer of UniFi / OPNsense / pfSense) better than baked into multicast core. Ship if/when an SMPTE 2110 broadcast operator asks for it.

  Both deferrals captured in the issue close-out comment.

- ✅ **PowerDNS authoritative driver — second driver alongside BIND9**
  ([#127](https://github.com/spatiumddi/spatiumddi/issues/127))
  — Full second authoritative DNS driver with native REST API +
  LMDB embedded backend, multi-arch (`linux/amd64` + `linux/arm64`)
  `ghcr.io/spatiumddi/dns-powerdns` image, agent + supervisor +
  long-poll integration, frontend driver picker, and per-driver
  capability gating (ALIAS / LUA / online DNSSEC / catalog zones
  reject 422 against non-PowerDNS groups). Five phases + two
  post-test fix waves shipped across 15 commits between
  2026.05.07 and 2026.05.08:

  | Phase | Commit | What landed |
  |---|---|---|
  | 1 | `047fb66` | Driver class, agent driver, container image, multi-arch build matrix |
  | 2 | `cd6311c` | Frontend driver picker (PowerDNS joins BIND9 + Windows DNS in the dropdown) |
  | 3a | `559bafa` | ALIAS records (CNAME-at-apex) — `_DRIVER_GATED_RECORD_TYPES` |
  | 3b | `5f8875f` | LUA records (computed responses — `pickrandom` / `ifportup` / `createReverse` …) |
  | 3c.1 | `d967556` | DNSSEC online-signing backend (sign/unsign endpoints, agent dispatch via cryptokey API) |
  | 3c.fe | `f8c5208` | DNSSEC frontend (`DnssecCard`, DS-rrset list with copy-to-clipboard); migration `e7f94b21c8d5` adds `dnssec_ds_records` + `dnssec_synced_at` |
  | 3d | `c32bf0d` | RFC 9432 catalog zones, **producer-only** (consumer waits for pdns 4.10+) |
  | 4a | `2e1901c` | Standalone-VM compose plumbing — new `docker-compose.agent-dns-powerdns.yml`; renamed `agent-dns.yml` → `agent-dns-bind9.yml` |
  | 4b | `494f5d6` | Helm chart per-flavor StatefulSet — `dnsAgents.flavors.powerdns.{repository,tag}`; mount path swaps to `/var/lib/powerdns` for LMDB |
  | 4c | `be5579d` | Kind-cluster smoke test extension — both bind9 + powerdns flavors install side-by-side; `dig id.server CH TXT` on the powerdns pod |
  | 4d | `57004e1` | Backup DNSSEC restore advisory — `RestoreOutcomeResponse.warnings[]` flags signed zones whose keys live on the (un-archived) agent LMDB volume |
  | 4e | `f3a08b5` | Operator Copilot `propose_create_dns_zone` write-tool with `driver_hint` (one of `bind9` / `powerdns` / `windows_dns`) |
  | 5 | `8c7a7bd` | Docs pass — `DNS_DRIVERS.md` Section 4 chapter, `features/DNS.md` Section 0 driver-choice subsection, `TOPOLOGIES.md` PowerDNS-hybrid + BIND→PowerDNS migration recipe; LUA snippet dropdown; DNSSEC online-signing kind smoke test |
  | post-test wave 1 | `00e5405` | 8 bugs from a live-test pass: `pdns_server` CLI flag-syntax (`--config-dir=/path` not space-separated); LMDB pre-seed (let pdns init the env on first start); ZSK creation needs explicit `algorithm=ecdsa256`; `PRESIGNED` metadata path removed (REST API rejects the kind for online-signing); DS state-sync handler trailing-dot bug; `enable-lua-records=yes` global flag instead of rejected per-zone metadata; `expand-alias=yes` + upstream resolver; multi-record rrset PATCH GET-merge-PATCH so pool fan-out works. Same drop pinned `docker-compose.dev.yml` build-able services to `image: spatiumddi-<svc>:dev` so `make dev` always builds locally |
  | post-test wave 2 | `982bc1b` | (a) Server-group RecordsTab record-type colour-coding (shared `RECORD_TYPE_BADGE` map); (b) DNS Pools page deep-links into zone Pools sub-tab via `subtab=pools`; (c) destroy + recreate container restores config cleanly — `DNS_REQUIRE_AGENT_APPROVAL` now actually gates the fingerprint-mismatch lockout, cold-boot reconcile starts pdns + waits for the REST API + propagates errors so the structural_etag doesn't advance on a botched apply; (d) PowerDNS query logs surface in `/logs` end-to-end — `log-dns-queries=yes` + `loglevel=6` rendered when toggled, agent captures pdns stderr, new `pdns_parser`, ingest dispatches by `server.driver` |

  **End-to-end verified** against a fresh `make dev` install with
  the `dns-powerdns` profile: zone CRUD via API, dig answers for
  every record type (A/AAAA/CNAME/MX/TXT/SOA), ALIAS at apex
  resolves through the configured upstream, LUA `pickrandom`
  rotates correctly, online DNSSEC produces RRSIG-signed answers
  + 3 DS records (SHA-1/256/384) synced to the control plane,
  GSLB pool with TCP health-check returns only healthy members
  (multi-A fan-out works), container destroy + persistent volume
  wipe + recreate-with-same-PSK re-pushes all zones + records,
  and query logs flow into the DNS Queries tab within one batch
  interval (~5 s).

  **Operator-elective tail (deferred — open separate issues if
  needed):** gpgsql-backend image variant (LMDB stays the
  default); pdns 4.10+ catalog-zone consumer support (waiting on
  upstream image bump); bundled-Postgres subchart for the gpgsql
  variant. Plus a known soft-delete-on-PowerDNS limitation: the
  soft-delete + trash-purge paths don't enqueue a `record_op` for
  PowerDNS-driver groups (only `?permanent=true` deletes do), so
  soft-deleted records keep getting served until the operator
  hard-deletes via the trash UI.

- ✅ **Windows DNS — Path A (RFC 2136, agentless)** — `WindowsDNSDriver`
  in `backend/app/drivers/dns/windows.py`. Record CRUD only (A / AAAA /
  CNAME / MX / TXT / PTR / SRV / NS / TLSA) via dnspython over RFC 2136;
  zones are managed externally in Windows DNS Manager. Optional TSIG
  signing; GSS-TSIG and SIG(0) are Path B. Control plane sends updates
  directly; `record_ops.enqueue_record_op` short-circuits the agent queue
  for servers whose driver is in `AGENTLESS_DRIVERS`.

- ✅ **Windows DHCP — Path A (WinRM, read-only lease monitoring)** —
  `WindowsDHCPReadOnlyDriver` in `backend/app/drivers/dhcp/windows.py`.
  Implements `get_leases` via `Get-DhcpServerv4Scope` /
  `Get-DhcpServerv4Lease` over WinRM (`pywinrm`). All write methods
  (`apply_config`, `reload`, `restart`, `validate_config`) raise
  `NotImplementedError` — Path A is strictly read-only. Credentials are
  stored Fernet-encrypted on `DHCPServer.credentials_encrypted`. Driver
  registry gains `AGENTLESS_DRIVERS` + `READ_ONLY_DRIVERS` sets mirroring
  the DNS side. Scheduled Celery beat task
  `app.tasks.dhcp_pull_leases.auto_pull_dhcp_leases` fires every 60 s;
  task gates on `PlatformSettings.dhcp_pull_leases_enabled` /
  `_interval_minutes`. Leases are upserted by `(server_id, ip_address)`
  and mirrored into IPAM as `status="dhcp"` + `auto_from_lease=True` rows
  when the lease IP falls inside a known subnet; the existing lease-
  cleanup sweep handles expiry uniformly. Manual "Sync Leases" button
  on the server detail header for agentless drivers. Beat ticks every
  10 s and the per-run interval is stored in seconds
  (`PlatformSettings.dhcp_pull_leases_interval_seconds`, default 15 s)
  so operators can tune near-real-time IPAM population — Windows
  DHCP has no streaming primitive, so short-interval polling is the
  practical upper bound without putting an agent on the DC.

- ✅ **OUI/vendor lookup** — opt-in IEEE OUI database fetched by
  `app.tasks.oui_update.auto_update_oui_database` (hourly beat, task
  honours `PlatformSettings.oui_lookup_enabled` +
  `oui_update_interval_hours`, default 24 h). `oui_vendor(prefix
  CHAR(6) PK, vendor_name, updated_at)` replaced atomically each run
  so lookups always see a consistent snapshot. `services/oui.py`
  exposes `bulk_lookup_vendors` + `normalize_mac_key`; IPAM's
  `list_addresses` and DHCP's `list_leases` use them to attach a
  `vendor` field. Settings → IPAM → OUI Vendor Lookup carries the
  toggle + interval + "Refresh Now" (queues
  `update_oui_database_now`). MACs render as `aa:bb:cc:dd:ee:ff
  (Cisco Systems)` in the IP table + DHCP leases; feature off =
  vendor null + UI falls back to bare MAC.

- ✅ **SNMP polling / network device management** — vendor-neutral
  read-only polling of routers/switches via standard MIBs, with
  every result cross-referenced into IPAM. Lands the "every
  managed IP gets a heartbeat + switch-port automatically" payoff
  this whole roadmap pivots on. **Mirror scope:**
  - **Data model** (migration
    `c4e7a2f813b9_network_devices`): `network_device` (SNMP
    credentials Fernet-encrypted at rest; v1 / v2c / v3 USM all
    supported with auth + priv protocol enums), `network_interface`,
    `network_arp_entry` keyed `(device, ip, vrf)`, and
    `network_fdb_entry` keyed `(device, mac, vlan)` with the
    Postgres 15+ `NULLS NOT DISTINCT` unique index — so a single
    port can carry the same MAC across multiple VLANs (hypervisor
    with VMs in different access VLANs, IP phone with PC
    passthrough on voice + data VLANs).
  - **MIBs walked** — SNMPv2-MIB system group (sysDescr /
    sysObjectID / sysName / sysUpTime), IF-MIB `ifTable` +
    `ifXTable`, IP-MIB `ipNetToPhysicalTable` with legacy
    RFC1213 `ipNetToMediaTable` fallback, Q-BRIDGE-MIB
    `dot1qTpFdbTable` with BRIDGE-MIB `dot1dTpFdbTable` fallback.
    All standard, no vendor-specific MIBs — works on Cisco /
    Juniper / Arista / Aruba / MikroTik / OPNsense / pfSense /
    FortiNet / Cumulus / SONiC / FS.com / Ubiquiti out of the
    box.
  - **Polling pipeline** — `pysnmp` 6.x async (`bulkWalk` for
    table OIDs, ~10–50× faster than `getNext`).
    `app.tasks.snmp_poll.poll_device` runs sysinfo → interfaces
    → ARP → FDB sequentially under a per-device
    `SELECT FOR UPDATE SKIP LOCKED` so concurrent dispatches
    can't double-poll the same row. `dispatch_due_devices`
    beat-fires every 60 s and queues every active device whose
    `next_poll_at <= now`. Per-device interval default 300 s,
    minimum 60 s. Status: `success | partial | failed |
    timeout`, with `last_poll_error` populated for ops triage.
    Stale ARP entries are kept with `state='stale'` (no
    delete); `purge_stale_arp_entries` daily beat task removes
    rows older than 30 days.
  - **IPAM cross-reference** — after every successful ARP poll,
    `cross_reference_arp` finds matching `IPAddress` rows in
    the device's bound `IPSpace` and updates `last_seen_at`
    (max-merge), `last_seen_method='snmp'`, and fills
    `mac_address` only when currently NULL — operator-set MACs
    are never overwritten. When the per-device
    `auto_create_discovered=True` toggle is on (off by default;
    operator stays in control), inserts new
    `status='discovered'` rows for ARP IPs that fall inside a
    known `Subnet`. Returns counts (`updated`, `created`,
    `skipped_no_subnet`).
  - **API** — full CRUD at `/api/v1/network-devices` plus
    `POST /test` (synchronous SNMP probe, ≤10 s, returns
    `TestConnectionResult` with sysDescr + classified
    `error_kind`: `timeout | auth_failure | no_response |
    transport_error | internal`), `POST /poll-now` (queues
    immediate Celery task, returns 202 + task_id), and
    per-device list endpoints `/interfaces`, `/arp` (filter by
    ip/mac/vrf/state), `/fdb` (filter by mac/vlan/interface_id).
    The IP-detail surface gains
    `GET /api/v1/ipam/addresses/{id}/network-context` — joins
    `IPAddress.mac_address → NetworkFdbEntry → NetworkInterface
    → NetworkDevice` and returns one row per (device, port,
    VLAN, MAC) tuple so a hypervisor / IP phone surfaces every
    leg.
  - **Frontend** — top-level `/network` page in the core sidebar
    (always visible between VLANs and Logs — this is core IPAM
    functionality, not gated on a Settings → Integrations
    toggle). Per-device detail at `/network/:id` with Overview /
    Interfaces / ARP / FDB tabs, each filterable + paginated.
    Add/edit modal with SNMP-version-conditional credential
    fields (community for v1/v2c; security_name + level + auth
    + priv for v3) plus inline Test Connection (saves first on
    create, then probes against the saved row). New "Network"
    tab on the IP detail modal showing per-IP switch/port table
    sorted by `last_seen DESC`.
  - **Permissions** — single `manage_network_devices` permission
    gates all endpoints (read + write); new "Network Editor"
    builtin role gets it. Superadmin always bypasses.
  - **Tests** — 35 backend tests covering pysnmp wrapper paths
    (mocked: v1 / v2c / v3 auth construction, OID resolution,
    `ipNetToPhysical → ipNetToMedia` fallback,
    `Q-BRIDGE → BRIDGE` fallback, error classification),
    API CRUD + `/test` + `/poll-now` + the four list endpoints
    + `/network-context`, and three cross-reference paths
    (existing IP gets last_seen + MAC fill; operator-set MAC
    never overwritten; auto-create on/off; no-matching-subnet
    skip).

  **Deferred follow-ups:**
  - **CDP neighbour collection** + topology graph. LLDP shipped
    in the categorised brainstorm section below; CDP (Cisco-only)
    deferred — modern Cisco gear runs LLDP alongside CDP, so the
    standard-MIB path covers the typical case.
  - **VRF-aware ARP polling.** `network_device.v3_context_name`
    column exists for SNMPv3 context-name targeting, but the
    poller doesn't iterate per-VRF in v1. Per-VRF SNMPv2c
    community-string indexing
    (`<community>@<vrf-name>` Cisco convention) and SNMPv3
    context-name iteration are both pending.
  - **Standalone `snmp-poller` container** (per
    `docs/features/IPAM.md §13`). Today the polling lives in
    the existing Celery worker pool, which is fine to ~100
    devices on a 5-min interval. Splitting becomes interesting
    once SNMP traffic competes with the worker's other tasks
    or when the operator wants different network reachability
    for the poller (different VLAN, jumphost, etc).
  - **Beat-tick fan-out cap.** `dispatch_due_devices` queues
    every due device in one tick; at >1k devices this is a
    queue spike. Chunking + per-tick rate-limit is the cheap
    follow-up.
  - **Permission granularity** — read vs write split. Today
    `manage_network_devices` covers both; ops teams that want
    network-engineer read access without write privs need a
    `view_network_devices` companion permission.
  - **Stateless probe endpoint** — today `/test` requires the
    device row to exist; the create-then-test flow on the UI
    saves first then probes. A
    `POST /network-devices/probe` that accepts inline creds
    would let operators verify before committing the row.
  - **Frontend permission gating** — sidebar nav entry shows
    for every authenticated user (backend 403s unauthorised
    callers). A `useCurrentUser` hook + `hasPermission` check
    on the sidebar item is the polish step; depends on
    introducing those hooks (they don't exist anywhere in the
    frontend today).
  - **Vendor-specific MIBs** — CISCO-VRF-MIB for VRF
    auto-discovery, ENTITY-MIB for chassis info, vendor PoE
    MIBs. The vendor-neutral path covers the IPAM payoff;
    extensions are operator-pull additions.
  - **`/network-context` reverse-lookup pages.** "Show me every
    IP currently learned on switch X port 24" needs an
    interface-detail page or a query mode on the FDB tab.
    Today operators can filter the FDB tab by interface_id —
    the dedicated UI is a polish iteration.

  Original spec lives at `docs/features/IPAM.md §13` (the
  pre-build placeholder). The shipped behaviour matches that
  spec for the standard-MIB scope.

- ✅ **Nmap scanner** — on-demand nmap scans against any IPv4 /
  IPv6 host or **CIDR** from the SpatiumDDI host perspective.
  Three entry points: a per-IP "Scan with Nmap" button on the
  IPAM IP detail modal, a per-subnet "Scan with nmap" entry in
  the IPAM Tools dropdown (pre-fills target = subnet CIDR +
  preset = `subnet_sweep`), and a standalone `/tools/nmap` page
  for ad-hoc targets (including IPs that aren't in IPAM yet).
  Backend: `NmapScan` model + migration `d2f7a91e4c8b`,
  sanitised argv builder with preset table (quick /
  service+version / **service_and_os** / OS fingerprint /
  **subnet_sweep** / default-scripts / UDP top-100 / aggressive
  / custom), CIDR-aware target validation
  (`ipaddress.ip_network(strict=False)`, capped at /16 worth of
  hosts), async subprocess runner using `-oN -` on stdout for
  the live SSE viewer + `-oX <tmpfile>` in parallel for
  structured XML parsing. The XML parser walks every `<host>`
  element and emits a `hosts[]` list when more than one
  responded — single-host fields stay populated for backward
  compat. Celery task on the `default` queue, SSE endpoint at
  `GET /api/v1/nmap/scans/{id}/stream`. SSE auth uses
  `?token=<...>` because EventSource can't set Authorization
  headers; the router has no global `Depends(get_current_user)`
  because that would 401 before the query-token resolver runs
  (each non-SSE endpoint declares its own permission dep).
  Bulk operations on the history surface:
  `POST /nmap/scans/bulk-delete` (cap 500; per-row policy
  cancels queued/running scans + deletes terminal ones in one
  transaction) and
  `POST /nmap/scans/{id}/stamp-discovered` (claims alive hosts
  from a CIDR scan as `discovered` IPAM rows + stamps
  `last_seen_at` via nmap; integration-owned rows just bump the
  timestamp without changing status). `NmapToolsPage` rewritten
  as a 3-tab right panel (Live / History / Last result) that
  auto-switches Live → Last result on completion; History tab
  has a checkbox column + amber bulk-delete toolbar.
  `NmapResultPanel` renders both single-host and multi-host
  (CIDR) scans with "Copy alive IPs" + "Stamp alive hosts →
  IPAM" actions. New `manage_nmap_scans` permission seeded
  into the existing Network Editor builtin role. nmap installed
  in the api image.
  **Deferred follow-ups:**
  - **Trigger pipeline** — auto-scan on ARP/SNMP discovery (the
    `auto_create_discovered=True` path) and alert-rule-driven
    re-scans. Phase 2.
  - **Realtime fanout** — the SSE stream polls the DB-persisted
    `raw_stdout` column every 500 ms per active viewer. Fine
    at human cadence and a handful of operators; if many
    operators end up watching live scans simultaneously
    (>~20-30) it'll show up as Postgres load. Swap to Redis
    pub/sub or `LISTEN/NOTIFY` behind the same HTTP shape.
  - **Privileged scans** — nmap runs as the api container's
    non-root user; raw-SYN (`-sS`) and unprivileged OS-detect
    silently fall back to TCP-connect. Bare-metal deployments
    can give the API process `CAP_NET_RAW` to unlock those
    modes; containerised deployments can't and shouldn't.

- ✅ **API tokens with auto-expiry** (Phase 1 close-out) — `APIToken`
  model already existed; this session wires the create/list/revoke
  router, extends `get_current_user` to accept `spddi_*` bearer
  tokens alongside JWTs, tracks `last_used_at`, and adds an admin UI
  at `/admin/api-tokens`. Tokens are hashed at rest (SHA-256) and
  shown in plaintext once at creation.

- ✅ **Syslog + event forwarding** — every successful `AuditLog`
  commit is optionally forwarded to an external syslog target
  (RFC 5424 over UDP / TCP) and/or a generic HTTP webhook. Hook is
  a SQLAlchemy `after_commit` session listener in
  `services/audit_forward.py`; delivery is fire-and-forget on a
  dedicated asyncio task so audit writes never block on network I/O.
  Configured in Settings; targets live on `PlatformSettings`
  (single syslog + single webhook for now — multi-target moves to a
  dedicated table when a second customer asks).

- ✅ **DHCP state failover (Kea HA)** — under the group-centric data
  model (shipped 2026.04.21-2), a `DHCPServerGroup` with two Kea
  members is implicitly an HA pair. HA tuning (mode, heartbeat /
  max-response / max-ack / max-unacked, auto-failover) lives on the
  group; per-peer URL lives on each `DHCPServer.ha_peer_url`.
  `DHCPFailoverChannel` is gone — merged into the group.
  `ConfigBundle` carries a `FailoverConfig` when the server's group
  is an HA pair; the agent's `render_kea.py` injects `libdhcp_ha.so`
  + `high-availability` alongside the always-loaded
  `libdhcp_lease_cmds.so` hook. Peer URLs are resolved agent-side
  (`_resolve_peer_url`) before render because Kea's Boost asio
  parser only accepts IP literals. Kea image splits ports: `:8000`
  is owned by the HA hook's `CmdHttpListener`, `:8544` by the
  operator-facing `kea-ctrl-agent`. Agent supervises an
  `HAStatusPoller` thread that calls `status-get` (Kea 2.6 folded
  HA state into the generic status command; pre-2.6 `ha-status-get`
  shapes still accepted) and POSTs state to
  `/api/v1/dhcp/agents/ha-status`. Bootstrap-from-cache issues
  `config-reload` with retry so Kea picks up HA on agent restart.
  HA config lives in the main DHCP tab's server-group edit modal;
  dashboard shows one row per HA pair with a live state dot per
  peer. Scope mirroring is automatic — all servers in a group
  render the same scopes, pools, statics, and client classes.
  **`PeerResolveWatcher`** re-resolves peer hostnames every 30s
  and triggers render + reload on IP drift, so compose
  `--force-recreate` / k8s pod restarts heal without operator
  action. Kea daemons (`kea-dhcp4` + `kea-ctrl-agent`) run under
  per-daemon supervise loops with stale-PID-file scrubbing, 5-in-
  30s crash-loop guards, and SIGTERM forwarding. **Deferred
  follow-ups:**
  - **Kea version skew guard.** The `status-get` HA shape shifted
    between Kea 2.4 and 2.6. Pairing peers on mismatched Kea
    versions is accepted today. Cheap fix: ship Kea version in the
    heartbeat, reject group membership changes if peers differ.
  - **DDNS double-write under HA.** Agent-side DDNS
    (`apply_ddns_for_lease`) doesn't gate on HA state — if the
    standby ever serves a lease (pre-sync window, partner-down),
    both peers could try to write the same RR. Kea's hook
    coordinates DHCP serving but not our DDNS pipeline.
  - **State-transition actions** (`ha-maintenance-start`,
    `ha-continue`, force-sync) — observable today but operators
    can't drive the HA state machine from the UI.
  - **Peer compatibility validation** (refuse groups with ≥ 3 Kea
    members because `libdhcp_ha.so` only supports pairs), per-pool
    HA scope tuning for load-balancing.
  - **HA e2e test.** `.github/workflows/agent-e2e.yml` stands up
    a single-agent DNS pair today; an HA DHCP variant would have
    caught the bootstrap / port-split / `status-get` / wire-shape
    regressions we hit in 2026.04.21-2.

- ✅ **DHCP MAC blocklist (group-global)** — `DHCPMACBlock` table
  hung off `DHCPServerGroup`, unique on `(group_id, mac_address)`,
  indexed on `expires_at`. Per-row fields: `mac_address` (MACADDR),
  `reason` (`rogue` / `lost_stolen` / `quarantine` / `policy` /
  `other`), `description`, `enabled`, `expires_at`, `created_at` +
  `created_by_user_id`, `updated_by_user_id`, `last_match_at`,
  `match_count`. `MACBlockDef` added to `ConfigBundle`; the control
  plane strips `enabled=False` + expired rows pre-render so the
  ETag naturally shifts on expiry transitions and agents long-poll
  pick it up. **Kea path**: agent's `render_kea.py` wraps the active
  MAC list in Kea's reserved `DROP` client class via an OR-ed
  `hexstring(pkt4.mac, ':') == '...'` expression — packets are
  silently dropped before allocation; if the operator hand-defined
  a `DROP` class the renderer steps aside rather than clobber it.
  **Windows DHCP path**: `WindowsDHCPReadOnlyDriver.sync_mac_blocks`
  diffs desired-set against `Get-DhcpServerv4Filter -List Deny` and
  ships one batched PS script per WinRM round trip. Beat tick every
  60 s (`app.tasks.dhcp_mac_blocks.sync_dhcp_mac_blocks`) reconciles
  Windows servers — Kea doesn't need the task since blocklist
  changes flow through the bundle. CRUD at `/api/v1/dhcp/server-
  groups/{gid}/mac-blocks` (list + create) + `/api/v1/dhcp/mac-
  blocks/{id}` (update + delete). List endpoint joins OUI vendor
  lookup and an `IPAddress.mac_address` cross-reference so the UI
  surfaces vendor + any IPAM rows tied to the blocked MAC.
  Frontend `MacBlocksTab` on the DHCP server detail view (mirrors
  where `ClientClassesTab` lives): filterable table, reason pills,
  status pill (active / disabled / expired), IPAM link-outs, add /
  edit modal accepting any common MAC format. Permission gate
  `dhcp_mac_block`; built-in "DHCP Editor" role gets it. Migration
  `d4a18b20e3c7_dhcp_mac_blocks`. **Deferred follow-ups:**
  - **Bulk import / paste** from CSV — the current UI is one-at-a-
    time; large blocklists need a paste-a-list path.
  - **Per-scope restriction.** Kea's class/pool pinning supports
    "block this MAC only on subnet X" — would mean dropping the
    `DROP` shortcut in favour of per-pool `client-class` + a
    per-subnet class. Windows can't do per-scope deny at all
    (deny-list is server-global). Group-global is the right
    default; per-scope is a Phase 5 precision tool.
  - **`last_match_at` + `match_count` wiring.** The columns exist
    but nothing writes to them yet. Kea has a lease-event hook and
    Windows has a `FilterNotifications` event channel we already
    surface in Logs — either can drive the counter.
  - **HA pair compatibility**: the beat task iterates every
    agentless server regardless of group HA state. Should still be
    idempotent but worth a targeted test when HA + Windows DHCP
    land together.


- ✅ **Alerts framework (v1)** — `AlertRule` + `AlertEvent` tables;
  evaluator at `services/alerts.py:evaluate_all()` runs from
  `app.tasks.alerts.evaluate_alerts` on a 60 s beat tick. Two rule
  types on launch: `subnet_utilization` (honours
  `PlatformSettings.utilization_max_prefix_*` so PTP / loopback
  subnets can't trip the alarm) and `server_unreachable` (DNS /
  DHCP / any). Delivery reuses the audit-forward syslog + webhook
  send helpers against the platform-level targets; per-rule
  override of targets deferred. Admin UI at `/admin/alerts` with
  live events viewer + "Evaluate now". SNMP trap delivery is the
  remaining v2 work (SMTP + chat-flavored webhooks landed in
  2026.04.30-1).

- ✅ **SMTP delivery for alerts + audit forward** — landed in
  2026.04.30-1. New SMTP target type with host / port / username /
  Fernet-encrypted password / TLS mode (`starttls` / `ssl` /
  `none`) / from-address / to-list fields. Wired through stdlib
  `smtplib` driven via `asyncio.to_thread` so we don't take a
  dependency on `aiosmtplib`. Audit-forward targets gain
  `kind="smtp"`; alert rules gain a `notify_smtp` toggle alongside
  the existing syslog + webhook channels. Migration
  `30cda233dce9_add_smtp_chat_flavor_to_audit_forward`.

- ✅ **Chat-flavored webhooks (Slack / Teams / Discord)** — landed
  in 2026.04.30-1. New `webhook_flavor` column on
  `audit_forward_target` selects between generic JSON (default),
  Slack `mrkdwn` block, Teams `MessageCard`, and Discord `embed`
  body renderers. Single payload renderer per flavor, no extra
  dependency. Operator pastes the platform's incoming-webhook URL
  into a webhook target and picks the matching flavor.

- ✅ **Typed-event webhooks (generic outbound on resource
  changes)** — landed in 2026.04.30-1. Curated automation surface
  separate from audit-forward. `EventSubscription` + `EventOutbox`
  tables (migration
  `0f83a227b16d_event_subscription_outbox_tables`). 96 typed
  events derived from a `resource_namespace × verb` cross-product
  (`space.created`, `subnet.bulk_allocate`, `dns.zone.updated`,
  `dhcp.scope.deleted`, `auth.user.created`,
  `integration.kubernetes.created`, …). SQLAlchemy
  `after_flush` + `after_commit` listeners snapshot committed
  `AuditLog` rows and write one outbox row per matching
  subscription. Celery beat (`event-outbox-drain`, every 10 s)
  drains via `SELECT … FOR UPDATE SKIP LOCKED`, signs each POST
  with `hmac(secret, ts + "." + body, sha256)`, and retries with
  exponential backoff (2 / 4 / 8 … 600 s capped) up to
  `max_attempts` (default 8 ≈ 8.5 min cumulative). Permanent
  failures flip to `state="dead"` for operator review. Reserved
  `X-SpatiumDDI-*` headers (Event / Delivery / Timestamp /
  Signature) are protected from operator override; custom headers
  are applied last so platform-owned headers can't be silently
  overridden. Admin UI at `/admin/webhooks` with one-time secret
  reveal on create (auto-generated 32-byte hex unless an operator
  supplies their own), event-type multi-select with filter,
  custom-headers editor, per-row test button (synthesizes a
  `test.ping` through the live pipeline), expandable deliveries
  panel with auto-refresh + manual **Retry now** on failed/dead
  rows, and a secret rotation toggle on edit. **Worker engine
  fix bundled** — first cut of `event_outbox` imported
  `AsyncSessionLocal` from `app.db`, which binds asyncpg
  connections to the loop that first checked them out. Celery's
  prefork pool reuses processes across tasks so the second
  `asyncio.run` re-entered with a different loop and surfaced as
  `Future attached to a different loop` followed by cascading
  `cannot perform operation: another operation is in progress`
  errors. Replaced with a per-tick `NullPool` ephemeral engine —
  same pattern `audit_forward._ephemeral_session` and
  `event_publisher._ephemeral_session` use.

- ✅ **Dashboard time-series (MVP)** — agent-driven DNS query rate +
  DHCP traffic charts, self-contained (no Prometheus / InfluxDB
  required). BIND9 agents poll `statistics-channels` XMLv3 on
  `127.0.0.1:8053` (injected into rendered `named.conf`); Kea
  agents poll `statistic-get-all` over the existing control socket.
  Both report per-60s-bucket deltas to `POST
  /api/v1/{dns,dhcp}/agents/metrics`. Counter resets on daemon
  restart are detected agent-side (`delta < 0`) and drop the
  bucket rather than emitting a phantom spike. Storage in two
  narrow tables `dns_metric_sample` + `dhcp_metric_sample` keyed
  on `(server_id, bucket_at)`. Dashboard reads `GET
  /api/v1/metrics/{dns,dhcp}/timeseries?window={1h|6h|24h|7d}`
  with server-side `date_bin` downsampling (60 s for ≤24 h,
  5 min for 7 d). Retention by nightly `prune_metric_samples`
  Celery task (default 7 d). Migration
  `bd4f2a91c7e3_metric_samples`. **Deferred follow-ups:**
  - **Windows DNS / DHCP stats** — needs `Get-DnsServerStatistics`
    + `Get-DhcpServerv4Statistics` driver methods over WinRM.
    Chart currently shows "no data yet" for Windows-only
    deployments.
  - **Prometheus export** of the same samples — one Gauge per
    column with `server_id` label would make the existing
    `/metrics` endpoint a full-featured scrape target for
    operators who prefer Grafana.
  - **InfluxDB push export** (`InfluxDBTarget` spec in
    `docs/features/SYSTEM_ADMIN.md §8.2`) — shape exists, writer
    still pending.
  - **Per-qtype** (BIND) / **per-subnet** (Kea) breakdowns —
    `statistic-get-all` already carries per-subnet counters; the
    agent strips them for MVP. Adding them is column-only, no
    protocol change.
  - **Alert rule types `dns_query_rate` / `dhcp_lease_rate`** —
    threshold-based alerts keyed off the timeseries data.

- ✅ **ACME / Let's Encrypt — DNS-01 provider for external clients**
  — landed in the 2026.04.22-1 wave. Lets certbot / lego / acme.sh
  on a client box prove control of a FQDN hosted in (or delegated
  to) a SpatiumDDI-managed zone and issue public certs (wildcards
  included). Implementation shipped as an `acme-dns`-compatible HTTP
  surface under `/api/v1/acme/` with the following pieces:
  - **Data model** — `ACMEAccount` (`app/models/acme.py`,
    migration `ac3e1f0d8b42`): `username` + `password_hash`
    (bcrypt), UUID `subdomain`, FK `zone_id`, optional
    `allowed_source_cidrs`, `last_used_at`. Credentials shown once
    at registration; only the hash persists.
  - **Endpoints** — `POST /register` (JWT auth, gated by
    `manage_acme` / `write:acme_account`), `POST /update` +
    `DELETE /update` (acme-dns `X-Api-User` / `X-Api-Key` auth,
    subdomain must match authenticated account), `GET /accounts`
    / `DELETE /accounts/{id}` admin ops.
  - **Record write** — routes through the normal
    `enqueue_record_op` pipeline so TXT records land in the UI +
    audit log + DDNS pipeline uniformly. `subnet_id`-equivalent
    routing here is the zone's primary server.
  - **Propagation wait** — `/update` blocks up to 30 s polling
    `DNSRecordOp.state` until `applied`, so the CA's subsequent
    DNS-01 poll finds the record live. Returns 504 on timeout,
    502 on primary driver error.
  - **Wildcard support** — keeps the 2 most-recent TXT values per
    subdomain so wildcard + base cert issuance (which presents two
    different validation tokens at the same record name) works.
  - **Protocol choice** — `acme-dns` compat means certbot
    (`dns-acmedns` plugin), lego, acme.sh all work out of the box
    with no custom plugin. Delegation pattern documented in
    `docs/features/ACME.md` — operator CNAMEs
    `_acme-challenge.<their-fqdn>` to
    `<account.subdomain>.<our-acme-zone>` and delegates the small
    subzone via NS records, so a leaked credential can't rewrite
    anything outside that label.
  - **Audit** — every register / update / delete / revoke lands in
    `audit_log`. TXT values logged as a 12-char prefix only, never
    in full. Credentials never logged, hashed or otherwise.
  - **Tests** — `backend/tests/test_acme.py`, 24 tests covering
    crypto roundtrip, source-CIDR allowlist edge cases, HTTP auth
    paths, wildcard rolling window, revocation, cleanup.

  **Deferred follow-ups:**
  - **Dedicated rate-limit bucket** for `/api/v1/acme/*` + Fail2Ban-
    style temp-ban on repeated `/update` auth failures. Today the
    endpoint rides the general API rate limit (none in v1; add
    slowapi or similar when it lands).
  - **Per-op `asyncio.Event` ack channel** to replace the DB-polling
    `wait_for_op_applied` loop. ~250 ms latency savings on the
    typical path; the polling approach is simple and correct but
    opens/closes a DB session every 500 ms.
  - **Celery janitor task** for the 24 h stale TXT sweep — service-
    level function `acme.sweep_stale_txt_records` is written and
    unit-testable; wiring it into the beat schedule is pending.
  - **Metric exposure** for ACME activity (registrations,
    /update rate, sweep counts) on the admin dashboard.


- ✅ **Kubernetes integration (read-only cluster mirror).** Poll one
  or more Kubernetes clusters with a read-only service-account
  token and mirror the stable bits of cluster state into SpatiumDDI.
  **Deliberately not mirroring pod IPs** — they churn too fast to
  be useful in IPAM. The value is reserving cluster CIDRs in IPAM
  (so operators can't accidentally overlap), surfacing LoadBalancer
  VIPs with their owning `namespace/service`, and auto-generating
  DNS records for Ingress hostnames. Pure pull, SpatiumDDI never
  writes to the cluster.

  **UX shape (agreed):**
  - **Settings → Integrations** is a new top-level settings section
    that hosts one card per integration type. Each card carries its
    own independent enable toggle on `PlatformSettings` (no single
    master `integrations_enabled` flag — granular by design so
    enabling Kubernetes doesn't also enable a future Terraform
    Cloud integration). When an integration's toggle is on, its
    corresponding top-level sidebar nav item appears (flat, not
    nested under an "Integrations" meta-item — matches how DHCP /
    DNS already surface).
  - **`KubernetesCluster` rows** are the per-cluster config, not
    PlatformSettings. Each cluster binds to exactly one
    `IPAMSpace` (required — discovered IPs / blocks land there)
    and optionally one `DNSServerGroup` (for ingress → DNS sync).
    Many clusters supported, same or different space/group per
    cluster.
  - **Credentials**: API server URL + CA bundle PEM + bearer token,
    with the token Fernet-encrypted at rest alongside the other
    driver creds (`DHCPServer.credentials_encrypted`,
    `DNSServer.credentials_encrypted`).
  - **Modal** shows an embedded setup guide with the exact
    ServiceAccount / ClusterRole / ClusterRoleBinding YAML plus
    `kubectl` commands to extract the token + CA bundle. Cluster
    version + node count round-trip via a **Test Connection**
    button before save.
  - **Operator also enters pod CIDR + service CIDR** in the form —
    service CIDR is not reliably extractable from the API; pod CIDR
    could be derived from Node objects but asking is simpler and
    matches how the DHCP / DNS server forms work.

  **Phased scope:**

  - ✅ **Phase 1a — Scaffolding.** Settings → Integrations UX +
    per-integration toggle on PlatformSettings + sidebar gating.
    `KubernetesCluster` model + migration `f8c3d104e27a` + CRUD API
    + admin UI page + setup-guide modal (embedded YAML + kubectl
    extract commands) + Test Connection button that probes
    `/version` + `/api/v1/nodes` with structured error
    reporting (401/403/TLS/network distinguished).

  - ✅ **Phase 1b — Read-only reconciliation.** Every 30 s beat
    tick; per-cluster `sync_interval_seconds` (min 30 s) gates the
    actual reconcile. Gated overall by
    `PlatformSettings.integration_kubernetes_enabled`. Provenance
    via dedicated `kubernetes_cluster_id` FK on `ip_address`,
    `ip_block`, `dns_record` (migration `a917b4c9e251`); FK is
    `ON DELETE CASCADE` so removing a cluster sweeps every mirror
    row atomically. What gets mirrored:
    - Pod CIDR + Service CIDR → one `IPBlock` each under the bound
      space.
    - Node objects → `IPAddress` with `status="kubernetes-node"`,
      hostname = node name.
    - `Service` objects with `spec.type=LoadBalancer` + populated
      `status.loadBalancer.ingress[0].ip` → `IPAddress` with
      `status="kubernetes-lb"`, hostname = `<service>.<namespace>`.
    - `Ingress` objects with `status.loadBalancer.ingress[0].ip`
      → DNS **A** record per `rules[].host` in the longest-suffix-
      matching zone from the bound DNS group; `ingress[0].hostname`
      (cloud LBs) → **CNAME**. `auto_generated=True` + fixed 300 s
      TTL. Rows missing a matching subnet / zone increment
      `skipped_no_subnet` / `skipped_no_zone` on the reconcile
      summary — non-fatal, surfaced in logs + audit. Diff is
      create / update / delete (option 2a: delete, not orphan).
    **Admin UI**: "Sync Now" button per cluster (fires
    `sync_cluster_now` Celery task, bypasses interval gating) plus
    per-row `last_synced_at` / `last_sync_error` display. K8s
    client is a thin `httpx`-based REST wrapper — no
    `kubernetes-asyncio` dep.

  - ⬜ **Phase 2 — external-dns webhook provider (separate
    feature).** Implement the external-dns webhook provider HTTP
    protocol so teams already running external-dns can just point
    it at SpatiumDDI as a DNS backend. Different protocol, different
    testing story — deliberately not bundled with the pull-based
    integration above.

  **Explicit non-goals:**
  - Mirroring pod IPs (too dynamic, too noisy — the CIDR block is
    what matters).
  - Writing to the cluster (no CRD create, no annotation updates).
    If we want write-back, Phase 2's external-dns webhook is the
    right pattern — it's what ops teams already expect.
  - Managing the kubeconfig / kubectl flow — operator brings their
    own cluster-admin credentials to create the ServiceAccount; we
    only ever see the resulting read-only token.


- ✅ **Docker integration (read-only host mirror).** Poll one or
  more Docker daemons with a read-only connection and mirror the
  networks + (opt-in) containers into IPAM. Same UX shape as the
  Kubernetes integration above — `DockerHost` rows bound per-host
  to one `IPAMSpace` + optional `DNSServerGroup`, Settings →
  Integrations → Docker toggle, sidebar item gated on the toggle,
  Fernet-encrypted TLS client key, per-row Test Connection + Sync
  Now buttons, Setup guide with copy-paste TCP+TLS daemon config
  or Unix socket mount instructions.

  **Transport:** `unix` socket or `tcp` with optional mTLS. SSH
  (`docker -H ssh://`) is deferred — needs paramiko +
  `docker system dial-stdio` stream shuffling. No Docker Python
  SDK dep; we hit three Engine API endpoints (`/networks`,
  `/containers/json`, `/info`) over `httpx`.

  **What's mirrored:**
  - Every non-skipped Docker network → IPAM subnet under an
    enclosing operator block when one exists, else a cluster-
    owned wrapper block at the CIDR. Default `bridge` / `host` /
    `none` / `docker_gwbridge` / `ingress` are skipped unless
    `include_default_networks=true`. Swarm overlay networks
    always skipped (cluster-wide — would duplicate across nodes).
  - Network gateway → one `reserved`-status `IPAddress` per
    subnet (mirrors the LAN placeholder that `/ipam/subnets`
    creates for operator-made subnets).
  - Containers (opt-in via `host.mirror_containers`) → one
    `IPAddress` per (container × connected network) with
    `status="docker-container"` and hostname = either
    `<compose_project>.<compose_service>` when Docker Compose
    labels are present, else the container name. Stopped
    containers skipped unless `include_stopped_containers=true`.

  **Phase-3 placeholder (deferred).** Rich per-host management
  surface like mzac/uhld's Docker plugin: container actions
  (start/stop/restart), log streaming, shell exec (pty over
  websocket), image management, compose project up/down,
  volume browser, live events feed. Queries the stored
  connection live — no schema migration needed. Same treatment
  for Kubernetes (pod logs, shell exec, YAML apply, scaling).
  Scoped as a separate feature because it's a full management UI,
  not IPAM, and needs a websocket pipeline we don't have today.

- ✅ **Network sidebar section — group VLANs / VRFs / ASNs /
  Devices** (issue #84). Pure sidebar nav reorg replacing the
  standalone "Network" (SNMP routers/switches) and "VLANs"
  top-level entries with a non-clickable "Network" parent
  mirroring the Administration shape. Devices replaces the old
  top-level Network entry; VLANs lifts up from its own slot.
  Routes moved to `/network/devices`, `/network/vlans`,
  `/network/vrfs`, `/network/asns`; old `/network` and `/vlans`
  paths redirect so existing bookmarks keep working, and the
  legacy `/network/:id` device-detail URL is preserved alongside
  the canonical `/network/devices/:id` form.

- ✅ **ASN management** (issue #85). First-class entity for the
  autonomous systems carrying our IP space, with RDAP holder
  refresh, RPKI ROA tracking, holder-drift detection, four
  ASN/RPKI alert rule types, BGP peering relationships, and the
  BGP communities catalog (issues #88 + #89 ride along).
  - **Data model.** `asn` table — BigInteger `number` to fit the
    full 32-bit AS range; `kind` (public / private) auto-derived
    from RFC 6996 + RFC 7300; `registry` (RIR) auto-derived from
    a hand-curated IANA ASN delegation snapshot at
    `app/data/asn_registry_delegations.json`; WHOIS columns
    populated by the refresh task. `asn_rpki_roa` sibling table —
    prefix CIDR + max_length + validity window + trust_anchor +
    state, ON DELETE CASCADE from `asn`. Migrations
    `f59a5371bdfb_asn_management` + `4a7c8e3d51b9_asn_phase2`.
  - **RDAP holder refresh.** `app/services/rdap_asn.py` derives
    the RIR via `derive_registry()` and queries the RIR's RDAP
    base directly (`rdap.arin.net`, `rdap.db.ripe.net`,
    `rdap.apnic.net`, `rdap.lacnic.net`, `rdap.afrinic.net`) —
    `rdap.iana.org/autnum/<n>` is a bootstrap *registry* not a
    query proxy and returns HTTP 501 for every real query, so
    routing-by-RIR is mandatory. `app/tasks/asn_whois_refresh.
    refresh_due_asns` ticks hourly; per-row Refresh button drives
    the same code path synchronously (`POST /api/v1/asns/{id}/
    refresh-whois`). `next_check_at` cadence tunable via
    `PlatformSettings.asn_whois_interval_hours` (default 24, range
    1..168).
  - **RPKI ROA pull.** `app/services/rpki_roa.py` fetches the
    global ROA dump from Cloudflare (`rpki.cloudflare.com/rpki.
    json`, ~80 MB JSON, ~850k entries) or RIPE NCC's validator
    JSON, filters by AS number, and caches the multi-MB payload
    in-memory for 5 min via `_get_cached_roas` so a beat sweep
    refreshing 50 ASNs makes a single HTTP call instead of 50.
    `app/tasks/rpki_roa_refresh.refresh_due_roas` ticks hourly,
    derives state (`valid` / `expiring_soon` / `expired` /
    `not_found`) off `valid_to`, audit-logs adds / removes / state
    transitions. `_parse_validity` accepts Cloudflare's `expires`
    (Unix epoch) + RIPE's `notBefore` / `notAfter` (ISO 8601) on
    the same row. Per-row Refresh RPKI button (`POST
    /api/v1/asns/{id}/refresh-rpki`) reuses `_refresh_one_asn`
    for the synchronous path. Two new `PlatformSettings` knobs:
    `rpki_roa_source` (cloudflare | ripe) and
    `rpki_roa_refresh_interval_hours` (default 4, range 1..168).
  - **Holder-drift diff viewer.** `asn_whois_refresh` persists
    `previous_holder` into `whois_data` on every successful RDAP
    refresh — drift or not — so the WHOIS tab can render the
    side-by-side without consulting the audit log.
  - **Alert rules.** Four new types: `asn_holder_drift` (latched
    via `alert_event.last_observed_value` JSONB so a single flip
    fires exactly one event auto-resolved after 7 d),
    `asn_whois_unreachable`, `rpki_roa_expiring` (severity
    escalation at threshold/4 + threshold/12 around
    `threshold_days`, default 30 d), `rpki_roa_expired`.
  - **BGP peering relationships (issue #89).** `bgp_peering`
    table — `peer | customer | provider | sibling`. Both
    endpoints FK ON DELETE CASCADE; unique on `(local, peer,
    relationship_type)`. Column named `relationship_type` so it
    doesn't shadow the imported `sqlalchemy.relationship`
    function. `router.local_asn_id` FK ON DELETE SET NULL stamps
    which AS a router originates routes from. `PeeringsTab` on
    ASN detail with directional listing (`→ outbound` / `←
    inbound` from this AS's POV) and clickable counter-AS;
    `PeeringFormModal` lets the operator pick either side as
    "local" + normalises to canonical shape on submit. Migration
    `d3f2a51c8e76_bgp_peering`.
  - **BGP communities catalog (issue #88).** `bgp_community`
    table; `asn_id` nullable so platform-level rows (RFC 1997 /
    7611 / 7999 well-knowns: no-export, no-advertise,
    no-export-subconfed, local-as, graceful-shutdown, blackhole,
    accept-own) are shared across all ASes. `kind` denormalises
    on-the-wire shape: `standard` / `regular` (`ASN:N` per RFC
    1997) / `large` (`ASN:N:M` per RFC 8092).
    `app.services.bgp_communities` owns the well-known catalog +
    seeds it on first boot via a hook in `main.py`'s lifespan
    (subsequent boots refresh the description text so upgrades
    that reword a row land without an admin edit). CRUD: `GET
    /asns/communities/standard`, `GET|POST /asns/{asn_id:uuid}/
    communities`, `PATCH|DELETE /asns/communities/{community_id:
    uuid}`. Standard catalog rows refuse PATCH / DELETE with a
    400. `CommunitiesTab` with collapsible standard catalog +
    "Use on this AS" buttons + per-AS list grouped by kind.
    Migration `f4a6c8b2e571_bgp_communities`.
  - **BGP FK on IPSpace / IPBlock.** `asn_id` UUID column on
    both, FK to `asn.id` ON DELETE SET NULL, indexed. Shared
    `AsnPicker` at `components/ipam/asn-picker.tsx` wired into
    the four IPSpace / IPBlock modals as an optional "Origin ASN
    (BGP)" field. Migration `c9f1e47d2a83_bgp_asn_fk`.
  - **Detail page tabs.** WHOIS · RPKI ROAs · Linked IPAM ·
    BGP Peering · Communities · Alerts.

- ✅ **VRFs as first-class entities** (issue #86). Replaces the
  freeform `vrf_name` / `route_distinguisher` / `route_targets`
  text fields on IPSpace with a relational `vrf` table.
  - **Data model.** `vrf` table carries name, description,
    optional `asn_id` FK, RD with format validation, split
    import / export RT lists, tags, custom_fields. `ip_space.
    vrf_id` + `ip_block.vrf_id` FKs ON DELETE SET NULL. Migration
    backfills new VRF rows from every distinct (vrf_name, rd,
    rt-list) triple on existing IPSpace rows and stamps each
    space's `vrf_id` at the matching new row; freeform columns
    stay in place for one release cycle so operators can verify
    the mapping landed before they get dropped.
  - **Cross-cutting RD / RT validation.** Each `ASN:N` entry
    whose ASN portion does not match `vrf.asn.number` produces a
    non-blocking warning on the response. `PlatformSettings.
    vrf_strict_rd_validation` flips to escalate the same
    mismatch to 422. Second warning fires when `vrf.asn_id` is
    null but the RD is in `ASN:N` form. `IPBlock` responses also
    carry `vrf_warning` flagging when a block's pinned VRF
    differs from its parent space's VRF (intentional in
    hub-and-spoke designs but worth a heads-up).
  - **VRF picker** at `components/ipam/vrf-picker.tsx` wired into
    the New / Edit IPSpace + Create / Edit IPBlock modals,
    replacing the freeform text inputs. Space-detail header
    surfaces the linked VRF's name + RD + RTs read-only via the
    cached VRF list.
  - **Permission + UI.** `manage_vrfs` permission seeded into
    the Network Editor builtin role. List page at `/network/
    vrfs` with bulk-select + draggable create/edit modal. Detail
    page at `/network/vrfs/:id` with linked IP spaces / blocks
    tabs and a Pencil HeaderButton wired to the shared
    `VRFEditorModal`. Migrations `2c4e9d1a7f63_vrf_first_class`
    + `b7e2a4f91d35_vrf_phase2`.

- ✅ **Domain registration tracking** (issue #87). Distinct from
  DNSZone — tracks the registry side of a name (registrar /
  registrant / expiry / nameservers / DNSSEC) versus the records
  SpatiumDDI serves.
  - **Data model.** `domain` table with the registry-side fields,
    expected-vs-actual nameservers, `whois_state` (ok / drift /
    expiring / expired / unreachable), `consecutive_failures`
    counter. Migrations `3124d540d74f_domain_registration` +
    `4a9e7c2d18b3_domain_phase2`.
  - **RDAP refresh.** `app.services.rdap` httpx client (10 s
    per-call / 15 s total budget). TLD → RDAP-base lookup driven
    by the IANA bootstrap registry at `data.iana.org/rdap/dns.
    json`, cached in-process for 6 h with an asyncio lock against
    thundering-herd refetch + a stale-cache fallback if the
    bootstrap fetch fails — `rdap.iana.org/domain/<n>` returns
    404 for any non-test domain so per-TLD routing is mandatory.
    Routes `.com` → `rdap.verisign.com/com/v1/`, `.net` →
    `rdap.verisign.com/net/v1/`, etc. Beat-fired `app/tasks/
    domain_whois_refresh.refresh_due_domains` ticks hourly,
    self-paces via `PlatformSettings.domain_whois_interval_hours`
    (default 24 h, 1–168 h range). `derive_whois_state` decision
    tree (unreachable → expired → expiring < 30 d → drift → ok)
    + `compute_nameserver_drift` shared between sync `POST
    /domains/{id}/refresh-whois` endpoint and the task.
  - **Alert rules.** `domain_expiring` (severity escalation at
    threshold/4 + threshold/12 around the operator-set
    `threshold_days`, default 30 d), `domain_nameserver_drift`,
    `domain_registrar_changed`, `domain_dnssec_status_changed`.
    The two transition-once rules latch the observed value into
    a new `alert_event.last_observed_value` JSONB column so a
    single flip fires exactly one event, auto-resolved after
    7 d. `alert_rule.threshold_days` is the new params column
    (mirrors the existing `threshold_percent` shape).
  - **DNSZone linkage.** `dns_zone.domain_id` nullable FK ON
    DELETE SET NULL. Picker on the DNS zone create / edit modal —
    "Auto-match by zone name" remains the default for backward-
    compat. Domain detail's "Linked DNS Zones" tab prefers the
    explicit FK and falls back to a left-anchored suffix match
    (`zone === domain || zone.endsWith("." + domain)`) when
    `domain_id` is unset, so child zones inherit (`test.example.
    com` shows up under `example.com`) but `example.com.au`
    correctly does NOT match `example.com`. Sub-zones get a
    "sub-zone" badge. Migration `e7b8c4f96a12_dns_zone_domain_fk`.
  - **List + detail UI.** Domains nav lives in the core sidebar
    (between DNS Pools and Logs) — registration tracking is core
    operational data, not platform admin. List page at `/admin/
    domains` with sticky table, expiry countdown badges (green >
    90 d / amber 30–90 d / red < 30 d / dark-red expired),
    per-row Refresh + Edit + Delete, multi-select bulk refresh /
    bulk delete, expandable row with raw RDAP payload. Detail
    page at `/admin/domains/:id` with registration card,
    expected-vs-actual NS diff panel, raw WHOIS / Linked DNS
    Zones / Alert History tabs.

- ✅ **Multi-group DNS publishing — split-horizon at the IPAM
  layer** (issue #25). `IPBlock.dns_split_horizon` boolean +
  the existing `dns_inherit_settings` walk. When set,
  descendant subnets publish records to `dns_zone_id`
  (internal) AND every entry in `dns_additional_zone_ids`
  (DMZ / external). Per-record routing is decided by the new
  `IPAddress.dns_zone_overrides` JSONB list
  (`[{zone_id, record_type}]`) so an operator can pin one
  address to publish only into the internal zone. Auto-sync
  task respects the split. Closes the IPAM-layer half of the
  split-horizon roadmap; DNS Views (#24) covers the
  recursive-resolver-side split.

- ✅ **IPAM template classes — reusable stamp templates with
  child layouts** (issue #26). New `ipam_template` table
  captures default tags / custom-fields / DNS / DHCP / DDNS
  settings (plus an optional sub-subnet `child_layout`) and
  stamps them onto blocks or subnets at apply time.
  ``applies_to`` locks each template to one of the two
  carriers so apply-time semantics stay unambiguous.
  ``force=False`` fills only empty/null target columns;
  ``force=True`` overwrites unconditionally and is the path
  ``/reapply-all`` uses to refresh drift across every recorded
  instance (cap 200). ``IPBlockCreate.template_id`` /
  ``SubnetCreate.template_id`` add optional pre-fill on the
  create paths; carrier rows now carry an
  ``applied_template_id`` SET-NULL FK so a "reapply across
  instances" sweep can find every row touched. Block templates
  with ``child_layout`` carve sub-subnets sequentially on
  apply (and on create); idempotent — sub-subnets already at a
  target CIDR are skipped. ``/admin/ipam/templates`` page (list
  + tabbed editor: General / Tags + CFs + DNS-DHCP / DDNS /
  Child layout) + per-row Reapply with typed-name confirm. New
  ``manage_ipam_templates`` permission seeded into the IPAM
  Editor builtin role.

- ✅ **Move IP block / space across IP spaces** (issue #27).
  `POST /ipam/blocks/{id}/move` accepts a target `space_id` +
  a typed-name confirmation. Pre-flight validates target
  space exists, no CIDR overlap in the target tree, every
  dependent row (DNS records, DHCP scopes, addresses with
  custom-field inheritance) survives the move.
  `MoveBlockModal` walks the operator through the
  consequences with a chevron-revealed list of affected
  resources before the typed-name confirm unlocks Move.

- ✅ **Operator Copilot — Ask-AI everywhere with multi-vendor
  failover** (issue #90). Two phases:
  - **Phase 1 — provider config + LLM driver foundation, MCP
    HTTP endpoint, chat orchestrator + SSE chat endpoint,
    floating chat drawer, observability + caps.** Migration
    `a4b8c2d619e7` adds `ai_provider` (Fernet-encrypted
    api_key, kind discriminator, ordered priority for
    failover, JSONB options bag). LLM driver ABC at
    `app/drivers/llm/base.py` defines neutral
    request / chunk / tool dataclasses; concrete drivers
    translate at the SDK boundary. OpenAI-compat driver
    covers OpenAI / Ollama / OpenWebUI / vLLM / LM Studio /
    llama.cpp server / LocalAI / Together / Groq / Fireworks.
    Tool registry mirrors the driver registry shape. 18
    read-only tools cover the common operator asks
    (`list_subnets`, `get_subnet`, `list_ips`, `get_ip`,
    `list_zones`, `list_records`, `list_dhcp_scopes`,
    `list_leases`, `list_alerts`, `list_audit`,
    `list_devices`, `list_circuits`, `list_customers`,
    `list_sites`, `list_providers`, `list_asns`, `list_vrfs`,
    `list_overlays`). MCP-shaped HTTP endpoint at
    `/api/v1/ai/mcp` exposes the same set so external MCP
    clients (Claude Desktop / Cursor / Cline) can connect
    directly. Chat orchestrator runs the iterative
    tool-calling loop. SSE endpoint at
    `POST /api/v1/ai/chat/{session_id}/messages` streams
    `text-delta` / `tool-call-delta` / `tool-result` events;
    sets `X-Accel-Buffering: no` so nginx doesn't batch the
    stream. Floating chat drawer renders Markdown + code
    blocks + tool-call collapsed-by-default cards;
    optimistically renders the user's just-sent message.
    Migration `c8e3a7f10b54` adds `ai_usage_event`; pricing
    table at `services/ai/pricing.py` covers the major
    hosted models; per-user daily cap enforced in the
    orchestrator; live token chip in the drawer header; AI
    usage card on Platform Insights aggregates the last 7
    days by provider + model.
  - **Phase 2 — Anthropic / Azure OpenAI / Google Gemini
    drivers, failover chain, "Ask AI about this"
    affordances, custom prompts library, Cmd-K palette
    entry, daily digest, write tools.** Anthropic driver
    translates to Messages API (system prompt as a top-level
    field; tool-use blocks vs. tool-calls). Azure OpenAI
    driver adapts the OpenAI-compat shape to per-deployment
    URL + `?api-version=` query param. Google Gemini driver
    translates to `generateContent` API and reassembles
    streamed function calls. Orchestrator walks providers in
    priority order on transient failures (5xx / timeout /
    rate-limit) — first successful chunk wins; permanent
    errors (4xx / auth) surface immediately. Compact "Ask AI
    about this" icon button on subnets / IPs / DNS zones /
    records / alerts / audit / DHCP / network devices
    pre-fills the chat drawer with a templated prompt + the
    resource UUID. Custom prompts library
    (`ai_custom_prompt`) with a built-in starter pack (Find
    unused IPs, Audit recent changes, Summarize subnet
    utilization, Triage open alerts). Cmd-K palette top
    entry; fixed shortcut conflict with the existing search
    hotkey. Daily Operator Copilot digest fired by Celery
    beat at 0900 local through audit-forward / SMTP /
    webhook channels (off by default). Write tools follow a
    two-phase preview / apply contract: model proposes via
    `propose_*` returning a planned `proposed_change`
    envelope, operator clicks Apply in the chat drawer,
    frontend sends `apply_change` follow-up that hits the
    real CRUD endpoint. Three pilot tools:
    `propose_create_ip`, `propose_update_ip_status`,
    `propose_create_dns_record`. System prompt now
    interpolates platform stats (subnet count, alert count,
    recent audit summary), the operator's role + scoped
    permissions, and "today's interesting things" (services
    expiring < 30 d, alerts opened in the last hour, devices
    last seen > 7 d ago).

- ✅ **Customer / Site / Provider — logical ownership
  entities** (issue #91). Three first-class rows that
  cross-cut IPAM / DNS / DHCP / Network so operators can
  answer "who owns this?", "what's at NYC?", and "which
  circuits does Cogent supply us?" without resorting to
  free-form tags. `Customer` is soft-deletable; `Site` is
  hierarchical (`parent_site_id`) with a unique-per-parent
  `code` (NULLS NOT DISTINCT for top-level deduping);
  `Provider` carries an optional `default_asn_id` FK + a
  `kind` enum (transit / peering / carrier / cloud /
  registrar / sdwan_vendor). Cross-reference columns added
  on every existing IPAM/DNS/DHCP/Network table with
  `ON DELETE SET NULL` so a customer/site/provider deletion
  never cascades into core rows — operators want to re-tag,
  not lose data. Three new admin pages (Customers / Sites /
  Providers) + shared `CustomerPicker` / `SitePicker` /
  `ProviderPicker` (with optional kind filter) + matching
  Chip components plug into every IPAM / DNS / circuit /
  overlay create + edit modal. RBAC seeded into Network
  Editor + IPAM Editor. Migration
  `c2a7e4f81b69_logical_ownership_entities`. **Deferred
  follow-ups:** customer-scoped dashboard KPIs, site-scoped
  DNS auto-pin, customer-bulk-assign tool, freeform
  `Domain.registrar` text → FK backfill.

- ✅ **WAN circuits — first-class transport tracking** (issue
  #93). New `circuit` table — carrier-supplied logical pipe
  (the contract + transport class + bandwidth + endpoints +
  term + cost), distinct from the equipment lighting it up
  (CMDB territory, intentionally out of scope).
  `provider_id` is `ON DELETE RESTRICT` (carrier
  relationship too load-bearing to silently null);
  `customer_id` and the four endpoint refs (a/z-end site +
  subnet) are `ON DELETE SET NULL`. Nine transport classes
  (mpls / internet_broadband / fiber_direct / wavelength /
  lte / satellite + three cloud cross-connects:
  direct_connect_aws / express_route_azure /
  interconnect_gcp). Soft-deletable so `status='decom'` is
  the operator-visible end-of-life flag while history stays
  restorable. CRUD under `/api/v1/circuits` with filters
  (provider_id / customer_id / site_id matching either end /
  subnet_id / transport_class / status / expiring_within_days
  / search) + `/by-site/{site_id}` convenience endpoint.
  `/network/circuits` page with bulk-action table + tabbed
  editor modal (General / Endpoints / Term + cost / Notes) +
  colour-coded term-end badge + asymmetric bandwidth display.
  Two new alert rule types — `circuit_term_expiring` (mirrors
  `domain_expiring` shape with severity escalation per
  `threshold/4` / `threshold/12`) and `circuit_status_changed`
  (transition-style — fires once on `suspended` or `decom`
  with auto-resolve after 7 d; routine `active` ↔ `pending`
  flips during commissioning are intentionally excluded).
  Migration `d9f3b21e8c54_wan_circuits`.

- ✅ **Service catalog — MPLS L3VPN service modeling
  (extensible)** (issue #94). First-class
  customer-deliverable bundle. `NetworkService` is one row
  per thing the operator delivers (`mpls_l3vpn` + `custom`
  shipped in v1; `sdwan` lit up alongside #95). Polymorphic
  `NetworkServiceResource` join row binds a service to VRF
  / Subnet / IPBlock / DNSZone / DHCPScope / Circuit / Site
  / OverlayNetwork. `customer_id` is `ON DELETE RESTRICT`
  (contractual weight too load-bearing to silently null).
  Hard rule: `mpls_l3vpn` services may have at most one VRF
  attached (422 on second VRF, 422 on kind-flip-to-L3VPN if
  >1 VRF already linked). Soft rules surfaced as warnings on
  `GET /summary`: missing VRF, fewer than 2 edge sites, edge
  subnet's enclosing block in a different VRF than the
  service. Endpoints: standard CRUD + bulk-delete,
  `POST/DELETE /{id}/resources` for attach / detach,
  `GET /{id}/summary` with kind-aware shape (L3VPN view
  returns canonical VRF + edge sites + edge circuits + edge
  subnets + warnings), `GET /by-resource/{kind}/{id}`
  reverse lookup. Two new alert rule types —
  `service_term_expiring` (mirrors circuit shape) and
  `service_resource_orphaned` (sweep over join rows whose
  target was deleted; auto-resolves on detach). Migration
  `e1d8c92a4f73_network_service_catalog` +
  `f2c8d49a1e76_alert_event_subject_type_widen` (VARCHAR(20)
  → VARCHAR(40) to fit `network_service_resource`).
  `/network/services` page (bulk-action table) + tabbed
  editor modal (General / Resources / Term + cost / Notes /
  Summary) with per-kind resource pickers (cross-group
  fan-out for DNS zones + DHCP scopes). RBAC into Network
  Editor + IPAM Editor. **Deferred follow-ups:** service
  templates, per-customer monthly spend rollup, service
  status timeline, L2VPN / VPLS / EVPN / DIA / hosted-DNS /
  hosted-DHCP kinds, right-click "show services using this
  resource" entry points (issue #99).

- ✅ **SD-WAN — overlay networks + transport-aware routing
  policies** (issue #95). Vendor-neutral source of truth for
  SD-WAN overlay topology + routing-policy intent. Vendor
  config push (vManage / Meraki Dashboard / FortiManager /
  Versa Director) and real-time path telemetry are
  explicitly out of scope. Four tables landing together:
  `overlay_network` (soft-deletable; six kinds — sdwan /
  ipsec_mesh / wireguard_mesh / dmvpn / vxlan_evpn /
  gre_mesh; free-form vendor + encryption_profile so
  non-curated vendors plug in without enum migration),
  `overlay_site` (m2m binding sites with role hub / spoke /
  transit / gateway, edge device, loopback subnet, ordered
  `preferred_circuits` jsonb — first wins, fall through on
  outage), `routing_policy` (declarative per-overlay policy
  with priority + match-kind + match-value + action +
  action-target + enabled), and `application_category`
  (curated SaaS catalog used by `match_kind=application`,
  seeded at startup with 33 well-known apps following the
  BGP-communities pattern). CRUD under `/api/v1/overlays`
  (with sites + policies sub-resources) and
  `/api/v1/applications`. `GET /overlays/{id}/topology`
  returns nodes (sites + roles + device + loopback +
  preferred-circuits) + edges (site pairs whose
  `preferred_circuits` lists overlap — `shared_circuits` is
  the intersection so the UI can colour by transport class)
  + policies. `POST /overlays/{id}/simulate` is pure
  read-only what-if; body specifies `down_circuits`,
  response shows per-site fallback resolution (surviving
  preferred chain + primary circuit + `blackholed` flag) and
  per-policy effective-target with `impacted` flag + a
  human-readable note. Three new RBAC resource types
  (`overlay_network` / `routing_policy` /
  `application_category`) into Network Editor.
  Service-catalog (#94) integration unlocked: `sdwan` added
  to `SERVICE_KINDS_V1`, `overlay_network` lit up as a real
  attach target, `service_resource_orphaned` alert sweep
  covers deleted overlays. `/network/overlays` list page +
  detail page at `/network/overlays/{id}` with five tabs —
  Overview / Topology (SVG circular layout, role-coloured
  nodes, transport-coloured edges with solid for
  single-class and dashed for mixed) / Sites / Policies
  (priority-ordered with up/down reorder + per-kind editors)
  / Simulate (toggle circuits down + see per-site fallback
  + per-policy impact). Migration
  `c4f7e92d3a18_sdwan_overlay`. **Deferred follow-ups (issue
  #100):** vendor read-only mirror integrations (Meraki /
  Viptela / Fortinet / VeloCloud / Versa), topology diff,
  cross-vendor policy lint, application catalog enrichment
  from upstream feeds, path-quality SLA targets, overlay
  templates, plus v1 UX polish — D3 force-directed topology
  layout, drag-and-drop reordering, Applications admin UI
  page, "Used by N services" link on the overlay detail
  header.


## Integration roadmap

- ✅ **Proxmox VE** — `ProxmoxNode` model + REST client
  + reconciler landed. Auth is API-token (`user@realm!tokenid`
  + UUID), token secret Fernet-encrypted at rest. One row
  covers a standalone host OR a whole cluster (PVE API is
  homogeneous across cluster members; `/cluster/status` surfaces
  cluster name + node count). Mirror scope:
  - **SDN VNets** (`/cluster/sdn/vnets/{vnet}/subnets`) →
    `Subnet` named `vnet:<vnet>` with the declared gateway.
    Authoritative over bridge-derived rows for the same CIDR —
    operator intent from PVE SDN wins. Returns 404 when SDN
    isn't installed; reconciler treats that as "no SDN" and
    keeps going.
  - **SDN VNet subnet inference** (opt-in via
    `infer_vnet_subnets` toggle, default off) — for VNets that
    exist but have no declared subnets, derive the CIDR from
    guests. Priority: (1) exact `static_cidr` from a VM's
    `ipconfigN gw=` or LXC's inline `ip=`/`gw=`; (2) /24 guess
    around guest-agent runtime IPs with a `proxmox_vnet_cidr_guessed`
    warning hinting at the `pvesh create` replacement. Solves
    the "PVE is L2 passthrough, gateway lives on upstream
    router" case where operators have many VNets with zero
    declared subnets. Migration `e5a72f14c890`.
  - Bridges + VLAN interfaces with a CIDR → Subnet (under
    enclosing operator block when present, else auto-created
    RFC 1918 / CGNAT supernet via the shared helper). Bridges
    without a CIDR skipped.
  - VM + LXC NICs → IPAddress with `status="proxmox-vm"` /
    `"proxmox-lxc"`, MAC from config. Runtime IP from QEMU
    guest-agent (when `agent=1` + agent running) or LXC
    `/interfaces`; falls back to `ipconfigN` static IP (VMs)
    or inline `ip=` (LXC); NIC silently skipped when nothing
    resolves. Link-local + loopback addresses filtered out.
  - Bridge gateway IP → `reserved` placeholder row per subnet.
  `mirror_vms` + `mirror_lxc` default **true** (PVE guests are
  long-lived, unlike Docker CI containers). **Discovery
  modal** — the reconciler writes a `last_discovery` JSONB
  snapshot on every successful sync (category counters + a
  per-guest list with single top-level `issue` code + operator-
  facing `hint`); admin page gets a magnifier-icon button per
  endpoint that opens a filterable Discovery modal showing
  agent-state pills + IPs-mirrored split + copy-ready fix
  hints like "install qemu-guest-agent inside the VM". Default
  filter is `Issues` so operators land on what needs attention.
  Migration `e7b3f29a1d6c`. Covered by 38 tests:
  `test_proxmox_client.py` (NIC + ipconfig + SDN-id parsing) +
  `test_proxmox_reconcile.py` (pipeline end-to-end, SDN
  subnet merge, VNet inference with both static-CIDR and
  runtime-IP paths, cascade delete). Migration
  `d1a8f3c704e9` (base) + `e5a72f14c890` (infer toggle) +
  `e7b3f29a1d6c` (discovery payload).
  **Deferred follow-ups:**
  - **Phase 2 per-cluster management surface** — VM / LXC
    start/stop/shutdown, console access, live migrate,
    snapshot, backup browser. Mirrors the Kubernetes /
    Docker Phase-3 pattern; needs websocket pipeline we
    don't have today.
  - **Pool / resource-tag awareness.** PVE has a "pool" object
    for grouping resources and an arbitrary tag system; neither
    surfaces in IPAM today. Low-effort to add as custom-field
    passthrough once an operator asks.
  - **Cluster-quorum alerting.** `/cluster/status` carries a
    `quorate` bool — wire that into the alerts framework so an
    HA cluster losing quorum pages operators.


- ✅ **Tailscale — Phase 1: device mirror.** Per-`TailscaleTenant`
  row, PAT token + tailnet name (default `-`), Fernet-encrypted
  at rest. 60 s default sync interval (Tailscale rate-limits
  `/api/v2` at 100 req/min, so 30 s floor is the same as the
  other integrations). The reconciler hits
  `GET /api/v2/tailnet/{tn}/devices?fields=all` and mirrors each
  device's `addresses[]` (both IPv4 in `100.64.0.0/10` and IPv6
  ULA in `fd7a:115c:a1e0::/48`) as IPAddress rows under the
  bound IPAM space. The CGNAT + IPv6-ULA blocks are
  auto-created on first sync — operator can override the CGNAT
  CIDR per tenant for non-default tailnets. Per-row shape:
  - `status="tailscale-node"`, hostname = device FQDN
    (`<host>.<tailnet>.ts.net`), MAC null (no L2 on the overlay).
  - `description` = `<os> <clientVersion> — <user>`.
  - `custom_fields` = `{os, client_version, user, tags,
    authorized, last_seen, expires, key_expiry_disabled,
    update_available, advertised_routes, enabled_routes,
    node_id}`.
  - Tailnet domain is auto-derived from the first device's FQDN
    (no separate config field; mirrors the uhld pattern).
  Read-only-ish: integration-owned status + the
  `user_modified_at` lock keep operator edits sticky across
  reconciles (same pattern as Proxmox / Docker / Kubernetes).
  Provenance via `tailscale_tenant_id` FK on
  `ip_address`/`ip_block`/`subnet` with `ON DELETE CASCADE`.
  Subnet-router routes (`enabledRoutes`) are stored in
  custom_fields today; promoting them to first-class IPBlock
  rows is a follow-up. Test Connection probe + Sync Now button
  in the admin page mirror the other integrations.


- ✅ **Tailscale — Phase 2: synthetic tailnet DNS surface.**
  Implemented as Option 2 from the original plan (synthetic
  `DNSZone` materialised by the reconciler). When a
  `TailscaleTenant` has `dns_group_id` bound, every reconcile
  pass derives `<tailnet>.ts.net` from the first device FQDN,
  upserts a `DNSZone` with `is_auto_generated=True` and
  `tailscale_tenant_id=<tenant>`, and materialises one A / AAAA
  `DNSRecord` per device address. Records also carry
  `auto_generated=True` + the tenant FK. **Read-only enforcement**:
  `update_zone` / `delete_zone` / record CRUD reject writes
  with 422 when `tailscale_tenant_id IS NOT NULL`; UI renders a
  "Tailscale (read-only)" badge near the zone title and disables
  the Edit / Delete / Add Record buttons; the per-record lock
  badge in the records table branches on `tailscale_tenant_id`
  to read "Tailscale" instead of "IPAM" for synthesised rows.
  **Diff semantics**: keyed on `(name, record_type, value)` —
  removed devices have their records deleted on the next sync;
  idempotent on stable input. **Conflict safety**: a pre-
  existing operator-managed zone with the same name is left
  untouched, with a `summary.warnings` entry that surfaces in
  the audit log. **Filtering**: expired-key devices skipped per
  Phase 1's `skip_expired` toggle; devices with no FQDN or
  foreign tailnet suffix are skipped without error. **Bonus**:
  because we land actual `DNSRecord` rows, the existing BIND9
  render path picks them up automatically — non-Tailscale LAN
  clients can resolve `<host>.<tailnet>.ts.net` through
  SpatiumDDI's managed BIND9 with no forwarder plumbing. TTL
  is 300 s (short by design — IP assignments shift after re-auth).
  Migration `e6f12b9a3c84_tailscale_phase2_dns`.

  **Deferred follow-ups:**
  - **Per-tenant zone-name override.** Today we use the auto-
    derived `<tailnet>.ts.net`. Some operators run Tailscale
    with a custom split-DNS arrangement and want the synthetic
    zone published under a different name (e.g.
    `tailnet.internal`). Adding a `synthetic_zone_name` column
    on `TailscaleTenant` (defaulting to null = derive) would
    cover that without much code.
  - **Subnet-router routes (`enabled_routes`) → first-class
    `IPBlock` rows.** Phase 1 stores them in `custom_fields`;
    promoting them to real IPBlocks (one per route, FK to
    tenant) would let the IPAM tree show "this CIDR is reachable
    via tailnet device X." Worthwhile if operators start
    advertising LAN subnets through Tailscale.
  - **BIND9 forwarder zone for `100.100.100.100`.** Optional
    alternative for operators who don't want SpatiumDDI's BIND9
    serving the records itself but DO want to forward `*.ts.net`
    queries through MagicDNS (which only listens on the local
    Tailscale daemon — works only if the BIND9 host is itself
    on Tailscale).


## Future ideas — categorised

### Discovery & network awareness

- ✅ **LLDP neighbour collection** — vendor-neutral via
  LLDP-MIB (IEEE 802.1AB) `lldpRemTable`. Per-device opt-in
  `poll_lldp` toggle (default on); polled as a 5th step after
  ARP / FDB in `app.tasks.snmp_poll.poll_device`.
  `network_neighbour` table keyed
  `(device_id, interface_id, remote_chassis_id, remote_port_id)`
  with absence-delete every poll so stale neighbours fall off
  cleanly. Captures remote chassis ID + port ID (with subtype
  decoding — MAC addresses formatted, interface names left
  raw), system name + description, port description, and a
  decoded capabilities bitmask (Bridge / Router / WLAN AP /
  Phone / Repeater / Other / Station / DocsisCableDevice).
  API: `GET /api/v1/network-devices/{id}/neighbours` with
  `sys_name` (ilike) / `chassis_id` / `interface_id` filters.
  Frontend: new "Neighbours" tab on the network device detail
  view, with vendor-aware enable hints (Cisco IOS / NX-OS,
  Junos, Arista EOS, ProCurve / Aruba, MikroTik RouterOS,
  OPNsense / pfSense) when no rows are present. Migration
  `b9e4d2a17c83_network_neighbour`. **Deferred:** CDP polling
  (Cisco-only — LLDP usually runs alongside on modern gear);
  topology graph rendering (the data is captured, the graph UI
  isn't built yet); IP cross-reference via
  `lldpRemManAddrTable`; per-port enrichment via
  `lldpLocPortIfIndex` to resolve LLDP's own port numbering to
  ifIndex (today the local port is recorded by SNMP-side
  ifIndex via the FDB / interface walk, not LLDP's local
  port-num index).

- ✅ **Switch-port mapping in the IP table (column-level).** The
  IPAM IP table now carries a "Network" column showing
  `<device> · <port> [VLAN N]` for the most-recent FDB hit on
  each IP's MAC, with a `+N more` badge + hover tooltip listing
  every (device, port, VLAN) tuple when the MAC is learned in
  multiple places (hypervisor host + VMs across access VLANs,
  trunk ports). Backed by a batched
  `GET /api/v1/ipam/subnets/{id}/network-context` endpoint that
  returns `{ip_address_id: NetworkContextEntry[]}` in one round
  trip — no N+1 fan-out per page-of-IPs. The per-IP detail
  modal's "Network" tab still works as the deep-dive surface.
  **Deferred follow-ups:**
  - Reverse "click MAC → all IPs on this port" drilldown
    starting from an interface row in the network detail page.
    Useful when an operator's looking at a switch port and
    wants to see "what's plugged in here?".
  - Per-user column show/hide (lives under the broader Saved
    Views work in UX polish below).
  - Filter input on the Network column (the column header has
    no filter today; operators can still filter via the device
    detail page's ARP/FDB tabs).

### Subnet planning & calculation tools

- ✅ **Built-in CIDR calculator** — utility page at `/tools/cidr`
  with sidebar nav under Tools. Pure client-side, no API. Accepts
  IPv4 or IPv6 (CIDR or bare address), renders network / netmask /
  wildcard / broadcast / first-last usable / total addresses /
  decimal + hex / binary breakdown (v4) and compressed +
  expanded forms (v6). Quick-paste preset buttons for the common
  RFC 1918 / CGNAT / ULA blocks. BigInt math throughout so v6
  prefixes work cleanly.

- ✅ **Subnet planner / "what-if" workspace** — `/ipam/plans`.
  Operator designs a multi-level CIDR hierarchy as a draggable
  tree (one root + nested children, arbitrary depth), saves it
  as a `SubnetPlan` row, validates against current state, then
  one-click applies — every block + subnet created in a single
  transaction. **Data model:** `subnet_plan(id, name,
  description, space_id, tree JSONB, applied_at,
  applied_resource_ids JSONB, created_by_user_id)`. Tree node
  shape: `{id, network, name, description, kind, existing_block_id?,
  dns_group_id?, dns_zone_id?, dhcp_server_group_id?,
  vlan_ref_id?, gateway?, children[]}`. **`kind` is explicit**
  per node (`block` or `subnet`) — root must be a block
  (subnets need a block parent), and a subnet may not have
  children (validation enforces both). **Resource bindings**
  (DNS group, DHCP group, gateway) are optional per-node;
  `null` = inherit, explicit value sets the field on the
  materialised row and flips the corresponding
  `*_inherit_settings=False`. UI exposes DNS group + DHCP
  group dropdowns + gateway field on subnets; VLAN + DNS-zone
  fields exist in the model but UI binding is deferred (VLAN
  is router-scoped — needs a flat list endpoint first). **Root
  modes:** new top-level CIDR (creates a fresh block at the
  space root) OR anchor to an existing `IPBlock` (descendants
  land as children of the existing block, root not re-created).
  **Validation** (`/plans/{id}/validate` + `/plans/validate-tree`
  for in-flight trees) checks duplicate node ids, kind rules,
  parent-containment of every child, sibling-overlap, and
  overlap against current IPAM state in the bound space.
  Auto-fires every 300 ms as the operator edits — conflicts
  surface inline as red ring on offending nodes + a banner
  above the tree. **Apply** opens a confirmation modal with
  block + subnet counts ("this will create N blocks + M
  subnets"), then re-validates inside the txn; any conflict
  → 409 with the full conflict list and nothing is written.
  Once applied, the plan flips read-only and
  `applied_resource_ids` records every created block + subnet
  for audit. **Reopen** (`/plans/{id}/reopen`) flips an
  applied plan back to draft state, but only if every
  materialised resource has been deleted from IPAM —
  otherwise 409 with the survivor list. Lets operators
  iterate on the same plan after a teardown rather than start
  fresh. **Frontend:** `@dnd-kit/core` (already a dep) for
  drag-to-reparent; drops onto descendants OR onto subnet
  targets are refused. Properties panel on the right edits
  CIDR / name / kind / DNS / DHCP / gateway for the selected
  node. Sidebar entry "Subnet Planner" alongside NAT
  Mappings. Migration `c8e1f04a932d_subnet_plan`.
  **Deferred:** sibling reordering via `@dnd-kit/sortable`
  (today reparenting only appends as last child); split-into-N
  action on a node; VLAN dropdown in the planner UI (model
  field exists; needs a flat VLAN list endpoint first); DNS
  zone selector that's gated on the chosen DNS group; per-node
  custom-fields / tags / status (those are per-IP and edited
  through normal IPAM use after apply).

- ✅ **Address planner** — `POST /api/v1/ipam/blocks/{id}/plan-
  allocation` accepts a list of `{count, prefix_len}` requests
  (e.g. `4 × /24, 2 × /26, 1 × /22`) and packs them into the
  block's free space using largest-prefix-first ordering with
  first-fit-by-address placement (so sequential same-size
  requests pack contiguously from low addresses rather than
  chasing small isolated free islands).
  Returns the planned allocations + any unfulfilled rows + the
  remaining free space after the plan. Reuses the same
  `address_exclude` walk that powers `/free-space`. UI: "Plan
  allocation…" button next to the Allocation map on the block
  detail; modal lets the operator add/remove rows and shows the
  packed result. Preview only — no writes — so the operator can
  iterate freely. **Deferred:** one-click apply that chains the
  preview into N `POST /subnets` calls inside a transaction.

- ✅ **Aggregation suggestion** — `GET /api/v1/ipam/blocks/{id}/
  aggregation-suggestions` runs `ipaddress.collapse_addresses` on
  the block's direct-child subnets; any output that subsumes more
  than one input is a clean merge opportunity (the inputs pack
  perfectly into a supernet with no gaps). Read-only banner on the
  block detail surfaces them when present (e.g. `10.0.0.0/24 +
  10.0.1.0/24 → /23`). **Deferred:** one-click merge flow — needs
  to handle the cascade across IP rows + DNS records owned by the
  deleted siblings, plus operator confirmation. Today operators
  see the suggestion and act manually (delete + recreate).

- ✅ **Free-space treemap** — Recharts squarified Treemap on the
  block detail, toggled via a Band / Treemap selector next to
  the Allocation map header (selection persisted in
  sessionStorage per block). Cells are coloured by kind (violet
  child blocks, blue subnets, hashed-zinc free) and sized by
  raw address count. Pixel-thin slices on the 1-D band become
  visible squares here, surfacing fragmentation that's easy to
  miss otherwise. Uses the existing `recharts` dep — no new
  packages added.


### DNS-specific

- ✅ **TSIG key management UI** — `DNSTSIGKey` model with
  Fernet-encrypted `secret_encrypted`, `algorithm` enum
  (hmac-sha1/224/256/384/512), `name`, `purpose`, `notes`, and
  `last_rotated_at`. CRUD lives at
  `/api/v1/dns/groups/{gid}/tsig-keys` with a side `/generate-secret`
  helper that returns a fresh random base64 secret of the right
  size for the chosen algorithm, and a `/{kid}/rotate` endpoint
  that re-randomises the secret. Plaintext is returned **once** on
  the create / rotate response — list / get never expose it.
  Operator-managed rows distribute to every BIND9 agent in the
  group via the existing `tsig_keys` block in the ConfigBundle
  (alongside the legacy auto-generated agent loopback key);
  named.conf renders one `key { algorithm …; secret …; };`
  stanza per row. UI: new "TSIG Keys" tab on the DNS server
  group view, with create / edit / rotate / delete plus a
  one-shot "Copy this secret now" modal after each
  create / rotate. Operators reference keys from a zone's
  allow-update / allow-transfer fields as `key keyname.;`.
  Migration `7c299e8a5490_dns_tsig_keys`.
  **Deferred:** zone-level dropdown picker that suggests
  available keys (today operators paste the key reference into
  the existing free-text allow-update / allow-transfer fields);
  per-key audit-log of which zones reference it.

- ✅ **Conditional forwarders** — per-zone forwarding for mixed-AD
  environments. `DNSZone` carries `forwarders` (JSONB list of IPs)
  + `forward_only` (true → `forward only;`, false → `forward
  first;`). When `zone_type = "forward"` the BIND9 driver renders
  `zone "X" { type forward; forward only; forwarders { ... }; }`
  in `zone.stanza.j2` and the agent's wire-format renderer
  (no zone file written, no allow-update); the form gates the
  forwarders/policy fields on `zone_type === "forward"` and
  refuses submit when no upstreams are listed. Migration
  `a07f6c12e5d3_dns_zone_forwarders`. **Deferred:** Windows DNS
  via `Add-DnsServerConditionalForwarderZone` (the field
  shape is identical; just needs the WinRM dispatch).

- ✅ **BIND9 catalog zones (RFC 9432)** — opt-in per group via
  `DNSServerGroup.catalog_zones_enabled` + `catalog_zone_name`
  (defaults to `catalog.spatium.invalid.`). The producer is the
  group's `is_primary=True` bind9 server; every other bind9
  member joins as a consumer. Bundle assembly emits a `catalog`
  block per server: `mode=producer` ships the member zone list,
  `mode=consumer` ships the producer's IP. The agent renders
  the catalog zone file per RFC 9432 §4.1 — SOA + NS at apex,
  `version IN TXT "2"`, and one `<sha1-of-wire-name>.zones IN
  PTR <member>` per primary zone — and on consumers injects a
  single `catalog-zones { zone "<catalog>." default-masters {
  <producer-ip>; } in-memory yes; };` directive into the
  options block. The catalog block is part of the structural
  ETag so membership changes trigger a daemon reload, and SHA-1
  hashing uses the proper wire format (length-prefixed labels +
  null terminator) so consumer BINDs find the same labels.
  Frontend toggle lives in the server-group create / edit
  modal alongside the recursion checkbox. Migration
  `d8e4a73f12c5_dns_catalog_zones`. **Deferred:** version-skew
  guard (refuse the toggle on groups with a server <9.18);
  visualising consumer pull state (today operators rely on
  the existing per-server zone-serial drift pill);
  per-member properties (epoch, change-of-ownership) — most
  homelab / SMB groups won't need them.

- ✅ **Response Policy Zones (RPZ)** — DNS-level malware / phishing
  / ad blocking via BIND9 `response-policy { zone … };`. Full
  `DNSBlockList` + `DNSBlockListEntry` + `DNSBlockListException`
  model with categories / source types (manual / url /
  file_upload) / block modes (nxdomain / sinkhole / refused) /
  per-list scheduled refresh. Service-side aggregation produces
  effective entries per server-group or view; agent renders one
  RPZ master zone per blocklist with CNAME-based actions
  (`. = NXDOMAIN`, `rpz-drop. = sinkhole`, target = redirect,
  `rpz-passthru. = exception). CRUD lives at
  `app/api/v1/dns/blocklist_router.py`. BIND9-only — Windows
  DNS has no RPZ equivalent (closest is Query Resolution
  Policies which lack the wire format).

- ✅ **Curated RPZ blocklist source catalog** — ships a static
  JSON catalog at `backend/app/data/dns_blocklist_catalog.json`
  with 14 well-known public blocklists drawn from AdGuard's
  HostlistsRegistry + Pi-hole defaults + Hagezi / OISD: AdGuard
  DNS Filter, StevenBlack Unified, OISD Small/Big, Hagezi Pro
  / Pro+, 1Hosts Lite, Phishing Army Extended, URLhaus,
  DigitalSide Threat-Intel, EasyPrivacy, plus StevenBlack
  fakenews / gambling / adult add-ons. Each entry carries
  `{id, name, description, category, feed_url, feed_format,
  license, homepage, recommended}`. `GET /dns/blocklists/catalog`
  returns the snapshot (cached in-process). `POST
  /dns/blocklists/from-catalog` creates a normal `DNSBlockList`
  row with `source_type="url"` prefilled — leverages the
  existing `parse_feed` + `_refresh_blocklist_feed_async` task
  for fetch / parse / ingest with no new beat task. Frontend
  has a "Browse Catalog" button on the Blocklists tab opening a
  filterable picker (category + free-text), with already-
  subscribed entries flagged. Catalog snapshot moves in
  lockstep with releases; operators can still add custom
  sources via the existing "New Blocking List" flow.
  **Deferred:** "Refresh catalog from upstream" button that
  re-fetches `filters.json` from HostlistsRegistry between
  releases.

- ✅ **DNS query analytics aggregation** — `POST
  /api/v1/logs/dns-queries/analytics` returns top-10 qnames +
  top-10 clients + complete qtype distribution in a single
  round trip. Computed on-demand via `GROUP BY` against the
  existing `dns_query_log_entry` rows (24 h retention) — no
  new schema, no new beat task. The Logs → DNS Queries tab
  renders an Analytics strip above the raw event grid: three
  cards each showing key + count + percentage of total, with
  every row clickable to seed the corresponding filter
  (qname / client_ip / qtype). The strip refetches only when
  `(server_id, since)` changes, so per-keystroke filter edits
  on the events grid don't pay for a re-aggregation.
  Deliberately mirrors the query log's retention window —
  longer history belongs in Loki, not Postgres. **Deferred:**
  rcode breakdown (BIND9's `query` log channel doesn't emit
  rcode; would need a parallel `client error` channel + parser);
  pre-aggregated `dns_query_aggregate` table with longer
  retention (only worth it if the on-demand `GROUP BY` becomes
  slow, which it won't for typical 24h windows).

- ✅ **Zone delegation wizard** — `services/dns/delegation.py` finds
  the longest-suffix-matching parent zone in the same group
  (forward zones excluded), reads the child's apex NS records,
  and computes the NS records the parent needs to delegate the
  child plus glue (A / AAAA) for any in-bailiwick NS hostnames.
  Diffs against existing parent records so a second run is a
  no-op, surfaces warnings ("ns1 is in-bailiwick but has no
  A/AAAA in child"), and applies through the normal
  `enqueue_record_op` pipeline so the parent zone's serial bumps
  once and the agent / Windows-driver push fires uniformly.
  Endpoints: `GET /dns/groups/{gid}/zones/{zid}/delegation-preview`
  + `POST /dns/groups/{gid}/zones/{zid}/delegate-from-parent`.
  Frontend: a contextual "Delegate" button appears in the zone
  header only when an eligible parent has missing records;
  `DelegationModal` shows the exact records that would land in
  the parent before commit, with skipped/already-present rows
  in a separate section.

- ✅ **DNS template wizards** — static catalog at
  `backend/app/data/dns_zone_templates.json` with four starter
  shapes (Email zone with MX + SPF + DMARC + optional DKIM
  selector, Active Directory zone with the standard LDAP /
  Kerberos / GC SRV records + optional `_sites` entries, Web
  zone with apex A + optional AAAA + `www CNAME`, Kubernetes
  external-dns target — empty zone). `services/dns/zone_templates.py`
  loads the catalog once per process, validates required
  parameters, and substitutes `{{key}}` placeholders (plus a
  built-in `{{__zone__}}`) at materialise time; records can
  declare `skip_if_empty: ["param"]` so optional fields drop
  out cleanly. Endpoints: `GET /dns/zone-templates` +
  `POST /dns/groups/{gid}/zones/from-template`. Frontend
  `ZoneTemplateModal` with a left-column category-grouped
  template list and a right-column parameter form; submitting
  creates the zone + every materialised record in one
  transaction and navigates straight into the zone detail.
  Mounted as a "From Template" button on the ZonesTab header,
  alongside "Add Zone".

- ✅ **Multi-resolver propagation check** — `POST
  /dns/tools/propagation-check` fires the same query against
  Cloudflare / Google / Quad9 / OpenDNS in parallel using
  `dnspython`'s `AsyncResolver` (each query carries its own
  timeout so a slow resolver can't poison the others) and
  returns per-resolver `{resolver, status, rtt_ms, answers,
  error}`. UI surfaces as a Radar button on each record row in
  the records table; modal lets the operator switch record
  type and re-check. Driver-agnostic — queries are made from
  the API process, doesn't touch the BIND9 / Windows drivers.
  **Deferred:** operator-customisable resolver list (today the
  curated set is hard-coded server-side; the API accepts an
  override but the UI doesn't yet expose it).

- ✅ **DNS pool with health monitoring (GSLB-lite)** — named
  pool of A / AAAA targets where one DNS name (e.g.
  `www.example.com`) returns one record per healthy + enabled
  member. Health checks (`tcp | http | https | icmp | none`)
  run on a per-pool interval, members flip in/out of the
  rendered record set as state changes; operator can also
  manually enable/disable members like a load-balancer pool.
  Driver-agnostic: pool members render as **regular A/AAAA
  records** in the bound zone (one per healthy + enabled
  member) via the normal `enqueue_record_op` pipeline, so
  BIND9 and Windows DNS render unchanged.

  **Data model** (migration `f5b1a8c3d927_dns_pool_healthcheck`):
  - `dns_pool(id, group_id, zone_id, name, description,
    record_name, record_type, ttl, enabled, hc_type,
    hc_target_port, hc_path, hc_method, hc_verify_tls,
    hc_expected_status_codes JSONB, hc_interval_seconds,
    hc_timeout_seconds, hc_unhealthy_threshold,
    hc_healthy_threshold, next_check_at, last_checked_at,
    created_at, modified_at)`. Unique on
    `(zone_id, record_name)`.
  - `dns_pool_member(id, pool_id, address, weight, enabled,
    last_check_state, last_check_at, last_check_error,
    consecutive_failures, consecutive_successes,
    created_at, modified_at)`. Operator-set `enabled=False`
    keeps the member out of the rendered set regardless of
    health.
  - `dns_record` gains a nullable `pool_member_id` FK with
    `ON DELETE CASCADE`. When set, the records-tab UI
    renders a violet "Pool" lock badge with a click-through
    to the Pools tab and disables the row's edit/delete
    buttons; record CRUD endpoints reject writes with 422.

  **Pipeline:**
  - `app.tasks.dns_pool_healthcheck.dispatch_due_pools` is
    a Celery beat tick (30 s) that queues
    `run_pool_check(pool_id)` for every pool whose
    `next_check_at <= now`. Per-pool task uses
    `SELECT FOR UPDATE SKIP LOCKED` so concurrent
    dispatches can't double-poll. Mirrors the
    `app.tasks.snmp_poll` shape.
  - `services/dns/pool_healthcheck.run_check` fires per
    member with the chosen check type. TCP =
    `asyncio.open_connection`, HTTP/HTTPS = `httpx`,
    ICMP = `asyncio.create_subprocess_exec` calling
    `/bin/ping` (Debian's `iputils-ping` ships
    `cap_net_raw+ep` so the non-root app user can fire ICMP
    without `CAP_NET_RAW` on the container — the API
    Dockerfile installs the package explicitly).
    `hc_type=none` always reports healthy (operator opted
    out of health checks but still wants the record-set
    semantics).
  - State transitions only after
    `consecutive_failures >= unhealthy_threshold` (or
    successes for recovery), so a single flapping check
    doesn't churn DNS records.
  - On state change, `services/dns/pool_apply.apply_pool_state`
    diffs current pool DNSRecords against desired (healthy
    + enabled members) and emits create / delete via
    `enqueue_record_op` — pool records ride the normal
    record pipeline (audit log, ETag bump, agent push).
  - Pool ops carry `rrset_action` on the record payload
    (`"add"` for create, `"delete_value"` for delete). The
    bind9 driver branches on this so multiple members share
    a single (name, rtype) RRset cleanly. Without it, the
    driver's default `dns.update.replace()` semantics would
    delete every existing RR at the name+type before adding
    the new one, silently clobbering siblings — for an N-
    member pool, only the most-recently-applied member
    would survive in BIND9's running zone.
  - Rename / type-change branch in `apply_pool_state`:
    when `record_name` or `record_type` shifts on an
    existing pool, emit a `delete_value` op for the old
    (name, type, value) and an `add` op for the new one
    on the same target_serial. Operator-edit of those
    fields actually moves the live records instead of
    leaving stale entries under the old labels.

  **HTTPS TLS validation toggle** — `hc_verify_tls` controls
  whether httpx verifies the server cert. Default off because
  internal pool members commonly ship self-signed certs; on
  for public-facing targets where a bad cert should itself be
  a signal (self-signed / expired / hostname mismatch all
  fail the check fast).

  **Operator UX:**
  - **Top-level "DNS Pools" sidebar item** under DNS — lands
    on `/dns/pools` showing every pool across every zone with
    pool name (clickable to navigate to its zone), FQDN,
    group, type, check config, health summary, TTL, last
    check. Create flow asks for group → zone first
    (`PickGroupZoneModal`) before launching the pool form.
  - **Per-zone "Pools" tab** on the zone detail page mirrors
    the Records tab — same `PoolModal` reused for create /
    edit. Pool members render with status dot (`healthy`
    green, `unhealthy` red, `unknown` gray pre-first-check,
    `disabled` zinc when operator-toggled), manual enable
    toggle, and live last-check error.
  - Records tab on the zone detail surfaces pool-managed
    rows with a violet "Pool" lock badge and an Info button
    that switches to the Pools tab.

  **Tradeoff (operator-facing warning in the UI):** TTL
  races. DNS is cached client-side; a member dropping out
  doesn't take effect until TTL expires, so this is **not**
  the same as a real L4/L7 load balancer — clients can
  still hit a dead box for up to `ttl` seconds. UI defaults
  TTL to 30 s with an inline note pointing operators at the
  LB-mapping roadmap item for real load balancing.

  **Permission gate** `manage_dns_pools` seeded into the
  existing "DNS Editor" role.

  **Deferred follow-ups:**
  - **Phase 2: delegate checks to a chosen DNS agent** so
    the probe originates from the same network vantage as
    the DNS server. Matters for split-horizon setups + when
    targets are on a private network the API process can't
    reach. Today checks fire from the API/worker container.
  - **Weighted record sets.** `dns_pool_member.weight` exists
    but isn't honoured on render — every healthy member gets
    one record. BIND9 supports per-record weight via the
    `dnsrps` plugin and Route 53 has weighted routing
    natively; would need driver-specific render paths.
  - **Geo / latency routing** — picking which subset of
    healthy members to return based on client IP or RTT.
    Real GSLB territory; would need view-aware record
    rendering plus a geo-IP database. Phase 5+.
  - **Per-pool / per-member metrics in dashboard** — the
    health-check engine has all the data; pairs with the
    existing dashboard timeseries work.
  - **Pre-aggregated health-event log** — today operator
    triage of "why did this member flap?" goes through the
    normal audit log entries from `enqueue_record_op`. A
    dedicated `dns_pool_event` table would let the UI render
    a per-member uptime timeline.


- ✅ **DNS server detail modal** — clickable server rows in
  the DNS group's Servers tab open a tabbed read-only
  inspector (`frontend/src/pages/dns/ServerDetailModal.tsx`).
  Surfaces everything we know about a server without
  requiring the operator to SSH in. Tabs (last three are
  bind9-only):
  - **Overview** — name, host:port, driver, role
    (primary / secondary), agent version, last heartbeat,
    JWT expiry, structural ETag (current vs. last-acked =
    drift indicator), health status dot. Includes a live
    `rndc status` panel showing daemon uptime + zone count
    + the same fields operators normally `ssh + rndc` for.
  - **Zones** — table of zones served by this server with
    per-zone serial + drift state pulled from
    `DNSServerZoneState`. Click-through to zone detail.
  - **Sync** — pending / in-flight / failed `DNSRecordOp`
    rows for this server. Lets operators triage stuck
    record pushes without trawling the audit log.
  - **Events** — last N audit-log rows scoped to this
    server (`resource_id` match).
  - **Logs** — filterable parsed BIND9 query log
    (`dns_query_log_entry`, 24 h retention) scoped to this
    server. qname / qtype / client IP filters + auto-refresh
    every 30 s. Reuses the existing `/logs/dns-queries`
    endpoint with `server_id` filter — no new schema.
  - **Stats** — query-rate Recharts time-series
    (queries_total / NOERROR / NXDOMAIN / SERVFAIL, per-
    second rate) over selectable 1h / 6h / 24h / 7d windows
    plus three rollup cards: top qnames, top clients, qtype
    distribution. Sources: `metricsApi.dnsTimeseries` (the
    same `dns_metric_sample` table the dashboard MVP uses)
    + `/logs/dns-queries/analytics`.
  - **Config** — file picker (named.conf + every rendered
    zone file) with monospace viewer + copy-to-clipboard.
    Driven by a new agent push pipeline (see below).

  **Backing endpoints**: `GET /api/v1/dns/servers/{sid}/{zone-state,pending-ops,recent-events,rendered-config,rndc-status}`.
  The first three are thin server-scoped projections of
  existing data; the last two read the new
  `dns_server_runtime_state` table populated by agent
  pushes (one row per server, idempotent overwrite).

  **Agent push pipeline** for Config + rndc:
  - `POST /api/v1/dns/agents/admin/rendered-config`: agent
    walks `state_dir/rendered/`, ships every text file's
    relative path + content (size-capped at 5k files,
    256 KB/file, 8 MB total). Fires after every successful
    structural reload + once on bootstrap-from-cache so the
    snapshot lands the moment the agent comes up.
  - `POST /api/v1/dns/agents/admin/rndc-status`: a new
    `RndcStatusPoller` thread shells out to
    `rndc -c <state_dir>/rndc.conf status` every 60 s ±3 s
    jitter and POSTs the stdout. Migration
    `c3e9a2b71f48_dns_server_runtime_state`.

  **Agent rndc credentials** are agent-owned (not the
  alpine package's `/etc/bind/rndc.key`, which lives in a
  dir `spatium` can't traverse). The container entrypoint
  generates a fresh keypair via `rndc-confgen` at
  `/var/lib/spatium-dns-agent/rndc.{key,conf}` on first
  boot, and the bind9 driver renders an explicit
  `controls { … keys { "spatium-rndc"; }; };` block in
  named.conf referencing the same key — both ends agree by
  construction, so `rndc reconfig` + the rndc-status push
  authenticate cleanly. Without this, BIND9's auto-
  generated controls channel uses an in-memory key that
  doesn't match the on-disk file (causing "bad auth"
  failures even when the file contents are byte-identical).

  Implementation deliberately landed as a modal (not a
  per-server page) because the seven tabs all fit
  comfortably and operators stay in the group context for
  comparing sibling servers side-by-side.

  **Deferred follow-ups:**
  - **Windows DNS Stats** — `Get-DnsServerStatistics` via
    WinRM driver method. Today the Stats tab is bind9-only;
    Windows servers don't get a Stats button.
  - **`/api/v1/dns/agents/admin/stats` push from BIND9
    statistics-channels** — could surface richer per-zone
    or per-qtype counters than the dashboard MVP carries,
    but the existing `dns_query_log_entry` analytics path
    already covers operator triage so this is a future-
    polish item, not a blocker.


### DHCP-specific

- ✅ **DHCP option library / templates** — `DHCPOptionTemplate`
  row, group-scoped, holds a named bundle of option-code → value
  pairs (e.g. "VoIP phones", "PXE BIOS clients"). CRUD at
  `/api/v1/dhcp/server-groups/{gid}/option-templates` +
  `/api/v1/dhcp/option-templates/{id}` plus a server-side
  `POST /scopes/{id}/apply-option-template` for programmatic
  apply (mode=`merge` template-wins, mode=`replace` drop-existing).
  UI: new "Option Templates" tab on the DHCP server-group view
  (mirrors the Client Classes / MAC Blocks tabs) with the
  shared `DHCPOptionsEditor` for authoring, plus an
  "Apply template…" picker above the options editor on the
  scope create / edit modal that does a client-side merge into
  the editor's local state (operator still hits Save to
  persist; conflict-key list surfaces inline). Apply is a
  stamp, not a binding — later template edits do not
  propagate back to scopes that already used it. Permission
  gate `dhcp_option_template`, seeded into the existing "DHCP
  Editor" role. Migration `e7f218ac4d9b_dhcp_option_templates`.
  **Deferred:** pool / static template-apply (scope only first),
  auto-apply default template on scope create, drift report
  showing which scopes diverged from a template after apply.

- ✅ **Option-code library lookup** — static catalog at
  `backend/app/data/dhcp_option_codes.json` covering 95 RFC
  2132 + IANA `bootp-dhcp-parameters` v4 entries operators
  actually configure (each entry: `code`, `name`, `kind`,
  `description`, `rfc`). Loaded once per process via
  `services/dhcp/option_codes.py` (lru_cache); `search()`
  helper does case-insensitive name/description matching with
  numeric-prefix code lookup. `GET /api/v1/dhcp/option-codes`
  returns the catalog (with optional `q=` substring filter
  + `limit`). Frontend wires it into `DHCPOptionsEditor`'s
  custom-options row: the bare numeric code input becomes a
  combobox that searches by code or name, surfaces the
  description as a hint under the row, and auto-fills `name`
  on pick. Catalog is fetched once per session
  (`staleTime: Infinity`) and filtered client-side, so
  per-keystroke search has no server round-trip.
  **Deferred:** v6 option-code catalog (separate namespace —
  ships once v6 UI lands).

- ✅ **Device profiling — Phase 1: active layer (auto-nmap on
  DHCP lease).** Subnet-level opt-in
  (`auto_profile_on_dhcp_lease` + `auto_profile_preset` +
  `auto_profile_refresh_days` on `Subnet`). When a fresh DHCP
  lease lands the lease-event handler calls
  `services/profiling/auto_profile.maybe_enqueue_for_lease`,
  which gates on three guards before dispatch: (1) the
  subnet's master toggle, (2) a per-(IP, MAC) refresh window
  read from `IPAddress.last_profiled_at` so churning Wi-Fi
  clients don't fan out, (3) a per-subnet concurrency cap of
  4 in-flight scans counted across the existing `NmapScan`
  queue (operator-driven scans count too — better to defer
  the next auto-profile than queue ahead of a human). Pass
  through the existing `app.tasks.nmap.run_scan_task` Celery
  pipeline; the runner finaliser at
  `services/nmap/runner.py` stamps `last_profiled_at` +
  `last_profile_scan_id` on completion and mirrors
  `summary_json["os"]["name"]` into `IPAddress.device_type`
  when the passive layer hasn't already set a more specific
  value.

  Operator endpoint `POST /api/v1/ipam/addresses/{id}/profile`
  exposes "Re-profile now" — bypasses the refresh-window
  dedupe, returns 429 when the per-subnet cap is hit. UI:
  `ProfilingSettingsSection` reused in the create + edit
  Subnet modals; the IP detail modal grew a "Device profile"
  section that surfaces the most recent scan's OS guess +
  top 8 open services with a deep-link to the full nmap row.
  Migration `d4f2a86c5b91_device_profiling_phase1`.

  **Image / cluster-side fix-ups (landed alongside Phase 1).**
  Operator-driven OS detection (`nmap -O`) and SYN scans need
  raw-socket privilege, which the non-root `app` user in the api
  + worker images doesn't have by default. We grant
  `cap_net_raw,cap_net_bind_service+eip` to `/usr/bin/nmap` via
  `setcap` in the runtime layer of `backend/Dockerfile`, then
  set `NMAP_PRIVILEGED=1` as a baked-in env var so Debian's nmap
  doesn't bail on its early `getuid()==0` check (which ignores
  file caps). The K8s side adds
  `securityContext.capabilities.add: ["NET_RAW"]` to the worker
  pod in `k8s/base/worker.yaml`; the Helm chart gates the same
  on `worker.netRawCapability` (default true). On permissive
  clusters this is a no-op (NET_RAW is in containerd's default
  cap set); on restricted PSA / OpenShift-SCC / GKE Autopilot
  it's required for the cap to actually reach the process.

  **Deferred:** block / space inheritance for `auto_profile_*`
  fields (Phase 1 is subnet-only — added once operators ask for
  cascade), per-subnet `auto_profile_on_snmp_discovery` toggle
  (depends on the still-pending SNMP discovery task), IPAM list
  column for `device_type` (operator show/hide).

- ✅ **Device profiling — Phase 2: passive DHCP fingerprinting
  + fingerbank.** The companion to Phase 1's active-layer
  auto-nmap. The DHCP agent's
  `DhcpFingerprintShipper` thread runs `scapy.AsyncSniffer` with
  the BPF filter `udp and (port 67 or port 68)`, extracts
  option-55 / option-60 / option-77 / option-61 from each
  DISCOVER + REQUEST, dedupes per-MAC at 1/min, and POSTs
  batches of up to 50 to `POST /api/v1/dhcp/agents/dhcp-fingerprints`
  every 10 s. Default off — operators flip
  `DHCP_FINGERPRINT_ENABLED=1` and add `cap_add: [NET_RAW]` in
  their compose override (the shipped `docker-compose.yml`
  doesn't grant the cap unconditionally because most installs
  don't need it).

  Control-plane side: new `dhcp_fingerprint` table
  (MAC-keyed, one row per device) with the raw signature plus
  cached fingerbank result. New
  `services/profiling/fingerbank.py` (async httpx client against
  `https://api.fingerbank.org/api/v2/combinations/interrogate`,
  7-day cache window, swallows 404 / 429 / 5xx / network errors
  to avoid breaking ingestion). New
  `app.tasks.dhcp_fingerprint.lookup_fingerprint_task` Celery
  task (idempotent + retry-3) does the slow lookup off the
  ingestion request path and stamps `IPAddress.device_type` /
  `device_class` / `device_manufacturer` on every matching
  IPAM row sharing the MAC — respecting the `user_modified_at`
  lock the integration reconcilers already use.
  Operators set the fingerbank API key in **Settings → IPAM
  → Device Profiling** (Fernet-encrypted at rest, password-
  style input with a "Configured ✓ Replace… Clear" view once
  set; the encrypted bytes never round-trip through the API
  response — `SettingsResponse` exposes only a boolean
  `fingerbank_api_key_set`). No key = passive collection
  still happens, enrichment doesn't.

  IPAM `IPAddressResponse` extended with the three device_*
  columns + a new `GET /api/v1/ipam/addresses/{id}/dhcp-fingerprint`
  endpoint returning the joined `dhcp_fingerprint` row by
  MAC. Migration `e5a3f17b2d8c_device_profiling_phase2`. New
  agent dep on scapy (>=2.5) + Alpine `libpcap` package; both
  are pure-python at install time, no compilation, multi-arch
  clean. Tests at `backend/tests/profiling/` +
  `agent/dhcp/tests/test_dhcp_fingerprint.py` (parser-only,
  scapy.importorskip so the suite still passes without the
  optional dep).
  **Deferred:** v6 DHCPv6 fingerprint capture (DHCPv6 has its
  own option-numbering namespace), Wi-Fi-roaming-aware MAC
  privacy randomisation handling (the per-MAC dedupe means
  randomised MACs each look like a fresh device — by design
  in Phase 2; revisit when MAC-hashing-aware grouping is
  scoped).

- ✅ **IPAM bulk allocate + table polish** (2026-04-30, post
  device profiling). Five-item polish wave on the IPAM IP table
  surface that landed alongside the device-profiling work.
  - **Bulk allocate.** New
    `POST /ipam/subnets/{id}/bulk-allocate/{preview,commit}`
    pair stamps a contiguous IP range plus a name template in
    one transaction. Template language: `{n}` iterator,
    `{n:03d}` zero-pad, `{n:x}` / `{n:X}` hex, `{oct1}`–
    `{oct4}` IPv4 octets — anything else literal. Capped at
    1024 IPs per call; preview returns counts plus a
    first-5/last-2 sample of rendered hostnames; commit runs
    under a `FOR UPDATE` subnet lock and applies per-row
    conflict detection (already-allocated, dynamic-DHCP-pool
    membership, hostname+zone collisions including
    within-batch duplicates). `on_collision: skip | abort`
    chooses whether conflicts skip or refuse the whole batch.
    Per-row audit + one summary `bulk_allocate` audit at the
    subnet level. `BulkAllocateModal` lives first in the IPAM
    Tools dropdown (alphabetical) with a three-phase form →
    preview → committed flow and a live client-side template
    preview that mirrors the backend `_BULK_TEMPLATE_RE` regex
    so the operator sees rendered hostnames as they type.
  - **IPAM Tools dropdown.** Subnet header collapsed from 9
    buttons to 6 by folding Resize / Split / Merge / Clean
    Orphans + the new "Scan with nmap" + "Bulk allocate…" into
    one `[Tools ▾]` menu (alphabetised). Standard IPAM-style
    outside-click handler; mirrors the existing `[Sync ▾]` and
    `[Import / Export ▾]` patterns.
  - **"Seen" recency column.** New `SeenDot` component renders
    a 4-state coloured dot derived from
    `IPAddress.last_seen_at`: alive (<24h) green, stale
    (24h–7d) amber, cold (>7d) red, never grey. Source method
    (`via dhcp` / `via nmap` / `via snmp` / `via arp` / etc.)
    sits in the tooltip. Orthogonal to lifecycle status — an
    `allocated` IP can be down, a `discovered` IP can be live.
    Same dot also sits next to the status pill in
    `IPDetailModal`. Companion: `discovered` added to
    `IP_STATUSES_INTEGRATION_OWNED` so `Stamp alive hosts →
    IPAM` writes integration-owned rows the operator can
    transition by editing.
  - **Sticky thead, finally.** The IP table's `<thead>` is
    `sticky top-0 bg-card` so column headers stay visible
    while scrolling long IP lists. The earlier landing
    silently failed in Chrome because an inner
    `<div className="overflow-x-auto">` wrapper around the
    `<table>` was establishing a Y-scroll context per CSS
    spec (`overflow-x: auto` with `overflow-y: visible`
    computes to `overflow-y: auto` automatically), which
    anchored the head's sticky context to a non-scrolling
    intermediate parent. Removed the wrapper so sticky
    resolves to the outer `flex-1 overflow-auto` scroll
    container; horizontal overflow now happens at that level
    too (the table's `min-w-[640px]` still triggers x-scroll
    on narrow viewports).
  - **Shift-click range select on IP checkboxes.** `onChange`
    doesn't carry `shiftKey` so we stash the modifier state in
    `onClick` (which fires first) plus the previously-clicked
    id, then the change handler walks the IP-only ordered list
    from `tableRows` (display order — survives sort changes)
    between the two endpoints and toggles every selectable row
    to the new state. Standard Gmail-style multi-select.
  - **Free-IP gap markers.** New `kind: "gap"` row variant
    rendered as a slim half-height row with a dashed emerald
    border (`192.168.0.11 · 1 free` for single-IP gaps,
    `.11 – .13 · 3 free` for ranges). Inserted between
    non-adjacent IP rows during the same build loop that
    interleaves DHCP pool boundary markers. Suppressed when a
    pool boundary just got emitted (the boundary already shows
    the discontinuity) and when the gap falls fully inside a
    dynamic DHCP pool (those slots are owned by the DHCP
    server, not operator-allocatable). Heads-up for "you
    deleted something and might have missed the hole" — easy
    to miss otherwise.

  **Two debug fixes caught during testing:** (1) The
  module-level `_BULK_ALLOWED_STATUSES` constant collided with
  the existing subnet-bulk-edit name; Python's silent rebind
  meant the IPAddress-status validator was reading the subnet
  status set (`active / deprecated / quarantine / reserved`)
  at request time. Renamed the new one to
  `_BULK_ALLOC_ALLOWED_STATUSES`. (2) `BulkAllocateRequest.tags`
  was originally typed `list[str]` but `IPAddress.tags` is a
  JSONB **dict** (matching the rest of IPAM); 11 bulk-allocated
  rows landed with `tags=[]` and broke `GET /addresses`
  response validation for the whole subnet. Schema corrected to
  `dict[str, Any]` (frontend type `Record<string, unknown>`),
  broken rows repaired with
  `UPDATE ip_address SET tags = '{}'::jsonb WHERE jsonb_typeof(tags) = 'array'`.

- ✅ **PXE / iPXE provisioning profiles** (issue #51). New
  `pxe_profile` table — operator-curated profiles per
  architecture (`bios_x86` / `efi_x86_64` / `efi_arm64` /
  `efi_x86`). Each profile binds to a TFTP `next-server` + a
  `boot-filename` per arch, plus an optional iPXE script body.
  `DHCPScope.pxe_profile_id` SET-NULL FK; on render, the Kea
  driver emits one `client-class` per arch-match guarded by
  `option dhcp.user-class` matching the iPXE signature so
  legacy PXE clients see the BIOS bootfile and iPXE clients
  see the iPXE script. New `/dhcp/groups/{id}/pxe` admin page
  with profile CRUD and a per-scope "PXE profile" picker on
  the scope editor.

### Security & compliance

- ✅ **TOTP MFA for local users** (issue #69). New
  `user_mfa_secret` table with Fernet-encrypted TOTP shared
  secret + backup-codes JSONB list. Enrolment flow: Settings
  → Security → "Enable MFA" → scan QR (`pyotp` + `qrcode`
  libraries) → enter 6-digit code to confirm → backup codes
  shown once. Login flow gains a second step when MFA is
  enabled — JWT pre-token issued on username+password,
  exchanged for full token after TOTP code or backup code
  accepted. Backup codes are single-use and persisted hashed.
  Admin can force-disable MFA per user (audit-logged).

- ✅ **API-token scopes** (issue #74). `api_token` rows gain
  a `scopes` JSONB column listing the resource_types the
  token is allowed to touch (vs. inheriting all of the user's
  permissions). Scope set is permission-name granularity
  (`subnet:read`, `subnet:admin`, `*` for full inheritance).
  Token create modal lets the operator pick scopes via a
  chip selector grouped by resource family. Authorization
  enforces scope intersection: token can do at most what the
  scope set allows AND what the user has permission for.

- ✅ **Subnet classification tags** (issue #75).
  `subnet.pci_scope`, `subnet.hipaa_scope`,
  `subnet.internet_facing` first-class boolean columns,
  each individually indexed (partial index `WHERE col =
  true`) so the auditor's "show me every PCI subnet"
  filter hits an index without competing. List filters
  across the IPAM page + the API. Compliance dashboard at
  `/admin/compliance` shows the three buckets side-by-side.
  Feeds the compliance-change alert rule (#105) and
  conformity policy filters (#106). Inheritance through
  the IP block tree is intentionally not implemented today
  — flags live on the subnet row only; revisit when block
  / space-level classification is needed.

- ✅ **Compliance change alerts** (issue #105). New
  `compliance_change` rule type plus three disabled seed
  rules covering each classification flag. Migration
  `e3f1c92a4d68` adds three columns to `alert_rule`:
  `classification` (one of `pci_scope` / `hipaa_scope` /
  `internet_facing`), `change_scope` (one of `any_change`
  / `create` / `delete`), and `last_scanned_audit_at`
  watermark. The evaluator scans `audit_log` on the
  existing 60 s alert tick, opens one event per mutation
  against a classification-flagged subnet (or descendant
  IP / DHCP scope via the subnet FK), and auto-resolves
  after 24 h. Watermark baselines to `now()` on first run
  so historical audit rows don't retro-page operators.
  Resource resolution falls back to
  `audit_log.old_value.subnet_id` for delete actions
  where the live row no longer exists. Per-pass scan
  capped at 1000 audit rows so a long-disabled rule
  flipping on doesn't pause the evaluator. Frontend
  `AlertsPage` rule-type picker + form gain a Compliance
  optgroup with classification + change-scope fields.

- ✅ **Conformity evaluations + auditor PDF export**
  (issue #106). Companion to the reactive #105 alerts:
  declarative `ConformityPolicy` rows pin a `check_kind`
  against a target set; a beat-driven engine ticks every
  60 s and runs every enabled policy on its
  `eval_interval_hours` cadence (default 24 h). On-demand
  re-evaluation via `POST /conformity/policies/{id}/
  evaluate`. Migration `b5d8a3f12c91` creates
  `conformity_policy` (declarative check definitions) and
  `conformity_result` (append-only history, indexed twice
  on `(policy_id, evaluated_at)` and `(resource_kind,
  resource_id, evaluated_at)` so both natural drilldowns
  hit an index). Six starter `check_kind` evaluators ship
  in `services/conformity/checks.py`: `has_field`,
  `in_separate_vrf`, `no_open_ports` (warn-not-fail when
  no recent scan), `alert_rule_covers`,
  `last_seen_within`, `audit_log_immutable`. Eight
  disabled seed policies span PCI-DSS / HIPAA / SOC2.
  Built-in rows accept narrow updates only (enabled /
  interval / severity / fail_alert_rule_id / description)
  — clone first to author a variant. `pass→fail`
  transitions emit `AlertEvent` rows against the policy's
  wired alert rule (when set) so conformity drift surfaces
  in the existing alerts dashboard. Synchronous
  `reportlab`-based PDF export at `/conformity/
  export.pdf` with per-framework section, failing-row
  enumeration with diagnostic JSON, SHA-256 integrity
  hash over `(result_id, status)` tuples in the trailer.
  New `conformity` permission resource type plus two new
  built-in roles (Auditor + Compliance Editor). Frontend
  `/admin/conformity` page + Platform Insights conformity
  card.

### UX polish

- ✅ **API docs link in sidebar + header** (issue #96).
  Surface the existing Swagger UI / ReDoc at `/docs` and
  `/redoc` from the navigation itself instead of expecting
  operators to know the URL. New "API Docs" entry under Help
  in the sidebar; new external-link icon in the header next
  to the user menu.

