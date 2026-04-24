# Changelog

All notable changes to SpatiumDDI are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/); versioning uses [CalVer](https://calver.org/) (`YYYY.MM.DD-N`).

---

## 2026.04.24-1 — 2026-04-24

Proxmox VE integration release. The headline work is a read-only
PVE endpoint mirror with first-class SDN + VNet-inference support,
plus a per-guest discovery modal so "why isn't this VM showing up
in IPAM?" is a two-click answer instead of a log-trawl. Also bundles
four UX polish fixes (real source IP behind the reverse proxy,
alphabetised Integrations nav, wider Custom Fields page, search-row
amber highlight) and a shared IP-space quick-create component so a
fresh install doesn't dead-end on the integration modals.

### Added

- **Proxmox VE integration (read-only endpoint mirror).** Settings →
  Integrations → Proxmox toggle (`integration_proxmox_enabled`)
  lights up a Proxmox nav item in the sidebar. `ProxmoxNode` rows
  bind per-endpoint to one IPAM space + optional DNS group; a single
  row represents a standalone host *or* a whole cluster — the PVE
  REST API is homogeneous across cluster members, so one endpoint
  surfaces the full cluster state via `/cluster/status` and
  `/nodes`. Auth is API-token only (no password, no cookie+CSRF):
  operators issue a read-only token with `PVEAuditor`, paste the
  `user@realm!tokenid` + UUID secret into the admin page's setup
  guide, and hit Test Connection. Same 30 s beat sweep + per-node
  `sync_interval_seconds` gating + on-demand Sync Now as Kubernetes
  / Docker, plus FK cascade on endpoint delete. Mirrors:
  - **SDN VNets** (`/cluster/sdn/vnets` + per-vnet `/subnets`) →
    `Subnet` named `vnet:<vnet>`, with the declared gateway. This
    is the authoritative source when the operator runs PVE SDN:
    the backing bridge often doesn't carry a host IP (split-
    responsibility setup where a router upstream owns L3), so the
    bridge pass alone would miss every overlay VLAN. SDN wins over
    a bridge advertising the same CIDR because the VNet label
    carries the operator's intent. PVE without SDN installed
    returns 404 on the endpoint — the reconciler treats that as
    "no SDN configured" and moves on.
  - **VNet subnet inference** (opt-in via the endpoint's new
    `infer_vnet_subnets` toggle, default off) — when a VNet exists
    in `/cluster/sdn/vnets` but has no declared subnets, the
    reconciler derives a CIDR from the guests attached to that
    VNet. Priority order: exact `static_cidr` from a VM's
    `ipconfigN` (gateway from the accompanying `gw=`) or an LXC's
    inline `ip=`/`gw=`; falling back to a /24 guess around
    guest-agent runtime IPs. The /24 fallback is speculative and
    logs a warning with a `pvesh create` hint; operators running
    /23 or /25 should declare SDN subnets properly instead. Solves
    the common "PVE is L2 passthrough, gateway lives on an
    upstream router" layout where operators have 14 VNets but
    zero declared subnets and have to chase each CIDR by hand
    today. Migration `e5a72f14c890`.
  - **Bridges + VLAN interfaces with a CIDR** → `Subnet` (nested
    under enclosing operator blocks when present, otherwise under
    an auto-created RFC 1918 / CGNAT supernet). Bridges without a
    CIDR are skipped — they're the common L2-span case and would
    pollute IPAM with empty subnets.
  - **VM NICs** → `IPAddress` with `status="proxmox-vm"`, hostname
    = VM name, MAC from `netN` config. Runtime IP comes from the
    QEMU guest-agent (`/nodes/{n}/qemu/{vmid}/agent/network-get-interfaces`)
    when the agent is enabled + running; falls back to the
    `ipconfigN` static IP; otherwise the NIC contributes no row.
    Link-local and loopback IPs are stripped from the agent
    response so `fe80::…` / `127.0.0.1` don't land in IPAM.
  - **LXC NICs** → `IPAddress` with `status="proxmox-lxc"`,
    hostname = container hostname (or name fallback), MAC from
    config. Runtime IP comes from `/nodes/{n}/lxc/{vmid}/interfaces`
    when the container is running; falls back to the inline
    `ip=` value on the netN config.
  - **Bridge gateway IPs** → `reserved`-status `IPAddress` per
    subnet, matching the LAN placeholder shape used by
    operator-created subnets.
  - **Mirror toggles default ON** (`mirror_vms` + `mirror_lxc`) —
    unlike Docker containers (CI-ephemeral, noisy), PVE guests are
    typically long-lived operator inventory, so the integration is
    useful without flipping anything extra after setup.
  Minimal `httpx`-based client (no `proxmoxer` / `pveapi-py` dep).
  Admin page at `/proxmox` with a copy-paste `pveum` token-setup
  guide, Test Connection probe that distinguishes 401 / 403 / TLS
  / connect errors with human-readable messages, and Sync Now
  button per endpoint. Dashboard Integrations panel grows a
  Proxmox column when the toggle is on, with the same
  green/amber/red staleness dot + click-through to the admin page.
  Migration `d1a8f3c704e9`. 38 tests covering parse helpers, SDN
  subnet pipeline, VNet inference (both static-CIDR + runtime-IP
  paths), discovery payload shape, and cascade delete.

- **Proxmox discovery modal** — the reconciler now persists a
  `last_discovery` JSONB snapshot on every successful sync
  containing (a) category counters (VM agent reporting / not
  responding / off, LXC reporting / no IP, SDN VNets resolved /
  unresolved, addresses skipped because no subnet encloses them)
  and (b) a per-guest list with a single top-level `issue` code
  + operator-facing `hint`. New magnifier button on each endpoint
  row opens a "Discovery — {endpoint}" modal: counter pills along
  the top, filter bar (`Issues (N)` / `All (N)` / per-issue tabs),
  search box, and a filterable table with agent-state pills +
  IPs-mirrored split (`N from agent / M from static`) + inline
  hints like "install qemu-guest-agent inside the VM:
  `apt install qemu-guest-agent && systemctl enable --now
  qemu-guest-agent`". Default filter is `Issues` so operators land
  directly on what needs attention. Migration `e7b3f29a1d6c`.

- **Shared `IPSpacePicker` component with inline quick-create.**
  Proxmox / Docker / Kubernetes endpoint modals all require an IPAM
  space; operators on a fresh install had to cancel out of the
  endpoint form, create a space on the IPAM page, and come back.
  The picker wraps the select with a `+ New` button that opens a
  minimal quick-create modal (name + description + colour only —
  DNS/DHCP defaults still live on the full IPAM page). On success
  the new space auto-selects in the outer form. Lives at
  `frontend/src/components/ipam/space-picker.tsx`; wired into all
  three integration pages.

### Fixed

- **Source IP in audit log behind the reverse proxy.** The backend
  already captured `request.client.host` into `AuditLog.source_ip`
  and the audit UI already surfaces the field, but every deployment
  behind the frontend nginx container was logging the nginx IP
  instead of the real user IP. Uvicorn now runs with
  `--proxy-headers --forwarded-allow-ips=*` in `backend/Dockerfile`
  so the ASGI scope's client host is populated from the
  `X-Forwarded-For` header that nginx already sends. Wildcard is
  safe — only nginx can reach the api container on the compose /
  k8s network.

- **Settings → Integrations: stable alphabetical order.** Within
  the Integrations sidebar group, entries now sort by title so
  Docker appears before Kubernetes regardless of source-file
  order. Other groups (IPAM, DNS, DHCP) keep their declared
  ordering — alphabetisation is intentionally scoped to the
  integrations cluster where "which comes first" isn't meaningful.

- **Custom Fields settings page width.** The CF page was capped at
  the same `max-w` as the narrow single-column settings panes,
  which truncated the rightmost columns on the CF table. Bumped to
  the wide-table cap used by the roles + audit pages.

- **Search-result row highlight actually fires + stays amber +
  one-shot.** The amber highlight on subnet-detail navigation from
  global search had three bugs stacked on top of each other:
  `useStickyLocation` was calling `navigate(…, {replace: true})`
  which dropped `location.state`; `selectSubnet` was then calling
  `setSearchParams(…, {replace: true})` on the detail view which
  dropped what remained; and the CSS animation used
  `animation-fill-mode: none` so the amber faded back out instead
  of staying visible. All three patched — the highlight now fires
  the first time only, paints the row amber for ~2 s with a hold
  until the user clicks elsewhere, and clears when navigating
  between subnets.

- **Proxmox settings toggle actually persists.** The settings
  router's Pydantic response + update schemas were missing
  `integration_proxmox_enabled`, so toggling it on in the UI
  silently round-tripped to `false` on save. Added to both schemas
  — the Kubernetes and Docker toggles were already correct.

### Changed

- **Proxmox, Kubernetes, Docker endpoint modals now embed the new
  IPSpacePicker**, replacing the plain `<select>` that previously
  listed only existing spaces. Operators can create a new IPAM
  space without leaving the endpoint form.

---

## 2026.04.22-1 — 2026-04-22

Integrations-heavy release. The headline work is **Docker** and
**Kubernetes** read-only mirror integrations that pull host/cluster
network state into IPAM automatically, plus three dashboard additions
(platform-health card, integrations panel, collapsible sidebar) that
make the control plane easier to eyeball at a glance. Also bundles
the multi-target audit-forwarding rewrite, the ACME DNS-01 provider
for external certbot / lego / acme.sh clients, the DHCP MAC
blocklist, self-contained DNS+DHCP traffic charts on the dashboard,
and runtime-version + GitHub-release-check wiring.

### Added

- **Dashboard: Platform Health card.** Five-up grid showing the live
  status of every control-plane component SpatiumDDI ships — API,
  PostgreSQL, Redis, Celery workers, Celery beat. Per-component dot
  (green / amber / red) + one-line detail ("SELECT 1 in 1 ms",
  "1 alive", "last tick 5s ago"). Worker list surfaces on hover.
  Backed by a new `/health/platform` endpoint that probes each
  piece — worker liveness via `celery_app.control.inspect().ping()`
  in a threadpool with a 3 s outer timeout (so a dead broker can't
  hang the call), beat liveness via a new
  `app.tasks.heartbeat.beat_tick` task that writes
  `spatium:beat:heartbeat` to Redis every 30 s with a 5-minute TTL
  and folds the key's age into ok (≤90 s) / warn (>90 s) / error
  (missing). Endpoint always returns 200 with per-component status
  so partial failures surface without the UI losing the whole card.
  Runtime-agnostic — same output on Docker Compose, Kubernetes, or
  bare metal.

- **Dashboard: Integrations panel.** Appears when Kubernetes or
  Docker integrations are enabled. Two columns (one per enabled
  integration type) with one row per registered cluster / host:
  status dot that folds `last_sync_error` + staleness into a single
  green (synced recently) / amber (stalled > 3× interval) / red
  (sync error) / gray (disabled or never synced) signal, name,
  endpoint, node / container count, humanized last-synced age.
  Section header click-throughs to `/kubernetes` / `/docker` full
  pages. Panel auto-hides when both integration toggles are off —
  default deployments stay clean.

- **Sidebar: collapsible sections + Core header.** All three
  sections now carry an uppercase chevron-header — **Core** (was
  unlabeled), **Integrations**, **Admin** — and each toggles
  open/closed independently. Per-section expanded state persists to
  `sessionStorage` via the existing `useSessionState` helper so it
  survives in-session navigation but not tab close. Collapsed-
  sidebar mode still uses separator lines (no labels to hide at
  that width). Groundwork for adding more sections without the
  sidebar becoming a wall of links.

- **Docker integration (read-only host mirror).** Settings →
  Integrations → Docker toggle (`integration_docker_enabled`)
  lights up a Docker nav item in the sidebar. `DockerHost` rows
  bind per-host to one IPAM space + optional DNS group; bearer-
  equivalent secret is the Fernet-encrypted TLS client key. Two
  transports: **Unix socket** (requires mounting
  `/var/run/docker.sock` into the api + worker containers) and
  **TCP+TLS** (CA bundle + client cert + client key). Same
  reconciler pattern as Kubernetes Phase 1b: 30 s beat sweep,
  per-host `sync_interval_seconds` gating, on-demand
  `sync_host_now` for the UI's Sync Now button, FK cascade on
  host delete. Mirrors **every Docker network** into IPAM
  (smart parent-block detection when an enclosing operator block
  exists), with network gateway stamped as a `reserved` IP row so
  the subnet looks like a normal LAN. `bridge` / `host` / `none`
  / `docker_gwbridge` / `ingress` skipped by default; Swarm
  overlay networks always skipped. **Container mirroring is
  opt-in** per host (`mirror_containers`, default off) — matches
  the `mirror_pods` shape on the k8s side. When on, each
  container's IP lands as `status="docker-container"` with
  hostname = either `<compose_project>.<compose_service>` (via
  the `com.docker.compose.*` labels) or the container name.
  Stopped containers skipped unless
  `include_stopped_containers=true`. Minimal `httpx`-based
  client (no `docker` SDK dep). Migration `c9e2b0d3a5f7`.
  Frontend admin page at `/docker` with setup guide (copy-paste
  TCP+TLS `daemon.json` or unix-socket compose mount snippet),
  Test Connection probe, and Sync Now button. SSH transport
  (`ssh://user@host`) deferred.

- **Kubernetes integration — Phase 1a scaffolding + 1b read-only
  reconciler.** Settings → Integrations is a new settings section;
  per-integration toggle `integration_kubernetes_enabled` drives
  whether the Kubernetes nav item appears in the sidebar. Per-
  cluster config lives on `KubernetesCluster` rows bound to exactly
  one IPAM Space (required) + optionally one DNS server group.
  Bearer token Fernet-encrypted at rest; CA bundle optional
  (system CA store used when empty, for cloud-managed clusters).
  Admin page at `/kubernetes` with setup guide (YAML +
  ServiceAccount / ClusterRole / ClusterRoleBinding / Secret +
  `kubectl` extract commands) shown in the Add modal, Test
  Connection probe that calls `/version` + `/api/v1/nodes` on the
  apiserver and distinguishes 401 / 403 / TLS / network errors
  with human-readable messages. Migrations `f8c3d104e27a`
  (`kubernetes_cluster` table + toggle) and `a917b4c9e251`
  (`kubernetes_cluster_id` FK on `ip_address`, `ip_block`,
  `dns_record` with `ON DELETE CASCADE`). Every 30 s Celery beat
  tick sweeps every enabled cluster whose
  `sync_interval_seconds` (min 30) has elapsed and runs the
  reconciler: pod CIDR + service CIDR become `IPBlock`s under the
  bound space; node `InternalIP`s become `IPAddress` rows with
  `status="kubernetes-node"`; `Service`s of type `LoadBalancer`
  with a populated VIP become `IPAddress` with
  `status="kubernetes-lb"` and hostname `<svc>.<ns>`; `Ingress`
  hostnames become A records (or CNAME if the LB surfaces a
  hostname rather than an IP) in the longest-suffix-matching zone
  in the bound DNS group. Create / update / **delete** semantics
  — removed cluster objects immediately drop their mirror rows
  (not orphaned). Deleting the cluster itself cascades every
  mirror via the FK. "Sync Now" button per cluster fires the same
  reconciler on demand, bypassing the interval. Covered end-to-end
  by `backend/tests/test_kubernetes_reconcile.py` with the k8s
  client stubbed — 9 tests across block diff, node mapping, LB
  VIP mapping, Ingress → A, Ingress → CNAME, zone-miss skipping,
  subnet-miss skipping, and cascade delete.
  **Deferred follow-ups**:
  - Pod IP mirroring (deliberately out of scope — pods churn;
    CIDR-as-IPBlock is the value).
  - external-dns webhook provider protocol (Phase 2 — separate
    feature).
  - Service annotation-driven DNS (`external-dns.alpha.kubernetes.io/hostname`
    on non-Ingress Services) — trivially additive on top of the
    existing Ingress path once a user asks for it.
  - ClusterRoleBinding check in Test Connection (detects the common
    "applied the SA but forgot the binding" case by probing
    `/apis/networking.k8s.io/v1/ingresses` explicitly).

- **Runtime version reporting + GitHub release check.** The sidebar
  footer now shows the actual running version instead of the
  hardcoded `0.1.0` that came from `package.json`. Mechanism: the
  release workflow tags Docker images with the git tag, and
  operators pick a tag via `SPATIUMDDI_VERSION` in their `.env`.
  That value flows through `docker-compose.yml` to a `VERSION` env
  var on the api/worker/beat containers, and through a
  `VITE_APP_VERSION` build arg on the frontend Dockerfile (now
  honored) as a build-time fallback. New public endpoint `GET
  /api/v1/version` returns `{version, latest_version,
  update_available, latest_release_url, latest_checked_at,
  release_check_enabled, latest_check_error}`. A daily Celery beat
  task `app.tasks.update_check.check_github_release` queries
  `api.github.com/repos/{github_repo}/releases/latest`
  unauthenticated (60/hour rate limit is plenty for a daily tick),
  compares the tag against the running version (CalVer
  lexicographic compare; `dev` is treated as outdated vs any real
  tag), and stores `latest_version`, `update_available`,
  `latest_release_url`, `latest_checked_at`, and `latest_check_error`
  on `PlatformSettings`. Gated by the existing
  `github_release_check_enabled` flag — air-gapped deployments
  flip it off and the task no-ops. When an update is available,
  the sidebar shows a small "update" pill linking directly to the
  release notes page. Migration `e5b21a8f0d94`.

- **DHCP MAC blocklist at the server-group level.** Block a MAC
  address from getting a lease anywhere in the group. Covers both
  Kea (rendered into Kea's reserved `DROP` client class via the
  ConfigBundle → agent path, packets are silently dropped before
  allocation) and Windows DHCP (`Add-DhcpServerv4Filter -List Deny`
  pushed over WinRM by a 60 s Celery beat task that diffs desired
  against the server's current deny-list). Group-global: one entry
  blocks the MAC on every scope served by every member of the
  group — no per-subnet pinning. Per-entry fields: `mac_address`,
  `reason` (`rogue` / `lost_stolen` / `quarantine` / `policy` /
  `other`), `description`, `enabled` (soft-disable without losing
  history), `expires_at` (nullable; expired rows stay in the DB
  but are stripped from the rendered config), `created_at` /
  `created_by_user_id` / `updated_by_user_id` / `last_match_at` /
  `match_count`. List reads enrich each row with the OUI vendor
  name (via the existing `oui_vendor` table) and an IPAM cross-
  reference (any `IPAddress` rows currently tied to the blocked
  MAC, with IP + subnet + hostname). Admin UI under the DHCP server
  → "MAC Blocks" tab with filter-as-you-type over MAC / vendor /
  IP / hostname, reason pills, status pill, IPAM links, expiry
  formatting, and a modal for add / edit. MACs accept the common
  operator formats (colon, dash, dotted, bare hex) and canonicalize
  to colon-lowercase server-side. CRUD endpoints: `GET/POST
  /api/v1/dhcp/server-groups/{gid}/mac-blocks`, `PUT/DELETE
  /api/v1/dhcp/mac-blocks/{id}`. Permission gate on
  `dhcp_mac_block` (builtin "DHCP Editor" role gets it
  automatically). Migration `d4a18b20e3c7_dhcp_mac_blocks`.
  Covered by `backend/tests/test_dhcp_mac_blocks.py` (model,
  bundle-filter on enabled + expiry, API round-trip, validation
  rejections) and four new Kea-renderer cases in
  `agent/dhcp/tests/test_render_kea.py` (DROP emission, empty
  list skip, invalid-entry resilience, user-defined `DROP` not
  clobbered). **Deferred follow-ups:** bulk import from CSV,
  per-scope restriction (Kea supports class/pool pinning; Windows
  doesn't), `last_match_at` wiring from Kea lease-event hooks +
  Windows DHCP FilterNotifications event log.

- **Multi-target audit forwarding + pluggable wire formats.** The
  single-syslog + single-webhook slot on `PlatformSettings` is
  replaced by a dedicated `audit_forward_target` table: one row per
  destination with independent transport, format, and filter. Five
  syslog output formats — `rfc5424_json` (current default),
  `rfc5424_cef` (ArcSight), `rfc5424_leef` (QRadar), `rfc3164`
  (legacy BSD), and `json_lines` (bare NDJSON for Logstash / Vector).
  Three transports — UDP, TCP, **new TLS** (with optional per-target
  PEM CA bundle). Per-target filters `min_severity` and
  `resource_types` cut noisy events before they leave the box.
  Admin UI under Settings → Audit Event Forwarding gains an
  add / edit / delete table plus a **Test** button that sends a
  synthetic event to the target so operators get instant feedback on
  a new collector. Migration seeds one row per previously configured
  flat target so existing deployments keep forwarding through the
  upgrade; the flat columns remain as a fallback for one release.
  Admin-only endpoints: `GET /POST /PUT /DELETE
  /api/v1/settings/audit-forward-targets{/id}` and `POST
  /api/v1/settings/audit-forward-targets/{id}/test`. Migration
  `c7e2f5a91d48_audit_forward_targets`. Covered by 17 new tests in
  `backend/tests/test_audit_forward.py`.

- **Built-in DNS query rate + DHCP traffic charts on the dashboard.**
  Two new time-series cards under the activity row. BIND9 agents
  emit per-60s-bucket deltas of `QUERY` / `QryAuthAns` /
  `QryNoauthAns` / `QryNXDOMAIN` / `QrySERVFAIL` / `QryRecursion`
  pulled from `statistics-channels` XMLv3 on `127.0.0.1:8053`
  (injected into the rendered `named.conf`). Kea agents emit
  deltas of `pkt4-{discover,offer,request,ack,nak,decline,release,
  inform}-{received,sent}` pulled from `statistic-get-all` over
  the existing control socket. Counter resets from a daemon
  restart are detected agent-side and drop one bucket rather than
  emitting a spurious spike. Control plane stores samples in two
  small tables — `dns_metric_sample` + `dhcp_metric_sample` —
  keyed on `(server_id, bucket_at)`. Dashboard reads via
  `GET /api/v1/metrics/{dns,dhcp}/timeseries?window={1h|6h|24h|7d}`
  with server-side `date_bin` downsampling (60 s buckets for ≤24 h,
  5 min for 7 d). Nightly `prune_metric_samples` Celery task
  enforces retention (default 7 days). New `recharts` dep on the
  frontend. Windows DNS/DHCP drivers don't report yet — the card
  shows an empty state explaining where data comes from.
  Migration `bd4f2a91c7e3_metric_samples`.

- **ACME DNS-01 provider — external-client flow.** New
  `/api/v1/acme/` surface implementing the
  [acme-dns](https://github.com/joohoi/acme-dns) protocol so
  certbot / lego / acme.sh can prove control of a FQDN hosted in
  (or CNAME-delegated to) a SpatiumDDI-managed zone and issue
  public certs (wildcards included). Five endpoints: `POST
  /register` (admin, returns plaintext creds once), `POST /update`
  (acme-dns auth via `X-Api-User` / `X-Api-Key`, writes TXT with
  60 s TTL and blocks up to 30 s until the primary DNS server
  acks), `DELETE /update` (idempotent cleanup), `GET /accounts` +
  `DELETE /accounts/{id}` (admin list / revoke). Keeps the two
  most-recent TXT values per subdomain so wildcard + base cert
  issuance works. bcrypt-hashed passwords at rest; optional
  `allowed_source_cidrs` per-account allowlist. Delegation pattern
  documented in [`docs/features/ACME.md`](docs/features/ACME.md)
  with worked certbot / lego / acme.sh examples. New permission
  resource type `acme_account`. Migration
  `ac3e1f0d8b42_acme_account`. Covered by 24 new tests in
  `backend/tests/test_acme.py`.

- **RFC 1918 + CGNAT supernet auto-creation on integration
  reconcile.** When the Kubernetes or Docker reconciler detects
  that a mirrored network is contained in `10.0.0.0/8`,
  `172.16.0.0/12`, `192.168.0.0/16`, or `100.64.0.0/10`, and no
  enclosing parent block exists in the target IPAM space, the
  reconciler now auto-creates the canonical private supernet as
  an unowned top-level block (`Private 10.0.0.0/8`, etc). The
  mirrored subnet then nests under it via the existing smart
  parent-block detection. Unowned = integration-FK null, so the
  supernet survives removal of the integration that caused it
  and can be shared across Docker + Kubernetes + hand-made
  allocations. Applies to IPv4 only; matches `ipaddress.IPv4Network.subnet_of`
  semantics.

- **Block creation accepts strict supernets of existing siblings
  and auto-reparents.** `_assert_no_block_overlap` previously
  rejected any new top-level block that enclosed a sibling with
  409. The rule now admits one specific exception: if the new
  block is a strict supernet of one or more siblings (e.g.
  operator creates `172.16.0.0/12` when `172.20.0.0/16` and
  `172.21.0.0/16` already exist at top level), the new block is
  inserted and the existing siblings are reparented under it in
  the same transaction. Duplicates (same CIDR), strict subsets
  (new block contained in a sibling), and partial overlaps are
  still rejected. Matching behaviour lives in `create_block` +
  `update_block` (the reparent path). 4 new tests in
  `backend/tests/test_ipam_block_overlap.py`.

### Fixed

- **Frontend Docker image fails to build for `linux/arm64`.** The
  frontend Dockerfile's builder stage ran emulated under QEMU for
  each `--platform` target, which on `linux/arm64-musl` triggered
  the npm optional-dependency bug
  ([npm/cli#4828](https://github.com/npm/cli/issues/4828)): the
  committed `package-lock.json` only resolves concrete `node_modules/`
  entries for `@rollup/rollup-linux-x64-{gnu,musl}`, so `npm install`
  on emulated arm64 couldn't find `@rollup/rollup-linux-arm64-musl`
  and rollup bailed with "Cannot find module". Fixed by pinning the
  builder stage to `--platform=$BUILDPLATFORM` (usually amd64) — the
  output `dist/` is static JS/CSS/HTML and platform-independent, so
  the nginx final stage still ships both amd64 and arm64. Surfaced
  during the initial `2026.04.22-1` release build; caught + fixed
  before tag retargeted.

- **Copy-to-clipboard fails on insecure origins (HTTP LAN deploys).**
  `navigator.clipboard.writeText` is only exposed on secure contexts
  (HTTPS or `localhost`), so the API-token reveal modal's Copy
  button silently no-op'd on plain-HTTP LAN deployments served by
  IP / mDNS / Tailscale hostnames. Shared helper now falls back to
  a detached `<textarea>` + `document.execCommand("copy")` when the
  clipboard API is unavailable, so Copy works in every deployment
  topology. Tests cover both the secure-context happy path and the
  fallback path.

- **CodeQL alert #15 (`py/incomplete-url-substring-sanitization`).**
  The ACME account-registration test asserted
  `body["fulldomain"].endswith("acme.example.com")`, which
  CodeQL's URL-host rule flags as an unsafe substring check even
  in test code. Reworked to an exact-match comparison against
  `f"{subdomain}.acme.example.com"` — silences the static analyzer
  and tightens the test (now fails on any unexpected affix between
  the subdomain and zone name, not just on a wrong zone).

- **`audit_forward` crash in Celery workers.** The `after_commit`
  listener fired `loop.create_task(_dispatch(...))` which called
  `_load_targets()` / `_load_forward_config()` against the global
  `AsyncSessionLocal` from `app.db`. Celery wraps each async task
  in its own `asyncio.run()` loop, so the global engine's pool
  held asyncpg connections bound to a previous, now-defunct
  loop — reusing one raised `asyncpg.exceptions.InterfaceError:
  cannot perform operation: another operation is in progress`.
  Fixed by routing those two loaders through a new
  `_ephemeral_session()` helper that creates a short-lived engine
  with `NullPool` (no loop-bound pool state to leak), yields a
  session, and disposes on exit. Surfaced as a loud stack trace
  every time the Docker or Kubernetes reconciler wrote an audit
  row — now silent. (Separate issue not addressed here: forwards
  fired from Celery-committed audit rows may still be cancelled
  mid-HTTP when `asyncio.run` closes the loop; crash-free, but
  delivery is best-effort from reconcile-task context.)

---

## 2026.04.21-2 — 2026-04-21

Large consolidation release covering three waves of Kea HA work:

1. **End-to-end HA shake-out** — four distinct agent bugs surfaced the
   first time we actually brought two Kea peers up against `2026.04.21-1`
   (peer URL hostname resolution, port collision, `ha-status-get`
   removal, bootstrap reload), plus UI polish on the failover UX.
2. **DHCP data model refactor to group-centric** — scopes, pools,
   statics, and client classes now belong to `DHCPServerGroup` not
   `DHCPServer`. HA is implicit when a group has ≥ 2 Kea members. The
   standalone `DHCPFailoverChannel` is dropped. **Breaking** — see the
   Migration section below.
3. **Agent rendering + resiliency** — the Kea config renderer's
   long-standing wire-shape mismatch is fixed (no Kea install before
   this release was actually serving the control-plane-defined scope
   — every reload rendered `subnet4: []`), HA peer-IP drift
   self-heals via a new `PeerResolveWatcher` thread, and the Kea
   daemons run under a supervisor that handles stale PID files, bind
   races, and signal forwarding.

Also ships standalone agent-only compose files for distributed
deployments, and a refresh button on the DHCP server group detail
view.

### Breaking

- **DHCP data model is group-centric.** API surface everything scoped
  at `/dhcp/servers/{id}/...` for config objects moves to
  `/dhcp/server-groups/{id}/...`:
  - `GET/POST /dhcp/servers/{id}/client-classes` → `/dhcp/server-groups/{id}/client-classes`
  - The `/dhcp/failover-channels` CRUD router is **deleted**; HA
    fields live on `PATCH /dhcp/server-groups/{id}`.
  - `/dhcp/subnets/{id}/dhcp-scopes` still works as the IPAM-side
    pivot (same URL), but its request body takes `group_id` instead
    of `server_id`. A new alias `/dhcp/server-groups/{id}/scopes`
    exists for group-first lookups.
  - `DHCPScope.server_id` field dropped from the response; use
    `group_id` instead.
  - `DHCPClientClass.server_id` field dropped; use `group_id`.

- **UI navigation.** The "DHCP Failover" sidebar entry under Admin is
  removed. Old `/admin/failover-channels` URLs redirect to `/dhcp`.
  Configure HA on the DHCP server group from its edit modal.

Why: under the old model, scopes were pinned to one server. Pairing
two servers in a failover channel configured the HA hook but did
**not** mirror scope config — operators had to create every scope
twice and keep them in sync manually. Under the new model, you
configure scopes once on the group and every member renders the same
Kea `subnet4`. This matches how every mature DDI product (Infoblox,
BlueCat, Microsoft DHCP server groups) treats the server-group
abstraction.

### Migration

Alembic migration `e4b9f07d25a1_dhcp_group_centric_refactor` performs
the backfill automatically:

1. Every `DHCPServer` without a `server_group_id` gets a per-server
   singleton group named after the server (existing groupless servers
   keep working — they just become single-member groups).
2. `dhcp_scope.group_id` and `dhcp_client_class.group_id` are
   populated from the owning server's group, de-duplicated when
   multiple servers in the same group had overlapping rows (oldest
   wins by `created_at`).
3. Each existing `DHCPFailoverChannel` collapses into the primary
   server's group: mode + HA tuning copy onto the group, each peer's
   URL copies onto the matching `DHCPServer.ha_peer_url`. If the two
   peers were in different groups, the secondary moves into the
   primary's group (it had to be there anyway for HA to work).
4. The `dhcp_failover_channel` table is dropped.

**Downgrade is shape-only**, not semantic. Scopes / classes on multi-
server groups collapse onto whichever server is first by `created_at`.
Not round-trip safe for production rollback — exists for local dev
reset.

### Added

**DHCP — group-centric**

- **Group-centric DHCP page.** The DHCP tab remains the single
  navigation point; no sidebar item moves. HA tuning (heartbeat / max-
  response / max-ack / max-unacked / auto-failover) lives in the
  server group edit modal, shown only when the group's mode is
  `hot-standby` or `load-balancing`. Server edit modal grows a
  "HA Peer URL" field for Kea servers.
- **`DHCPServerGroup.kea_member_count`** — computed on the
  `/dhcp/server-groups` response so the UI can decide whether to
  render the HA panel without a second round-trip. ≥ 2 means the
  group renders `libdhcp_ha.so` on every peer.
- **`DHCPServerGroup.servers[]`** — member servers are rolled up into
  the group response (id, name, driver, host, status, `ha_state`,
  `ha_peer_url`, `agent_approved`) so the dashboard's HA panel can
  paint one network request instead of N.
- **`GET /dhcp/server-groups/{id}/scopes`** — group-first scope list
  endpoint.
- **Refresh button on the DHCP server group detail view.** Invalidates
  `dhcp-servers` + `dhcp-groups` queries so HA state + mode pill
  repaint on demand after switching HA mode (hot-standby ↔
  load-balancing) instead of waiting on the 30 s auto-refetch.
  Toolbar order aligned with IPAM / DNS:
  `[Refresh] [Edit] [Delete] [+ Add Server]`.
- **HA state pill in group detail server list** — inline per-server
  `ha_state` badge on the group detail view, so you can see live HA
  state for each peer without drilling into the server detail.

**DHCP — HA UX (shake-out)**

- **Refresh button on the failover UI** — invalidates both the
  channels list and the DHCP servers query so per-peer HA state
  updates on demand. Uses the shared `HeaderButton` primitive.
- **HA panel on the dashboard DHCP column.** When any group has ≥ 2
  Kea members, the DHCP column adds a `FAILOVER (N)` section under
  the server list. Each row shows group name + mode + two colored
  state dots (per peer) with the live `ha_state` strings. Green for
  `normal` / `hot-standby` / `load-balancing` / `ready`; amber for
  `waiting` / `syncing` / `communications-interrupted`; red for
  `partner-down` / `terminated`; muted for unknown.
- **Peer URL help text overhaul.** Renamed fields to "Primary server
  URL" / "Secondary server URL", added a highlighted info box
  explaining each URL is that peer's own HA-hook endpoint reachable
  from the other peer, placeholder values now show the compose
  hostnames (`http://dhcp-kea:8000/` etc.).

**Kea agent resiliency**

- **`PeerResolveWatcher`** (new 30 s background thread) re-resolves
  HA peer hostnames and triggers a render + reload if any peer's IP
  has drifted. Closes the long-standing "peer URL goes stale after a
  container restart / pod reschedule" failure mode for good.
- **Daemon supervisor** — both `kea-dhcp4` and `kea-ctrl-agent` now
  run under a retry loop with a 5-in-30 s crash-loop guard. SIGTERM
  traps forward to the live daemon AND flip a stopping flag so the
  loop doesn't retry during container shutdown.
- **Stable sha256-derived `subnet-id`** — Kea keys leases off
  `subnet-id`; using a deterministic hash of the CIDR (rather than an
  enumeration counter) guarantees the same CIDR always gets the same
  id across renders, so Kea's lease database doesn't orphan leases
  on config reload.

**Deployment**

- **`docker-compose.agent-dhcp.yml`** — standalone compose file for a
  Kea agent (or HA pair via `--profile dhcp-ha`) on a host *without*
  the control plane. Requires `SPATIUM_API_URL` + `SPATIUM_AGENT_KEY`
  — enforced at compose-config time so misconfiguration fails before
  pull.
- **`docker-compose.agent-dns.yml`** — companion file for a standalone
  BIND9 agent.
- Both files use bridge networking with lab-friendly host-side port
  remaps (5353 for DNS, 6767 for DHCP) so they don't collide with
  systemd-resolved or a host dhclient. Documented host-networking
  swap for real serving.

### Changed

- **Dashboard HA panel** now queries `/dhcp/server-groups` instead of
  the removed `/dhcp/failover-channels`. Each HA pair renders one
  row with a state dot + name per Kea member. The panel only appears
  when at least one group has ≥ 2 Kea members.
- **`CreateScopeModal`** picks the target group (no longer a server).
  The "inherited DHCP group from IPAM block / space" UX still works
  — we default the group picker rather than filtering a server
  picker.
- **Windows DHCP write-through** fans out across every Windows member
  of the scope's group. Editing a scope in a Windows-only group with
  two servers now pushes the cmdlet to both.
- **`pull_leases`** keys scope lookups by `server.server_group_id`.
  Two Windows DHCP servers in the same group pulling the same subnet
  converge on one scope row (replace-all on pool/static state is
  unchanged).
- **Subnet resize** walks every group hosting a scope for the subnet
  and refreshes every Kea member of each group, not just the
  originating server.
- **Kea agent bootstrap reload is now retried for 15 s** on agent
  restart, covering the Kea-startup race (agent and Kea launch
  together from the entrypoint; the control socket may not exist
  for a second or two). Without this retry, cached bundles never
  got applied on restart and Kea stayed on the baked image config.
- **Group detail "never synced" now driver-aware.** Kea members
  report liveness via `agent_last_seen` (heartbeat-driven); Windows
  DHCP reports via `last_sync_at` (lease-pull-driven). The label
  branches on driver so Kea members stop showing a perpetual
  "never synced" regardless of actually-alive heartbeat state.
- **Compose: DHCP HA is a single-flag opt-in.** `dhcp-ha` profile on
  both `docker-compose.yml` and `docker-compose.dev.yml` adds
  `dhcp-kea-2` as a second Kea agent. Enable with
  `docker compose --profile dhcp --profile dhcp-ha up -d`.
- **`.env.example`** carries an inline `openssl rand -hex 32` hint
  for `SECRET_KEY`; Fernet key generation command normalized from
  `python` → `python3` to match the host binary.
- **Compose dev overlay service naming** — `dhcp-1` / `dhcp-2` →
  `dhcp-kea` / `dhcp-kea-2`, per-service volumes split out so the
  second peer isn't contending for the primary's memfile lease CSV.

### Fixed

**Kea HA core (first shake-out)**

- **Peer URL hostname resolution.** Kea's HA hook parses peer URLs
  with Boost asio and only accepts IP literals. `_resolve_peer_url`
  in the agent's renderer now resolves hostnames (via Docker DNS /
  k8s DNS) before the config is emitted. IPv4/v6 literals pass
  through unchanged.
- **Kea port collision between HA hook and `kea-ctrl-agent`.** Kea
  2.6's HA hook spins up its own `CmdHttpListener` bound to the
  `this-server` peer URL. Collocating on `:8000` raced with
  `kea-ctrl-agent`; second binder died with `Address in use`.
  `kea-ctrl-agent` moved to `:8544`, HA hook owns `:8000`.
- **`ha-status-get` command removed in Kea 2.6.** Agent's
  `HAStatusPoller` was calling the standalone command; Kea 2.6 folded
  HA status into `status-get` under
  `arguments.high-availability[0].ha-servers.local.state`.
  `_extract_state` now accepts both new and pre-2.6 shapes.
- **Bootstrap-from-cache never reloaded Kea.** Cached bundle was
  re-rendered to `/etc/kea/kea-dhcp4.conf` but Kea was not told to
  reload, so it stayed on the Dockerfile-baked config; next long-poll
  returned `304 Not Modified` and no reload followed. Bootstrap now
  issues `config-reload` with a 15 s retry window.
- **PATCH `/dhcp/failover-channels/{id}` 500 on UUID fields.**
  Audit-log payload switched to `model_dump(mode="json", ...)` so
  raw `uuid.UUID` values don't crash JSONB serialization.
- **Missing `kea-hook-ha` package in the Kea image.** Dockerfile
  installed `kea-hook-lease-cmds` but not `kea-hook-ha`; config
  reference to `libdhcp_ha.so` fataled on every reload. Added
  `kea-hook-ha` to the apk install line.

**Kea agent rendering + runtime (biggest latent bug)**

- **Kea agent: scopes + pools now actually render.** The agent's
  `render_kea.py` has been reading `bundle["subnets"]` since the
  first Kea commit (Apr 15), but the control plane has always
  shipped `bundle["scopes"]` with `ScopeDef` fields (`subnet_cidr`,
  `pools` with `{start_ip, end_ip, pool_type}`, `statics` with
  `{ip_address, mac_address}`). The shape mismatch meant every Kea
  config reload emitted `subnet4: []` — no scopes, no pools, no
  leases served. Only surfaced now because HA + a real scope in the
  same bundle hit the empty-subnet path on a fresh install.
  Renderer now consumes the canonical wire shape natively;
  excluded/reserved pools filtered out (those are IPAM-only
  bookkeeping, not Kea pools).
- **Kea HA: peer IP drift now self-heals** (see `PeerResolveWatcher`
  under Added).
- **Kea agent: stale PID files no longer block restart.** Kea only
  removes its own PID file on graceful shutdown — SIGKILL and hard
  crashes leave it behind, and `createPIDFile` refuses to start with
  `DHCP4_ALREADY_RUNNING` / `DCTL_ALREADY_RUNNING`. Entrypoint now
  scrubs `/run/kea/*.pid` both at container start and before each
  supervise-loop retry, so `docker compose restart` (and any signal
  storm) brings Kea back cleanly.
- **Kea agent: daemons now supervised with crash-retry + signal
  forwarding** (see Daemon supervisor under Added).

**DHCP polish follow-ups (group-centric refactor close-out)**

- **`MissingGreenlet` on `GET /dhcp/server-groups`** — `servers`
  relationship on `DHCPServerGroup` eager-loaded (`lazy="selectin"`)
  so serialization of the rollup doesn't lazy-load after the
  session's greenlet context ends.
- **Cache invalidation on scope create from IPAM.** The IPAM → DHCP
  scope creation path invalidates the group-level scope query
  (`dhcp-scopes-group`) + the pool list, so the newly-created
  scope shows up in the DHCP tab without a hard page reload.
- **Group detail "never synced" label fixed for Kea members.** See
  Changed → driver-aware label.

### Docs

- `docs/features/DHCP.md` — rewritten data-model section + HA
  paragraph: scopes live on groups; HA is a property of a group
  with two Kea members.
- `docs/drivers/DHCP_DRIVERS.md` — HA coordination subsection
  covers the group-centric model, port split (8000 HA /
  8544 ctrl-agent), peer URL resolution, `status-get` shape,
  bootstrap reload retry, PeerResolveWatcher, supervised daemons.
- `docs/deployment/DOCKER.md` — new §10 "Distributed Agent
  Deployments" covering the two standalone agent compose files,
  two-VM HA pair, host vs bridge networking.
- `CLAUDE.md` — Kea HA roadmap entry trimmed (scope mirroring,
  peer IP re-resolve, daemon supervisor are all shipped now);
  remaining deferred items (Kea version skew guard, DDNS
  double-write under HA, state-transition UI actions, peer
  compatibility validation, HA e2e test) kept for future work.

### Tests

- `agent/dhcp/tests/test_render_kea.py` — 4 new tests pin the wire
  shape (dynamic-only pools, reservation mapping from `statics`,
  `match_expression` → `test` renaming, stable subnet-id invariance).
- `agent/dhcp/tests/test_peer_resolve.py` — 7 new tests cover the
  watcher: initial seed doesn't fire reload, IP change fires one
  reload, unchanged IP is a no-op, transient DNS failure doesn't
  thrash, IP-literal peers are skipped, empty failover is a no-op,
  `apply_fn` exceptions don't kill the watcher.

---

## 2026.04.21-1 — 2026-04-21

Big feature push — Kea HA failover, OUI vendor lookup, IPv6
auto-allocation, a first-cut alerts framework, the umbrella Helm
chart with OCI publishing, agent-side Kea DDNS + block/space
inheritance, per-server DNS zone serial reporting, API tokens,
audit-event forwarding, near-real-time Windows DHCP lease polling,
plus a healthy batch of infrastructure hardening (dev-stack
healthchecks, Trivy gate, kind-based e2e workflow).

### Added

**DHCP**

- **Kea HA failover channels** — new `DHCPFailoverChannel` model
  pairs two Kea DHCP servers in an HA relationship. Mode
  (`hot-standby` / `load-balancing`), per-peer `kea-ctrl-agent` URL,
  heartbeat / max-response / max-ack / max-unacked tuning, and
  auto-failover toggle live on the channel; each server may belong
  to at most one channel (unique FK constraints). The agent's
  `render_kea.py` injects `libdhcp_ha.so` + `high-availability`
  alongside the existing `libdhcp_lease_cmds.so` hook. Fourth agent
  thread (`HAStatusPoller`) polls `ha-status-get` every ~15 s and
  POSTs state to `/api/v1/dhcp/agents/ha-status` — control plane
  stores it on `DHCPServer.ha_state` + `ha_last_heartbeat_at`.
  Admin UI at **`/admin/failover-channels`** does CRUD; DHCP server
  detail header shows a live colored HA pill. Deferred: state-
  transition actions (`ha-maintenance-start` / `ha-continue` /
  force-sync), peer compatibility validation, per-pool HA scope
  tuning. See [`docs/features/DHCP.md` §14](docs/features/DHCP.md).
- **Near-real-time Windows DHCP lease polling** — beat ticks every
  10 s; interval now stored in seconds (default 15) via
  `PlatformSettings.dhcp_pull_leases_interval_seconds`, so
  operators can tune live cadence in the UI without restarting
  celery-beat. Closes the last agentless-DHCP visibility gap.
- **Agent-side Kea DDNS** — `/api/v1/dhcp/agents/lease-events` now
  calls `apply_ddns_for_lease` after mirroring a lease into IPAM
  and `revoke_ddns_for_lease` before deleting the mirror on
  expire / release. Errors are logged but never block lease
  ingestion.

**DNS**

- **Per-server zone serial reporting** — new `DNSServerZoneState`
  table (unique on `(server_id, zone_id)`); agents POST
  `{zones: [{zone_name, serial}, ...]}` to
  `/api/v1/dns/agents/zone-state` after each successful structural
  apply. Read endpoint
  `GET /dns/groups/{gid}/zones/{zid}/server-state` joins the
  servers for a group with their latest report. Frontend: new
  `ZoneSyncPill` on the zone detail header with 30 s refetch —
  emerald "N/N synced · serial X", amber "1/N drift · target X"
  with per-server tooltip, muted "not reported" for fresh agents.

**IPAM**

- **Full IPv6 `/next-address`** — three strategies at the API
  boundary (`sequential` / `random` / `eui64`) + a subnet-level
  default via the new `Subnet.ipv6_allocation_policy` column. EUI-64
  derives per RFC 4291 §2.5.1 (u/l bit flip + FF:FE insert); random
  uses CSPRNG with collision retry and skips the all-zero suffix
  (RFC 4291 §2.6.1 subnet-router anycast); sequential is a first-
  free linear scan capped at 65k hosts. Pydantic + UI exposed;
  `/next-ip-preview` accepts `?mac_address=` so the UI can show the
  EUI-64 candidate pre-commit. Unit coverage in
  `tests/test_ipv6_allocation.py` pins the RFC 4291 Appendix A
  example.
- **OUI vendor lookup** — opt-in IEEE OUI database fetched by the
  new `app.tasks.oui_update` Celery task on an hourly beat tick;
  task self-gates on `PlatformSettings.oui_lookup_enabled` +
  `oui_update_interval_hours` (default 24 h). Incremental diff-
  based upsert keeps each prefix's `updated_at` meaningful. New
  **Settings → IPAM → OUI Vendor Lookup** section shows source
  URL, toggle, interval, last-updated timestamp, vendor count, and
  a "Refresh Now" modal that polls task state via
  `/settings/oui/refresh/{task_id}` and renders added / updated /
  removed / unchanged counters on completion. IPAM address table +
  DHCP leases show `aa:bb:cc:dd:ee:ff (Vendor)` when enabled; the
  IPAM MAC column filter also matches vendor names so `apple` /
  `cisco` work without knowing the prefix. See
  [`docs/features/IPAM.md` §12](docs/features/IPAM.md).

**Alerts**

- **Rule-based alerts framework (v1)** — new `alert_rule` +
  `alert_event` tables. Two rule types at launch: `subnet_
  utilization` (honours `PlatformSettings.utilization_max_prefix_*`
  so PTP / loopback subnets can't trip the alarm) and
  `server_unreachable` (DNS / DHCP / any). Evaluator opens events
  for fresh matches and resolves on clear; partial index on
  `(rule_id, subject_type, subject_id) WHERE resolved_at IS NULL`
  keeps dedup O(1). Delivery reuses the audit-forward syslog +
  webhook targets. Celery beat fires every 60 s; a
  `POST /alerts/evaluate` endpoint lets the UI force a run.
  Admin page at `/admin/alerts` — rules CRUD + live events viewer
  (15 s refetch) + per-event "Resolve".

**DDNS inheritance**

- **Block + space DDNS inheritance** — `IPSpace` / `IPBlock` now
  carry `ddns_enabled` / `ddns_hostname_policy` /
  `ddns_domain_override` / `ddns_ttl`. `Subnet` / `IPBlock` carry
  `ddns_inherit_settings`. `services/dns/ddns.resolve_effective_
  ddns` walks `subnet → block chain → space` and returns an
  `EffectiveDDNS` with a `source` field for UI / debug. Both the
  hostname resolver and the apply path now consult the effective
  config instead of reading subnet fields directly — fixes the
  "space-level DDNS toggle doesn't cascade" behaviour.

**Auth / API**

- **API tokens with auto-expiry** — CRUD at `/api/v1/api-tokens`;
  `sddi_` prefix branch in `get_current_user`; tokens hashed at
  rest (sha256), shown plaintext exactly once on creation. Admin
  page at `/admin/api-tokens`.
- **Audit-event forwarding** — RFC 5424 syslog (UDP / TCP) and / or
  HTTP webhook. SQLAlchemy `after_commit` listener in
  `services/audit_forward.py`; delivery is fire-and-forget on a
  dedicated asyncio task so audit writes never block on network
  I/O. Configured under **Settings → Audit Event Forwarding** on
  platform-level `PlatformSettings` columns.

**Deployment**

- **Umbrella Helm chart (`charts/spatiumddi/`)** — replaces the
  narrow `charts/spatium-dns/` with a full application chart
  covering API, frontend, Celery worker, Celery beat, the migrate
  Job, Postgres + Redis via Bitnami subcharts, and optional DNS +
  DHCP agent StatefulSets (one per values entry). Chart-owned
  secret preserves `SECRET_KEY` across upgrades via `lookup`.
  Migrate Job runs as a pre-install + pre-upgrade Helm hook.
  Release workflow publishes to
  `oci://ghcr.io/<owner>/charts/spatiumddi` on every CalVer tag
  (CalVer → SemVer normalised: `2026.04.21-1` → `2026.4.21-1`).
  Chart README + NOTES.txt cover install, upgrade, external DB /
  Redis, and agent enablement.

**Infrastructure / CI**

- **Kind-based agent e2e workflow** — new `.github/workflows/
  agent-e2e.yml` spins up a kind cluster, installs the umbrella
  chart with one ns1 DNS agent, port-forwards the API for
  `/health/live`, execs `dig +short version.bind CH TXT` in the
  DNS agent pod, and checks restart count. Fires on
  `agent/**` / `charts/spatiumddi/**` / `backend/**` / `frontend/**`
  PRs + `workflow_dispatch`.
- **Trivy gate enforced** — `exit-code: "0"` → `"1"` with
  `ignore-unfixed: true` on both `build-dns-images.yml` and
  `build-dhcp-images.yml`, so HIGH/CRITICAL CVEs with an available
  fix block image builds. Un-fixed CVEs don't block the pipeline.

**UI polish**

- **IP Space tree interleaves blocks + subnets by network** —
  previously the tree rendered all child blocks first and all
  subnets second, so a block like `10.255.0.0/24` would bubble
  above sibling `/24` subnets regardless of address. Now
  `buildBlockTree()` merges children and sorts by network per
  level. New `lib/cidr.ts:compareNetwork()` + `addressToBigInt()`
  helpers work for both IPv4 and IPv6 at any prefix length.
- **Small-subnet suppression** —
  `PlatformSettings.utilization_max_prefix_ipv4` (default 29) and
  `_ipv6` (default 126). Subnets whose prefix exceeds the max are
  excluded from dashboard utilization counts, the heatmap, Top
  Subnets list, and the `subnet_utilization` alert rule — so
  `/30` / `/31` / `/32` (PTP, loopback) and `/127` / `/128` (RFC
  6164 PTP) no longer skew reporting. Shared
  `lib/utilization.ts:includeInUtilization` predicate.

### Changed

- **ISC DHCP support is now explicitly not supported.** Upstream
  entered maintenance-only mode in 2022 and the ISC team
  recommends Kea as the successor. Removed from the roadmap and
  every doc section, replaced with an explicit "not supported"
  note where the question would otherwise come up
  (`docs/features/DHCP.md`, `docs/drivers/DHCP_DRIVERS.md`). The
  `VALID_DRIVERS` check in the CRUD router rejects `driver:
  "isc_dhcp"` with a clean `422`.
- **Agent sync loop now unwraps the long-poll envelope.** The
  DHCP agent's `_apply_bundle` was passing the full envelope
  (`{server_id, etag, bundle, pending_ops}`) to `render_kea`,
  which expects the inner bundle dict. The agent would render a
  Kea config with no subnets or client classes. Fix: unwrap once
  in `_apply_bundle`, which also makes the new `failover` block
  actually reach the Kea renderer.

### Fixed

- **Frontend nginx cached the api upstream IP.** Recreating the
  `api` container changed its Docker-assigned IP; nginx held the
  stale one from config-load time and every `/api/v1/*` call
  started returning 502. `frontend/nginx.conf` now declares
  `resolver 127.0.0.11 valid=10s ipv6=off` + uses variable-based
  `proxy_pass` so each request re-resolves via Docker's embedded
  DNS. Adds a new `location = /nginx-health` that answers `200 ok`
  directly — no more upstream hop in the healthcheck.
- **Worker + beat healthchecks were wrong.** Both inherited the
  api's `http://localhost:8000/health/live` probe from
  `backend/Dockerfile` but neither process listens on HTTP; both
  kept flipping to `unhealthy`. Overrode in `docker-compose.yml`:
  worker uses `celery -A app.celery_app inspect ping -d celery@
  $HOSTNAME` (broker round-trip); beat uses
  `grep -q 'celery' /proc/1/cmdline`.
- **Frontend healthcheck resolved to IPv6.** Busybox wget prefers
  `::1` for `localhost`; nginx binds `0.0.0.0:80`, so the probe
  returned "Connection refused". Switched to
  `http://127.0.0.1/nginx-health`.
- **CodeQL `actions/missing-workflow-permissions` on agent-e2e**
  — new workflow missed its top-level `permissions:` block.
  Added `contents: read` (least-privilege).

### Docs

- `docs/features/DHCP.md` — §14 rewritten as a real "Kea HA
  failover channels" spec (data model, modes, agent-side rendered
  hook payload, state-reporting cadence, managing channels), and
  Rules & constraints gets a new "Failover channels" subsection.
  §15 "Parent / child setting inheritance" explicitly calls out
  the DDNS inheritance chain.
- `docs/features/IPAM.md` §12 — OUI section rewritten to match
  the shipped behaviour (source URL, gating fields, diff-based
  atomic replace, manual refresh endpoint, inline display).
- `CLAUDE.md` — multiple roadmap status updates: Phase 1 IPv6,
  DDNS agent path, DDNS block/space inheritance, per-server zone
  serial, Trivy-clean + kind e2e, alerts framework (v1), and
  Kea HA (core) flipped to ✅. ACME DNS-01 provider + embedded
  client entries added to Future Phases with full shape.
- `README.md` + `CLAUDE.md` + `docs/drivers/DHCP_DRIVERS.md` +
  `docs/PERMISSIONS.md` — ISC DHCP scrubbed.

---

## 2026.04.20-2 — 2026-04-20

Follow-on polish release. Dark sidebar so the nav is distinct from
content in light mode, per-zone / per-space color tagging, zebra
striping across every long-list table, and a batch of delete-flow
bug fixes turning silent failures into actionable 409s. New
troubleshooting doc + "Rules & constraints" sections in the feature
specs so operators hitting a 409 can jump straight to the
enforcement site.

### Added

**Theme + color**
- Dark sidebar in both themes (`--sidebar-*` CSS tokens wired through
  tailwind.config). In light mode the sidebar is dark slate with a
  white-pill active item so it no longer blends into the page; in
  dark mode it sits slightly darker than content for separation.
- `DNSZone.color` (migration `f4a9c1b2d6e7`) and `IPSpace.color`
  (migration `a5b8e9c31f42`). Curated 8-swatch set
  (slate/red/amber/emerald/cyan/blue/violet/pink) — free-form hex
  is deliberately rejected so every choice stays legible in both
  themes. Zones render a colored dot on tree rows, list rows, and
  the zone detail header. Spaces paint the tint as the *row
  background* (since spaces sit at the top of the tree); selection
  uses a `ring-1` so the color stays visible when the space is
  selected. Closes [#20].
- Shared `<SwatchPicker>` (`components/ui/swatch-picker.tsx`) + the
  `SWATCH_COLORS` / `swatchCls` / `swatchTintCls` helpers in
  `lib/utils.ts` so DNS and IPAM stay coherent.

**Zebra striping across long-list tables**
- `zebraBodyCls` utility applied to every substantial `<tbody>`:
  IPAM addresses / blocks / subnets / aliases, DNS zones / records,
  DHCP scopes / pools, VLANs, Users / Groups / Roles / Audit, and
  the Logs grid.
- Uses `bg-foreground/[0.05]` + hover `bg-foreground/[0.09]` instead
  of `bg-muted/40`. The old `muted` tint in light mode was only ~4%
  lightness darker than white and effectively invisible; the
  foreground-based tint gives consistent contrast in both themes.

**Docs**
- New `docs/TROUBLESHOOTING.md` covering the recovery recipes that
  aren't obvious from the feature specs: accidentally deleting a
  DNS / DHCP server from the UI (agent auto-rebootstraps via PSK on
  404; manual escape is wiping `agent_token.jwt` + `agent-id` and
  restarting), admin-password reset, and the new subnet-delete 409
  behaviour.
- "Rules & constraints" sections added to `IPAM.md` / `DHCP.md` /
  `DNS.md` / `AUTH.md`. Each rule: one-line intent, short
  why-it-exists where non-obvious, and `file:line` + HTTP status so
  operators can jump from a response `detail` to the enforcement
  site. ~100 rules across the four domains (delete guards, overlap
  checks, pool / collision rules, enum validators, Windows
  push-before-commit).
- `CLAUDE.md` doc map gains the TROUBLESHOOTING.md entry.

### Changed

- **Subnet delete is now refused when non-empty.** The endpoint used
  to cascade silently (wiping IPs + scopes with the subnet); it now
  returns `409` with a breakdown (*"Subnet is not empty: N allocated
  IP addresses, M DHCP scopes"*) matching the existing block-delete
  behaviour. Opt into the cascade with `?force=true`; the pre-delete
  WinRM remove-scope + Kea bundle rebuild still run either way so
  nothing is orphaned on a running server.
- Dashboard live-activity column widths. Long audit action names
  like `DHCP.SERVER.SYNC-LEASES` were breaking on the hyphen and
  bleeding into the adjacent resource column. Action column widened
  from `w-14` (56px) to `w-36` (144px); resource-type bumped to
  `w-20`.

### Fixed

- **Silent failures across every subnet / block delete path.**
  Single-subnet delete from the tree, single-subnet delete from the
  Edit Subnet modal, block-level bulk subnet delete, space-level
  bulk delete (mixed subnets + blocks), and block delete from the
  tree context menu all now capture 409 responses and render the
  detail inline in `ConfirmDestroyModal`. Bulk paths use
  `Promise.allSettled` + per-item messages so one blocker doesn't
  hide the rest; successes still commit.
- Space color stayed invisible when the space was selected because
  `bg-primary/5` overrode the tint. Selection now uses `ring-1
  ring-primary/60` so the color stays visible alongside the
  selection indicator.
- Multi-line errors in the confirmation modal — `whitespace-pre-line`
  + `max-h-48 overflow-auto` on the error box so long failure lists
  from bulk deletes scroll instead of pushing the buttons off-screen.

[#20]: https://github.com/spatiumddi/spatiumddi/issues/20

---

## 2026.04.20-1 — 2026-04-20

CI-only release to fix the multi-arch build in the release workflow
and publish the previously-missing agent images.

### Fixed

- Release workflow matrix was pushing each platform to the same tag
  separately, so the second push overwrote the first — the resulting
  images had no `linux/amd64` manifest and `docker compose pull`
  failed on amd64 hosts. Switched to a single job with
  `platforms: linux/amd64,linux/arm64` via QEMU so the push produces
  a proper multi-arch manifest list.
- Added `build-dns` and `build-dhcp` jobs so `dns-bind9` and
  `dhcp-kea` images are actually built and published alongside
  `spatiumddi-api` and `spatiumddi-frontend`. These images were
  referenced by `docker-compose.yml` but never produced by the
  release pipeline.

---

## 2026.04.19-1 — 2026-04-19

The **performance, polish, and visibility** release. Batched WinRM
dispatch turns multi-minute Windows DNS / DHCP syncs into a handful of
round trips. A new **Logs** surface exposes Windows Event Log + per-day
DHCP audit files over WinRM with filter / auto-fetch / date-picker UX.
The IPAM tree gains subnet + block **resize** with blast-radius preview,
subnet-scoped IP **import**, **DHCP pool awareness** (pool boundary
rows + dynamic-pool gates + next-IP preview), and **collision warnings**
on hostname+zone / MAC. Sync menu + DHCP sync modal + combined
Sync-All modal replace the silent lease-sync button. Dashboard rebuilt
around a **subnet-utilization heatmap** + live activity feed. Every
modal is now **draggable**; every detail-page header uses the same
**`HeaderButton`** primitive. DDNS (DHCP lease → DNS A/PTR) ships for
the agentless lease-pull path.

### Added

**DDNS — DHCP lease → DNS A/PTR reconciliation**
- Migration `e1f2a3b4c5d6` adds four subnet-level DDNS control fields:
  `ddns_enabled` (default False — opt-in),
  `ddns_hostname_policy` (`client_provided` | `client_or_generated` |
  `always_generate` | `disabled`),
  `ddns_domain_override` (publish into a different zone), and
  `ddns_ttl` (override the zone's TTL for auto-generated records).
  Independent of `DHCPScope.ddns_enabled` — that pair still drives
  Kea's native DDNS hook.
- New service `app/services/dns/ddns.py` with `resolve_ddns_hostname`,
  `apply_ddns_for_lease`, and `revoke_ddns_for_lease`. Static-assignment
  hostnames always win over policy; client hostnames are sanitised to
  RFC 1035 labels and truncated at 63 chars; generated hostnames use
  the last two IPv4 octets (`dhcp-20-5` for `10.1.20.5`) or the low
  32 bits hex for IPv6.
- Integration points: `services/dhcp/pull_leases.py` fires DDNS after
  each mirrored IPAM row (agentless lease-pull path);
  `tasks/dhcp_lease_cleanup.py` calls `revoke_ddns_for_lease` before
  deleting the mirrored row.
- Idempotent — repeat polls over the same lease state are a no-op.
- `_sync_dns_record` is lazy-imported from the IPAM router at call
  time to dodge a module-load cycle.
- `SubnetCreate` / `SubnetUpdate` / `SubnetResponse` + `Subnet`
  frontend type gain the four DDNS fields.
- New `DdnsSettingsSection` React component — enable toggle, policy
  dropdown, domain-override input, TTL input, live preview of what
  `always_generate` would produce for the subnet's first IP. Wired
  into `CreateSubnetModal` and `EditSubnetModal`.
- Docs: `features/DNS.md §7` rewritten to describe the shipped
  implementation (architecture diagram, subnet fields, policy
  semantics, static override, idempotency, enable walkthrough).

**Logs surface — Windows Event Log + DHCP audit (WinRM)**
- New top-level `/logs` page + sidebar entry. Pulls events on demand
  over WinRM from any agentless server that has credentials set — no
  new env vars, no migrations.
- `app/drivers/windows_events.py` — shared helper `fetch_events()`
  builds a `Get-WinEvent -FilterHashtable` script from neutral filters
  (log_name, level 1-5, max_events 1-500, since datetime, event_id).
  Filters run server-side so the full log never crosses the wire.
- Drivers expose log inventory through
  `available_log_names()` + `get_events()`:
  `WindowsDNSDriver` → `DNS Server` (classic) +
  `Microsoft-Windows-DNSServer/Audit`;
  `WindowsDHCPReadOnlyDriver` → `Operational` +
  `FilterNotifications`. Analytical log omitted (noisy, per-query —
  better viewed in MMC).
- `GET /logs/sources` — lists every server with WinRM creds + its
  available log names for the picker.
- `POST /logs/query` — runs the filtered `Get-WinEvent`; 400 on
  missing creds, 502 on upstream PowerShell failure with the Windows
  error surfaced to the UI.
- Dispatch goes through the abstract driver interface — logs router
  never imports `windows_events` directly (non-negotiable #10).
- **DHCP audit tab** — separate endpoint `POST /logs/dhcp-audit`
  reads `C:\Windows\System32\dhcp\DhcpSrvLog-<Day>.log` (the
  CSV-style per-lease trail Windows DHCP writes by default) over
  WinRM. Handles UTF-16 + ASCII encodings; event-code → human label
  map covers documented codes; unknown codes come through as
  `Code <n>` so new Windows releases don't drop silently. Access
  denied / missing file / locked-by-rotation all return `[]` instead
  of 500.
- Frontend: **Event Log | DHCP Audit** tab switcher reusing shared
  `ServerPicker` / `MaxEventsPicker` / `FilterSearch` / `RefreshButton`
  helpers. Audit tab columns: Time / Event (code + label) / IP /
  Hostname / MAC / Description with event-dot colours mirroring
  Windows severity families; event-code distribution picker (e.g.
  `10 — Lease granted (238)`) for targeted filtering; day picker
  defaults to Today with Mon-Sun backfill.
- Auto-fetch via `useQuery` keyed on every filter so page entry +
  filter changes trigger refetch; `staleTime: Infinity` means
  tab-switch doesn't spam the DC. Explicit **Refresh** button calls
  `refetch()` to bypass cache.
- Date picker uses native `<input type="datetime-local">` + `since`
  on `LogQueryRequest`; `×` clear button inline.

**IPAM — subnet + block resize (grow-only, preview + commit)**
- `POST /ipam/subnets/{id}/resize/preview` + `POST /ipam/subnets/{id}/resize`;
  parallel endpoints at `/ipam/blocks/{id}/resize/...`. Shrinking is
  explicitly out of scope — it silently orphans addresses.
- Preview returns a blast-radius summary (affected IPs, DHCP scopes,
  pools, static assignments, DNS records, reverse zones to create)
  + a `conflicts[]` list that disables the commit button when
  non-empty.
- Rules enforced server-side: grow only; same address family; old
  CIDR ⊂ new CIDR; new CIDR fits inside the parent block; no overlap
  with any subnet or block anywhere in the space (cross-subtree
  scan); block children must still fit; per-resource pg advisory
  lock during commit; commit re-runs every validation (TOCTOU guard)
  before mutating; optional gateway-to-first-usable move; rejects at
  preview when the new CIDR has no usable range
  (`/31 /32 /127 /128`).
- Renamed or DNS-bearing placeholder rows are preserved across
  resize; only default-named network/broadcast rows get recreated at
  the new boundaries.
- Reverse-zone backfill runs on commit so `/24 → /23` creates the
  second reverse zone automatically.
- Single audit entry per resize with old → new CIDR + counts.
- UI `ResizeSubnetModal` / `ResizeBlockModal` with typed-CIDR
  confirmation gate. Commit button hidden (not just disabled) on
  conflict so the user has no false sense they can force-commit.

**IPAM — subnet-scoped IP address import**
- Space-scoped importer already existed; the new subnet-scoped flow
  handles the common "export IPs from vendor X, load into
  SpatiumDDI" migration. `POST /ipam/import/addresses/preview` +
  `/commit`.
- Parser auto-routes CSV / JSON / XLSX rows by header:
  `address` / `ip` → addresses, `network` → subnets. Unrecognised
  columns drop into `custom_fields` so other-vendor exports work
  without rename passes.
- Validates each IP falls inside the subnet CIDR; respects
  fail / skip / overwrite strategies; writes audit rows; calls
  `_sync_dns_record` so rows with hostnames publish A + PTR through
  the same RFC 2136 path the UI uses.
- Frontend `AddressImportModal` + a combined `Import / Export`
  dropdown on the subnet header.

**IPAM — IP assignment collision warnings**
- Two non-fatal guardrails on IP create / update:
  **FQDN collision** on same `(lower(hostname), forward_zone_id)`
  across any subnet, and **MAC collision** on same MAC anywhere in
  IPAM.
- Server: new `_normalize_mac` + `_check_ip_collisions` helpers in
  `backend/app/api/v1/ipam/router.py`; `force: bool = False` added
  to `IPAddressCreate` / `IPAddressUpdate` / `NextIPRequest`. When
  `force=false` and the pending assignment collides, the endpoint
  returns 409 with
  `detail = {warnings: [...], requires_confirmation: true}`. Clients
  re-submit with `force=true` to proceed.
- Update path only checks fields the client explicitly set
  (`model_dump(exclude_unset=True)`), so unchanged rows never
  surface a pre-existing collision on unrelated edits;
  `exclude_ip_id` keeps the row from colliding with its own current
  state.
- UI: shared `CollisionWarning` type + amber
  `CollisionWarningBanner` in `IPAMPage.tsx`. Allocate and edit
  modals parse the 409, render one line per collision, and flip the
  submit button to "Allocate anyway" / "Save anyway". Editing any
  collision-relevant field clears the pending warning so the next
  submit re-checks fresh.

**IPAM — DHCP pool awareness**
- IP listing interleaves ▼ start / ▲ end pool boundary rows with
  existing IP rows. Dynamic pools tint cyan, reserved violet,
  excluded zinc. Each marker shows pool name + full range so the
  user sees pool extents even when no IP is assigned inside.
- `create_address` rejects with 422 when `body.address` lands inside
  a **dynamic** pool — the DHCP server owns that range. Excluded /
  reserved pools still allow manual allocation.
- `allocate_next_ip` uses a hoisted `_pick_next_available_ip` helper
  that skips dynamic ranges during its linear search.
- New `GET /ipam/subnets/{id}/next-ip-preview?strategy=...` returns
  `{address, strategy}` without committing.
- `AddAddressModal` "next" mode loads the preview on open and shows
  `Next available: 10.0.1.42 (skips dynamic DHCP pools)` in emerald
  — or a destructive "no free IPs" line with submit disabled when
  exhausted. Manual mode renders an inline red warning + disables
  submit when the typed IP falls in a dynamic range.

**IPAM — Sync menu + DHCP sync modals**
- `[Sync ▾]` dropdown in the subnet detail header with **DNS**,
  **DHCP** (gated on scope presence), and **All** entries. DHCP fans
  out `POST /dhcp/servers/{id}/sync-leases` across every unique
  server backing a scope in this subnet (deduped,
  `Promise.allSettled` so one bad server doesn't mask the others).
- `DhcpSyncModal` — per-server result cards (pending spinner → Done
  / Failed) with a counter grid: active leases, refreshed, new,
  **removed** (deleted on server), IPAM created, **IPAM revoked**.
  `removed` + `ipam_revoked` highlight amber when non-zero —
  they're the rows the stale-lease fix cleaned up. Close disabled
  until every server reports.
- `SyncAllModal` — one modal, two sections. DHCP panel uses the
  same `useDhcpSync` hook + body component. DNS panel fetches the
  existing drift summary (`missing / mismatched / stale / total`)
  and either shows "In sync" or an amber block with a "Review DNS
  changes…" button that chains into the existing `DnsSyncModal`.

**IPAM — refresh buttons**
- `[↻ Refresh]` buttons added to DNS zone records page, IPAM subnet
  detail, and the VLANs sidebar. Each invalidates every React Query
  key the surface consumes.

**Dashboard rewrite**
- Six compact KPI cards (IP Spaces / Subnets / Allocated IPs /
  Utilization / DNS Zones / Servers), tone-coloured left accent
  stripe, hover state, most click through to their module page.
- **Subnet Utilization Heatmap** (hero) — every managed subnet is
  one grid cell coloured by utilization. Auto-fill flow, hover
  tooltip (network + name + %/allocated/total), click opens the
  subnet in IPAM. Header has a colour legend; footer shows avg /
  p95 / hot counts.
- Two-column split: Top Subnets by Utilization + Live Activity
  feed (auto-refreshes every 15 s, action-family colour coding,
  relative timestamps).
- Services panel: two-column DNS + DHCP server list with status
  dots (pulsing for active + enabled) + driver / group /
  last-checked columns.

### Changed

**Batched WinRM dispatch** — the major perf win.
- New `apply_record_changes` on DNSDriver and
  `apply_reservations` / `remove_reservations` / `apply_exclusions`
  on DHCPDriver. Default ABC impls loop the singular method, so
  BIND9 / Kea inherit the batch interface without changes.
- Windows drivers override with real batching. DNS driver ships
  one PowerShell script per zone chunked at `_WINRM_BATCH_SIZE = 6`
  ops — empirically the ceiling given `pywinrm.run_ps` encodes
  UTF-16-LE + base64 through `powershell -EncodedCommand` as a
  single CMD.EXE command line (8191-char cap). Each chunk ships a
  compact data-only JSON payload (short keys
  `i/op/z/n/t/v/ttl/pr/w/p`) + one shared wrapper that dispatches
  per record type with per-op try / catch + JSON result array.
  One bad record doesn't abort the batch.
- DHCP driver batches at `_WINRM_BATCH_SIZE = 30` ops — DHCP
  payloads are leaner so the cmdline limit is further away, but
  capped to stay safe.
- RFC 2136 record ops run in parallel via `asyncio.gather` — cheap
  enough per-op that batching isn't needed but serial was still
  slow.
- `enqueue_record_ops_batch` in `record_ops.py` groups pending ops
  by zone and calls the plural driver method once per group.
- IPAM Sync-DNS stale-delete path switched to batch; DNS tab
  bulk-delete got a real server-side endpoint
  (`POST /dns/groups/{g}/zones/{z}/records/bulk-delete`) so the
  frontend no longer fans out N HTTP requests + the zone serial
  bumps once per batch instead of N times.
- DHCP `push_statics_bulk_delete` groups by (server, scope); the
  IPAM purge-orphans path went from N×M WinRM calls to one per
  server.
- 40-record Sync DNS: 2-3 min → ~5 s.

**Sync menu + combined Sync All** — replaces the per-page "Sync DNS"
button on the subnet detail (see Added). Blocks and spaces keep the
single "Sync DNS" button since they have no DHCP scopes.

**UI consolidation — draggable modals.**
- 7 near-identical `function Modal({...})` definitions (one per
  page) collapsed into a single `<Modal>` primitive at
  `frontend/src/components/ui/modal.tsx`.
- Title bar is a drag handle (`cursor-grab` /
  `active:cursor-grabbing`). Drags starting on buttons / inputs /
  selects / textareas / anchors are ignored — controls in the header
  stay clickable. Backdrop dimmed to `bg-black/20` so the page
  behind the dialog stays readable. Esc closes.
- Custom modal shapes (header with border-b + footer slot) use
  `useDraggableModal(onClose)` + `MODAL_BACKDROP_CLS` from
  `components/ui/use-draggable-modal.ts` (split out so Vite's
  fast-refresh doesn't warn about mixed component / utility
  exports).
- Migrated every standard modal across admin, DNS, DHCP, VLANs,
  IPAM plus `ResizeModals`, `ImportExportModals`, and the inline
  `DnsSyncModal`. Net ~168 lines removed.

**UI consolidation — standardised header buttons.**
- New `<HeaderButton>` primitive
  (`frontend/src/components/ui/header-button.tsx`) with three
  variants (`secondary` / `primary` / `destructive`) on a shared
  `inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm`
  base. Forwards refs + spreads `ButtonHTMLAttributes` for
  disabled / title / onClick without ceremony.
- Logical left → right ordering applied everywhere:
  `[Refresh] [Sync …] [Import] [Export] [misc reads] [Edit] [Resize]
  [Delete] [+ Primary]`. DNS was `text-xs`; DHCP was a mixed
  `text-xs px-3 py-1.5`; VLANs was `text-xs px-3 py-1`. All bumped
  to match IPAM's dominant `text-sm px-3 py-1.5 gap-1.5`.
- Migrated surfaces: IPAM `SubnetDetail` / `BlockDetail` /
  `SpaceTableView`; DNS `ZoneDetailView`; DHCP `ServerDetailView`;
  VLANs `RouterDetail` / `VLANDetail`.

**Kea driver — Dhcp6 option-name translation.** New
`_KEA_OPTION_NAMES_V6` map + `_DHCP4_ONLY_OPTION_NAMES` set;
`_render_option_data` takes `address_family` and routes accordingly.
v4-only options (`routers`, `broadcast-address`, `mtu`,
`time-offset`, `domain-name`, tftp-*) are dropped from v6 scopes with
a warning log instead of being emitted under the wrong space (which
Kea would reject on reload). Scope / pool / reservation /
client-class renderers all thread `address_family` through. Closes
the Phase 1 Dhcp6 TODO.

**Architecture SVG** — added explicit `width="1200" height="820"` +
`preserveAspectRatio="xMidYMid meet"` to the root `<svg>` element.
GitHub's blob view was falling back to ~300px when the image was
clicked through from the README because `viewBox` alone isn't
enough.

**README rewrite — hero + Why section.** Old two-paragraph "What is
SpatiumDDI?" prose split into a punchy tagline ("Self-hosted DNS,
DHCP, and IPAM — one control plane, real servers underneath"), a
new `## Why SpatiumDDI` with 5 scannable bold claims, and a
`## What's in the box` section holding the feature bullets.
Architecture and Getting Started sections untouched.

### Fixed

- K8s worker queue mismatch — `k8s/base/worker.yaml` listed
  `ipam,default`; compose widened to `ipam,dns,dhcp,default` in the
  Windows release. DNS + DHCP health sweeps + scheduled sync tasks
  were silently hanging on K8s.
- Windows DNS TLSA dispatch — `_SUPPORTED_RECORD_TYPES` listed TLSA
  (RFC 2136 handles it fine via dnspython) but `_ps_apply_record`
  raised ValueError for TLSA, so creating a TLSA record on a
  Windows server with credentials failed unpredictably. Added
  `_WINRM_UNSUPPORTED_RECORD_TYPES`; `apply_record_change` now falls
  back to RFC 2136 for those types even when credentials are set.
- `DHCPPage.tsx` lease-sync handler was invalidating
  `["ipam-addresses"]` which matches nothing; changed to
  `["addresses"]` (broad match) so the subnet-level address list
  refreshes after lease sync mirrors new rows.
- Frontend `DHCPPool` type now declares optional
  `existing_ips_in_range` so `CreatePoolModal` no longer needs an
  `as any` cast.
- **DHCP stale-lease absence-delete.** `pull_leases` was upsert-only
  but the Windows DHCP driver only returns *active* leases, so when
  an admin deleted a lease on the server it silently persisted in
  our DB + in IPAM (as `auto_from_lease=True` rows). After the
  upsert loop, `pull_leases` now finds every active `DHCPLease` row
  for this server whose IP wasn't in the wire response and deletes
  both the lease row and its IPAM mirror. `PullLeasesResult` gains
  `removed` + `ipam_revoked` counters; `SyncLeasesResponse` + the
  scheduled-task audit / log lines follow suit. The time-based
  `dhcp_lease_cleanup` sweep continues to handle leases that drift
  past `expires_at` between polls — the two mechanisms overlap
  harmlessly.
- **Sync DNS classifier — PTR overwrite on unassigned forward zone.**
  If a subnet had a reverse zone but the forward zone had been
  unassigned, the classifier built an "expected" PTR value of
  `hostname.` (bare label, no FQDN); existing PTRs got classified
  `mismatched` and the commit rewrote them to the broken value
  instead of deleting them. Now: when only reverse is effective,
  existing PTRs are classified `stale` with reason
  `no-forward-zone` so the sync deletes them.
- **Sync DNS classifier — A-record orphans invisible.** Matching
  bug on the A-record side. Unassigning the forward zone left A
  records orphaned in the old zone, but the classifier's
  `if forward_zone and not is_default_gateway` branch skipped them
  entirely — Sync DNS reported 0 drift. Now:
  `elif not forward_zone and ip_a_records` classifies them `stale`
  for the same reason.
- **Sync DNS cache invalidation.** When Sync-DNS deleted a stale
  record linked to an IPAddress, the cached `ip.fqdn` /
  `ip.forward_zone_id` / `ip.dns_record_id` (A/AAAA/CNAME) and
  `ip.reverse_zone_id` (PTR) stuck around; the UI kept showing the
  old FQDN after the records were gone. The stale-delete path now
  clears the matching cached fields.
- **Agentless bulk-delete silent failure.** `_apply_agentless_batch`
  caught wire failures, marked the op rows failed, and returned
  normally — so `_apply_dns_sync` deleted the DB rows anyway and
  told the user "deleted" while the records were still published on
  Windows. Same hole in `bulk_delete_records`. Both paths now zip
  through the returned op rows and only delete when
  `state == 'applied'`; the rest surface as per-record errors /
  skipped entries.
- **Logs — `EventLogException` handling.** `Get-WinEvent` raises
  `System.Diagnostics.Eventing.Reader.EventLogException` when the
  log name doesn't exist on the target host, when zero events match,
  or when a FilterHashtable key doesn't apply to the log — bypasses
  `-ErrorAction SilentlyContinue`. The shared helper now wraps the
  cmdlet in try/catch, explicitly catches `EventLogException`, and
  falls through to a generic catch matching common "no data / bad
  log" patterns — any of those return `[]` cleanly instead of
  surfacing "The parameter is incorrect" / "not an event log" to the
  UI as a 502. Dropped the bogus
  `Microsoft-Windows-Dhcp-Server/AdminEvents` log name (it isn't a
  real log); remaining `Operational` + `FilterNotifications` pair is
  reliable across Server 2016+.

---

## 2026.04.18-1 — 2026-04-18

The **Windows Server integration** release. Adds agentless drivers for
Windows DNS (Path A — RFC 2136, always available; Path B — WinRM +
PowerShell for zone CRUD and AXFR-free record pulls) and Windows DHCP
(Path A — WinRM lease mirroring + per-object scope / pool / reservation
write-through). IPAM gains full DHCP server-group inheritance parallel to
the existing DNS model, a two-action delete (Mark as Orphan / Delete
Permanently), and a right-click context menu across every top-level
module. Settings gets a per-section "Reset to defaults" button, the two
DNS sync sections were renamed with a layer diagram showing which
boundary each one reconciles, and three new doc sets (Getting Started,
Windows Server setup, DHCP driver spec) land alongside a redrawn
architecture SVG.

### Added

**DNS — Windows Server driver (agentless, Path A + B)**
- `WindowsDNSDriver` (`backend/app/drivers/dns/windows.py`) implementing
  record CRUD for `A / AAAA / CNAME / MX / TXT / PTR / SRV / NS / TLSA`
  over RFC 2136 via `dnspython`. Optional TSIG; GSS-TSIG and SIG(0) are
  Path B follow-ups.
- `AGENTLESS_DRIVERS` frozenset + `is_agentless()` in the DNS driver
  registry. `record_ops.enqueue_record_op` short-circuits straight to the
  driver for agentless servers instead of queueing for a non-existent
  agent; logs a warning when a record op is dropped for lack of a
  primary.
- **Path B (credentials required)** — `DNSServer.credentials_encrypted`
  (Fernet-encrypted WinRM dict, same shape as Windows DHCP) unlocks
  `Add-DnsServerPrimaryZone` / `Remove-DnsServerZone` for zone CRUD and
  `Get-DnsServerResourceRecord`-based record pulls that sidestep the
  AD-integrated zone AXFR ACL which otherwise returns REFUSED. All
  PowerShell paths are idempotent — guard on `Get-DnsServerZone
  -ErrorAction SilentlyContinue` before acting. Record writes still ride
  RFC 2136 to avoid paying the PowerShell-per-record cost.
- **Write-through for zones** — `_push_zone_to_agentless_servers` pushes
  zone create / delete to Windows *before* the DB commit; a WinRM
  failure surfaces as HTTP 502 and rolls back, so DB and server never
  drift. Mirrors the Windows DHCP write-through pattern.
- **Shared AXFR helper** — `app/drivers/dns/_axfr.py` now used by both
  BIND9 and the Windows RFC path. Filters SOA + apex NS and absolutises
  `CNAME / NS / PTR / MX / SRV` targets.
- `POST /dns/test-windows-credentials` — runs
  `(Get-DnsServerSetting -All).BuildNumber` as a cheap probe; wired into
  the server create modal's "Test Connection" button.
- Migration `d3f1ab7c8e02_windows_dns_credentials.py`.

**DHCP — Windows Server driver (agentless, Path A)**
- `WindowsDHCPReadOnlyDriver` (`backend/app/drivers/dhcp/windows.py`)
  speaks WinRM / PowerShell against the `DhcpServer` module. Reads:
  `Get-DhcpServerv4Lease` for lease monitoring,
  `Get-DhcpServerv4Scope` + options + exclusions + reservations for
  scope topology pulls. Writes (per-object, idempotent): `apply_scope`
  / `remove_scope` / `apply_reservation` / `remove_reservation` /
  `apply_exclusion` / `remove_exclusion`.
- `services/dhcp/windows_writethrough.py` pushes scope / pool / static
  edits to Windows before DB commit — same rollback guarantee as the
  Windows DNS path.
- `AGENTLESS_DRIVERS` + `READ_ONLY_DRIVERS` sets on the DHCP driver
  registry. The `/sync` bundle-push endpoint rejects read-only drivers;
  the UI hides "Sync / Push config" and substitutes "Sync Leases" +
  per-object CRUD instead.
- Scheduled Celery beat task `app.tasks.dhcp_pull_leases` (60 s cadence;
  gates on `PlatformSettings.dhcp_pull_leases_enabled` /
  `_interval_minutes`). Upserts leases by `(server_id, ip_address)` and
  mirrors each lease into IPAM as `status="dhcp"` + `auto_from_lease=
  True` when the IP falls inside a known subnet. The existing lease-
  cleanup sweep handles expiry uniformly.
- Admin UX: transport picker (`ntlm` / `kerberos` / `basic` / `credssp`),
  "Test WinRM" button, Windows setup checklist (security group + WinRM
  enablement), partial credential updates that preserve the stored blob
  across transport changes. Agentless servers auto-approve on create.
- Migration `b71d9ae34c50_windows_dhcp_support.py`.

**IPAM → DHCP server-group inheritance**
- New `dhcp_server_group_id` on `IPSpace` / `IPBlock` / `Subnet`, plus
  `dhcp_inherit_settings` on Block / Subnet — mirrors the existing DNS
  pattern.
- Three `/effective-dhcp` endpoints walk Space → Block → Subnet; subnet
  resolution falls through to the space when no block overrides.
- `CreateScopeModal` prefills the server from the effective group,
  restricts the dropdown to that group, and exposes an override
  checkbox. Space / Block / Subnet modals gain a DHCP section parallel
  to `DnsSettingsSection`.
- Migration `a92f317b5d08_ipam_dhcp_server_group_inheritance.py`.

**DNS — bi-directional zone reconciliation**
- Group-level "Sync with Servers" button iterates every enabled server,
  auto-imports zones found on the wire but missing from SpatiumDDI
  (skipping system zones TrustAnchors / RootHints / Cache), pushes
  DB-only zones back via `apply_zone_change`, then pulls records
  (AXFR for BIND9 / Windows Path A, `Get-DnsServerResourceRecord` for
  Path B) and reconciles against DB state. Additive-only — never
  deletes on either side.
- Dedup keys fold `CNAME / NS / PTR / MX / SRV` to canonical absolute
  FQDNs so IPAM-written (FQDN-with-dot) and AXFR-read (bare label)
  values no longer duplicate. Out-of-zone glue records are filtered.

**DNS server enable/disable**
- `DNSServer.is_enabled` — user-controlled pause flag separate from
  health-derived `status`. Disabled servers are skipped by the health
  sweep, bi-directional sync, and the record-op dispatcher.
- Migration `c4e8f1a25d93_dns_server_is_enabled.py`.
- `dhcp_health` + `dns` health tasks refactored to per-task async
  engines (fixes "Future attached to a different loop" when the worker
  queue is widened) and now call `driver.health_check()` for agentless
  drivers so the dashboard stops showing "never checked" for Windows
  DHCP / DNS. Compose worker queues re-widened to
  `ipam,dns,dhcp,default`.

**IPAM — two-action delete + cache propagation**
- Allocated-IP delete now offers two distinct actions: **Mark as
  Orphan** (amber — keeps the row, clears ownership metadata) and
  **Delete Permanently** (destructive). No double-confirm — the two
  coloured buttons are the confirmation.
- Every IPAM mutation that invalidates `["addresses", …]` now also
  invalidates `["dns-records"]`, `["dns-group-records"]`, and
  `["dns-zones"]`. A newly-created PTR shows up in the reverse-zone
  record list without a full page reload.

**Settings — per-section reset + DNS sync renames**
- `GET /settings/defaults` introspects column defaults from the
  `PlatformSettings` model (single source of truth — no frontend drift).
- Per-section **Reset to defaults** button populates only that section's
  fields; Save is still required so the user can back out.
- Renamed *DNS Auto-Sync* → **IPAM → DNS Reconciliation** and
  *DNS Server Sync* → **Zone ↔ Server Reconciliation**. Each gets a
  three-pill layer diagram (IPAM → SpatiumDDI DNS ↔ Windows / BIND9)
  with the relevant arrow highlighted.

**Delete guards + bulk actions + right-click menus**
- IP space, IP block, DNS server group, DHCP server group: **409** on
  delete if populated (plain text error with count).
- Subnet delete now cascades DHCP scope cleanup to Windows
  (`push_scope_delete`) and Kea (`config_etag` bump + `DHCPConfigOp`).
- DNS ZonesTab: compact table replaces card-per-zone; checkbox column
  + bulk delete toolbar. IPAM space table: bulk-select leaf blocks.
- Right-click context menus across IPAM IP rows, IPAM space headers,
  DNS zone tree + record rows, DHCP scope / static / lease rows, VLAN
  rows.
- DNS group picker: single-select dropdown; Additional Zones hidden
  behind a themed `<details>` expander.
- VLAN router delete: two-step confirmation with checkbox.
- `ConfirmDestroyModal` / `DeleteConfirmModal` surface 409 errors
  inline.
- Space-table refresh button: `forceRefetch` instead of invalidate.
- Migration `e5b831f02db9` enforces `subnet.block_id NOT NULL` and
  fixes FK drift from SET NULL to RESTRICT.

**Auth — provider form UX**
- Auth-provider form defaults to `is_enabled=True` and
  `tls_insecure=False`. Pre-save "Test Connection" probe validates
  before creation (instead of after a save that might fail at login
  time). Applies to all five provider types.

**UI — selection persistence**
- IPAM / DNS / DHCP selection (subnet / zone / server) now survives tab
  switches. IPAM + DNS had a race in `useStickyLocation`'s restore
  effect; DHCP had no URL backing at all and was pure in-memory state.
  Both fixed; DHCP gets `spatium.lastUrl.dhcp` + a `setSelection()`
  wrapper that mirrors into `?group=…&server=…`.

**Documentation**
- [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) — recommended
  setup order from fresh install to allocating the first IP, with three
  topology recipes (all-SpatiumDDI / hybrid Windows DNS / hybrid
  Windows DNS + DHCP).
- [`docs/deployment/WINDOWS.md`](docs/deployment/WINDOWS.md) — shared
  Windows-side checklist: WinRM enablement, transport / port matrix,
  firewall rules, service accounts (`DnsAdmins` / `DHCP Users`), zone
  dynamic-update settings, diagnosis recipe with a pywinrm snippet,
  hardening checklist.
- [`docs/drivers/DHCP_DRIVERS.md`](docs/drivers/DHCP_DRIVERS.md) —
  filled in the driver spec CLAUDE.md was already pointing at. Kea
  agented + Windows DHCP agentless, with `AGENTLESS_DRIVERS` /
  `READ_ONLY_DRIVERS` classification.
- README — Windows Server DNS/DHCP feature bullet; Architecture
  section reframed around agented vs agentless split; doc-index
  refreshed.
- [`docs/assets/architecture.svg`](docs/assets/architecture.svg) —
  redrawn. Two-lane data-plane split: agented (`dns-bind9` + `dhcp-kea`
  with sidecar-agent pills) vs agentless (Windows DNS Path A/B,
  Windows DHCP Path A read-only); scheduled-sync arrow from Beat →
  agentless lane.
- [`features/DNS.md`](docs/features/DNS.md) — new §12 "Sync with
  Servers" reconciliation, §13 Windows DNS Path A + B, §14 scheduled
  reconciliation jobs.
- [`features/DHCP.md`](docs/features/DHCP.md) — new §15 Windows DHCP
  Path A.
- [`drivers/DNS_DRIVERS.md`](docs/drivers/DNS_DRIVERS.md) — removed
  orphaned PowerDNS stub. New §3 Windows DNS driver with both paths,
  write-through pattern, shared AXFR helper. Section numbering cleaned
  up (1–6).
- `docs/index.md`, `CLAUDE.md` — document maps point at the new files.

### Fixed

- `ipam.create_space` — return 409 on duplicate `ip_space` name instead
  of letting `UniqueViolationError` surface as a bare 500. Matches the
  pre-check pattern already in DHCP server-group CRUD; demo-seed
  retries are idempotent again.
- `frontend/src/lib/api.ts` — `ipamApi.updateBlock`'s Pick was missing
  `dhcp_server_group_id` and `dhcp_inherit_settings`, so the prod
  `tsc -b && vite build` failed even though dev `tsc --noEmit` passed.
- **Subnet inheritance editing bug** — editing a subnet back to
  "inherit from parent" used to still push records to the previously-
  pinned server. The inheritance walk now goes subnet → block
  ancestors → space and respects `dns_inherit_settings` at every level.
  Same walk applied in `services/dns/sync_check.py`.
- **Login crash on external user group assignment** — LDAP / OIDC /
  SAML logins were throwing `MissingGreenlet` during the group-
  membership replace step of `sync_external_user`. Fixed by:
  (1) adding `AsyncAttrs` mixin to `Base` so models expose
  `awaitable_attrs`; (2) awaiting `user.awaitable_attrs.groups` before
  assigning `user.groups = groups` in
  `backend/app/core/auth/user_sync.py` — SQLAlchemy's collection
  setter computes a diff against the currently-loaded collection, and
  that lazy-load under AsyncSession would otherwise raise.
- **`is_superadmin` vs RBAC wildcard mismatch** —
  externally-provisioned users with the built-in Superadmin role got
  403 from every `require_superadmin`-gated endpoint because the
  legacy `User.is_superadmin` flag defaults False and
  `sync_external_user` never flipped it. `require_superadmin` now
  admits either the legacy flag *or* any user whose groups → roles
  include an `action=*, resource_type=*` permission. Function-local
  import of `user_has_permission` dodges the circular import against
  `app.api.deps`.
- **Dynamic-lease mirrors are read-only** — `auto_from_lease=true` IPs
  now return 409 from update / delete endpoints and are skipped by
  bulk-edit. Prevents manual edits from being overwritten on the next
  lease pull.
- **IP delete cascades to DHCP static reservation** — on Windows the
  FK was set to NULL, orphaning the reservation. Now cascades
  correctly.
- **Tree chevrons** — DHCP / DNS server-group sidebars + VLANs tree
  swapped to the IPAM `[+] / [−]` boxed toggle for consistency. DNS
  zone-tree folder icons left alone.
- **DNS group expand-stuck** — selecting a zone no longer latches its
  group's expanded state.
- **IPAM address list** gains a `tags` column rendering clickable chips
  that populate the tag filter.
- **`seed_demo.py`** creates DNS group + zone and DHCP server group
  *first*, then wires both into the IP Space so blocks/subnets inherit
  by default.

### Security

- **CodeQL alert #13 (CWE-601, URL redirection from remote source)**
  closed. Previous attempts added `_safe_error_suffix()` and then a
  `urlparse`-based sanitiser defence, neither of which CodeQL's taint
  tracker recognises as sanitisers. Replaced with a closed-set
  allowlist: all redirect reasons are now selected from
  `_LOGIN_ERROR_REASONS` (a frozenset of literals); anything else
  becomes `"unknown"`. The three interpolation sites that previously
  threaded IdP error codes / exception reason fields into f-strings
  now pass fixed literals. Actual IdP error strings still land in the
  server log + audit row — only the URL-visible part is generic.

### Changed

- Backend + frontend: `make lint` now mandatory before push —
  CI-mirroring `make ci` target (added in 2026.04.16-3) catches
  formatter drift before it hits GitHub Actions.

---

## 2026.04.16-3 — 2026-04-16

Third same-day iteration. First wave of substantive post-alpha work:
external auth providers (LDAP / OIDC / SAML + RADIUS / TACACS+ with
backup-server failover), group-based RBAC enforced across every API
router, partial IPv6 (storage + UI + Kea Dhcp6), inherited-field
placeholders on edit modals, mobile-responsive layout, IPAM block/subnet
overlap validation, scheduled IPAM↔DNS auto-sync, bulk-edit DNS zone
assignment, shared zone-picker dropdown with primary / additional
grouping, and a `make ci` target that mirrors GitHub Actions locally.

### Added

**Auth — Wave A (external identity providers)**
- `AuthProvider` + `AuthGroupMapping` models with Fernet-encrypted secrets
  (`backend/app/core/crypto.py`). Admin CRUD at `/api/v1/auth-providers`
  with per-type structured forms on `AuthProvidersPage`.
- **LDAP** — `ldap3`-based auth (`backend/app/core/auth/ldap.py`).
  Password-grant fallthrough from `/auth/login`. TLS / LDAPS / StartTLS
  support with optional CA cert path.
- **OIDC** — authorize / callback redirect flow with signed-JWT state+nonce
  cookie, discovery + JWKS caching, `authlib.jose` ID-token validation.
  Login page renders enabled providers as "Sign in with …" buttons.
- **SAML** — `python3-saml` SP flow: HTTP-Redirect AuthnRequest, ACS POST
  binding, `GET /auth/{provider_id}/metadata` for IdP-side SP metadata.
- Unified user sync (`backend/app/core/auth/user_sync.py`) creates / updates
  `User` rows, replaces group membership from group mappings, and
  **rejects logins with no mapping match** (configurable per provider).

**Auth — Wave B (network-device protocols)**
- **RADIUS** — `pyrad` driver (`backend/app/core/auth/radius.py`).
  Built-in minimal dictionary; extra vendor dicts via `dictionary_path`.
  Group info from `Filter-Id` / `Class` by default.
- **TACACS+** — `tacacs_plus` driver (`backend/app/core/auth/tacacs.py`).
  Separate `authorize()` round-trip pulls AV pairs; numeric `priv-lvl`
  values are surfaced as `priv-lvl:N` for group mapping.
- Both share the same password-grant fallthrough as LDAP via
  `PASSWORD_PROVIDER_TYPES = ("ldap", "radius", "tacacs")`.
- Per-provider "Test connection" probe in the admin UI returns
  `{ok, message, details}` for all five provider types.

**Auth — backup-server failover (LDAP / RADIUS / TACACS+)**
- Each password provider's config now accepts an optional list of backup
  hosts (`config.backup_hosts` for LDAP, `config.backup_servers` for
  RADIUS/TACACS+). Entries can be `"host"` or `"host:port"`; bracketed
  IPv6 literals (`[::1]:389`) are supported. The UI adds a "Backup hosts /
  servers" textarea (one per line).
- LDAP uses `ldap3.ServerPool(pool_strategy=FIRST, active=True,
  exhaust=True)` — dead hosts are skipped for the pool's lifetime.
- RADIUS and TACACS+ iterate primary → backups manually. A definitive
  auth answer (Accept / Reject, `valid=True/False`) stops iteration;
  network / timeout / protocol errors fail over to the next server.
- All backups share the primary's shared secret and timeout settings.

**Auth — Wave C (group-based RBAC enforcement)**
- Permission grammar `{action, resource_type, resource_id?}` with wildcard
  support; helpers in `backend/app/core/permissions.py`
  (`user_has_permission`, `require_permission`, `require_any_permission`,
  `require_resource_permission`).
- Five builtin roles seeded at startup: Superadmin, Viewer, IPAM Editor,
  DNS Editor, DHCP Editor.
- `/api/v1/roles` CRUD + expanded `/api/v1/groups` CRUD with role/user
  assignment. Router-level gates applied across IPAM / DNS / DHCP / VLANs
  / custom-fields / settings / audit. Superadmin always bypasses.
- `RolesPage` + `GroupsPage` admin UI. See `docs/PERMISSIONS.md`.

**Auth — Wave D UX polish**
- Per-field opt-in toggles on bulk-edit IPs (status / description / tags /
  custom-fields / DNS zone individually) plus a "replace all tags" mode.
- `EditSubnetModal` + `EditBlockModal` now surface inherited custom-field
  values as HTML `placeholder` with "inherited from block/space `<name>`"
  badges. New `/api/v1/ipam/blocks/{id}/effective-fields` endpoint for
  parity with the existing subnet endpoint.

**IPv6 (partial)**
- `DHCPScope.address_family` column (migration `d7a2b6e9f134`) + Kea
  driver `Dhcp6` branch renders a v6 config bundle from the same scope
  rows. Dhcp6 option-name translation TODO is flagged in
  `backend/app/drivers/dhcp/kea.py`.
- `Subnet.total_ips` widened to `BigInteger` (migration `e3c7b91f2a45`)
  so a `/64` (`2^64` addresses) fits. `_total_ips()` clamps at `2^63 − 1`.
- Subnet create skips the v6 broadcast row; `_sync_dns_record` emits AAAA
  + PTR in `ip6.arpa`.
- `/blocks/{id}/available-subnets` accepts `/8–/128` (was `le=32`) with
  an explicit address-family guard. Frontend "Find by size" splits the
  prefix pool into v4 (`/8–/32`) and v6 (`/32, /40, /44, /48, /52, /56,
  /60, /64, /72, /80, /96, /112, /120, /124, /127, /128`) and dynamically
  filters to prefixes strictly longer than the selected block's prefix.
- `/ipam/addresses/next-address` returns 409 on v6 subnets (EUI-64 / hash
  allocation is a future enhancement).
- IPAM create-block / create-subnet placeholders now include an IPv6
  example next to the IPv4 one (`e.g. 10.0.0.0/8 or 2001:db8::/32`).

**IPAM — block / subnet overlap validation**
- `_assert_no_block_overlap()` rejects same-level duplicates and CIDR
  overlaps in `create_block` and in the reparent path of `update_block`.
  Uses PostgreSQL's `cidr &&` operator for a single-query overlap check.

**IPAM — scheduled IPAM ↔ DNS auto-sync**
- Opt-in Celery beat task `app.tasks.ipam_dns_sync.auto_sync_ipam_dns`
  (`backend/app/tasks/ipam_dns_sync.py`). Beat fires every 60 s; the task
  gates on `PlatformSettings.dns_auto_sync_enabled` +
  `dns_auto_sync_interval_minutes`, so cadence changes in the UI take
  effect without restarting beat. Optional deletion of stale auto-
  generated records (`dns_auto_sync_delete_stale`).
- Settings UI: new **DNS Auto-Sync** section on `/admin/settings`
  (enable / interval / delete-stale toggle).

**IPAM — shared zone picker + bulk-edit DNS zone**
- New `ZoneOptions` component (`frontend/src/pages/ipam/IPAMPage.tsx`)
  renders the primary zone first, then an `<optgroup label="Additional
  zones">` separator. Used in Create / Edit / Bulk-edit IP modals.
- Zone picker is restricted to the subnet's explicit primary + additional
  zones when any are pinned; falls back to every zone in the group only
  when the admin picked a group without pinning specific zones.
- `IPAddressBulkChanges.dns_zone_id` — bulk-editing a set of IPs routes
  every selected address through `_sync_dns_record` for move / create /
  delete.

**IPAM — mobile responsive**
- Sidebar becomes a drawer on `<md` with backdrop + `Header` hamburger
  toggle.
- 10+ data tables wrapped in `overflow-x-auto` with `min-w` so wide
  columns scroll horizontally instead of overflowing the viewport.
- All modals sized `max-w-[95vw]` on `<sm`.

**IPAM — IP aliases polish**
- Adding or deleting an alias now also invalidates
  `["subnet-aliases", subnet_id]`, so switching to the Aliases tab after
  an add/delete no longer shows a stale list.
- Delete alias from the subnet Aliases tab now pops a single-step
  `ConfirmDeleteModal` ("Delete alias `<fqdn>`? The DNS record will be
  removed.") matching the standard IPAM delete flow.

**Developer tooling**
- `make ci` — new Makefile target that runs the exact three lint jobs
  CI runs (`backend-lint`: ruff + black + mypy; `frontend-lint`: eslint +
  prettier + tsc; `frontend-build`: `npm run build`). Backend checks run
  inside the running `api` container; ruff/black/mypy are installed on
  first run if missing.
- `.github/ISSUE_TEMPLATE/{bug_report,feature_request,config}.yml` and
  `.github/pull_request_template.md` — structured issue + PR templates
  with dropdown areas (IPAM / DNS / DHCP / Auth / RBAC / Audit / UI / API
  / Deployment / Docs), repro steps, a private Security Advisory link,
  and a test-plan checklist.

### Changed

- IPAM modal input focus ring switched to `focus:ring-inset` so the 2px
  ring draws inside the border. Prevents horizontal clipping by the
  modal's `overflow-y-auto` container (browsers clamp `overflow-x` when
  `overflow-y` is set), which previously cut the left edge of any focused
  box in the Create / Edit Block / Subnet forms.
- `CLAUDE.md` phase roadmap updated to reflect Waves A–D. Tech-stack Auth
  row now lists actual deps (`python-jose + bcrypt`, `ldap3`, `authlib`,
  `python3-saml`, `pyrad`, `tacacs_plus`, `Fernet`).

### Fixed

- `user_sync._matched_internal_groups` used one `res` variable name for
  two `db.execute()` calls with different result types, tripping mypy
  after the dev extras finally ran in `make ci`. Renamed to `map_res` /
  `group_res`.
- CI lint was still failing on `main` after `f38d533` — residual ruff
  warnings (20) and prettier issues (12 files). Now clean; `make ci`
  passes end-to-end.
- SAML ACS handler: `SAMLResponse` / `RelayState` form fields kept their
  spec-mandated casing; added `# noqa: N803` so ruff stops complaining.

### Security

- CodeQL alert #13 (CWE-601, URL redirection from remote source): the
  OIDC callback interpolated the IdP-provided `error` query parameter
  directly into the `/login?error=…` redirect. The redirect target was
  already a relative path (so no open-redirect in practice) but the
  tainted value still flowed into the URL. Added `_safe_error_suffix()`
  to strip any provider-supplied error code down to `[a-z0-9_]` (max 40
  chars) and applied it at every `f"…_{error}"` / `f"…_{exc.reason}"`
  site in the OIDC and SAML callback handlers.

---

## 2026.04.16-2 — 2026-04-16

First post-alpha iteration — same-day follow-up to the alpha. Adds IP
aliases across the stack, multi-select/bulk ops on the IP address table,
an always-visible per-column filter row on the audit log, a DNS zone
tree that can create sub-zones with a click, and switches the base
Compose file to pull release images from GHCR.

### Added

**IPAM**
- IP aliases — Allocate/Edit IP modal supports extra CNAME/A records tied to the IP. Auto-deleted on IP purge.
- `+N aliases` pill next to the hostname in the subnet IP table when an IP has user-added aliases (new `alias_count` on `IPAddressResponse`).
- New "Aliases" subnet tab listing every CNAME/A alias in the subnet (name · type · target · IP · host · delete). `GET /ipam/subnets/{id}/aliases`.
- Multi-select on the subnet IP table with a bulk-action bar inline on the tab row (no banner push-down). `POST /ipam/addresses/bulk-delete` (soft → orphan or permanent) and `POST /ipam/addresses/bulk-edit` (status, description, tags *merge*, custom_fields *merge*). System rows auto-excluded.
- Reverse-zone backfill — dedicated button on Space / Block / Subnet headers (`POST /ipam/{scope}/{id}/reverse-zones/backfill`). Also backfills opportunistically on every IP allocation.
- DHCP Pool membership column on subnet IP table — cyan/violet/zinc badge per IP shows which pool (dynamic/reserved/excluded) it falls in.
- Bulk orphan cleanup modal on subnet header.
- `IPAddress.auto_from_lease` column distinguishes DHCP-lease-mirrored rows from manual allocations (migration `e2a6f3b8c1d4`).

**DNS**
- Real RPZ blocklist rendering in the BIND9 agent — `response-policy { } break-dnssec yes`, CNAME trigger zone files (nxdomain/sinkhole/redirect/passthru). Wildcards block both apex and subdomains.
- Blocklist entries get a `reason` column (migration `b4d1c9e2f3a7`) and per-entry `is_wildcard` toggle (defaults true).
- Inline edit for blocklist entries + exceptions (`PUT .../entries/{id}`, `PUT .../exceptions/{id}`).
- Blocklist page reorganized into red **Blocked Domains** and green **Allow-list** sections.
- DNS records table: always-visible edit/delete, clickable record name, single-step delete confirm, multi-select bulk delete (IPAM records excluded).
- Zone tree folder click → Create-Zone modal pre-filled with the parent suffix (e.g. clicking `example.com` opens "New zone `*.example.com`"). TLD folders (org/com/net/…) just toggle, don't prompt. Zone names in the tree render without the trailing dot.
- DNS agent re-bootstraps on 404 (not just 401) — recovers from stale server rows.

**DHCP**
- Pool overlap validation + existing-IP warning on pool create.
- Static DHCP ↔ IPAM sync (creates `status=static_dhcp` rows, fires DNS sync on create/update/delete).
- Lease → IPAM mirror: active leases create `dhcp` rows; expired leases remove them (`auto_from_lease` flag only).
- Celery `sweep_expired_leases` task (every 5min) catches missed lease events.
- Force-sync coalesces repeated clicks into one pending op.
- Kea agent: UDP socket mode for relay-only deployments; `/run/kea` perms; lease op acks via heartbeat.
- DHCP scope options default-prefill from Settings (DNS/NTP/domain/lease-time).
- Static assignments moved from DHCP Pools tab into IPAM Allocate IP flow.

**Audit log**
- Per-column filter row on `/admin/audit` — User/Summary/IP text inputs, Action/Resource/Result dropdowns, always visible, Clear-all X in the actions column. Backend adds `resource_display` / `result` / `source_ip` query params.

**Platform**
- Base `docker-compose.yml` now pulls release images from GHCR (`ghcr.io/spatiumddi/spatiumddi-{api,frontend}`, `ghcr.io/spatiumddi/dns-bind9`, `ghcr.io/spatiumddi/dhcp-kea`); pin with `SPATIUMDDI_VERSION=<tag>` in `.env`.
- `docker-compose.dev.yml` is a standalone self-contained file that keeps `build:` stanzas for local dev builds — use `docker compose -f docker-compose.dev.yml …` or `export COMPOSE_FILE=docker-compose.dev.yml`.
- Jekyll docs site config (`docs/_config.yml`, `docs/index.md`).
- CHANGELOG; alpha banner; clickable screenshot thumbnails in README.
- Seed script (`scripts/seed_demo.py`).
- Alembic migrations now tracked in git (were `.gitignore`d — CI was broken).
- `COMPOSE_PROFILES` documented.

### Changed

- `CLAUDE.md` slimmed to a navigational entry point — Phase 1 / Waves 1–5 / DHCP Wave 1 implemented-lists moved to this CHANGELOG; added a Repo Layout section and a Cross-cutting Patterns section (driver abstraction, ConfigBundle+ETag long-poll, agent bootstrap/reconnection).

### Fixed

- Full audit of IPAM/VLANs/DNS/DHCP frontend ↔ backend API contracts; 10+ mismatches fixed.
- `allocate_next_ip` — `FOR UPDATE` on outer join, now `of=Subnet` + `.unique()`.
- Workflow permissions hardened (CodeQL alerts resolved).
- Ruff (import sort, unused `datetime.UTC`), Black (4 files), Prettier (3 files) — unblocked CI.

---

## 2026.04.16-1 — Alpha

First public release. **Alpha quality** — expect rough edges and breaking changes between releases.

### Added

**IPAM**
- Hierarchical IP management: spaces, blocks (nested), subnets, addresses
- Subnet CIDR validation with "Did you mean?" hints
- Next-available IP allocation (sequential / random)
- Soft-delete IP addresses (orphan → restore / purge)
- Bulk orphan cleanup modal on subnet view
- Subnet-by-size search ("Find by size" in create modal)
- Per-column filters on address & block tables
- Drag-and-drop reparenting of blocks and subnets
- Free-space band on block detail with click-to-create
- Import/export (CSV, JSON, XLSX with preview)
- Bulk-edit subnets (tags, custom fields)
- Custom field definitions per resource type
- DNS assignment at space / block / subnet level with inheritance
- IPAM ↔ DNS drift detection and reconciliation (subnet / block / space scope)
- DNS sync indicator column on IP address table
- DHCP pool membership column on IP address table

**DNS**
- Server groups, servers, zones, records — full CRUD
- BIND9 driver with Jinja templates, TSIG-signed RFC 2136 dynamic updates
- Agent runtime: bootstrap (PSK → JWT), long-poll config sync with ETag, on-disk cache
- Container image: `ghcr.io/spatiumddi/dns-bind9` (Alpine 3.22, multi-arch)
- Zone tree with nested sub-zone display
- Zone import/export (RFC 1035 parser, color-coded diff preview)
- Server health checks (heartbeat staleness → SOA fallback)
- ACLs, views, trust anchors
- Blocking lists (RPZ) with feed refresh, bulk-add, exceptions
- Query logging configuration (file / syslog / stderr)
- DNS defaults in Settings (TTL, zone type, DNSSEC, recursion)

**DHCP**
- Kea driver + agent runtime (bootstrap, long-poll, lease tail, local cache)
- Container image: `ghcr.io/spatiumddi/dhcp-kea` (Alpine 3.22, multi-arch)
- Server groups, servers, scopes, pools, static assignments, client classes
- DHCP options editor with NTP (option 42) as first-class field
- Pool overlap validation on create and resize
- Existing-IP-in-range warning on pool creation
- Scope auto-binds to sole server; gateway + settings defaults pre-filled
- Static DHCP ↔ IPAM sync (status=static_dhcp, DNS forward/reverse)
- DHCP defaults in Settings (DNS servers, domain, NTP, lease time)
- UDP socket mode for relay-only deployments (no broadcast / no NET_RAW)

**VLANs**
- Routers and VLANs with full CRUD
- Subnet ↔ VLAN association (router + VLAN columns in IPAM views)
- Delete protection when subnets still reference a VLAN/router

**Auth & Users**
- Local auth with JWT + refresh token rotation
- Forced password change on first login
- User management (create, edit, reset password, delete)

**Platform**
- Dashboard with utilisation stats, top subnets, VLAN/DNS/DHCP status sections
- Global search (Cmd+K / Ctrl+K) across IPs, hostnames, MACs, subnets
- Settings page (branding, allocation, session, DNS/DHCP defaults, utilisation thresholds)
- Audit log viewer with action/result badges and filters
- Docker Compose with `dns` and `dhcp` profiles (`COMPOSE_PROFILES=dns,dhcp`)
- Kubernetes manifests (StatefulSets, services, PVCs)
- GitHub Actions CI (lint, type-check, test) + release workflow (multi-arch images, GitHub Release)

### Security
- Workflow permissions hardened (CodeQL alerts resolved)
- All mutations audited before commit
- Agent re-bootstraps on 401/404 (no stale-token loops)

---

_For the full commit history, see the [GitHub compare view](https://github.com/spatiumddi/spatiumddi/commits/main)._
