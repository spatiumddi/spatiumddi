# Changelog

All notable changes to SpatiumDDI are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/); versioning uses [CalVer](https://calver.org/) (`YYYY.MM.DD-N`).

This file is hard-wrapped at ~70 chars for terminal reading. The
release workflow runs each section through `scripts/format_release_
notes.py` before pasting it into the GitHub release body, which:

* unwraps consecutive prose lines into single-line paragraphs (since
  GitHub's release renderer turns every `\n` into a forced `<br>`),
* wraps the top summary paragraph in `### 🚀 Highlights`,
* emoji-prefixes the standard section headings (`### ✨ Added`,
  `### 🔧 Changed`, `### 🐛 Fixed`, `### 🔒 Security`,
  `### 🗃️ Migrations`, `### ⚠️ Deprecated`, `### 💥 Breaking`).

So author each section here with the plain Keep-a-Changelog headings
(`### Added`, `### Changed`, …) and a hard-wrapped summary paragraph;
the formatter handles the rest.

---

## 2026.07.11-1 — 2026-07-11

**The firewall release.** SpatiumDDI has always been a read-only
observer of the devices it mirrors. This release closes the
detect→block loop: it could already *see* a rogue DHCP responder
(#370) or an unknown MAC (#459) and starve it of a DHCP lease, but a
device that self-assigned a static IP walked straight past the block.
**Active block sync** (#601) adds a narrow, heavily guarded write path
that pushes a real block at the natural enforcement point — an
OPNsense firewall table alias (by IP) or a UniFi L2 client quarantine
(by MAC) — so the block actually sticks. On top of it lands an
**enterprise-firewall family**: Palo Alto PAN-OS / Panorama (#605) as
the reference vendor, then Fortinet FortiGate and Cisco Meraki MX
(#606). Each contributes a read-only mirror of the firewall's "shadow
IPAM" into native rows plus its own *native* enforcement primitive —
and one of them inverts the credential model entirely.

Active enforcement is the deliberate exception to the
read-only-mirror stance. Every write path here is off by default and
layered behind a feature module, a per-target enforcement master
switch, distinct write-scoped credentials, preview + audit on every
push, a dedicated RBAC permission, and two-person approval (#62).

Also in this release: a **NetBird** mesh mirror (#603), **post-wake
verification** for Wake-on-LAN (#596), **DNS-standard name
validation** across IPAM / DNS / DHCP (#597), and a four-bug
correctness pass on **DHCP scope deletion** (#616–#619).

### Added

- **Active block sync — write-back enforcement (#601).** A new
  `security.block_sync` feature module (default-off) exposes a
  SpatiumDDI-owned block set (`network_block`: IP or MAC, with
  reason / source / optional auto-expiry) that a target-driven,
  idempotent reconciler converges onto every *armed* OPNsense /
  UniFi target and lifts when a block is disabled / expired /
  deleted. Convergence is non-destructive — SpatiumDDI only removes
  values it added (tracked in `network_block_push`), never alias
  members / blocked clients it doesn't own.
  - **OPNsense** enforcement mutates operator-pre-created firewall
    **table-alias** membership only (`alias_util/add|delete` +
    `alias/reconfigure`) — never rule CRUD.
  - **UniFi** enforcement issues the legacy `cmd/stamgr`
    `block-sta` / `unblock-sta` L2 quarantine (the public
    Integration v1 API doesn't expose client-block), replaying the
    captured `X-CSRF-Token` / `X-API-Key`.
  - REST surface at `/api/v1/block-sync` (blocks CRUD with a
    per-target preview diff, target arm/creds, force-reconcile,
    password-confirm credential reveal); a 60 s beat sweep plus an
    immediate on-create/lift converge.
  - The New Devices review-queue **Block** action grows an "also
    quarantine upstream" option that creates a MAC block when a
    UniFi target is armed.
  - Guardrails: per-target default-off `block_sync_enabled` master
    switch (independent of the mirror module), distinct
    Fernet-encrypted write-scoped creds, `manage_block_sync`
    permission (granted to Network Editor), full audit + two-person
    approval (#62) on every create, and `find_network_blocks` /
    `count_network_blocks` + a default-disabled
    `propose_create_network_block` Operator Copilot tool.
- **Palo Alto PAN-OS / Panorama read-only mirror (#605).** New
  `integrations.paloalto` feature module (default-off). One
  `PANOSFirewall` row per managed scope — a standalone NGFW `vsys` or
  a Panorama `device-group`. A 30 s beat sweep with per-firewall
  interval gating reconciles:
  - **Address objects + groups → a new `firewall_endpoint_object`
    mirror** (the "shadow IPAM" store, named to not collide with the
    appliance's own #285 firewall models), each resolved to a live
    IPAM `ip_address` / `subnet` where the value matches, with a
    two-way **drift report** (`GET /paloalto/firewalls/{id}/drift`):
    objects with no IPAM row + subnets with no object.
  - **NAT rules → `nat_mapping` rows** with `panos_firewall_id`
    provenance — making the previously manual-only NAT table a live
    data source.
  - **Zones/interfaces → IPAM subnets** and **DHCP-server leases →
    IPAM addresses** (both opt-in).
  - REST client (`X-PAN-KEY`, `/restapi/v{ver}/Objects|Policies/…`)
    for objects/NAT + legacy XML API (`type=keygen` / `op` /
    `user-id`) for key minting, op-commands, and DAG tag register.
    Test-connection mints a key from admin creds via `type=keygen`
    or validates a pasted key.
  - Admin page at `/paloalto`; both dashboard surfaces (IPAM-tab
    `IntegrationsPanel` + the Integrations dashboard tab); MCP
    `list_panos_targets` / `find_firewall_objects` /
    `count_firewall_objects` read tools.
- **Palo Alto Dynamic Address Group enforcement (the #601 tier).**
  `paloalto` becomes a new Active-block-sync target kind: IP-kind
  blocks register an `IP → tag` mapping via the PAN-OS **User-ID
  API** with no policy commit; an operator-pre-created DAG matching
  the tag enforces it near-instantly. Convergence reads on-device
  state (`show object registered-ip`) so it adds only what's missing
  and unregisters only what it owns. Guardrails on top of the shared
  block-sync gates: targets a standalone firewall vsys (arming a
  Panorama target 422s), a distinct User-ID-capable write key
  (Fernet-encrypted, reveal behind re-auth), and a new
  **`manage_firewall_enforcement`** permission required to arm (an
  off-prem, broad-blast-radius write). Blocks still flow through the
  existing `network_block` desired-state + two-person approval, so
  `propose_create_network_block` already covers the PAN write path.
- **Fortinet FortiGate read-only mirror (#606).** New
  `integrations.fortinet` feature module (default-off). One
  `FortinetFirewall` row per FortiGate VDOM, driven over the FortiOS
  REST API (bearer-token auth, Fernet-encrypted at rest). A 30 s beat
  sweep with per-firewall interval gating mirrors address
  objects/groups → `firewall_endpoint_object`, VIPs (destination NAT)
  → `nat_mapping` provenance rows, and — opt-in — interface CIDRs →
  IPAM subnets + DHCP leases → IPAM addresses. Two-way IPAM drift
  report (objects with no IPAM row; in-space subnets no object
  covers). CRUD + test-connection + Sync-now + objects + drift at
  `/api/v1/fortinet`. Fleet-scoped page under Integrations →
  Fortinet.
- **Fortinet Threat-Feed enforcement — the "feed inversion" (#606).**
  New `security.firewall_feeds` module (default-on, discovery-only).
  A `FirewallFeed` row exposes a token-scoped URL
  (`GET /api/v1/firewall-feeds/feeds/{id}/blocklist.txt`) that
  renders the `NetworkBlock` set (the same intent Active block sync
  #601 pushes) as plain text, one IP/CIDR per line. A FortiGate
  External Threat Feed (or a Cisco Security-Intelligence feed) polls
  it — so **SpatiumDDI holds no write credentials on the firewall at
  all**. The token is Fernet-encrypted, revealed once on create + via
  a password-confirmed reveal, and rotatable; the public poll
  endpoint is authed purely by the token and stamps poll telemetry
  (`last_polled_at` / `_ip` / `poll_count`). Feeds page under
  Security.
- **Cisco Meraki MX read-only mirror (#606).** New
  `integrations.meraki` feature module (default-off). One `MerakiOrg`
  row per Meraki organization, driven over the cloud Dashboard API
  (API key + org id, nothing on-prem, Fernet-encrypted at rest).
  Mirrors per-network appliance VLANs → IPAM subnets, DHCP fixed-IP
  reservations → IPAM addresses, org policy objects/groups →
  `firewall_endpoint_object`, MX 1:1 NAT + port-forward →
  `nat_mapping`, and — opt-in — network clients → IPAM addresses.
  Rate-limit-aware (429 + `Retry-After`) with a slower 300 s default
  cadence. CRUD + test + Sync-now + objects + drift at
  `/api/v1/meraki`. Page under Integrations → Meraki.
- **Meraki per-client Blocked enforcement (the #601 tier).** A new
  `meraki` block-sync target kind (consumes `mac` blocks): the
  reconciler resolves a blocked MAC to its (network, client) across
  the org's appliance networks and moves it to the built-in `Blocked`
  device policy via the Dashboard API — the cloud applies it
  immediately, no on-prem deploy. Distinct write-scoped key,
  default-off per-target master switch, gated by the
  `security.block_sync` module + the `manage_firewall_enforcement`
  permission + two-person approval (#62), previewable + audited.
  Armed from the Active block sync page.
- **NetBird mesh mirror (#603).** New `integrations.netbird` feature
  module (default-off) — a read-only mirror for NetBird (managed
  WireGuard mesh), following the Tailscale shape. NetBird's real
  management API is what makes it a legitimate pull mirror, unlike
  raw WireGuard. **Phase 1** mirrors each peer's overlay IP into the
  bound IPAM space (`netbird-peer` status; OS / version / groups /
  connection state in custom fields) under an auto-created overlay
  block + subnet, with the owned-row + `user_modified_at` lock model.
  **Phase 2** adds an optional synthetic read-only DNS zone for the
  mesh domain (A records), with 422 write-guards blocking operator
  edits. Per-instance operator-supplied management URL + `verify_tls`
  toggle (SSRF-guarded at the test-connection boundary, since the
  host isn't fixed), Token auth, and a complete cross-integration
  ownership guard — NetBird and Tailscale both default to the
  `100.64.0.0/10` CGNAT range, so each reconciler now refuses to
  claim the other's rows. Management page + sidebar entry + both
  dashboard surfaces; `list_netbird_targets` MCP tool.
- **Wake-on-LAN post-wake verification (#596).** The follow-up to
  #586 Phase 3's ping-only verify. Post-wake liveness now resolves
  from multiple sources (`ping` / `tcp` / `seen` / `auto`), ad-hoc
  single-host wakes (`POST /ipam/addresses/{id}/wake`) opt into the
  same verify + bounded re-wake chain scheduled runs get (via a
  per-run `verify_params` snapshot), and every target carries a
  **verify evidence trail** — an ordered record of what each source
  actually said, so "down according to what?" has an answer ("ping
  timed out, TCP refused nothing, last seen 3 days ago" is a dead
  box; "ping timed out, TCP connected" is a contradiction worth
  investigating). New `wol_wake_failed` alert rule (seeded off) opens
  one event per schedule whose latest finalised run left hosts
  unconfirmed, with a per-schedule `verify_alert_enabled` mute so one
  deliberately-noisy lab schedule doesn't force the whole rule off.
  `find_wol_wake_failures` / `count_wol_wake_failures` MCP tools.
- **DNS-standard name validation across IPAM / DNS / DHCP (#597).**
  User-supplied name fields accepted arbitrary strings up to their
  column length — spaces, uppercase, unicode, leading/trailing
  hyphens, consecutive dots, and even embedded newlines all persisted
  verbatim. There is no single "valid DNS name" rule, so validation
  is per-context: host names (IPAM hostname, DHCP reservation) follow
  RFC 1123 LDH; DNS record owners follow RFC 2181, which permits `_`
  and `*` so `_acme-challenge`, `_443._tcp` SRV/TLSA owners, and
  `*.example.com` stay legal; FQDNs (zone name, `domain-name` option)
  are dotted RFC 2181 labels. A new shared `app/core/dns_names.py`
  holds every rule in one place and returns the normalized value
  (lowercased; unicode → IDNA `xn--` A-label). Client-supplied DHCP
  hostnames are folded to LDH at ingress, and the BIND9 + PowerDNS
  drivers strip control characters from every rendered name + rdata
  as defense in depth. **Back-compat: validate-on-write only —
  existing rows are never auto-mutated.** A read-only
  `GET /diagnostics/name-conformance` (superadmin) +
  `find_nonconforming_names` MCP tool surface pre-existing violations
  so operators fix them deliberately.

### Changed

- **Shared firewall-mirror engine.** The address-object / NAT /
  interface-subnet / DHCP-lease "shadow IPAM" mirror logic #605
  shipped inline in the PAN-OS reconciler is lifted into
  `app/services/firewall_mirror.py`, parameterized by a
  `FirewallOwner`. All three vendor reconcilers (PAN-OS migrated onto
  it, Fortinet + Meraki new) converge through one implementation, so
  there's a single place to fix mirror bugs. The PAN-OS reconcile
  behaviour is unchanged (its test suite pins it).
- `firewall_endpoint_object` generalized from a single
  `panos_firewall_id` owner to one-of-three vendor owners (PAN-OS /
  Fortinet / Meraki), enforced by a `num_nonnulls(...) = 1` CHECK.
  The eight pre-existing integration reconcilers' ownership guards
  learned the two new provenance columns so no mirror claims
  another's rows.
- `find_firewall_objects` / `count_firewall_objects` are now
  vendor-neutral — they query the shadow-IPAM store across Palo Alto
  / Fortinet / Meraki, filterable by `source_kind`.
- `DHCPScope.pools` / `.statics` switch from `lazy="joined"` to
  `lazy="selectin"`. As joined *collections* they forced every
  `select(DHCPScope)` to remember `.unique()` or raise at runtime —
  and several didn't. `selectin` also fixes child filtering: the
  global soft-delete filter registers `propagate_to_loaders=False`,
  so a joined child rode the parent's statement straight past it.
- Failed API mutations in the IPAM allocate/edit-address modals and
  the DNS blocklist modal now render through the shared
  `formatApiError` helper instead of dumping the raw response body,
  so a rejected hostname surfaces as its message rather than a raw
  Pydantic 422 `detail` array (#607). Newly visible now that #597
  rejects malformed hostnames server-side.
- CI: the backend test matrix fans out across 8 `pytest-split` shards
  instead of 4 (each still `-n auto` across the runner's vCPUs) as
  the suite has grown. The required-status-check aggregator name
  `Backend — Tests` is unchanged, so branch protection needs no
  update (#599).

### Fixed

- **DHCP scope deletion — agentless write-through never fired on the
  soft path (#616).** `push_scope_delete` only ran on the
  `permanent=true` branch and the UI never sends that flag, so a
  scope deleted from the UI vanished from SpatiumDDI and from Kea's
  rendered config while a **Windows DHCP server kept serving it — and
  its reservations — forever**. The push now fires on the soft path
  too, driven off the soft-delete batch, so every ancestor whose
  cascade can reach a scope (scope, subnet, block, space) is covered.
  Restore pushes the inverse, best-effort so an unreachable Windows
  box can't make a row unrestorable.
- **A DHCP scope was treated as a cascade leaf (#617).** `DHCPScope`
  has been soft-deletable since `c1f4a8b27d09`, but the cascade walk
  had no branch for it, so its pools and reservations were left live
  and un-stamped under a hidden parent — still answering the statics
  list, still enforcing the group-wide MAC conflict check (which 409'd
  naming a scope UUID the operator could no longer see), still
  visible to the `find_dhcp_statics` MCP tool, and reported as a zero
  blast radius by the approval preview. `DHCPPool` +
  `DHCPStaticAssignment` now carry `SoftDeleteMixin` and ride the
  scope's `deletion_batch_id`, so a restore brings the scope back
  whole.
- **The IPAM mirror was stranded on wholesale deletes (#618).**
  `_detach_ipam_for_static` was only reachable from the
  per-reservation handlers, so every path that destroys reservations
  in bulk (FK CASCADE or Core DELETE — no Python) left `ip_address`
  rows at `status="static_dhcp"` pointing at reservations Postgres had
  already dropped: not allocated, not free, not reclaimable. Hoisted
  to `services/dhcp/static_ipam.py` and wired into scope
  permanent-delete, trash permanent-delete, the purge sweep,
  server-group delete, and the DHCP importer's overwrite. The
  migration repairs already-stranded rows.
- **DHCP reservation validation gaps (#619).** A body `scope_id` was
  silently dropped with a 200 (Pydantic `extra="ignore"`), so a caller
  re-pointing a reservation got no error and no effect; it now 422s.
  Nothing validated that a reservation's IP fell inside its scope's
  subnet, even though Kea renders it nested inside that subnet's
  stanza — an out-of-CIDR reservation shipped structurally invalid
  config to the agent.
- **A readable sole etcd member must apply its peer-rule-less body
  (#610).** The #593 self-partition guard conflated two empties:
  membership *unreadable* and membership *readable with no other
  member*. A fresh single-node seed is a live etcd member whose
  correct firewall body has no peer rule, so the guard refused it
  every heartbeat and pinned the k3s-bootstrap firewall forever — the
  80/443 web-UI accepts never applied and **the appliance API/UI was
  unreachable from off-box** while `/health/*` stayed 200 on
  localhost. `observed_peer_cidrs()` now distinguishes the two
  (`None` = unreadable, `[]` = readable sole member) and the guard
  refuses only when membership is unknown or live peers exist without
  a rule.
- **A self-partition refusal that fails to persist is no longer
  silent (#611).** A real write failure (read-only fs / ENOSPC / a
  truly-missing mount) was swallowed at `log.debug` — the one path by
  which a refusal could go invisible to the host-side console reader.
  Elevated to `log.warning` with the target path.

### Migrations

Eight migrations, chained
`c9a2f61e740b → a71e5c30d9f4 → e4b8073af215 → c9a4e1f7b820 →
d3b9f42a1c05 → f4a1c9e072d5 → a7c3e91f4d28 → b3e7d21c9f04`:

- `c9a2f61e740b` — `wol_run.verify_params`, the per-run verify config
  snapshot that lets an ad-hoc wake carry the operator's chosen
  method / wait / retries (scheduled runs leave it NULL and keep
  reading their live `wol_schedule` row).
- `a71e5c30d9f4` — `wol_schedule.verify_alert_enabled`, the
  per-schedule mute for the new `wol_wake_failed` rule (defaults
  `true`; changes nothing until the rule itself is enabled).
- `e4b8073af215` — `wol_run_target.verify_evidence`, the ordered
  JSONB liveness-evidence array (one entry per source consulted).
- `c9a4e1f7b820` — `netbird_instance` table; `netbird_instance_id` FK
  on `ip_address` / `ip_block` / `subnet` (Phase 1) and `dns_zone` /
  `dns_record` (Phase 2), all ON DELETE CASCADE; the settings toggle
  and the seeded (disabled) `integrations.netbird` feature-module row.
- `d3b9f42a1c05` — `network_block` + `network_block_push` tables,
  block-sync enforcement columns on `opnsense_router` /
  `unifi_controller`, and the seeded (disabled) `security.block_sync`
  feature-module row.
- `f4a1c9e072d5` — `panos_firewall` + `firewall_endpoint_object`
  tables, `panos_firewall_id` FK on `ip_address` / `ip_block` /
  `subnet` / `nat_mapping`, the `integration_panos_enabled` platform
  setting, and the seeded (disabled) `integrations.paloalto`
  feature-module row.
- `a7c3e91f4d28` — creates `fortinet_firewall`, `meraki_org`, and
  `firewall_feed`; adds `fortinet_firewall_id` + `meraki_org_id`
  provenance FKs (ON DELETE CASCADE) to `firewall_endpoint_object`
  (dropping the NOT NULL on `panos_firewall_id` + adding the
  one-owner CHECK) and to `ip_address` / `ip_block` / `subnet` /
  `nat_mapping`; adds the two `integration_*_enabled` settings
  toggles; seeds the `integrations.fortinet` / `integrations.meraki`
  (off) + `security.firewall_feeds` (on) feature-module rows.
- `b3e7d21c9f04` — soft-delete columns on `dhcp_pool` +
  `dhcp_static_assignment` so a scope's cascade reaches them (#617),
  and a data repair for the `ip_address` rows an earlier hard-delete
  stranded at `status="static_dhcp"` (#618).

### Security

- Two new RBAC permissions gate the write paths. `manage_block_sync`
  (arm an OPNsense / UniFi block-sync target) is granted to the
  `Network Editor` builtin role. `manage_firewall_enforcement` — arm
  a Palo Alto DAG or Meraki client-block target, which pushes to a
  vendor cloud / User-ID surface with a separate write-scoped
  credential — is required *in addition to* `manage_block_sync` and
  is deliberately granted to **no builtin role at all**: only
  Superadmin (which bypasses every check) has it out of the box, so
  an operator must grant it on purpose.
- The `security.firewall_feeds` poll endpoint is authenticated purely
  by a Fernet-encrypted, rotatable bearer token and returns only the
  block list — it is the one deliberately unauthenticated-by-session
  surface, and it is read-only.
- #597's name validation closes an injection-adjacent gap: embedded
  newlines and control characters in hostnames / record owners
  previously persisted verbatim and reached the BIND9 + PowerDNS
  config renderers. Both drivers now strip control characters at the
  render boundary regardless of row provenance.

---

## 2026.07.09-1 — 2026-07-09

A **BGP visibility + HA-hardening** release. The headline feature is
the **BGP Looking Glass** — a receive-only GoBGP collector that peers
with your routers, ingests their live Adj-RIB-In, and links every
learned prefix back into IPAM, RPKI-validated. It ships complete
(Phases 1–6): Sessions + Routes grids with clickable peer / route
detail modals, IPAM + ASN + VRF linkage, six ``bgp_lg_*`` alert rule
types with a dashboard health card, an as-path-regexp Query tab with
collector-vantage ping / traceroute, and VPNv4 / VPNv6 Route-Target
matching. Riding with it, the long-deferred **MetalLB BGP mode** lights
up, so the appliance can advertise its control-plane VIP to the same
routers it peers with. Second feature: **Scheduled Wake-on-LAN** turns
the one-shot wake (#533) into a recurring, tag-targeted, calendar-aware
fleet wake that **verifies** which hosts actually booted and re-wakes
the ones that didn't — built for schools, labs, and re-imaged
workstation fleets.

The rest of the release is an **HA-hardening arc** driven by pulling
the power on a node of a real 3-node appliance. A cluster that was HA
at every documented layer still lost its API for five hours, and the
documented dead-node replace flow could not restore a 3/3 cluster.
Eight independent defects are fixed: Redis replicas silently stacked on
one node and then bricked on a torn AOF tail; Sentinel accumulated a
ghost per node loss until failover became arithmetically impossible;
api / worker / frontend had no pod spread; CNPG stacked the primary and
a replica together; a k3s re-join wiped the identity of a node that had
already joined; a failed re-join pushed its single-node manifests into
the shared cluster and uninstalled the CloudNativePG operator; firstboot
re-seeded those manifests on every boot of a joined member; and a
node's own firewall could partition it out of an etcd cluster it was
still a voting member of. Plus a headless-install fix that mattered
more than its size: a fully-preseeded install completed to disk and then
waited forever on a "Press OK to reboot" dialog no one was there to
dismiss.

The Operator Copilot tool registry grows by 19 (8 Looking Glass, 11
Wake-on-LAN).

### Added

* **#566 — BGP Looking Glass.** A receive-only GoBGP collector
  (``ghcr.io/spatiumddi/looking-glass``, multi-arch) peers with your
  routers, ingests the live Adj-RIB-In, and links every learned prefix /
  origin ASN / community back into IPAM. It **never advertises routes to
  your network**: a global ``default-export-policy: reject-route`` plus a
  per-peer max-prefix cap are rendered into the daemon config and
  re-asserted at runtime by ``_assert_receive_only`` and a live leak
  test. Behind the default-on ``network.looking_glass`` feature module.
  Six phases ship together:

  * **Sessions + Routes** grids, each row clickable into a rich detail
    modal. The **peer modal** rolls up session runtime, matched ASN /
    device links, an RPKI stacked meter, top origin ASNs + communities,
    and any open ``bgp_lg_*`` alerts. The **route modal** shows every
    path for a prefix across all routers with divergent-attribute
    highlighting and a best-path winner heuristic, headlined as a
    *possible hijack / route leak* when one prefix arrives from two
    distinct origin ASNs, or as an *anycast / multi-homed* candidate
    when it arrives from several routers with one origin.
  * **IPAM linkage at ingest** — a TTL-cached longest-prefix-match
    resolver stamps ``matched_{block,subnet,space,asn,vrf}_id`` on every
    route, with a 5-minute re-resolve sweep catching IPAM edits made
    between RIB pushes. Surfaces as advertised-prefix chips on subnet /
    block rows, BGP panels in the subnet / block detail, an IP
    reverse-lookup section, and *Learned Routes* tabs on ASN + VRF.
  * **Six ``bgp_lg_*`` alert rule types**, all seeded disabled:
    ``session_down``, ``rpki_invalid_route``, ``unexpected_origin``,
    ``more_specific`` (both joined against the #527 tracked-prefix
    table), ``route_flap``, and ``missing_advertisement`` (a subnet
    flagged ``bgp_should_advertise`` with no covering route). Plus a
    Looking Glass health card on the dashboard.
  * **Query tab** — Cisco / Juniper as-path regexps compiled to Postgres
    POSIX ERE over the rendered as-path, ``show route <prefix>``,
    community lookup, and session history. Collector-vantage
    ``ping`` / ``traceroute`` / ``dig`` / port-test / TLS-cert tools
    dispatch through the existing agent-command channel, so you can probe
    from the collector's network position.
  * **VPNv4 / VPNv6** accepted as peer address-families; the route
    identity key widens to include the route distinguisher —
    ``(peer_id, prefix, next_hop, route_distinguisher)`` — so
    overlapping-prefix VPNs through one peer can't collide.
    Route-Targets parsed from ext-communities match
    against a VRF's import / export target lists, taking precedence over
    the IPAM-effective VRF. Plus a multicast ↔ BGP reachability
    cross-reference tab.

  Eight MCP tools (7 read + ``propose_create_lg_peer``). Migrations
  ``cb279a6afd70`` / ``531494dbf44c`` / ``f7883fa6d413``. Per-vendor
  peering recipes (Cisco IOS / IOS-XR, Juniper, Arista, FRR, BIRD) and
  TCP/179 firewall rules in ``docs/features/LOOKING_GLASS.md``.

* **#566 D1 — MetalLB BGP mode.** The deferred VIP advertiser lights up:
  the appliance can advertise its control-plane VIP to the operator's
  routers over BGP, which is also the cleanest proof that the router will
  peer with the box at all. New ``platform_settings`` BGP columns
  (migration ``1440e72b9297``), a BGP form under Fleet → Network & Host,
  supervisor-driven ``HelmChartConfig`` overrides, and a Looking Glass
  peer form that can prefill from the MetalLB peer. **Default stays L2
  mode** — ``bgp.enabled`` and ``frrk8s.enabled`` are both false. Turning
  BGP mode on pulls in **FRRouting (GPL-2.0)**, the first GPL-v2
  component in the data path; it is opt-in only, bundled as an unmodified
  upstream image rather than linked, and recorded in ``NOTICE``.

* **#586 — Scheduled Wake-on-LAN.** The one-shot Wake-on-LAN from #533
  becomes a recurring, tag-targeted fleet wake, behind the
  ``tools.wake_scheduler`` feature module. Three phases:

  * **Schedule + resolve + dispatch.** DST-safe timezone-aware cron
    scheduling with a built-in holiday gate (blackout dates + term
    range). Targets resolve by ``address_tags`` / subnet / ``subnet_tags``
    / explicit hosts, with a MAC fallback chain (IP → ``ip_mac_history``
    → DHCP lease); MAC-less matches are reported as skipped rather than
    silently dropped, multicast is excluded, and fan-out is hard-capped.
    The beat sweep claims a due schedule with an atomic
    ``UPDATE … RETURNING`` plus an ``in_progress_since`` lease reaper, so
    overlapping ticks can't double-fire.
  * **Calendar subscriptions.** An iCal ``.ics`` URL (Google, published
    school calendars) or authenticated CalDAV (Nextcloud, Radicale)
    drives the gate, so a fleet wake follows the term calendar instead of
    hand-maintained blackout dates. Both ``skip_on_event`` (holiday
    calendar) and ``only_on_event`` (school-day calendar) modes, with an
    optional summary / category match regex. All-day events are flattened
    with the DTEND-exclusive correction; RRULE / RDATE / EXDATE expansion
    is bounded. Every feed URL runs through ``assert_safe_target`` with
    per-redirect-hop revalidation, and sync errors are generic so they
    can't act as a content-disclosure oracle. CalDAV passwords are
    Fernet-encrypted and write-only.
  * **Post-wake verify + retry.** After a run, each sent host is probed
    for liveness (bounded concurrency, never-raise), responders stamp
    ``IPAddress.last_seen_at`` into the existing *Seen* infrastructure,
    and a chained Celery task re-wakes **only** the non-responders up to
    ``verify_retries``. A ``verify_claimed_at`` lease plus an
    attempt-anchored claim makes an ``acks_late`` redelivery a no-op, and
    a reaper folded into the schedule sweep reclaims a run wedged at
    ``verifying`` by a worker crash. Stagger auto-tunes for large fleets.

  Eleven MCP tools (8 read + 3 propose). Migrations ``e9c47a1f3b28`` /
  ``d7e3f0a91c24`` / ``b4f2a9c17e63``. New deps ``icalendar``, ``caldav``,
  ``python-dateutil``. A Tools → Wake Schedules page with a per-schedule
  detail modal, and a FOG / PXE re-image runbook plus a per-vendor
  directed-broadcast matrix in ``docs/features/IPAM.md``. Deferred:
  auto-resolving an on-segment appliance vantage (agents don't advertise
  L2 segments cleanly), and a BMC / Redfish scheduled-shutdown companion.

* **#581 — ``spatium-install --check-preseed``.** An unprivileged,
  read-only, touch-nothing linter that validates a
  ``spatium-preseed.yaml`` on any machine before you boot the installer
  media. It parses through the **same** helper a real headless install
  uses, then re-runs the wizard's **own** rules against the resolved
  values and reports every error in one pass. Machine-specific checks
  (disk presence, the 32 GiB floor) downgrade to warnings so the lint
  stays host-portable. Exit 0 valid / 1 invalid / 2 usage. The pure
  core of the k3s CIDR validator is split out and shared with the
  interactive path, so the two rules cannot drift. Its real value is
  catching, offline and pre-boot, the static-network cases the parser
  accepts but a real install only rejects at the interactive re-check —
  i.e. after the operator has already committed the boot. Adds 61
  host-portable tests to a disk-wiping feature that shipped with none.

### Changed

* **#575 — ``kube-rbac-proxy`` repointed off the sunset
  ``gcr.io/kubebuilder`` mirror** to its maintained upstream home
  ``quay.io/brancz`` (same image, same ``v0.12.0`` tag). Google retired
  the mirror, which broke both the appliance image bake (an unconditional
  ``docker pull`` of every third-party image) and any non-airgap frr-k8s
  pull, where the metrics sidecar hit ``ErrImagePull``. Repointed in
  lock-step across ``bake-images.sh`` and the
  ``spatiumddi-metallb`` chart values.

* **#592 — the console Pods panel now shows the whole cluster.** It
  auto-filtered to "problem pods + pods on **this** node" above 20 visible
  pods; a 3-node appliance idles at ~22, so the filter was effectively
  always on for exactly the deployment where cluster-wide visibility
  matters most. Each node's console showed only its own third of the
  cluster while ``kubectl get pods -A`` showed everything, and the only
  hint was a dim suffix on the panel title. The filter also protected
  nothing — pods are sorted by problem-priority *before* the height
  clamp, so a crash-looping pod cannot be clipped, and the overflow
  already becomes a "… +N more pods · F3 to list" subtitle. Dropped, with
  its now-dead plumbing.

### Fixed

* **#590 — a 3-node HA cluster did not survive hard power loss of one
  node.** Every layer that was supposed to be HA did its job — k3s stayed
  up, etcd kept quorum, CNPG failed Postgres over cleanly — yet the API
  went down cluster-wide and stayed down for 5+ hours. Eight independent
  defects:

  * **Redis placement.** The Sentinel StatefulSet had *preferred*
    (best-effort) anti-affinity, which is silently useless here: the seed
    installs with one replica and promote scales to the control-plane
    size, so ``redis-1`` schedules while the freshly promoted members may
    not yet carry the control-plane node label. The seed is then the only
    schedulable node, and *preferred* cheerfully stacks ``redis-1`` beside
    ``redis-0`` — where ``persistence.enabled`` pins its local-path PV
    forever. One node loss took 2 of 3 replicas: both the data majority
    and the sentinel quorum. Now *required*.
  * **Redis AOF durability.** ``aof-load-corrupt-tail-max-size`` defaults
    to 0, so *any* corrupt tail is fatal — and a power cut tearing the
    last write is expected, not exceptional. A replica crash-looped
    forever over 158 bytes of expendable cache. Redis here is the cache +
    Celery broker and Postgres is the store of record, so a torn tail is
    now discarded (16 MiB budget). This is **not** redundant with
    ``aof-load-truncated``: a *short* tail (ends mid-record) is already
    handled by that knob, while a *corrupt* one (present but zero-filled,
    via ext4 delayed allocation) is what bricks redis. Spelled out
    wherever the value appears, so a future reader doesn't drop it.
  * **Sentinel ghosts.** Every Redis pod announced itself by pod IP, so a
    rescheduled pod returned with a new IP and its peers added a *second*
    entry — and Sentinel never forgets a Sentinel it has seen. Dead ghosts
    stay in the failover-quorum denominator and never vote: one ghost per
    node loss, and on the **third** loss a failover becomes arithmetically
    impossible, the ``sentinel://`` URL never resolves a master again, and
    the API is down permanently. Pods now announce their stable
    StatefulSet FQDN, so a returning pod's hello replaces its entry in
    place. No manual step on upgrade — the init container rewrites
    ``sentinel.conf`` on every pod start.
  * **Control-plane pod spread.** api / worker / frontend shipped no
    affinity and the default 300 s not-ready toleration, so a promote
    could stack every api pod on the seed and losing it left every node
    answering 502 for minutes. They now share a ``podAntiAffinity`` helper
    (``soft`` chart default, ``hard`` on the appliance where replicas
    track node count exactly) and get 20 s tolerations. The helper
    **merges** an operator-supplied affinity rather than replacing it.
  * **Rollout deadlock under the new anti-affinity.** A replica count
    equal to the number of eligible nodes, plus *required* anti-affinity,
    makes the default surge
    pod unschedulable, and the default ``maxUnavailable: 0`` then forbids
    freeing a node — the rollout can never converge. Under ``hard`` the
    knobs invert: retire one old pod, then schedule its replacement onto
    the node it just freed. ``frontend`` gets the same treatment for its
    exclusive ``hostPort :80`` (its old ``strategy: Recreate`` took the
    whole UI down on a chart upgrade once it ran one replica per node).
  * **CNPG Postgres spread.** CNPG enables pod anti-affinity by default,
    but only as *preferred*, so after a promote the primary and a replica
    both landed on the seed — losing that node would have taken both.
    The appliance renders *required*; the chart default stays *preferred*
    for BYO-Kubernetes installs that may run more instances than nodes.
    Because the Cluster CR carries ``helm.sh/resource-policy: keep``, the
    setting rides the supervisor's existing out-of-band merge-patch.
  * **k3s dead-node replace.** The identity wipe left
    ``/var/lib/rancher/k3s/server/token`` behind, so a join presented the
    seed's token against stale local bootstrap data and k3s fataled. The
    transition state machine also had no timeout, no failure ceiling, and
    no escape hatch: a reported ``failed`` didn't clear the desired state,
    so the supervisor re-fired a **destructive** wipe-and-rejoin every
    heartbeat (observed: 50+ minutes). Both join and leave now get a
    per-target attempt ceiling; ``POST …/clear-cluster-state`` is the
    operator escape hatch, gated on a staleness clock so clearing a
    *running* join can't strand the joiner as a live member that quorum
    math undercounts.
  * **A join that had already succeeded re-fired and wiped its own
    identity.** The guard checked only trigger-file presence, but the
    runner renames the trigger to ``.done`` the moment it succeeds while
    the desired state stays set until the backend sees a ``ready``
    heartbeat. In that window the next heartbeat re-fired the destructive
    join. Gated on what the runner last wrote instead, which also tells
    "already joined this seed" apart from "re-targeted at a different
    seed". And when such a join then failed, its rollback restored this
    node's **single-node** bootstrap manifests into the *shared* cluster,
    whose helm-controller applied them — **uninstalling the CloudNativePG
    operator** and parking an nginx pod on the frontend's ``:80``. The CNPG
    Cluster CR kept reporting "healthy" because no operator remained to
    update it: a silent loss of failover. ``spatiumddi-firstboot`` hit the
    same race from the other side, re-seeding those manifests on **every
    boot** of a joined member and minting a fresh self-signed cert over an
    operator-uploaded or ACME-issued one. Both paths now skip (never
    delete) on a joined member. Join failures are additionally classified
    into five operator-actionable causes — a bare "join failed" left
    nothing to act on for the permanent ones.

* **#593 — a node's own firewall could partition it out of a live etcd
  cluster.** The per-role nftables drop-in was rendered purely from the
  control plane's row, not from whether the node is actually an etcd
  member. Observed live: a node's row had gone ``cluster_role = NULL``
  after a failed re-join while the node was still a voting etcd member
  serving raft normally. The supervisor concluded "plain agent node",
  rendered a firewall with no peer rule, and the node's own nftables
  dropped its peers' inbound raft — they logged an i/o timeout every 5 s
  against a member that was up the whole time. It fails in the most
  damaging direction: dropping a voting member out of raft leaves a
  3-node cluster one node from losing quorum. Now keyed off **observed
  local state**: when the row supplies no peers but k3s labels the node an
  etcd member, the peer set is recovered from live kube-API membership;
  and the supervisor **refuses to write any drop-in that closes 2380** on
  a node k3s still calls a member, leaving the previous ruleset in place.
  Membership *unknown* never blocks an update (the guard fails open), and
  only a ``True`` is ever memoised — a stale ``True`` merely delays
  narrowing after a real demote, while a stale ``False`` would be this bug
  again. Refusals surface as a new ``appliance.firewall_state`` JSONB
  riding the heartbeat, rendered as a Fleet banner (migration
  ``d1f4b7c92e58``).

* **#549 — a fully-unattended install finished, then waited forever for
  someone to press OK.** ``do_install``'s final "Press OK to reboot"
  whiptail msgbox was shown unconditionally. On a headless box there is no
  operator on tty1 to dismiss it, so the wizard blocked and the
  ``systemctl reboot`` after it never ran: the appliance installed
  correctly to disk and never booted into the installed system. It can't
  be worked around hypervisor-side — the install ISO is the live boot
  medium, so its tray is locked closed for the whole install, and whiptail
  ignores an injected Enter. Now gated on ``FULLY_UNATTENDED``, like the
  welcome and final-confirm dialogs already were. Affects both roles
  (control-plane + appliance) on any fully-unattended install, i.e. every
  release ISO carrying the preseed installer that shipped in
  ``2026.07.04-1``. Interactive and partial-preseed installs are unchanged
  — they still get the media-removal reminder.

* **#571 — the GSLB pool zone picker offered reverse zones.** Pools render
  A/AAAA records, which only belong in a forward zone, so a pool targeting
  an ``in-addr.arpa`` / ``ip6.arpa`` zone can never render a valid record.
  Reverse zones slipped through because they carry
  ``zone_type="primary"``; the picker now filters on the dedicated ``kind``
  field, and the pool-create endpoint rejects a reverse-zone target
  server-side (API-first — the UI filter is not the enforcement layer).

* **#583 — the DHCP scope edit form blanked DNS Servers and PXE profile,
  and saving wiped them.** Two independent round-trip bugs. The frontend
  labelled option 6 with the IANA name ``domain-name-servers`` while the
  backend's canonical vocabulary is ``dns-servers``, so on read-back the
  name→code table returned code 0 and the value fell into the hidden
  custom-options bucket. The frontend is aligned, and a backend alias
  normalises the legacy name on write and still maps it to code 6 on read,
  so already-persisted rows recover without a migration. Separately,
  ``pxe_profile_id`` was missing from ``ScopeResponse`` entirely, so the
  picker always reset to "(none)" and a save silently detached the bound
  profile.

* **#576 — the Looking Glass never started on a k3s appliance.** The whole
  supervisor → HelmChart → DaemonSet → pod path was already in place; the
  only missing link was firstboot, which generates and injects
  ``DNS_AGENT_KEY`` / ``DHCP_AGENT_KEY`` but nothing for ``LG_AGENT_KEY``.
  So the API 503'd every collector registration and shipped an empty key to
  the supervisor. Mirrors the DNS / DHCP wiring, including a one-time
  backfill for an already-installed control plane that A/B-upgrades into a
  Looking Glass build (first-boot generation is skipped on those).

* **#576 — the collector crash-looped on the appliance from an early
  SIGHUP.** The supervisor started ``gobgpd`` then immediately started the
  sync thread, whose bootstrap-from-cache apply sent SIGHUP ~4 ms later.
  gobgpd installs its SIGHUP reload handler partway through startup, so a
  signal arriving before then hits the default disposition — terminate —
  and kills the daemon before it prints anything. The race was won under
  Docker locally and reliably lost on the slower k3s appliance. The first
  apply now waits until the gobgpd gRPC listener answers, which proves the
  handler is installed; a gobgpd that exits during startup (a real config
  or bind failure) short-circuits to a restart instead of running the
  threads against a dead daemon. The DaemonSet also gets an explicit
  ``NET_BIND_SERVICE`` capability, since binding privileged ``:179`` as
  non-root via a file capability is fragile under Kubernetes.

* **#573 — ``make build`` left a stale ``looking-glass:dev``.** The image
  was listed in ``bake-images.sh`` but the ``build`` target never built or
  retagged it, so ``make appliance-baked-iso`` could bake an old collector
  or trip the stale-source guard. Added the build line and the ghcr retag
  pair, mirroring the DNS / DHCP wiring.

### Migrations

Nine additive migrations, chaining linearly from the last release's head
``c9f2e1a4d7b6``: → ``cb279a6afd70`` (#566 — Looking Glass collector /
peers / routes) → ``531494dbf44c`` (#566 — ``bgp_lg_*`` alerts +
``Subnet.bgp_should_advertise``) → ``f7883fa6d413`` (#566 — route
distinguisher on ``bgp_lg_route``) → ``1440e72b9297`` (#566 D1 — MetalLB
BGP-mode ``platform_settings`` columns) → ``e9c47a1f3b28`` (#586 — wake
scheduler) → ``d7e3f0a91c24`` (#586 — wake-scheduler calendars) →
``b4f2a9c17e63`` (#586 — post-wake verify columns) → ``c7a3f1e28d94``
(#590 — ``appliance.cluster_join_state_at``) → ``d1f4b7c92e58`` (#593 —
``appliance.firewall_state``). New-table downgrades are baselined for the
shape linter.

### Notes

* **Existing HA installs need a one-time repair.** The #590 placement
  fixes are declarative, but a Redis replica or a CNPG instance whose PVC
  is *already* bound to an occupied node will go ``Pending`` when the
  cluster flips to *required* anti-affinity. Postgres stays available
  throughout (the primary is untouched and a surviving replica keeps
  failover possible). The repair for each is documented in
  ``k8s/README.md`` and ``charts/spatiumddi/README.md``, with the rule
  stated unambiguously: delete a **replica's** PVC only, never the
  primary's.

* **MetalLB BGP mode is opt-in and pulls in GPL-2.0 code.** Enabling it
  deploys FRRouting, the first GPL-v2 component in the data path. It is
  bundled as an unmodified upstream image rather than linked, both
  ``bgp.enabled`` and ``frrk8s.enabled`` default to false, and L2 mode is
  unchanged. See ``NOTICE``.

* **Scheduled Wake-on-LAN cannot use appliance vantage on a scheduled
  fire.** The agent-command channel is in-memory and per-replica, so the
  beat worker can't reach the supervisor. Use server vantage, or "Run
  now", until the Redis-backed dispatch lands.

---

## 2026.07.04-1 — 2026-07-04

A **network-intelligence + IPAM-at-scale** release. Four new
control-plane features land together: **GeoDNS / topology-aware GSLB
steering** (per-pool-member serving scope so a GSLB pool answers
differently by client CIDR or Site), **IPv6 Router Advertisements**
(``radvd`` rendered per RA-enabled DHCP scope + a rogue-RA passive
sniffer), **BGP prefix-hijack detection** (RIPEstat + RIS Live watching
your ASNs' prefixes for hijacks and more-specifics), and **DNSBL / RBL
reputation monitoring** (a daily sweep of egress / public / pinned IPs
against a curated blocklist catalog, surfaced in a Reputation panel on
the IP detail modal). Riding with them, an **IPAM-at-scale** pass makes
the address table survive a busy ``/16`` — server-side pagination +
search, a **cross-subnet IP search**, row-windowing with sortable
columns, and an hourly utilization-recount sweep — plus two DHCP
reliability fixes (an idempotent lease→IPAM mirror insert that no longer
races the unique index, and a **Celery schema-behind-head guard** so a
worker deployed ahead of ``migrate`` fails loudly instead of logging the
same error thousands of times). On the appliance side: **headless /
unattended install** via a cloud-init preseed, an **unattended-upgrades
policy** surface, two console operator-UX wins, a seven-issue bug sweep
across the host-config runners, and a k3s ``v1.35.6`` bump. The Operator
Copilot tool registry grows by 13 (GeoDNS, IPv6 RA, BGP hijack, DNSBL,
and cross-subnet IP search) to 229.

### Added

* **#530 — GeoDNS / topology-aware GSLB steering.** A GSLB pool member
  gains an optional **serving scope** — a list of client CIDRs and/or a
  ``Site`` — so a pool can answer differently depending on where the
  query came from. Scoped members render as BIND9 **geo views**
  composed on top of the operator's existing split-horizon views: geo
  views are evaluated **before** operator views, and a union fallback
  guarantees a scoped-only pool never blackholes a client outside every
  scope. Ships as a model + render change (member serving-scope columns
  via migration ``a3d7f1c9e6b2``) — no new MCP tools.

* **#524 — IPv6 Router Advertisements.** A DHCP scope can now emit
  **RAs** — ``radvd`` config is rendered per RA-enabled scope and shipped
  in the DHCP ``ConfigBundle`` (the agent starts ``radvd`` on the wire,
  stops it on disable), behind the ``ipv6.router_advertisements`` feature
  module (default-on discovery toggle — RA emission still needs per-scope
  opt-in + ``RADVD_MANAGED``, and the sniffer is gated on
  ``DHCP_RA_SNIFFER_ENABLED``). A companion **rogue-RA
  passive sniffer** watches for unexpected Router Advertisements with a
  ``(group, source_ip, source_mac)`` identity and a **seeded-disabled**
  ``rogue_ra`` alert. The DHCP-agent compose gains a commented
  ``NET_ADMIN`` cap + ``net.ipv6.conf.all.forwarding=1`` note (harmless
  when RA is off). Migration ``b7e4d1a92c30``; MCP tools for the RA
  config + rogue-RA sightings.

* **#527 — BGP prefix-hijack detection.** Watches your ASNs' announced
  prefixes for hijacks and unexpected more-specifics: a **RIPEstat**
  poll is the source of truth, with an optional **RIS Live** streaming
  consumer for near-real-time detection. Fires ``bgp_prefix_hijack`` /
  ``bgp_more_specific_announced`` alerts (RPKI-invalid vs -unknown drives
  severity), keeps evidence on a pruned victim prefix via ``SET NULL`` on
  the detection FK, refreshes on a cadence gate, and resolves stale
  detections outage-safely. Behind the ``network.asn`` feature module;
  migration ``a1c7f3e9b284``; MCP tools for tracked prefixes +
  detections.

* **#528 — DNSBL / RBL reputation monitoring.** A curated blocklist
  catalog + a daily sweep over NAT-egress / public / ``internet_facing``
  / pinned IPs, with an ``ip_blocklisted`` **latch** alert that
  auto-resolves on delist or de-scope, surfaced in a **Reputation
  panel** on the IP detail modal. Behind the ``security.dnsbl`` feature
  module (default-on discovery toggle — no external DNS queries until the
  ``dnsbl_monitoring_enabled`` sweep switch is on and at least one list is
  enabled); migration ``c9f2e1a4d7b6``; MCP tools for the reputation
  state.

* **#517 / #519 / #520 — IPAM address search, pagination + cross-subnet
  search.** ``GET /ipam/subnets/{id}/addresses`` gains optional
  ``q`` / ``hostname`` / ``mac`` / ``sort`` / ``order`` / ``limit`` /
  ``offset`` + an ``X-Total-Count`` header (backward-compatible — the
  bare-list response is unchanged). A new
  ``GET /ipam/addresses/search`` (paginated envelope with joined
  subnet/space context) + ``…/search/ids`` (capped id list for
  select-all-matches) power a new **cross-subnet IP search** modal —
  results grouped by subnet/space, with "select all matches" feeding the
  existing bulk-edit / bulk-delete plumbing. The IP table now **windows**
  rows (``ADDRESS_ROW_CAP=500`` with a "show all" opt-in so a busy
  ``/16`` doesn't mount 65k rows), renders a mobile card list **or** the
  desktop table (not both), and gains **clickable sortable column
  headers** (asc → desc → cleared). New MCP read tool
  ``find_ip_addresses``.

* **#549 — headless / unattended appliance install.** The disk
  installer `spatium-install` can now run non-interactively from a
  **preseed answer file**, for fleet rollouts, PXE/IPMI provisioning,
  cloud images, and CI boot-tests. On boot of the installer media it
  looks for an answer file before launching whiptail; present fields
  skip their prompt, a fully-preseeded run (disk + `confirm_wipe:
  true` + all fields) installs with zero console interaction, and a
  **partial** preseed falls through to interactive prompts for only
  the missing fields. A field that is present but invalid **halts
  loudly** (clear console message + non-zero exit) rather than
  silently defaulting on a disk wipe. A new parser
  `/usr/local/bin/spatium-preseed-parse` (PyYAML) reuses the wizard's
  own validators — hostname RFC 1123, k3s CIDR disjoint + ≤ /22 + no
  LAN overlap, pairing code = 8 digits, control-plane URL required for
  the appliance role. **Destructive-disk safety**: an unattended wipe
  needs both `confirm_wipe: true` and a `target_disk` that resolves to
  exactly one whole disk of at least the 32 GiB A/B-layout floor
  (prefer a stable `/dev/disk/by-id` id); a supplied-but-unresolvable
  disk drops to the interactive picker, and an under-floor disk halts
  loudly. Static networking requires `interface` + `ip` + `prefix` +
  `gateway` (IPv4 only) — a missing interface would otherwise silently
  fall back to DHCP. A URL-transport preseed retries the fetch to ride
  out network bring-up, and discovery skips a non-preseed cloud-init
  `user-data` on the CIDATA volume so it can't shadow a real preseed on
  another source.
  **Secrets**: supports `admin_password_hash` (crypt(3)) so the admin
  cleartext stays out of the file, and keeps the password + pairing
  code out of the console dashboard, install log, and `set -x` trace
  log. **Transports**: kernel cmdline `spatium.preseed=<url|path>`, a
  NoCloud `CIDATA` volume carrying `spatium-preseed.yaml`, or a file
  on the install medium — and the same `spatium_preseed:` block can be
  embedded in a cloud-init `user-data` document, which is how the AWS
  (`--user-data`) and Azure (`--custom-data`) cloud-image recipes
  deliver it. Docs + annotated control-plane / appliance examples
  under `appliance/cloud-init/`; retires the stale pre-#183 cloud-init
  framing in APPLIANCE.md §4.

* **#164 — appliance unattended-upgrades policy.** Extends the APT
  host-config plane (#155) with the **when / how** of auto-applying
  updates, orthogonal to ``apt_managed`` (the **where**): an operator can
  set a reboot policy without taking over apt sources. New
  ``platform_settings.apt_unattended_*`` columns (migration
  ``c9d4a1e8b672``): ``apt_unattended_origins`` (Allowed-Origins;
  **security-only default** = the locked-down baseline),
  ``apt_unattended_blocklist`` (Package-Blacklist globs), and
  ``apt_unattended_automatic_reboot`` + ``apt_unattended_reboot_time``
  (HH:MM). The ``apt_bundle`` now always carries the unattended block and
  folds it into ``config_hash`` so a policy change re-fires the host
  trigger even with ``apt_managed`` off; the ``spatiumddi-apt-reload``
  runner stages, validates via ``apt-config``, and installs both
  ``20auto-upgrades`` and ``50unattended-upgrades``. Surfaced on the
  ``find_apt_settings`` MCP tool + an **Unattended-upgrades policy**
  sub-section on the APT settings form, and rides the existing APT
  trigger / heartbeat / ``apt_state`` Fleet chip.

* **#556 — console operator-UX (headless recovery dashboard).** Two
  wins for the physical-console dashboard: a **control-plane
  reachability chip** (appliance role) — a ≤ 2 s TCP probe of
  ``CONTROL_PLANE_URL`` fanned out in the data-tier pool so an operator
  can tell "network-partitioned" from "unapproved" — and **cancel
  pending reboot (F9)**, which stops the reboot service mid-grace and
  removes the trigger, aborting a queued web-UI / Fleet reboot from the
  console (the footer shows F9 only while a reboot is pending).

### Changed

* **#523 / #521 / #522 — IPAM API symmetry + utilization recount +
  sweep perf.** Subnet-token scoping on the address list endpoints,
  preview/commit next-IP delegation parity, an ``allocate_next_ip``
  public-facing guard + ``extra_zone_ids`` / ``decom_date`` support, and
  CSV/XLSX formula-injection hardening on address export. A new **hourly
  idempotent utilization-recount** task (``ipam_utilization_recount``)
  recomputes ``Subnet.allocated_ips`` + block rollups via grouped SQL so
  a drifted counter self-heals. Two sweep-task perf fixes:
  ``dhcp_lease_cleanup`` loads the subnet cache once per sweep, and
  ``ipam_dns_sync`` pre-filters to zone-bound subnets via a CTE.

* **#516 — IPAM frontend polish.** A grab-bag: separated static-DHCP
  chained-create error reporting (partial success now shows only
  **Close** so the operator can't re-submit into a collision), resolved
  ``IPDetailModal`` zone names, ``onError`` toasts on space/subnet
  mutations, ``BulkEditSubnetsModal`` migrated to the shared ``Modal``,
  ``DnsSyncModal`` backfills on **Apply** not open, ``available`` /
  ``discovered`` status options, a readable skipped-rows import report,
  and IPv6-aware ``cidrSize`` (out-of-range prefixes clamp to 0).

* **#548 — k3s ``v1.35.5+k3s1`` → ``v1.35.6+k3s1``.** Patch bump of the
  pinned k3s the slot image bakes (Kubernetes 1.35.5 → 1.35.6, Go
  1.25.11) — no API removals, no manifest/chart edits. Pulls in the
  klipper-helm CVE fix, containerd ``v2.2.5-k3s2``, runc ``v1.4.2``, and
  etcd ``v3.6.12-k3s1`` (forward patch, multi-node rolling stays
  compatible). The Traefik-v40 ingress-nginx-provider breaking change
  does **not** apply — the appliance disables bundled Traefik and uses
  nginx + MetalLB. Reaches field appliances via the next OS release +
  A/B slot upgrade (or a fresh install), not an in-place swap.

### Fixed

* **#564 — DHCP lease→IPAM mirror-row insert raced the unique index.**
  Concurrent writers (the Kea agent ``/lease-events``, the Sync-DHCP
  poll, static-reservation mirroring, and the l2-sniff new-device-watch
  path) each did an unguarded ``SELECT``-then-``INSERT`` on
  ``uq_ip_address_subnet_address``, so the loser hit a 500 whose
  ``PendingRollbackError`` tail poisoned the rest of the batch. A new
  shared ``insert_ipam_mirror_row()`` inserts inside a ``SAVEPOINT`` and
  self-heals a unique-violation into the committed incumbent (matching
  the guard style in ``unifi/reconcile.py`` + ``ipam_discovery.py``),
  wired into all four mirror-insert sites.

* **#565 — Celery ran silently against a schema behind the bundled
  head.** The api gates ``/health/ready`` on the DB schema being at the
  bundled Alembic head, but the worker/beat had no equivalent and failed
  tasks silently when code was deployed ahead of ``migrate`` (one
  operator logged the same ``UndefinedColumnError`` ~2440× in a tight
  loop). The version-vs-head comparison is extracted into a
  framework-agnostic ``app.core.schema_check.schema_at_head()`` shared by
  ``health.py`` and Celery; added ``worker_ready`` / ``beat_init``
  startup checks, a 5-min periodic task that opens/auto-resolves a
  **schema-behind-head** alert on drift, and an opt-in
  ``STRICT_SCHEMA_CHECK`` ``task_prerun`` gate that ``Reject(requeue=True)``s
  tasks while behind (default off; mirrors ``STRICT_SECRET_KEY``).

* **#550–#555 — appliance host-config + install bug sweep.** The seven
  open bug-labelled issues from the 2026-07-03 appliance audit: a
  ``tz-reload`` runner shipped ``0644`` (203/EXEC — feature dead), an
  ``snmp-reload`` validated with the non-existent ``snmpd -t`` flag, the
  A/B ``slot-rollback`` called a non-existent ``commit`` subcommand, the
  recovery console froze ~25 s per refresh on a wedged cluster (blocking
  ``kubectl``/``systemctl`` calls now fan into the thread pool), the
  installer failed to validate static-network fields + had a
  control-plane-confirm dead loop, and a round of supervisor / host-script
  hardening (pcap ``capture_id`` path-traversal guard, owner-only session
  tokens, IPv6 seed URLs on cluster-join). Adds
  ``scripts/lint_appliance_runners.py`` (wired into CI) which asserts
  every systemd ``ExecStart`` runner is executable in the built image and
  ``bash -n`` clean — it catches the ``tz-reload`` 203/EXEC class.

### Migrations

Five additive migrations, chaining linearly from the last release's head
``b1f7c3a92e04``: → ``c9d4a1e8b672`` (#164 — ``apt_unattended_*`` policy
columns) → ``a3d7f1c9e6b2`` (#530 — ``dns_pool_member`` geo/topology
serving-scope columns) → ``b7e4d1a92c30`` (#524 — IPv6 RA management +
rogue-RA detection) → ``a1c7f3e9b284`` (#527 — BGP prefix-hijack: tracked
prefixes + detections tables + ``platform_settings`` columns) →
``c9f2e1a4d7b6`` (#528 — DNSBL / RBL reputation monitoring). New-table
downgrades are baselined for the shape linter.

---

## 2026.07.03-1 — 2026-07-03

A **hardening + Wake-on-LAN** release. The headline new feature is
**Wake-on-LAN** from three surfaces — a Wake button on the IP detail
modal, a standalone tool on the Network Tools page, and a
``propose_wake_host`` Operator Copilot proposal — each able to
broadcast the magic packet from the control-plane vantage or dispatch
it to a Fleet appliance on the target's own segment. Riding with it,
two smaller UX wins (**create / edit / delete DHCP static
reservations** directly from the Static Assignments tab, and **Add
block** promoted to a one-click space-header button) and a large
**bug-check sweep** across IPAM and DHCP: the one critical + all high
IPAM findings, the medium IPAM batch, a DHCP field-triage pass, and a
round of audit-log / agent-wire-contract fixes. On the security side,
the long-lived JWT **refresh token moves out of ``localStorage`` into
an HttpOnly cookie**, WoL gains the same SSRF denylist every network
tool applies, and two IPAM authorization gaps (per-row token scope on
space / block reads, per-type RBAC on structural handlers) are closed.
The Operator Copilot tool registry grows by 1 (``propose_wake_host``)
to 216 — and ~19 default-on read tools that a module-gating bug had
been silently stripping on every install are restored.

### Added

* **#533 — Wake-on-LAN.** Send a WoL magic packet to bring a host up,
  from three surfaces sharing one ``wol`` service
  (``backend/app/services/`` — ``wake_from_server`` /
  ``wake_via_appliance`` / ``resolve_wake_params``): a **Wake** button
  on the IPAM IP detail modal (shown only when the IP has a MAC,
  ``POST /api/v1/ipam/addresses/{id}/wake``), a standalone
  **Wake-on-LAN** tool on the **Network Tools** page (MAC + optional
  broadcast + port, ``POST /api/v1/tools/wol``, no IP row required),
  and a ``propose_wake_host`` Operator Copilot proposal (preview →
  Apply). Two vantages per call: **server** (the api container
  broadcasts) or **appliance** (dispatched over the existing generic
  nettool command channel to a Fleet appliance on the target's
  segment, with the supervisor's new ``_run_wol`` runner re-validating
  and sending from that segment). MAC + subnet broadcast are resolved
  server-side; gated on ``read:use_network_tools`` like the rest of the
  tools surface and audited (``action=wake_on_lan``).

* **#472 / #473 — Create / edit / delete DHCP static reservations from
  the Static Assignments tab.** The DHCP group's Static Assignments tab
  was read-only and the purpose-built ``CreateStaticAssignmentModal``
  was dead code, so a Kea reservation could only be made via the IPAM
  Allocate flow. The tab now has a **New static assignment** button
  (superadmin-gated to match the backend, with a scope picker when the
  group has more than one scope) plus **Edit… / Delete…** on the
  per-row menu, and its empty state distinguishes "no scopes yet" from
  "no reservations yet". The IPAM Add-address modal's "No DHCP scope
  exists" dead-end becomes an inline **Create a scope** button.

* **#538 — "Add block" as a visible space-header button.** A block is
  the top-level structural child of a space, so **Add block** is
  promoted out of the space header's ``Tools ▾`` dropdown into a
  first-class button next to **Add subnet** (``Find free…`` is promoted
  with it so no single-item dropdown is left). Header-button ordering
  grammar (#465) and narrow-viewport flex-wrap are preserved.

### Fixed

* **#489–#502 — critical + high IPAM review findings.** The one
  critical: next-IP allocation built ``list(net.hosts())`` before
  slicing, so a ``/64`` sequential or ``/8`` tried to materialize
  billions of address objects while holding the subnet ``FOR UPDATE``
  lock — now ``itertools.islice`` (DoS fix). Plus thirteen high-sev
  fixes: soft-deleted subnets/blocks/spaces now free their CIDR/name in
  the raw overlap / free-space / uniqueness queries (they bypassed the
  ORM soft-delete filter); a hostname rename ships delete-old +
  create-new so the stale A/AAAA RRset doesn't linger; a hostname-only
  edit preserves ``forward_zone_id`` instead of re-homing the record;
  ``create_subnet`` now enforces block containment; template child-carve
  aligns up and overlap-checks; the subnet importer catches partial
  overlaps; bulk-delete honors the ``auto_from_lease`` DDNS guard;
  ``dns_split_horizon`` round-trips; and several frontend correctness
  bugs (``reserved_until`` UTC drift, gap-markers over a filtered list,
  edit modals unable to clear fields, the "None (remove DNS record)"
  zone option).

* **#503–#515 — medium IPAM review findings.** Importer robustness
  (BIGINT ``total_ips`` clamp on v6 imports, header-casing
  normalization, status round-trip, MAC/gateway validation → error row
  not 500, intra-payload duplicate pre-flight); plan-apply now stamps
  the multicast ``kind``, validates the gateway, and creates
  placeholder rows; a version-aware placeholder gate so IPv6 subnets get
  network/broadcast rows; ``effective-dns`` falls through to the space
  like the record-routing path; per-type RBAC on the structural
  handlers (see Security); bulk-edit recomputes subnet + block
  utilization and refreshes the UI; a profiling commit-race retry so a
  scan row isn't stranded ``queued``; NAT-mapping IP literals validated
  → 422 not 500; subnet delete wakes DHCP on soft-delete and retracts
  agentless DNS on permanent delete; IPv6 CIDR containment so v6
  drag-drop re-parent works; select-all + shift-range honor the per-row
  write gate; and a per-subnet advisory lock so a slow discovery sweep
  isn't re-dispatched into a colliding second run.

* **#475–#478, #480–#483 — DHCP field triage + agent hardening.** Scope
  hostname→IPAM sync vocabulary converged so edits stop appearing to
  revert (and ``update_scope`` can clear nullable lease-time fields);
  the Kea host-reservation MAC is canonicalized before render so a
  dotted/run-together MAC no longer makes Kea reject the whole subnet;
  a config-test preflight before Kea reload surfaces the real rejection
  reason; expired agent-lease GC + a manual lease-delete endpoint +
  detached statics freed to ``available``; ``_sync_dns_record`` guards a
  ``None`` fqdn before the reverse-PTR block (and retracts an orphaned
  PTR); IPv6-aware DNS drift; the DHCP heartbeat gains
  ``extra="forbid"`` + a bounded ``ops_ack`` and a zero-wire lease-pull
  floor guard so an empty poll can't purge every tracked lease; and a
  record-op fan-out fix so a paused primary no longer drops record ops
  for a whole agent-based group.

* **#479 — Copilot tools silently stripped by module gating.**
  ``effective_tool_names`` gated tools on exact membership in the
  feature-module catalog, so ~19 default-on read tools (conformity /
  webhooks / DNS / diagnostics / appliance) shipped with module ids the
  catalog doesn't know and were stripped from the MCP/Copilot surface on
  every install (and ~15 ``propose_*`` tools were un-enable-able). The
  gate now fails **open** on an unknown module id (matching
  ``feature_modules.is_module_enabled``); known-but-disabled modules
  stay a hard kill-switch. A CI guard asserts every ``Tool.module`` is
  ``None`` or a real catalog id.

* **#474 / #485 — soft-deleted DHCP scope no longer wedges recreation.**
  ``uq_dhcp_scope_group_subnet`` was a plain unique constraint, but
  ``DHCPScope`` is soft-deletable — a trashed scope kept occupying the
  ``(group, subnet)`` slot (invisible to the ORM pre-check) and
  recreating a scope hit the DB constraint as a raw 500. Replaced with a
  partial unique index ``WHERE deleted_at IS NULL``; ``create_scope``
  translates the specific violation to a friendly 409.

* **#537 — Kea rejects config on a null reservation client-id.** The
  DHCP agent's Kea renderer guarded the reservation ``client-id`` on key
  presence, but the wire ``ScopeDef`` path always injects
  ``client_id: None`` for a static with no client-id, emitting
  ``"client-id": null`` — which ``kea-dhcp4`` rejects
  (``DHCP4_CONFIG_LOAD_FAIL``), wedging config delivery. Now guards on a
  truthy value (mirroring the control-plane driver's #375 fix, which
  never ran on OS appliances where the agent renders Kea itself); same
  fix on the sibling ``duid`` check.

### Security

* **#484 L1 — refresh token moved to an HttpOnly cookie.** The
  long-lived JWT refresh token no longer lives in ``localStorage``
  (XSS-stealable). It's delivered only as an ``HttpOnly`` +
  ``SameSite=Strict`` + ``Secure`` (on https) cookie
  (``spatium_refresh``), path-scoped to ``/api/v1/auth`` so it rides
  only refresh + logout. The short-lived access token lives in JS memory
  (new ``lib/authToken.ts`` store); a full reload runs one silent
  ``/auth/refresh`` against the cookie to restore the session. OIDC/SAML
  callbacks set the cookie and drop the refresh token from the URL
  fragment. **Existing sessions hit one forced re-login after deploy**
  (no cookie yet).

* **#533 — SSRF denylist on the WoL broadcast target.** The magic-packet
  broadcast was validated only as IPv4, so WoL could fire at loopback /
  link-local / cloud-metadata ranges. Every WoL path (wire schema,
  ``/tools/wol`` schema, ``resolve_wake_params``, and the supervisor
  runner) now reuses the shared ``nettools.is_blocked_target`` guard;
  legitimate broadcasts (``255.255.255.255``, RFC1918 directed
  broadcasts) are never blocked.

* **#484 / #400 L4 — per-row API-token scope on space / block reads.**
  ``get_space`` / ``get_block`` had no ``token_scope_allows`` re-check,
  a latent IDOR if ``ip_space`` / ``ip_block`` ever entered the
  resource-scoped-token vocabulary. Centralized as
  ``_enforce_token_scope`` (the subnet check now delegates to it) and
  applied on both by-id reads — a no-op for sessions / unscoped tokens.

* **#508 — per-type RBAC on IPAM structural handlers.** The router gate
  admits an any-of grant over the whole IPAM surface (incl.
  ``nat_mapping`` / ``custom_field``), so a peripheral grant could
  create/mutate spaces, blocks, and subnets. A ``_require_type_write``
  gate is added to the create/update space/block/subnet handlers, and
  plan-apply now requires subnet write.

### Migrations

Two additive migrations (both create a partial unique index; downgrades
are baselined for the shape linter). Chain order, continuing from the
last release's head: ``b7c2f1a9d4e6`` → ``a3f9c1e7b2d4`` (#485 — DHCP
scope ``(group, subnet)`` partial unique ``WHERE deleted_at IS NULL``)
→ ``b1f7c3a92e04`` (#491 — ``ip_space.name`` partial unique
``WHERE deleted_at IS NULL``).

---

## 2026.06.28-1 — 2026-06-28

A **migration + onboarding** release. Two headline ways to bring an
existing network into SpatiumDDI and watch it: a one-shot **NetBox
importer** that pulls a live IPAM estate into native rows so an
operator can evaluate without retyping their inventory, and
**arpwatch-style new-device detection** that alerts the moment a
never-before-seen MAC appears, with a trusted-MAC allowlist and
one-click block. Riding with them, a broad **IPAM UX overhaul** across
two review rounds — inline cell editing, a tree quick-filter, an
accessibility pass (modal focus-trap + ARIA tree), block-level
utilization, a mobile card view, and level-invariant header toolbars —
plus a **DNS A/AAAA guardrail** that stops a comma-separated value from
silently breaking a zone load. The Operator Copilot tool registry grows
by 8 (NetBox importer +2, new-device watch +6) to 215.

### Added

* **#36 — NetBox → IPAM one-shot importer.** A read-only **migration**
  importer (not a continuous reconciler — no target row, no beat sweep,
  no absence-delete) that live-pulls prefixes / IP addresses / VRFs /
  tenants→Customers / sites / VLANs out of a NetBox install (REST
  v3.x–4.6+) and stamps them into native IPAM rows. Stateless
  **test → preview → commit** flow at
  ``/api/v1/ipam/import/netbox/{test-connection,preview,commit}`` — the
  UI hands the unmodified ``PreviewOut`` straight back as the commit
  body, conflicts are re-detected fresh against current state, and each
  entity writes in its own savepoint so a failure on entity N never
  rolls back 1..N-1. Two space strategies: ``per_vrf`` (one ``IPSpace``
  per VRF + a Global space) or ``single`` (collapse into a chosen
  ``target_space_id``). Provenance ``import_source="netbox"`` +
  ``imported_at`` + ``netbox_id`` (in ``custom_fields`` / ``tags``)
  makes a re-run idempotent — conflicts default to **skip**, set
  **overwrite** to refresh. Connection ``base_url`` + ``token`` are
  supplied per-request and **never persisted**; superadmin-only, behind
  the **default-on** ``ipam.import.netbox`` feature module. Service in
  ``backend/app/services/netbox_import/`` (migration ``30135c361a47``).
  The Operator Copilot tool registry grows by 2 —
  ``find_netbox_import_preview`` (read) + ``propose_commit_netbox_import``
  (write proposal).

* **#459 — arpwatch-style new-device detection.** Alert the moment a
  never-before-seen MAC appears on the network, with a trusted-MAC
  allowlist and one-click block — behind the **default-off**
  ``security.new_device_watch`` feature module, mirroring the rogue-DHCP
  pipeline (observe → classify → upsert → alert). Extends the existing
  ``ip_mac_history`` observation store (not a parallel table) with a
  ``new`` / ``acknowledged`` / ``known`` classification + source +
  ``is_randomized`` + ack trail, plus a new ``mac_allowlist`` (MAC or
  OUI prefix). Three ingestion paths, all through
  ``record_mac_observation``: the DHCP lease-events handler, SNMP
  ARP/FDB cross-reference, and an opt-in L2 ARP/ND sniffer on the DHCP
  agent (``DHCP_MAC_SIGHTING_ENABLED`` + ``NET_RAW`` →
  ``POST /api/v1/dhcp/agents/mac-sightings``). A ``new_mac_seen`` alert
  rule (seeded disabled) opens one event per ``(ip, mac)`` —
  randomised, locally-administered MACs excluded by default; plus
  ``device.first_seen`` / ``device.acknowledged`` typed webhook events
  fired on ingest, ahead of the 60 s alert tick. Operator surface at
  ``/api/v1/new-devices`` (summary / sightings / acknowledge /
  baseline-import / allowlist CRUD / block, where block writes a
  ``DHCPMACBlock``), a **Tools → New Devices** review queue, and a "new
  devices 24h" dashboard KPI. Six Copilot tools (``find_new_devices`` /
  ``count_new_devices`` / ``find_mac_allowlist`` +
  ``propose_acknowledge_device`` / ``propose_allowlist_mac`` /
  ``propose_block_mac``). Migration ``a3f1d6c92b58``.

* **#465 — IPAM inline editing, tree quick-filter, and help layer.**
  Double-click a hostname / description / status in the subnet IP table
  to edit it in place — gated on the row's write permission, with a
  click-vs-double-click timer so a single click still opens the detail
  sheet. A new tree quick-filter narrows every IP space to blocks /
  subnets whose CIDR or name matches (and the space's own name),
  keeping ancestor paths, revealing a matched block's whole subtree,
  and hiding non-matching spaces. Plus an opt-in **Help (?)** layer
  (progressive disclosure) that reveals a glossary of the IPAM visual
  vocabulary, and the **NetBox import-provenance chip** is now
  dismissable.

### Changed

* **#465 — IPAM accessibility, block-level utilization, and consistent
  toolbars.** A shared ``StatusTag`` primitive carries icon + text +
  colour so status is never conveyed by colour alone (WCAG 1.4.1). The
  shared modal gains a focus-trap + ``role="dialog"`` + focus-return,
  and the IP-space tree gains ``role="tree"`` / ``treeitem`` /
  ``aria-expanded``. Block rows now show real **Used IPs** + size + a
  utilization bar (the backend caches ``allocated_ips`` / ``total_ips``
  on ``ip_block``; huge IPv6 blocks read as uncountable), and a
  ``BLOCK`` / ``SUBNET`` row-type badge labels the mixed tables. The
  Space + Block headers move their structural actions behind a
  **Tools ▾** menu to match the Subnet header, and the **Ask AI**
  action is demoted into it. Under the ``sm`` breakpoint the IP table
  becomes a tappable card list for on-call phone use, and the tree
  caps very large sibling groups with a "Show N more…" reveal.
  Migration ``b7c2f1a9d4e6``.

### Fixed

* **#465 — IPAM UX correctness.** Select-all no longer picks rows that
  the "Hide network/broadcast" toggle is hiding; inline edits and the
  provenance-chip dismiss now surface backend errors instead of
  silently reverting; ``isProvenanceKey`` matches an exact importer-key
  allowlist so an operator's own ``netbox_*`` custom field can't be
  swept into the chip and dropped; and the tree-row utilization dot plus
  a stray ``0`` rendered in the IP detail modal are corrected.

* **#467 — A/AAAA records reject multi-IP values.** Adding an A / AAAA
  record with a comma-separated value (e.g. ``10.0.0.1, 10.0.0.2``) was
  stored verbatim and rendered as malformed rdata that silently broke
  the zone load. The API now rejects any A / AAAA value that isn't a
  single IP of the matching family with a 422 + guidance (one record
  per IP for round-robin, or DNS Pools for health-checked failover) on
  create / update / bulk-create, and the record modal shows the same
  hint inline. Other record types are untouched.

### Migrations

Three additive migrations (each backfills in place; downgrades are
baselined for the shape linter). Chain order:
``a3f1d6c92b58`` (#459 — new-device classification on ``ip_mac_history``
+ ``mac_allowlist``) → ``30135c361a47`` (#36 — ``import_source`` /
``imported_at`` on the IPAM tables + the ``ipam.import.netbox`` module
row) → ``b7c2f1a9d4e6`` (#465 — cached ``allocated_ips`` / ``total_ips``
on ``ip_block``).

---

## 2026.06.25-1 — 2026-06-25

A **governance + scale** release. The headline is two RBAC
deliverables that let an operator hand out narrow authority without
giving away the keys: **#62 approval workflows** — a two-person rule
for high-blast-radius operations (deletes, and a documented path to
bulk ops / factory reset / large imports), default-off, with a
self-governance lock so an approver can't quietly rewrite the policy
that governs them; and **#103 address sets** — named IP ranges inside
a subnet that carry their own RBAC scope, so editing `.50–.99` can be
delegated without subnet-wide write. Riding with them: **#449
permission introspection** (the UI finally knows the caller's own
effective grants, so it can gray out what they can't touch), **#195
the DHCP server Stats tab** (the 6th tab carved out when #181
shipped), **#455 server-side pagination** for the two list endpoints
that shipped whole result sets (DNS records + DHCP leases), a **#453
Kea sync fix** (IPAM "Sync → DHCP" no longer 400s on agent-based
servers), and **#452 a 24-hour university-scale load + soak test
suite**. The Operator Copilot tool registry grows 198 → 206. All
three schema changes are additive.

### Added

* **#62 — Approval workflows for risky ops.** A two-person rule for
  high-blast-radius operations, behind the new default-off
  ``governance.approvals`` feature module (group Security). A risky
  action submitted by one operator is queued as a ``change_request``
  and only executes after a *different* eligible approver accepts it —
  the operation then replays under the approver's identity with the
  audit log carrying both user IDs. The 6 delete handlers (subnet /
  block / space / dns_zone / dhcp_scope / dhcp_server_group) were
  refactored so the inline path and the approver-replay path are one
  registered ``Operation.apply()`` (``preview()`` re-validates against
  stale state). 11 built-in ``approval_policy`` rows seed disabled; a
  **Change Approver** builtin role + ``change_request`` resource_type +
  event namespace land alongside. Lifecycle API at
  ``/api/v1/change-requests`` (list / get / approve / reject / cancel +
  policies CRUD, module-gated; approve enforces approver ≠ requester,
  the op's required permission, a fresh ``preview()``, and fails
  *closed* if the requester row was deleted). Celery sweep expires
  past-TTL pending rows. A **self-governance lock** (migration
  ``5443d2756647``) stops an approver from editing the policy that
  governs the approval controls themselves. Frontend: a **Change
  Requests** admin page (queue / approve / reject / cancel + policies
  tab, ``usePermissions``-gated) + 202-handling helper. MCP:
  ``find_change_requests`` / ``count_change_requests`` /
  ``propose_approve_change_request`` / ``propose_reject_change_request``.
  P2 (bulk ops / factory reset / import gating + approval
  notifications) is a documented follow-up.
* **#103 — Address sets.** Delegate write over a slice of a subnet
  without granting subnet-wide write, behind the new
  ``ipam.address_sets`` feature module (group Network). New
  ``AddressSet`` model (migration ``e2b9c4f1a7d6``) + ``address_set``
  resource_type + an **Address Set Editor** builtin role + event
  namespace. The per-IP write gate
  (``app.services.ipam.address_set_gate``) is wired into all 8 IPAM
  mutation chokepoints:
  an edit is allowed with subnet write **or** write/admin on a
  containing set; out-of-set single edits 403, bulk loops skip + report.
  CRUD router at ``/api/v1/address-sets`` (audited, module-gated).
  Frontend: an **Address Sets** subnet tab + client-side gray-out of
  rows the caller can't edit. MCP: ``find_address_sets`` /
  ``count_address_sets`` / ``propose_create_address_set``.
* **#449 — Permission introspection.** ``GET /auth/me/permissions``
  returns the caller's own effective grants (role ∪ live time-bound ∪
  token-narrowed; self-only) via a new
  ``permissions.effective_grants()`` helper. A ``usePermissions()``
  hook + pure
  ``permissionMatch()`` util (backend-identical semantics, fail-closed
  while loading) + a reusable ``<ResourceIdPicker>`` (RolesPage + the
  time-bound-grant modal swap the raw UUID input for it).
* **#195 — DHCP server Stats tab.** The sixth tab on the DHCP server
  detail modal (the carve-out left at 5/6 when #181 shipped):
  active-lease count + per-message-type
  (discover / offer / request / ack / nak / decline / release) counts
  bucketed over a 1h/6h/24h/7d window, aggregated from the
  agent-reported ``dhcp_metric_sample`` stream.
  ``GET /dhcp/servers/{id}/stats`` (read-only) + a new
  ``app.services.dhcp.stats`` shared by
  the endpoint and the ``find_dhcp_server_stats`` MCP tool so the two
  can't drift. Tab gated on ``!isReadOnly`` like Logs / Config.
* **#455 — Server-side pagination for DNS records + DHCP leases.** A
  new shared ``Page[T]`` envelope (``app.api.pagination`` —
  ``{items, total, page, page_size}``) + a ``paginate()`` helper.
  ``GET /dns/groups/{gid}/zones/{zid}/records``,
  ``GET /dns/groups/{gid}/records``, and
  ``GET /dhcp/servers/{id}/leases`` now return
  ``Page[...]`` (default 100/page, max 1000) with a server-side
  ``search`` filter + exact ``record_type`` / ``state`` filters.
  Frontend gets a shared ``<Pager>`` primitive + a search box; existing
  secondary filters + sort stay as client-side refinements over the
  current page.
* **#452 — 24-hour university-scale load + soak test suite.** A new
  top-level ``perf/`` harness + ``docs/PERFORMANCE_TESTING.md`` design
  spec that drives the whole DDI stack (Kea + BIND9/PowerDNS + control
  plane + Postgres/Redis) at university scale (~50k students,
  200–300k devices/day on a diurnal curve) to prove the database never
  becomes the bottleneck. Controller + setpoint bus + phase engine +
  device-fleet FSM + perfdhcp/dnsperf ceiling wrappers + a pg war-room
  poller + smoke / 24h / variant manifests. Runs **off** the appliance;
  authoritative-only DNS (recursion OFF, zero egress) is a hard
  constraint.

### Changed

* **Operator Copilot tool registry 198 → 206.** Eight new tools land
  with the features above (``find_dhcp_server_stats``;
  ``find_address_sets`` / ``count_address_sets`` /
  ``propose_create_address_set``; ``find_change_requests`` /
  ``count_change_requests`` / ``propose_approve_change_request`` /
  ``propose_reject_change_request``).
* The DHCP leases endpoint's old ``limit`` (500/5000) is replaced by
  real pagination, so the per-page fingerbank/OUI enrichment now runs
  on the page, not the first 500 rows.

### Fixed

* **#453 — IPAM Sync → DHCP no longer 400s on agent-based Kea.** The
  subnet "Sync → DHCP" modal fans ``POST /dhcp/servers/{id}/sync-leases``
  out to every server backing the subnet; ``sync-leases`` is an
  agentless-only (Windows DHCP) operation and used to hard-reject
  agent-based Kea with a 400, which read as a broken sync. Agent-based
  drivers stream lease events + converge via the ConfigBundle long-poll,
  so there's nothing to pull — the endpoint now returns a no-op
  ``SyncLeasesResponse`` with an explanatory ``note`` (and nudges the
  agent to re-poll its config) instead of erroring. The note renders as
  an info line in both the IPAM sync modal and the DHCP server-detail
  banner.

### Migrations

Three new additive migrations; chain head ``5443d2756647``.

* **#103 — Address sets** (``e2b9c4f1a7d6``). The ``address_set``
  table.
* **#62 — Change requests** (``2c24fe41a7ed``). ``change_request`` +
  ``approval_policy`` storage; 11 built-in policies seeded disabled.
* **#62 — Self-governance lock** (``5443d2756647``). Protects the
  approval-control surface from being rewritten by its own approvers.

---

## 2026.06.19-1 — 2026-06-19

A **certificates + DNS-hardening** release. Three headlines land
together: **#118 TLS certificate monitoring** — auto-discover the certs
serving from the hostnames SpatiumDDI already manages, probe the full
chain, and alert before they expire or break; **#438 the embedded ACME /
Let's Encrypt client** — a hand-rolled RFC 8555 client that issues a
CA-trusted cert for SpatiumDDI's own Web UI (DNS-01 over managed + cloud
zones, HTTP-01, manual fallback, auto-renewal); and **#146 native DNS
rate limiting** — BIND9 Response Rate Limiting + amplification defenses,
a dnsdist front that gives PowerDNS the RRL it lacks, and drop-rate
observability with a `dns_rate_limit_dropping` alert. Riding along:
**#77 saved views**, **#99 a "services using this resource"
reverse-lookup UI**, **#155 appliance APT host-config** (managed sources
/ proxy / GPG keys driven from the UI), a **rollback-safe k3s image
prune** (#441) that stops the shared `/var` partition from creeping full
over upgrades, and a supervisor **secret-handling hardening fix** (#446,
CodeQL alert #82). The Operator Copilot tool registry grows 186 → 198.
All ten schema changes are additive.

### Added

* **#118 — TLS certificate monitoring.** Watch the TLS certs serving
  from the hostnames SpatiumDDI already manages, so expiring or
  misconfigured certs surface before they break clients. New
  ``tls_cert_target`` (connect tuple + per-row schedule + denormalised
  latest cert identity) + immutable ``tls_cert_probe`` history. The probe
  service captures the served chain (leaf + intermediates) via pyOpenSSL,
  resolves the root from the system trust store, and validates trust in a
  separate pass (DNS-rebinding-safe: resolves once + pins the connect
  IP). A discovery reconciler projects targets from opted-in DNS A/AAAA
  records **and** IPAM ``web`` / ``api`` / ``lb`` roles, deduped on the
  connect tuple, re-enabling on return + disabling on drop. Four alert
  kinds (expiring / chain-invalid + SAN-mismatch / unreachable / changed),
  startup-seeded disabled. Surface at ``/api/v1/tls-certs`` (CRUD +
  probes + per-cert chain breakdown + synchronous probe-now), gated on
  the ``tls_cert`` permission + ``security.tls_certs`` feature module
  (granted to Network Editor admin + Auditor read). A **Network →
  Certificates** page (list + filters + saved views, click-through detail
  with the full leaf → intermediate(s) → root chain + PEM), Domain +
  DNS-zone Certs tabs, a DNS-record state pill, and a "Certs expiring
  ≤30d" dashboard KPI. MCP: ``find_tls_cert`` /
  ``count_tls_certs_expiring`` / ``get_cert_chain`` /
  ``count_tls_targets_by_state`` (read, default-on) +
  ``propose_run_cert_probe`` (write, default-off).
* **#438 — Embedded ACME / Let's Encrypt client.** A hand-rolled RFC
  8555 client (``backend/app/services/acme_client/`` — manual JWS over
  ``cryptography`` + ``httpx``) that issues a **CA-trusted cert for
  SpatiumDDI's own Web UI**, landing the chain in the existing
  ``ApplianceCertificate`` storage + deploy path with
  ``source="letsencrypt"``. DNS-01 self-solves over managed zones via the
  record-ops pipeline **and** over cloud zones (Cloudflare / Route 53 /
  Azure / Google agentless drivers), with an ``allow_manual`` TXT
  fallback; HTTP-01 via an unauthenticated ``GET /.well-known/acme-
  challenge/{token}`` route; ``tls-alpn-01`` reports 422 (unsupported on
  the nginx/k3s topology). A 12 h beat task re-issues active certs within
  30 d of expiry (idempotent + advisory-locked), and the
  ``secret_expiring`` alert now covers the LE Web-UI cert. Surface at
  ``/api/v1/appliance/acme`` (account upsert + ``POST /preview`` + ``POST
  /issue`` + orders list/get/cancel) behind the default-enabled
  ``security.certificates`` feature module. Account key + EAB HMAC are
  Fernet-encrypted + never returned. MCP: ``find_certificates`` /
  ``count_certificates_expiring`` (default on) + ``get_acme_account``
  (default off). Distinct from the shipped ACME *provider*
  (``/api/v1/acme/``). See ``docs/features/ACME.md``.
* **#146 — Native DNS rate limiting + amplification defenses.** BIND9
  **Response Rate Limiting** + amplification-reduction knobs
  (``rrl_*`` + ``minimal_responses`` / ``tcp_clients`` /
  ``clients_per_query`` / ``max_clients_per_query``) on
  ``DNSServerOptions`` (group-level), rendered into ``named.conf`` via the
  existing ConfigBundle → ETag → long-poll path. Every field defaults to
  a no-op, so existing groups render byte-identical config until an
  operator opts in. PowerDNS Authoritative has no RRL, so a new
  **dnsdist front** (``ghcr.io/spatiumddi/dns-dnsdist`` image,
  watch-and-reload entrypoint) puts ``MaxQPSIPRule`` + TC/Drop +
  ``dynBlockRulesGroup`` in front of pdns — opt-in via the
  ``dns-powerdns-with-dnsdist`` compose profile + Helm sidecar. Drop-rate
  observability: the agent ships BIND9 ``RateDropped`` / ``RateSlipped``
  into ``dns_metric_sample``, the server-detail Stats tab draws an "RRL
  drops/s" line, and a ``dns_rate_limit_dropping`` alert fires when drops
  over a 15-min window clear a floor (seeded disabled). MCP:
  ``find_dns_rate_limit_settings``. See ``docs/features/DNS.md`` §3.8/§3.9
  + ``docs/drivers/DNS_DRIVERS.md`` §2.6/§2.7.
* **#77 — Saved searches / views.** Per-user ``SavedView(user_id, page,
  name, payload, is_default)`` + ``/api/v1/saved-views`` CRUD (scoped by
  user, audited) behind the default-enabled ``ui.saved_views`` feature
  module, plus a reusable ``SavedViewsMenu`` header dropdown (save / load
  / set-default / auto-apply / delete) wired into the Services / Circuits
  / Sites list pages. MCP: ``find_saved_views`` / ``count_saved_views``.
* **#99 — "Services using this resource" reverse-lookup UI.**
  ``ServicesPage`` gains a ``?resource_kind=&resource_id=`` filter mode,
  and a ``ServicesUsingButton`` entry point (header + compact row) lands
  on VRF / Subnet / IPBlock / Circuit / Site / DNSZone / DHCPScope.
* **#155 — Appliance APT host-config.** Opt-in
  ``platform_settings.apt_*`` (managed sources / proxy / Fernet-encrypted
  GPG keys + private-mirror auth / unattended-upgrades) flows through the
  supervisor heartbeat as an ``apt_bundle`` to a new
  ``spatiumddi-apt-reload`` host runner that **validates a staged config
  with ``apt-get update`` before swapping the live files** (classifying
  failures: proxy-failed / mirror-unreachable / signature-mismatch /
  no-sources). ``POST /settings/apt/validate`` structural pre-check,
  ``find_apt_settings`` MCP tool, an APT Services-sidebar form + a per-row
  ``apt_state`` Fleet chip.

### Changed

* **Operator Copilot tool registry 186 → 198 tools** — the TLS-cert (5),
  ACME (3), saved-views (2), APT (1), and DNS-rate-limit (1) reads land
  this release.
* **dnsdist deployment shape (PowerDNS RRL).** When the dnsdist front is
  enabled, pdns binds ``127.0.0.1:5300`` and dnsdist owns ``:53``; opt in
  via the ``dns-powerdns-with-dnsdist`` compose profile or the
  ``dnsPowerdns.dnsdist.enabled`` Helm value (inherits the
  ``role-dns-powerdns`` node gate).

### Fixed

* **#441 — k3s image accumulation on shared ``/var``.** The containerd
  image store lives on the shared ``/var`` partition and nothing pruned
  superseded releases, so ``/var`` crept toward full over upgrades
  (a field appliance hit 91 %). New ``spatiumddi-image-prune`` removes
  only ``ghcr.io/spatiumddi/*`` images tagged with **neither** slot's
  installed version **and** not referenced by a live container — keeping
  both A/B slots bootable + the running set + all non-SpatiumDDI images,
  and pruning nothing if it can't name both slot versions. Triggered
  async after a healthy slot commit (per-box + each rolling-upgrade node)
  + a weekly timer backstop. **Not** a blunt ``crictl rmi --prune``,
  which would delete the inactive slot's images and break rollback.

### Security

* **#446 — Supervisor secret-bearing host-config triggers written
  owner-only at creation** (CodeQL ``py/clear-text-storage-sensitive-
  data``, alert #82, high). The host-config trigger writer
  (``_fire_host_config`` — SNMP community / APT mirror passwords + GPG
  armour / syslog CA / SSH config) and the k3s-join-token writer landed
  their ``.new`` temp at the umask default (typically ``0644``,
  world-readable) before a follow-up ``chmod`` — a TOCTOU window in the
  ``1777``-sticky ``release-state`` dir where another unprivileged host
  user could race-read the secret (the join token had no ``chmod`` at
  all). A shared ``_write_owner_only`` helper now creates the temp
  ``0o600`` atomically via ``os.open(..., O_CREAT|O_NOFOLLOW, 0o600)`` —
  the mode is set at creation so no window exists, and ``O_NOFOLLOW``
  refuses a planted symlink.
* **#438 — ACME account secrets at rest.** The ACME account key + EAB
  HMAC are Fernet-encrypted in the DB and never returned over the API
  (only an ``eab_hmac_set`` boolean).
* **#155 — APT config fingerprint over opaque ciphertext, not
  cleartext** (CodeQL ``py/weak-sensitive-data-hashing``). The
  ``apt_bundle`` change-detection hash is computed over the
  encrypted-at-rest token material (which changes iff the secret changes),
  marked ``usedforsecurity=False`` — no decrypted private-mirror password
  reaches the digest.

### Migrations

Ten new additive migrations; chain head ``d8f3a1c6e09b``.

* **#438 — ACME client** (``a7f2c9e4d1b8`` → ``b2f5a9c41e07`` →
  ``c3d8a1f9e62b``). Account / order / challenge storage, manual-DNS
  fallback, and the HTTP-01 challenge surface.
* **#77 — Saved views** (``d4e9f2a7c1b8``). Per-user ``saved_view``
  table.
* **#155 — Appliance APT settings** (``e1a4b8c92f3d``). ``apt_*`` columns
  on ``platform_settings`` + the ``apt_state`` reporting column.
* **#118 — TLS cert monitoring** (``f3e8b1d72a9c`` → ``c7d1f04e9a2b``).
  ``tls_cert_target`` (NULLS-NOT-DISTINCT connect-tuple unique) +
  ``tls_cert_probe``, the ``auto_tls_probe`` opt-ins, and the
  ``ip_address_id`` FK for IPAM-role-discovered targets.
* **#146 — DNS RRL + dnsdist** (``b9c3f5e1a8d4`` → ``c5a7e2f9b1d6`` →
  ``d8f3a1c6e09b``). RRL + amplification fields, ``rate_dropped`` /
  ``rate_slipped`` on ``dns_metric_sample``, and the ``dnsdist_*``
  options. All ``server_default``'d no-op.

---

## 2026.06.15-1 — 2026-06-15

A **troubleshooting + hardening** release. The headline is **#59 —
on-demand packet capture (tcpdump)**: a first-class, RBAC-gated, audited
Tools page that captures on the control-plane container *or* an
appliance's real host NICs (picked from a supervisor-reported interface
dropdown), watches live progress, keeps whatever was captured when you
press Stop, and downloads the ``.pcap`` for Wireshark — plus four
Operator Copilot tools. Riding along: a DDI replication + Windows-
integration hardening sweep (#426 / #428 / #430) that closes a systemic
integration-mirror mass-delete class and a dead Kea lease-push, SRV/MX
record-form fixes (#424), slot-upgrade robustness (#419 / #421), a 4-way
CI shard that cuts the backend-test job from ~18 min to ~5 min, and
security bumps for two flagged frontend transitive deps (form-data,
js-yaml). The only schema changes are the additive
``appliance.host_interfaces`` and ``appliance.lldpd_running`` columns.

### Added

* **#59 — Packet capture (tcpdump).** On-demand
  packet capture as a first-class, RBAC-gated, audited platform tool —
  for troubleshooting an appliance/container where there's no easy shell.
  A new **Tools → Packet Capture** page starts a capture (interface +
  BPF filter with presets + stop conditions: packets / duration / bytes +
  snaplen), watches live byte/packet progress, and downloads the
  ``.pcap`` for Wireshark. Mirrors the nmap scanner (persisted job rows,
  5-state lifecycle, history + bulk-delete, ``tools.pcap`` feature
  module, DEMO_MODE lockdown, ``setcap cap_net_raw`` on the binary) but
  the deliverable is a binary ``.pcap`` artifact on disk (download-only,
  auto-pruned after ``pcap_retention_days`` — default 7 — since captures
  are large + sensitive). The BPF filter is passed as tcpdump's single
  trailing argv element (never shell-interpolated) + charset-validated.
  Phase 1 runs tcpdump in the worker container (the control-plane network
  vantage); a new dedicated ``manage_packet_capture`` permission (seeded
  to **Network Editor**, not Viewer — captured bytes are a privileged
  read) gates every endpoint, and every start / download / cancel /
  delete is audit-logged. Operator Copilot gets read tools
  (``find_packet_captures`` / ``count_packet_captures`` /
  ``get_packet_capture`` — metadata only, never bytes) + a gated
  ``propose_run_packet_capture`` write. **Appliance-host vantage (Phase 2)**
  adds capture on an appliance's *real NICs*: the operator picks the
  appliance from a vantage dropdown (or the Fleet drilldown's "Packet
  capture" link), the supervisor's ``pcap_proxy`` long-polls a cert-authed
  DB-poll dispatch (any api replica can serve it — no in-memory queue to
  strand the job), drives a host-side ``spatium-pcap-runner`` over the
  existing trigger-file → systemd ``.path`` pattern (tcpdump runs in the
  real host net namespace — NOT a privileged ``hostNetwork`` pod), streams
  progress, and uploads the finished ``.pcap`` back over the supervisor
  channel. The appliance-vantage interface is a **dropdown of the host's
  real NICs** — the supervisor enumerates them from ``/run/udev/data`` and
  reports them on its heartbeat (persisted to ``appliance.host_interfaces``),
  so the operator picks ``ens18`` instead of guessing — with an *Other…*
  free-text escape hatch for NICs udev didn't name (bridges / overlay /
  VPN). Pressing **Stop** now **keeps the packets captured so far**
  (tcpdump flushes a valid savefile on SIGTERM) and offers the download
  right there, no trip to History. On a single-node appliance the api +
  worker share the pcap dir via a hostPath; multi-node server-vantage
  download needs the slot-image mirror proxy (a tracked follow-up).

### Changed

* **#435 — backend-test CI sharded 4-way.** The ~1,900-test suite is
  DB-bound (per-test connection churn) and ran with only 4-way
  parallelism on a single runner (~18 min). It now fans out across four
  concurrent runners via ``pytest-split`` (each still ``-n auto``), with a
  small aggregator job that preserves the required ``Backend — Tests``
  check name so branch protection is unchanged — wall-clock ~18 min →
  ~5 min. Coverage is dropped from the PR gate (it was a non-gating
  ``continue-on-error`` upload that added ~15-30% tracing overhead on
  every run; it can return as a nightly/main-only job).

### Fixed

* **#430 — DDI hardening sweep: integration-mirror mass-delete on
  degraded reads + agent wire-contract + ConfigBundle/wake gaps.** A
  three-area audit (integration mirrors, agent↔control-plane wire
  contracts, ConfigBundle ETag + wake coverage) surfaced one systemic
  data-loss class plus a batch of wire/lifecycle gaps. **Systemic
  (highest value):** every read-only integration mirror (Kubernetes,
  Docker, Proxmox, Cloud AWS/Azure/GCP, UniFi, OPNsense, Tailscale)
  could **mass-delete the entire mirror on a degraded read** — a 200
  with a wrong-shape body (proxy/auth error page, envelope change,
  ``data: null``) or a partial multi-scope pull collapsed to "zero
  items", and the unconditional absence-delete pass purged every
  mirrored IPAM/DNS row while reporting ``ok=True``. UniFi was the
  worst: a routine ``stat/sta`` 429 on one site mass-deleted that
  site's subnets + client addresses. Fixed in two layers — a shared
  shape guard (``app/services/_mirror_shape.py``) makes each client
  **raise** its typed error on a wrong-shape 200 (a legitimately-empty
  upstream like ``{"items": []}`` still returns ``[]``), and the
  reconcilers now **skip the absence-delete pass on a partial pull**
  (cloud connectors record ``failed_scopes`` + the reconciler upserts
  but doesn't delete + records the partial pull in ``last_sync_error``;
  UniFi aborts the whole reconcile on a per-site fetch failure like the
  Proxmox per-node pattern). **Agent wire-contract / bundle fixes:**
  DNS per-zone TTL was pinned to a constant 3600 (a ``default_ttl``
  getattr typo — the column is ``ttl``) so editing a zone's TTL never
  re-rendered; DNS per-server convergence (the ZoneSyncPill) never
  reported because the bundle shipped no per-zone serial, so the agent
  skipped every zone; DHCP static reservations dropped ``client_id`` +
  ``options_override`` on the wire (a client-id-keyed reservation
  silently fell back to MAC); DHCP scope ``min/max_lease_time`` were
  settable + in the ETag but never reached the agent (now rendered as
  Kea ``min/max-valid-lifetime``); and a DNS **view's
  ``allow_query`` / ``allow_query_cache`` ACL** round-tripped through
  the API but was never carried in the bundle or emitted by the BIND9
  renderer, so a view-scoped query ACL silently never took effect.
  **Wake / hardening:** the appliance fleet-firewall master switch
  (``PUT /enforcement``) and mgmt-UI CIDR lock (``PUT /web-ui-access``)
  published no agent wake, so security changes converged only when the
  parked heartbeat hold timed out (≤28 s) — they now wake immediately;
  DHCP server create/delete now wake the group (an HA-membership change
  re-renders peers); the DHCP lease shipper caps its pending buffer
  (was unbounded on a persistent reject); and the DNS heartbeat model
  gains ``extra="forbid"`` + a bounded ``ops_ack`` so a wrong-envelope
  ACK batch 422s loudly instead of silently clearing the agent's ACK
  queue. The supervisor's ``lldpd_running`` (shipped every heartbeat
  since #347 but silently dropped) is now persisted.

* **#428 — DHCP↔IPAM↔DNS replication gaps (Kea push dead + cleanup
  holes).** A replication audit found the **Kea lease-event push was a
  silent no-op**: the agent shipped ``{"events":[{"ip","mac"}]}`` but the
  server's ``LeaseEventBatch`` expects ``{"leases":[{"ip_address",
  "mac_address","expires_at"}]}`` with ``leases`` defaulting to ``[]``, so
  the POST validated to an empty batch, returned 200, and the agent
  cleared its queue — every Kea lease dropped, so a Kea-served client was
  never mirrored to IPAM and never DDNS-registered (the Windows pull path
  was unaffected; there's no Kea poll fallback). The agent now emits the
  server's exact shape (incl. a non-NULL ``expires_at`` so the time-based
  sweep can reap Kea mirrors), and the batch model gains ``extra="forbid"``
  so any future envelope drift 422s loudly instead of phantom-succeeding.
  Two cleanup/consistency fixes ride along: the scheduled
  ``ipam-dns-auto-sync`` backstop is now **DDNS-aware** — it regenerates a
  lease's DDNS record if a swallowed inline apply missed it (incl.
  generated ``dhcp-<x-y>`` names the drift check can't see), and it honors
  the DDNS opt-in by **not** publishing lease-mirrored rows for subnets
  whose DDNS is disabled (manual allocations still sync); and **subnet
  soft-delete now revokes** the lease mirrors' DDNS A/PTR records + drops
  the transient mirror rows (they were orphaned on the DNS server before),
  with a warning in the delete-subnet modal. DDNS stays opt-in by design.
  Also folds in the smaller audit findings: DDNS now consumes the
  previously-dead ``ddns_ttl`` (stamps the record TTL) and
  ``ddns_domain_override`` (publishes the forward record into the override
  zone); an agentless Windows-DNS record whose synchronous apply *fails*
  no longer leaves ``dns_record_id`` stamped (so DDNS retries instead of
  permanently skipping it); and Windows-pull DDNS now wakes the agent
  long-poll on commit so it converges immediately instead of on the safety
  tick.

* **#426 — Windows DNS/DHCP integration hardening (audit follow-up).** A
  full audit of the WinRM/PowerShell integration confirmed 19 defects;
  this fixes them. **Blocker:** DHCP bulk reservation/exclusion writes
  embedded a full PowerShell snippet per op and overflowed the 8191-char
  CMD.EXE ``-EncodedCommand`` cap at 3–4 ops (a 30-op batch was ~9× over)
  → 502 + rollback. The DHCP and DNS batch dispatchers now ship
  **data-only** payloads with one shared cmdlet body and pack each chunk
  under the encoded-command budget (a new ``app/drivers/_winrm.py``
  chokepoint measures it), so large batches — including DKIM/SPF/DMARC TXT
  records — split instead of overflowing, and an over-budget single op
  fails just itself. **Data-loss fixes:** the lease pull no longer
  pre-filters ``State -eq 'Active'`` scopes (leases under a deactivated
  scope were being absence-deleted), and a garbled/truncated lease
  response now re-raises instead of reading as "zero leases" (which
  drove a full lease + IPAM-mirror purge stamped ``success``).
  **Write-through fixes:** exclusion idempotency is now a locale-safe
  pre-check (the old English ``'already'`` substring match 502'd on
  non-English hosts), an IP-only reservation edit now does remove-then-add
  (Windows can't relocate a reservation's IP via ``Set-``), and the scope
  option reconcile is set-then-prune (a mid-reconcile failure no longer
  leaves options wiped). TXT values are quote-normalised consistently
  across all write paths. **Transport hardening:** a shared chokepoint
  adds WinRM operation/read timeouts, a one-time WARNING when TLS
  validation is off or basic auth runs over cleartext (matching the #289
  clients), TLS-aware port defaulting (5986 for HTTPS, not 5985), and
  transport/port validation on the credential schemas. The #424 SRV/MX
  field mapping was verified correct and unchanged.

* **#424 — couldn't define SRV records in the UI; MX priority could be
  NULL.** The Add/Edit DNS Record form only exposed a Priority field, so
  SRV records were created with NULL ``weight`` and ``port`` — and every
  driver (bind9 / powerdns / windows) substitutes ``0`` for a NULL,
  rendering a meaningless ``priority 0 0 target``. The form now shows
  Priority + Weight + Port for SRV (all required) and a clean
  Priority-only field for MX, and every record type gained a wire-format
  placeholder for its Value (CAA / NAPTR / SSHFP / TLSA / LOC / SVCB /
  HTTPS show the expected RDATA shape). The API enforces the per-type
  rules server-side: SRV requires priority + weight + port, MX takes only
  a priority and defaults it to 10 (no more NULL MX priority), and every
  other type rejects stray priority/weight/port with a 422. A one-shot data
  migration backfills existing rows the old UI left NULL (SRV
  priority/weight/port → 0, MX priority → 10 — the values the drivers
  already substitute, so wire output is unchanged) so those rows stay
  editable under the new validation. The Operator Copilot's
  ``create_dns_record`` proposal gained ``weight`` + ``port`` so the
  assistant can create valid SRV records too — and now correctly enqueues a
  record op so a Copilot-created record actually propagates to the agents
  (previously it landed in the DB but was never pushed).

* **#419 — slot-upgrade wedged at "in-flight" on pre-2026.06.12
  appliances.** The control plane appends a per-apply re-fire nonce to the
  slot-image URL as a ``#fragment``; the host runner only strips that
  fragment before fetching as of #386 (2026-06-12), so an appliance on an
  older supervisor handed the fragment straight to the downloader and the
  apply hung at "in-flight" forever. The nonce is now gated on the target
  supervisor's reported version — pre-2026.06.12 and unknown supervisors
  get a clean URL their runner can fetch, while current ones keep the
  nonce for same-image re-fire. (Recover an already-stuck box on the host
  with ``spatium-upgrade-slot apply <url> --checksum <url>`` then reboot.)

* **#421 — slot upgrade could hang at "in-flight" forever.** #419 fixed
  the one cause that wedged a field box; this is the general robustness
  gap — *any* mid-apply death (an OOM-killed ``dd``, a stalled download, a
  killed process, power loss) left the upgrade state stuck at "in-flight"
  with no terminal write, so the Fleet UI spun indefinitely and the
  operator got no retry signal. Now four guards make a dead/stalled apply
  surface as **failed** within minutes: the host runner re-stamps the
  in-flight marker every 60 s while alive (so a frozen stamp means the
  runner is gone), caps the apply with ``timeout`` so a stalled download /
  ``dd`` self-fails, and writes ``failed`` from an EXIT trap on any abrupt
  exit; the ``spatiumddi-slot-upgrade.service`` unit gains a
  ``TimeoutStartSec`` backstop above the runner's own cap; and the
  supervisor flips a stale in-flight (stamp older than 5 min — well above
  the 60 s tick, so a live-but-slow apply is never falsely failed) to
  ``failed``, healing the ``.state`` sidecar + lingering trigger so the
  operator can clear + re-apply from the Fleet drilldown. The drilldown
  also shows a "looks stuck — check the host or re-apply" hint if an
  apply sits in-flight past ~6 min.

### Security

* **Frontend transitive dependency bumps (Dependabot #18 / #19).**
  ``form-data`` 4.0.5 → 4.0.6 (GHSA-hmw2-7cc7-3qxx — CRLF injection via
  unescaped multipart field names/filenames, **high**) and ``js-yaml``
  4.1.1 → 4.2.0 (GHSA-h67p-54hq-rp68 — quadratic-complexity DoS in
  merge-key handling, moderate). Both are transitive (pulled by build/
  test tooling), so the change is ``package-lock.json``-only; ``npm
  audit`` reports zero vulnerabilities after the bump.

### Migrations

* **#59 — ``appliance.host_interfaces``** (``c3a1f9d24b80``). Adds a
  nullable JSONB column holding the host NICs the supervisor enumerates
  (for the appliance-vantage packet-capture interface picker). Additive +
  nullable; no backfill.
* **#430 — ``appliance.lldpd_running``** (``a3f7c1e84d59``). Adds a
  nullable Boolean column persisting the supervisor's LLDP daemon
  up/down status (companion to the already-wired ``lldp_neighbours``
  set). Additive + nullable; no backfill.

### Deprecated

* **#430 — DNS heartbeat ``zone_serials``.** Serial convergence is
  reported via the dedicated ``/dns/agents/zone-state`` endpoint; the
  current agent no longer sends ``zone_serials`` in its heartbeat. The
  field is retained (default ``{}``) on the request model so pre-#430
  agents still validate under the new ``extra="forbid"``.

---

## 2026.06.14-1 — 2026-06-14

An **appliance polish + ops** release rolling up four issues (#392 /
#415 / #416 / #417) plus a Cluster-dashboard redesign, a comprehensive
demo-seeder refresh, and a CodeQL fix. The headline is a browser
**Cluster console**: the appliance's Talos-style physical-console
dashboard reproduced in the web UI and merged into the Cluster → Overview
screen — an identity ribbon (role / host / version / A-B slot / pairing /
supervisor health), live node + workload health, and a streaming pod-log
tail, all over the existing authenticated SSE feed (no shell, no new
privilege). It also fixes a Permission-denied that blocked OS-upgrade
image uploads on the k3s appliance, stops old releases from hoarding
multi-GB binaries, and moves the frontend build to Node 22. No schema
changes.

### Added

* **#416 — browser Cluster console.** The appliance's physical-console
  dashboard, reproduced in the web UI and folded into Cluster → Overview
  as a single view: a live identity header (role, hostname, host IP,
  OS + app version, A/B slot + durable-default + trial-boot, upgrade
  state, nodes-ready + HA, supervisor approval + last-seen), the
  CPU / memory / nodes / workloads health ribbon and charts, and a
  live log tail that follows a **workload** (deployment / daemonset) —
  resolved server-side to the current pod *and* container, so it survives
  pod rolls, never shows churny pod names, and handles multi-container
  pods (e.g. redis) — plus Reboot-host and View-pods actions. Backed by a new `self_appliance` block on
  `GET /appliance/info` sourced from the supervisor heartbeat — a pure
  DB read, so the api never touches host journald or files. Reproduced
  in React rather than bridging a PTY, so it re-exposes no shell / root
  surface and works for remote agents too; the standalone TTY
  `spatium-console` is unchanged.

### Changed

* **#417 — frontend build moves to Node 22.** CI and the frontend
  Dockerfile build on `node:22-alpine` (Node 20 reached end-of-life in
  April 2026; the Vite 8 toolchain prefers 22.12+). Runtime is
  unaffected — Node is build-time only.
* **#392 — version-aware release-asset retention.** A new prune step
  (post-release plus a weekly scheduled workflow) trims the heavy
  appliance binaries every release attaches: the generic-named duplicate
  ISO / slot images come off every non-latest release, and the versioned
  `.iso` + slot `.raw.xz` come off releases beyond a configurable keep
  window (default 15). The release, tag, notes, and the versioned
  `.sha256` provenance sidecars are always kept, and a freshly-cut
  release is guarded against propagation races so its `/latest/`
  download URLs never break. The OS-upgrade picker already hides any
  release missing its slot image, so it stays honest after a prune.
* **Cluster "Pods" KPI no longer counts completed Jobs.** The dashboard
  tile divided running by *total* pods, so a healthy box read `10/14`
  (the four being finished migrate / helm-install Jobs) and looked
  broken. It now divides by *active* pods, excluding the `Succeeded`
  phase: a healthy box reads `10/10` with a small "N completed" note,
  and the tile turns rose only on a genuine shortfall.
* **`scripts/seed_demo.py` catches up to the data model.** The demo
  seeder gained the 17 resource families it had drifted behind on — NAT
  mappings, multicast domains / groups, customers / sites / providers,
  WAN circuits, the service catalog, SD-WAN overlays, DNS sub-resources
  (DNSSEC / blocklists / views / GSLB / catalog zones), DHCP
  sub-resources (classes / statics / MAC blocks / option templates /
  PXE profiles), a sample RBAC role + user + group, a scoped API token,
  a conformity policy, and a webhook subscription.
* **Appliance README gains a "Why k3s, not Docker Compose?" rationale**
  (declarative reconciliation, growing one box into an HA cluster, no
  hand-run container commands), cross-linked from the main README for
  readers wary of Kubernetes.

### Fixed

* **#415 — OS-upgrade image upload failed with Permission denied on
  k3s.** The api pod runs as uid 1000, but kubelet created the
  `/var/lib/spatiumddi/slot-images` hostPath as `root:root 0755`, so
  every upload / GitHub import died with `EACCES`. The
  `spatiumddi-firstboot` boot step now creates and owns that directory
  (1000:1000, mode 0700) on every boot — mirroring the existing
  release-state fix — and the api container pins its runtime uid for
  good measure. Also corrects a cosmetic `pathlib` bug that wrote the
  in-flight temp file with a doubled suffix
  (`<id>.raw.raw.xz.partial`) instead of `<id>.raw.xz.partial`.
* **Stale "upgrade images" pointer in the fleet upgrade modal.** The
  empty-state told operators to open a section that had moved; it now
  offers a button that closes the dialog and jumps straight to the
  Upgrade-images view.
* **celery-beat platform health check failed on HA Redis.** The beat
  probe used a raw `aioredis.from_url`, which rejects the `sentinel://`
  scheme used by HA Redis (`Redis URL must specify one of redis:// …`),
  so the Platform Health card showed beat as errored on a Sentinel
  deployment. It now uses the same Sentinel-aware helper as the redis +
  workers probes.
* **Fleet "Upgrade pending" no longer promises a fire that can't come.**
  When an appliance's supervisor predates the 2026.06.12 upgrade-progress
  telemetry it can't report progress, so the panel sat on "supervisor
  will fire the trigger in ≤30 s" forever. It now detects the old version
  and explains it, pointing at the host-side check / recovery
  (`spatium-upgrade-slot status`, `journalctl -u spatiumddi-slot-upgrade`).

### Security

* **Stack-trace exposure on the Cluster health endpoints (CodeQL
  `py/stack-trace-exposure`, alert #68).** The one-shot and SSE
  cluster-health handlers echoed the raw kubeapi exception text into the
  client-facing error (`kubeapi unreachable: <exc>`), which could leak
  internal detail to the operator's browser. Both now log the exception
  server-side and return a generic reason.

---

## 2026.06.13-2 — 2026-06-13

A focused **field-fix release** — five issues (#407–#411) surfaced while
bringing up a fresh multi-appliance install on a Kubernetes / Helm
control plane, shipped together as #412. The headline is a **hotfix for a
reboot regression that 2026.06.13-1 introduced**: that release's
auth-hardening (#400 C1) tightened the supervisor heartbeat and, as a
side effect, silently broke reboot, fleet OS-upgrade, and role delivery
for any approved appliance whose supervisor wasn't presenting a
validating client cert. The rest fix off-cluster agent registration, a
settings / UI gap that blocked appliance pairing on Helm control planes,
a live-progress UX bug, and SSO operators being locked out of every
sensitive-secret reveal. No schema changes.

### Fixed

* **#411 — reboot / fleet-upgrade / role delivery dead on
  2026.06.13-1.** #400 C1 made the supervisor `/heartbeat` require a
  client cert OR a valid session token, removing the prior
  `state==APPROVED` bypass. An approved supervisor whose cert isn't
  validating in the field had been riding that bypass; once it was gone
  every heartbeat 403'd, and because the heartbeat returns before firing
  desired-state triggers, this silently killed reboot **and** fleet
  OS-upgrade **and** role assignment — not just the reboot button the
  report noticed. Restored the session-token path: a cert-auth failure on
  the heartbeat now falls back to the session token (still a real
  per-appliance secret; the cluster-admin k3s join token stays gated on a
  valid cert), the supervisor keeps sending its token alongside the cert,
  and re-register mints a fresh token for already-cert'd rows so a box
  that lost its token can recover one. Every #400 C1 invariant is
  preserved (uuid-only rejected, wrong-token rejected, join-token
  cert-only). The mTLS-cert-auth steady state + an auto-recover-on-403 so
  a blanked-token box recovers without a manual re-pair are tracked as a
  follow-up.
* **#409 — DNS / DHCP agents fail to register with `[Errno -2] Name does
  not resolve` on an Application appliance.** An Application appliance is
  its own single-node k3s with no in-cluster `spatium-control` api
  Service, so the #292 hardcoded in-cluster URL didn't resolve. The three
  role-pod chart templates now prefer the supervisor-supplied external
  `controlPlaneUrl` (→ `inClusterApiUrl` → the in-cluster default),
  skipping TLS verification only on that external self-signed path — the
  same trust-on-first-use model the supervisor pod already uses
  (pairing-code + mTLS client cert are the real auth; TLS is just LAN
  transport). Control-plane / full-stack appliances send
  `controlPlaneUrl=""` and fall through to the unchanged in-cluster path.
* **#410 — appliance OS-upgrade progress needed a hard page reload.** The
  Fleet drilldown rendered a frozen open-time snapshot of the appliance
  row, so the per-phase upgrade progress shipped via the supervisor
  heartbeat never updated. It now renders the freshly-polled row by id (a
  pure derivation — no flicker) and fast-polls every 2 s while any upgrade
  or reboot is in flight.
* **#407 — no way to enable appliance registration on a Helm control
  plane.** `platform_settings.supervisor_registration_enabled` gates
  supervisor pairing but was never exposed in the settings API, so
  operators on a generic Kubernetes / Helm control plane (where the
  OS-appliance self-bootstrap that auto-enables it never fires) could only
  turn it on by hand-editing the database. Exposed it on the settings API
  and added a **superadmin-only** toggle to the Fleet → Pairing tab; the
  default stays FALSE, the write is superadmin-gated (mirroring
  maintenance mode), and a flip writes a dedicated audit row.

### Security

* **#408 — SSO operators can now re-confirm sensitive reveals.** The
  agent-keys / SNMP-community / appliance-kubeconfig / pairing-code
  reveals re-verified a *local password* and hard-rejected every
  external-auth account, and MFA enrolment was itself local-only — so an
  OIDC / SAML / LDAP / RADIUS / TACACS+ superadmin had no way to reveal
  those secrets. A new shared `reverify_operator` re-confirmation helper +
  MFA enrolment opened to every auth source close the gap: **local users
  prove their password; password-less SSO users prove a TOTP code** (enrol
  under Account → Two-factor). A shared re-confirm component (password or
  authenticator code) lands on all four reveal modals. An adversarial
  review caught a defense-in-depth downgrade in the first cut — accepting
  TOTP *in lieu of* the local password would let a hijacked session of a
  local superadmin self-enrol MFA and reveal without the password — so the
  shipped behaviour requires the password for local accounts and reserves
  TOTP for password-less SSO accounts only. Deferred follow-ups: a
  single-use replay guard on the reveal TOTP, widening the Security
  dashboard's MFA-coverage rollup to include SSO enrollees, and adopting
  the same helper for the delete-appliance / factory-reset flows.

---

## 2026.06.13-1 — 2026-06-13

The **appliance console + security-hardening** release. Six PRs: a
Talos-style operations cockpit and a live Cluster dashboard for the
appliance console / UI, a 3-mode console selector that fixes a
no-login regression, an in-place host-migration framework so
install-time / ESP changes reach already-installed boxes on a slot
upgrade, a firewall navigation refresh with a realtime
dropped-packet log viewer, and a full internal auth-bypass /
privilege-boundary review (#400) that remediates 1 critical, 3
high, and 12 medium / low findings with ~55 regression tests.

**#400 — auth-bypass / privilege-boundary review (#401).** The
headline security item. **C1 (critical):** the supervisor
`/heartbeat` endpoint was unauthenticated, so anyone who could
reach the control plane could register a fake appliance and pull a
signed supervisor cert → k3s cluster-admin; it now requires the
supervisor session token. **C2–C4 (high):** AI-apply re-checks the
caller's RBAC at apply time (not only at propose), the `dns_zone`
token path no longer trusts a caller-supplied zone id (IDOR), and
role / group edits are capped by the editor's own permission
ceiling so an admin can't grant beyond what they themselves hold.
Plus 12 medium / low hardening items spanning sessions, the
force-password-change gate, response info-leak trimming,
CORS / TrustedHost tightening, an SSRF guard on outbound-fetch
surfaces, and frontend token-handling + security-header fixes.

**Appliance console & Cluster UI.** The serial / VGA console gains
a Talos-style operations cockpit (#398) and the web UI gains a
consolidated **Cluster** tab with a live SSE-fed Overview dashboard
(#402) — both grounded in kubelet stats + node / pod listings since
there is no Prometheus on the appliance. A new firewall navigation
layout adds a realtime nftables dropped-packet log viewer that
works across remote appliances too (#404).

### Security

* **C1 (critical) — unauthenticated supervisor heartbeat → k3s
  cluster-admin (#400).** The `/api/v1/appliance/supervisor/heartbeat`
  endpoint accepted any caller, so anyone able to reach the control
  plane could impersonate a supervisor, get approved, and receive a
  signed supervisor certificate that grants k3s cluster-admin. The
  endpoint now requires the supervisor session token (regression
  test in `test_supervisor_heartbeat_authz.py`).
* **C2 (high) — AI-apply skipped the apply-time RBAC re-check.** A
  `propose_*` plan that passed the proposer's permissions could be
  applied by a caller who no longer (or never) held them; apply now
  re-validates the caller's RBAC against the concrete mutation.
* **C3 (high) — `dns_zone` token-scope IDOR.** The DNS token path
  trusted a caller-supplied zone id instead of deriving scope from
  the authenticated token, letting a scoped token act outside its
  zone. Scope is now resolved server-side.
* **C4 (high) — role / group edits could exceed the editor's
  ceiling.** Role and group mutations are now bounded by the
  editing user's own effective permissions, so an admin cannot
  grant a permission they do not themselves hold.
* **12 medium / low hardening items.** Session fixation + idle
  timeout, the force-password-change gate, response info-leak
  trimming, CORS / TrustedHost allow-list tightening, a new SSRF
  guard (`backend/app/core/ssrf.py`) on outbound-fetch surfaces, and
  frontend token-handling + security-header fixes. ~55 regression
  tests across `test_fix_c*` / `test_fix_m*` / `test_fix_l*`.

### Added

* **#398 — Talos-style operations cockpit on the appliance
  console.** Replaces the prior console with a 6-box KPI ribbon
  (Cluster / etcd / Postgres / API / Platform / Slot) fed by
  concurrent 5 s-tier collectors, an F7 Health drill-down with
  guarded recovery actions, multi-node quorum + auto pod-filter +
  cluster-wide Top-pods + a MetalLB speaker check, font-safe glyphs,
  content-sized panels, and level-token log colouring. Field-tested
  on ddi1 at ~2.5 % CPU.
* **#402 — live Cluster Overview dashboard.** The Overview section
  of the new Cluster tab streams cluster health over SSE (2 s) into
  a KPI ribbon, a Recharts CPU / memory hero chart, animated
  per-node radial gauges, a workload-health panel, and a top-pods
  leaderboard — all from kubelet stats + node / pod listings (no
  Prometheus on the appliance). Per-node disk shows the host's real
  partition list (supervisor-reported, with a kubelet `/var`
  fallback). Needs a `nodes/proxy [get]` RBAC grant and a read-only
  host-root mount on the supervisor, both shipped in the charts.
* **#404 — realtime nftables firewall-log viewer.** An opt-in global
  toggle appends a rate-limited `log` rule to the rendered input
  policy; the supervisor tails `/dev/kmsg` for `spatium-fw:` drops
  into a ring buffer, surfaced through a new `firewall_logs` nettool
  so the Firewall tab streams dropped / rejected packets live —
  including for remote appliances via the per-appliance nettool
  proxy. Ships a `kernel.dmesg_restrict=0` sysctl drop-in so the
  unprivileged supervisor can read the kernel ring buffer.
* **#395 — in-place host-migration framework.** A/B slot upgrades
  replace the rootfs but not the install-time, shared-ESP
  `grub.cfg`, so menu-structure or kernel-cmdline changes (like the
  verbose-boot branch from #393) previously required a full
  reinstall to reach an installed box. New `spatium-grub-render` (idempotent,
  single source of truth for grub.cfg, shared by the install + live
  paths) and `spatium-host-migrate` (version-gated, every-boot
  reconcile of numbered idempotent host-patches run before the
  health-commit, so a failed render blocks the slot commit and the
  A/B one-shot reverts) close that gap; failures surface as
  `host_migration_health` in the Fleet UI.

### Changed

* **#393 — 3-mode console selector replaces the verbose-boot
  toggle.** The old binary toggle conflated boot verbosity with the
  post-boot console (dashboard vs. plain login) and couldn't express
  "verbose boot, then dashboard". `console_mode` ∈ {`dashboard`
  (default), `verbose_dashboard`, `text_console`} maps to the
  grubenv `spatium_verbose` 0 / 2 / 1 the grub menuentries read
  (keeping dashboard = 0 + text_console = 1 so old grubenv values
  still resolve fail-closed); the NetworkTab toggle becomes a
  3-option radio.
* **#402 — Cluster tab consolidation.** The standalone Pods tab and
  the etcd-snapshots section fold into a single **Cluster** tab
  (Overview / Pods / etcd left sub-nav) — named "Cluster" rather
  than "Kubernetes" for operator clarity.
* **#404 — Firewall + Fleet navigation refresh.** The Firewall tab's
  top sub-tabs become a left sub-nav (Policies / Aliases / Preview /
  Effective / Logs), with the Enforcement + Web-UI-access cards
  nested compactly inside Policies; Rolling Upgrade, Releases (folded
  into Rolling Upgrade), and Web UI Certificate move into the Fleet
  sidebar, with legacy `?tab=` deep-links remapped.

### Fixed

* **#394 — helm-stuck-recover crashed on every boot.**
  `spatiumddi-helm-stuck-recover` (the #183 Phase 9 safety net) died
  with `JSONDecodeError: Invalid control character` because it
  shell-interpolated kubectl JSON into a Python heredoc parsed in
  strict mode, so a genuinely wedged HelmChart would never
  auto-clear. Now passes the JSON via an env var and parses with
  `strict=False`.
* **#393 — `text_console` mode left the appliance with no console
  login.** `spatium-console.service` still declared
  `Conflicts=getty@tty1.service`, which is registered at unit-load
  time, so even with the dashboard correctly inactive in
  `text_console` mode the recorded conflict dropped `getty@tty1`
  from the boot transaction — bare kernel text, no prompt. Removed
  the `Conflicts=` (dashboard and getty are already mutually
  exclusive via opposite `spatium-console=off` conditions);
  field-validated end-to-end on slot B.
* **#404 — dead Platform Insights → Containers link.** The Platform
  Insights card linked to the removed `?tab=containers`; it now
  points at `Cluster → Pods`.

### Migrations

* `a7c3e9f1b405` (#393) — adds `platform_settings.console_mode`,
  backfills it from the old `verbose_boot` flag (`True` →
  `text_console`), then drops `verbose_boot`.
* `b3e7d1f9a204` (#395) — adds `appliance.host_migration_health`
  (JSONB, not null, default `{}`).
* `c5f1a2b3d4e6` (#404) — adds
  `platform_settings.firewall_logging_enabled` (bool, default
  `false`).

---

## 2026.06.12-2 — 2026-06-12

Appliance **upgrade + host-config reliability** — three appliance
fixes, all field-validated on a single-node appliance.

**#386 — single-appliance OS upgrade now works end-to-end.** Applying
an imported / uploaded OS slot-image (the #199 flow) had failed every
time, retried silently forever, and showed nothing useful in the
Fleet UI: the download hit the appliance's own self-signed web cert,
a failed apply re-fired every ~30 s with no backoff (and the
failed→ready auto-heal masked it), and the drilldown showed only a
coarse state chip. Fixed across four parts — verify-by-hash +
relax-TLS-for-the-self-served-URL download, a fire-once /
honest-failure / sidecar-prune guard, a live per-phase progress
stepper, and a persistent upgrade-image store.

**#387 — host-config runners crash-loop silently.** The
GUI-configured NTP settings had **never** applied: the
`spatiumddi-chrony-reload` runner validated the staged config with
`chronyd -t -f <file>`, but `-t` is chrony's *timeout* flag (it takes
a numeric argument), so chrony parsed `-f` as the value and died with
`Fatal error : Invalid argument -f` on every apply. Since each
host-config runner only writes its applied-hash sidecar on SUCCESS,
the supervisor re-derived "desired ≠ applied → fire" every ~30 s
heartbeat and re-fired forever — **2374** `ntp-config-pending.failed`
sidecars on the test box (plus 2021 `slot-set-next-boot-pending.done`
from the same shape). #387 fixes the chrony flag and generalises
#386's fire-once / backoff / prune pattern to every hash-keyed
host-config runner (snmp / ntp / lldp / syslog / ssh / resolver /
firewall / timezone), so none of them can silently loop again, and
surfaces a stuck apply honestly in the Fleet drilldown.

**#389 — Fleet etcd-snapshot inventory always empty** — the
supervisor lists the k3s `ETCDSnapshotFile` CRs over the kubeapi, but
its ServiceAccount lacked the read grant, so the list reported empty
every heartbeat even though k3s was taking snapshots on schedule.

### Fixed

* **#386 — OS slot-upgrade download failed on self-signed TLS.** The
  scheduler points `desired_slot_image_url` at the appliance's OWN
  control plane (`https://<self>/api/v1/appliance/upgrade-images/<id>/raw.xz`),
  behind the self-signed web cert; the host runner's bare
  `urllib.urlopen` verifies TLS by default → `CERTIFICATE_VERIFY_FAILED`,
  so the apply never even downloaded. The scheduler now stamps the
  image's stored sha256 + a `tls_insecure` flag; the runner verifies
  the bytes against the hash (the real integrity guarantee) and skips
  cert-verify ONLY for the self-served URL. External public-CA URLs
  stay fully verified, and insecure-TLS is refused host-side unless an
  expected hash is present.
* **#386 — upgrade re-fired silently forever + hid the failure.** A
  failed apply renamed the trigger away but left `desired_version`
  set, so the supervisor re-fired every ~30 s heartbeat with no
  backoff (flooding `.failed.<ts>` sidecars), and the failed→ready
  auto-heal then read "ready" mid-failure. Now a per-apply nonce
  fragment on the URL makes each schedule a distinct desired-state,
  the supervisor fires exactly once per distinct URL (fire-once
  marker), a cleared/cancelled upgrade resets the marker + heals a
  stale `failed`, failures are reported honestly, and old sidecars are
  pruned. (This is the slot-upgrade guard #387 then generalised to the
  other host-config runners.)
* **#386 — imported upgrade-image bytes vanished on api restart.**
  Uploaded / GitHub-imported `.raw.xz` lived in the api's ephemeral
  container layer, so on any api restart the DB row survived but the
  bytes 404'd (`Upgrade image bytes missing on disk`) and a scheduled
  upgrade failed at download. Added a persistent `slot-images`
  hostPath mount on the api (single-node; the #296 slot-image mirror
  PVC already covers multi-node, so the mount is gated off when the
  mirror is enabled).
* **#387 — NTP config never applied on the appliance.** The chrony
  config-apply runner's syntax check used `chronyd -t -f <file>`;
  `-t` is the *timeout* option (numeric arg), not a "test config"
  flag, so the check died with `Fatal error : Invalid argument -f`
  and every operator-pushed NTP change silently failed to apply
  (chrony kept running on the image-baked pool config, so time sync
  itself was unaffected — only GUI NTP changes were dropped). Now
  uses `chronyd -p -f <file>` (parse-and-print, exits 0/non-0
  without touching the running daemon), with a comment documenting
  the `-t` vs `-p` trap so it isn't reintroduced.
* **#387 — host-config runners crash-loop silently.** A shared
  bounded-retry fire-guard now gates every hash-keyed host-config
  runner (snmp / ntp / lldp / syslog / ssh / resolver / firewall /
  timezone): a persistently-failing apply re-fires with exponential
  backoff (60 s → … → 15 min ceiling) per distinct config hash
  instead of every heartbeat, a fresh config hash resets the budget,
  and the guard never permanently gives up so a fixed runner
  auto-recovers. Stale timestamped trigger sidecars
  (`.failed` / `.done` / `.invalid.<ts>`) are pruned to the newest
  5 per family on every heartbeat, which culls the existing ddi1
  backlog on the first post-upgrade tick.
* **#389 — Fleet etcd-snapshot inventory always empty.** The
  supervisor reports recoverable snapshots by listing the k3s
  `ETCDSnapshotFile` CRs over the kubeapi, but its ServiceAccount
  lacked a `k3s.cattle.io/etcdsnapshotfiles` read grant, so the
  `GET` 403'd and the list reported empty every heartbeat even
  though k3s was taking snapshots on schedule. Added the read-only
  rule to the supervisor's `spatium-supervisor-nodes` ClusterRole;
  `list_etcd_snapshots()` now logs a warning on a 403 (was a silent
  empty list) so a future RBAC gap is self-diagnosing.

### Added

* **#386 — live OS-upgrade status in the Fleet UI.** The host runner
  now emits structured per-phase progress (queued → downloading[+%] →
  verifying → writing → bootloader → arming → reboot-pending) to a
  sidecar the supervisor ships on every heartbeat, alongside a tail of
  `slot-upgrade.log`. The appliance drilldown renders a live status
  stepper with the download percentage, an expandable log, a failure
  card with the reason, and a reboot-to-finish CTA — replacing the
  coarse single state chip. (The supervisor gains a read-only
  `/var/log/spatiumddi` mount to ship the log tail.)
* **#387 — host-config apply health in the Fleet UI.** The
  supervisor heartbeat ships `host_config_health` — per-plane
  `{state, attempts, at}` for any host-config runner whose desired
  config isn't applied yet (`retrying` while transient, `failing`
  once the apply keeps failing) — persisted to the new
  `appliance.host_config_health` column and rendered as a
  "Host-config apply health" table in the appliance drilldown, so a
  stuck apply is visible instead of looping invisibly. An
  all-healthy appliance reports `{}` and the section stays hidden.

### Migrations

* `e1c4a9d27b63` (#386) — adds `appliance.desired_slot_image_sha256`,
  `desired_slot_image_tls_insecure`, `last_upgrade_log_tail`,
  `last_upgrade_progress`.
* `f4a1c9e7b2d8` (#387) — adds `appliance.host_config_health` (JSONB,
  not null, default `{}`).

---

## 2026.06.12-1 — 2026-06-12

Hotfix for **directly-attached DHCP dead on 2026.06.11-1** (#383).
The prior release switched Kea's default socket mode to `raw`
(AF_PACKET) so it hears directly-attached broadcast DISCOVERs
(#365 / #366), but the Kea binaries only carried the
`cap_net_bind_service` file capability — not `cap_net_raw`. The
container's `NET_RAW` (from `securityContext.capabilities.add`)
only sets the bounding set and is dropped when the entrypoint
`su-exec`s to the unprivileged `spatium` user, so `kea-dhcp4`
failed to open the raw LPF socket (`DHCPSRV_OPEN_SOCKET_FAIL` /
`DHCPSRV_NO_SOCKETS_OPEN`) and served no leases on any
non-relayed network. The same missing-capability gap left the
opt-in scapy rogue-DHCP probe (#370) and passive fingerprint
sniffer with zero effective caps. Image-only change — no schema,
no API, no frontend.

### Fixed

- **Kea raw sockets under `su-exec` (#383).** The Kea image now
  grants `cap_net_raw` alongside `cap_net_bind_service` as a
  *file* capability on `/usr/sbin/kea-dhcp4` + `/usr/sbin/kea-dhcp6`
  (`setcap cap_net_bind_service,cap_net_raw=+ep`). File caps
  survive the `su-exec` 0→non-root privilege drop, where the
  container-level bounding-set capability does not — so the raw
  AF_PACKET/LPF socket opens and Kea serves leases on
  directly-attached networks again. Same fix shape as `named` /
  `pdns_server` in the DNS image family.
- **Python agent rogue-probe / fingerprint sniffer caps (#383).**
  The entrypoint now launches `spatium-dhcp-agent` via util-linux
  `setpriv --reuid spatium --regid spatium --init-groups
  --inh-caps +net_raw --ambient-caps +net_raw` so `CAP_NET_RAW`
  lands in the *ambient* set — which survives setuid + execve into
  the unprivileged user (a Python interpreter can't carry a file
  cap the way the Kea binaries can). The opt-in rogue-DHCP probe
  (`DHCP_ROGUE_PROBE_ENABLED=1`) and passive fingerprint sniffer
  (`DHCP_FINGERPRINT_ENABLED=1`) can now bind their scapy
  AF_PACKET sockets.

## 2026.06.11-1 — 2026-06-11

The **roadmap-batch + control-plane-HA-finish** release. Three
roadmap batches (#361 / #362 / #375) clear ~22 backlog issues:
global maintenance mode (#57), a built-in network-tools page (#58),
top-N reports (#47), decom-date awareness (#46), per-subnet
utilization history (#44), time-bound permissions (#65),
resource-scoped API tokens (#374), an Ansible dynamic-inventory
endpoint (#67), an internal-cert / token `secret_expiring` alert
(#76), a compliance / change-report PDF (#48), DHCPv6 prefix
delegation + DUID reservations (#368), rogue-DHCP-server detection
(#370), DNS query-anomaly alerting (#371), IP-reconciliation
hygiene alerts (#369), fingerbank device-class on the lease list
(#373), atomic next-available-subnet allocation (#372), an OPNsense
read-only mirror (#31), and the appliance host-OS config plane
(syslog #156 / SSH #157 / DNS resolver #158). Finishes
control-plane HA (#272) — data-plane resolver VIPs (DNS :53 +
DHCP-relay :67 behind MetalLB) and a guided etcd snapshot-restore —
and lands the #199 upgrade-image GitHub import plus the
slot-image → upgrade-image rename. Closes the #358 agent
config-wake arc (Redis pub/sub wake for the agent + supervisor
long-polls, so a committed change reaches a parked box in under a
second). Plus a dashboard-audit sweep (#364), a DHCP socket-mode
fix for directly-attached clients (#365 / #366), and an OpenSSL
CVE pin. Operator Copilot registry 148 → 180. 11 PRs since
2026.06.04-1.

### Added

Control-plane HA — finished (#272). The remaining HA code lands;
live multi-node VM testing (promote → restore → VIP claim) stays
the gate to formally close the issue.

- **Phase 10 — data-plane resolver VIPs.** New
  `platform_settings.dns_vip` (:53) drops BIND9 / PowerDNS off
  `hostNetwork` behind a MetalLB L2 LoadBalancer, and
  `dhcp_relay_vip` (:67) fronts the Kea relay → server unicast
  forward (Kea keeps `hostNetwork` for broadcast). The MetalLB
  endpoint carries both with VIP-in-pool + distinct-VIP
  validation; the seed upserts the `spatiumddi-appliance`
  HelmChartConfig (`dns.useMetalLBVIP` / `dns.vip` /
  `dhcpKea.relayVIP`) — the same reboot-safe overlay as the
  control-plane VIP. Fleet → Network & Host → MetalLB → Advanced
  gains the two pickers.
- **Phase 9b — guided etcd snapshot restore.** The seed reports
  its `k3s etcd-snapshot list` (read from the `ETCDSnapshotFile`
  CRs over the kubeapi — no host `k3s` binary needed) on heartbeat
  into `appliance.etcd_snapshots`. Fleet → Control plane grows an
  etcd-snapshots card; Restore is gated behind a typed-hostname
  confirm on top of superadmin, stamps `desired_restore_snapshot`,
  and fires the host-side `spatium-cluster-restore` runner
  (`k3s server --cluster-reset --cluster-reset-restore-path=…`).
  Single-node cluster-reset — other members are orphaned and
  re-paired via Replace; disaster recovery only, local snapshots
  only (S3 restore deferred).
- **Schema-head divergence panel** on Platform Insights → Postgres
  (follow-up A) — `GET /postgres/schema-health` surfaces
  schema-behind state during cold boot / mid-rolling-upgrade
  without a 502 guess.

Appliance upgrade images — pick from GitHub Releases (#199). For
appliances with internet access the control plane imports the
upgrade image straight from a release instead of the operator
downloading + re-uploading it:
`GET /api/v1/appliance/upgrade-images/available` lists releases
carrying the `spatiumddi-appliance-slot-*-amd64.raw.xz` + `.sha256`
sidecar (returns `github_reachable` for UI auto-detect);
`POST …/upgrade-images/import-from-github` fetches the sidecar,
short-circuits on a hash already held (idempotent), then streams +
sha256-verifies the `.raw.xz` through the same storage path the
upload flow uses. Fleet → Upgrade images grows a Pick from GitHub
Releases / Upload (air-gap) toggle that auto-defaults to GitHub
when reachable, else upload.

Agent config-wake — Redis pub/sub (#358), end-to-end. The agent
`/config` long-poll and the supervisor heartbeat long-poll now
wake on a Redis pub/sub bus published to after each config commit,
so a committed change reaches a parked agent / supervisor in under
a second instead of waiting out the interval — while the
ETag / desired-state compare stays the source of truth and the
loops degrade to a bounded poll when Redis is down (never the sole
delivery path, per non-negotiable #5).

- `backend/app/core/agent_wake.py` adds `publish_wake`
  (fire-and-forget; swallows every Redis error so an outage can't
  500 a CRUD write) and `wake_subscription` (subscribes before the
  first bundle build to close the register race). Reuses the
  Sentinel-aware Redis client so it follows failover; every api
  replica subscribes, so a wake on any replica / Celery worker
  reaches whichever replica holds the poll.
- **Supervisor heartbeat long-poll wake** —
  `SupervisorHeartbeatRequest` gains an opt-in `wait_seconds`;
  when set with no concrete command pending, the handler holds on
  the appliance + `HOSTCONFIG_ALL` wake channels up to 28 s. The
  supervisor stays HTTP-only (the Redis wake lives server-side), so
  remote / Application supervisors that can't reach the
  cluster-internal `sentinel://` Redis still benefit. Fleet OS /
  reboot / role / firewall changes `publish_wake` after commit, so
  they start in ~0 s.
- **Redis dashboard** — a superadmin-gated, degrade-friendly Redis
  tab in Platform Insights (role / memory / clients / hit-ratio
  cards + a config-wake-bus panel showing publishes-by-class +
  parked subscribers per channel + keyspace), backed by
  `GET /redis/{overview,keyspace,wake-bus}` (never 500s) and a new
  `get_redis_stats` MCP read tool.

Appliance host-OS config plane (#156 / #157 / #158) — siblings of
the SNMP / NTP / LLDP host-config plane, same
PlatformSettings → ConfigBundle / heartbeat → host-runner pattern,
default-off, surfaced under Fleet → Services:

- **Syslog / rsyslog forwarding (#156)** — `syslog_targets`
  (host / port / protocol / format + Fernet-encrypted CA PEM),
  Fleet chip, MCP. Strict host / filter validators on both the
  REST and AI paths.
- **SSH authorized_keys + sshd hardening (#157)** —
  keys / password-auth / root-login / port / source-CIDRs, with
  lockout-safety guards in both the server + host runner and a
  source-scoped nftables drop-in.
- **DNS resolver override (#158)** — a systemd-resolved drop-in
  (override only; never touches `DNSStubListener`; `Domains=~.`),
  rejected (422) if override mode is set with no servers.

Global maintenance mode (#57). Middleware 503s mutating requests
during a change window (`Retry-After`, superadmin bypass,
agent / auth / health exempt per non-negotiable #5);
PlatformSettings-driven, audited, with a global banner, a Settings
toggle, and `maintenance_status` / `set_maintenance_mode` MCP
tools.

Built-in network tools page (#58). A `/tools` page —
ping / traceroute / mtr / dig / whois (sandboxed argv),
port-test / TLS-cert (sockets), DNS-propagation, MAC-vendor —
permission-gated + Redis rate-limited, with 7 MCP tools. Network
tools can also run from a selected Fleet appliance's vantage (the
"Run from" selector) reusing the supervisor poll/reply-over-
outbound channel — NAT-friendly, no inbound holes, server-rebuilt
argv (never a shell string).

Top-N reports (#47). A `/reports` surface — top subnets by
utilization, owners by IP count, most-modified resources (via
`audit_log`), noisiest DNS clients — feature-module-gated with 4
MCP read tools.

Decom-date awareness (#46). First-class `decom_date` on subnet +
IP, a `decom_expiring` alert rule (severity escalation reused from
the other `*_expiring` rules), a dashboard widget, and a
`find_subnets_decommissioning` MCP tool.

Per-subnet utilization history (#44). A daily beat task snapshots
each subnet's allocated / total IP counts (pruned > 90 d); a Trend
tab on the subnet detail renders a 30 / 90-day % used line chart;
`get_subnet_utilization_trend` MCP tool reports the series +
first → last delta.

Time-bound permissions (#65). A `time_bound_grant` table —
auto-expiring additive RBAC grants ({action, resource_type,
resource_id?} to a group until `expires_at`) consulted live by
`user_has_permission`, soft-revoked by a 60 s beat sweep (never
hard-deleted), with per-group UI + MCP.

Resource-scoped API tokens (#374). `api_token.resource_grants`
binds a token to a specific subnet or DNS zone in the RBAC grammar
(building on #74's coarse scopes) — the binding only narrows the
owner, never widens, so a leaked CI / Terraform secret can't touch
anything else. Create-time validation checks shape, resource
existence, and can't-grant-more-than-yourself; the tokens page
gains a resource picker.

Ansible dynamic-inventory endpoint (#67). `GET /api/v1/ansible/
inventory` returns standard Ansible dynamic-inventory JSON built
from IPAM — hosts grouped by space / block / subnet / tag /
custom-field, with `_meta.hostvars`. Read-only; point Ansible at
it with a read-scoped API token.

OPNsense read-only mirror (#31). A first-class read-only pull
integration cloned end-to-end from the Proxmox mirror (model,
client, reconciler, beat sweep, CRUD / test / sync, both dashboard
surfaces per non-negotiable #15, sidebar, page, `list_opnsense_
targets` MCP tool), gated by the `integrations.opnsense` feature
module.

Compliance / change-report PDF (#48). `GET /api/v1/audit/
export.pdf` renders an auditor-facing PDF of every `audit_log`
mutation in a date range, grouped by user / resource / action,
with a per-row SHA-256 tamper-evidence trailer over the
audit-chain hash. Export PDF button on the Audit Log page.

`secret_expiring` alert rule (#76). One rule that fires per
internal credential expiring within `threshold_days` — supervisor
mTLS certs (`appliance.cert_expires_at`) + API tokens
(`api_token.expires_at`) — with the standard
warning / critical escalation.

DHCPv6 prefix delegation + DUID reservations (#368).
`DHCPPool.pool_type='pd'` (`pd_prefix` / `delegated_length` /
`excluded_prefix`) renders a Kea `pd-pools` entry inside `subnet6`
on stateful v6 subnets (RFC 6603 excluded-prefix); `DHCPStatic
Assignment.duid` keys v6 reservations on DUID with
`host-reservation-identifiers ['duid','hw-address']`. Pool + static
forms gain the IA_PD type / DUID field on v6 scopes.

Rogue DHCP server detection (#370). An opt-in agent probe thread
broadcasts a spoofed-MAC DISCOVER (never a REQUEST — no lease
consumed), collects OFFERs, and ships responders; the backend
classifies each against the group's known servers + an operator
allowlist into expected / acknowledged / rogue, with a
disabled-by-default `rogue_dhcp` alert rule, a Responders tab +
Acknowledge action, and a `find_dhcp_responders` MCP tool.

Fingerbank device class on the lease list (#373). The leases
endpoint returns device class / name / manufacturer / score per
lease via a single batch join, with an optional `?device_class=`
filter, a Device column + filter on the leases tab, and the fields
surfaced on `find_dhcp_leases`.

DHCP per-group socket mode (#365 / #366). `DHCPServerGroup.dhcp_
socket_mode` — `direct` → Kea raw / AF_PACKET sockets (default;
hears broadcast DISCOVERs from directly-attached clients that have
no IP yet) / `relay` → udp (relay-only opt-out), delivered through
the ConfigBundle long-poll + folded into the ETag, with a "Client
reachability" toggle on the server-group modal.

Atomic next-available-subnet allocation (#372).
`POST /ipam/blocks/{id}/allocate-subnet` carves the lowest free
child CIDR of a requested prefix (or a chosen free in-block CIDR)
in one block-locked transaction — the subnet analog of the
next-available-IP allocate — delegating to `create_subnet` so
placeholders, templates, and DDNS inheritance match. New
`propose_allocate_subnet` MCP write tool; the AddSubnet "Find by
size" flow now allocates through it.

IP-reconciliation hygiene alerts (#369). Three alert rule types
(disabled by default) over the `last_seen_at` liveness signal —
`ip_free_but_responding`, `stale_reservation`, and
`unknown_mac_in_static_range` (a squat) — with the ping / ARP
sweep + SNMP ARP cross-reference now logging observed MACs into
`ip_mac_history` (operator-set MAC never overwritten), and a
`find_ip_hygiene_findings` MCP tool.

DNS query-anomaly alerts + per-view analytics (#371). Two alert
rule types over the per-server rcode deltas agents already report —
`dns_nxdomain_spike` (NXDOMAIN ratio over a 15-min window, catches
DGA beacons) and `dns_query_rate_spike` — plus a "By view" card in
the Logs analytics strip (split-horizon servers) and a
`find_dns_query_stats` MCP tool.

DNS config-drift report backend (#61, partial). `GET …/zones/{id}/
drift` AXFRs the live zone from every server in the group and diffs
it against the DB — extra-on-server (manual host change) /
missing-on-server / in-sync, per server, read-only — with a
`find_dns_zone_drift` MCP tool. UI deferred.

DHCP server-detail Stats tab (#195, partial). The 6th tab on the
DHCP server detail modal renders the per-server lease-rate
timeseries, reusing the dashboard DHCP traffic card scoped to one
server.

### Changed

- **Operator-facing rename "slot image" → "upgrade image" (#199).**
  Model `ApplianceSlotImage` → `ApplianceUpgradeImage` (+
  data-preserving table rename); module `slot_images.py` →
  `upgrade_images.py`; endpoints `/slot-images/*` →
  `/upgrade-images/*`; Pydantic schemas, audit actions, logger
  events, UI strings, and the frontend client follow. The
  lower-level A/B `dd` mechanism stays named "slot" deliberately
  (the `slot-image-mirror` PVC, `services/appliance/slot.py`, the
  `/var/lib/spatiumddi/slot-images` store + env + volume, the
  `desired_slot_image_url` columns / wire fields, the slot
  download-token HMAC, and the `spatiumddi-appliance-slot-*.raw.xz`
  release asset name — pure plumbing, not operator-facing).
- Config-mutating DNS / DHCP / IPAM / host-config endpoints (and
  the `dns_pool_healthcheck` / `ipam_dns_sync` / blocklist-refresh
  Celery workers) now `publish_wake` after commit so parked agents
  converge in ~0 s; the belt-and-braces idle tick
  (`WAKE_TICK_SECONDS`) is raised 2 s → 12 s since the wake carries
  the common case (#358).
- `find_control_plane_vip` MCP tool widened to also report the
  DNS + DHCP-relay data-plane VIPs (#272 Phase 10).
- `make appliance-baked-iso` now refuses local source images older
  than 24 h unless `--allow-stale-images` / `ALLOW_STALE_IMAGES=1`
  is passed, so a stale local bake can't silently ship into the ISO
  (#272 follow-up B); `appliance-bake-images` threads a
  `BAKE_FLAGS` knob.
- DHCP socket-mode default flips upgraded installs from the old
  hard-coded `udp` render to `direct` / raw via the migration's
  `server_default` — every existing group starts answering
  directly-attached clients with no operator action (#366).
- `NET_RAW` granted on all Kea services in the docker-compose files
  (the appliance DaemonSet already had it) so raw socket mode works
  (#366); the supervisor + backend images gain
  iputils / traceroute / mtr / dig / whois for the network-tools
  surface (#58 / #364).
- Fleet → Services nav items alphabetized
  (DNS Resolver / LLDP / NTP / SNMP / SSH / Syslog), with a
  keep-sorted note (#363). Dashboard tab bar + heatmap / server /
  integration lists get isolated horizontal scroll + `min-w`
  wrappers on mobile (#364).
- Cloud DNS drivers (Route 53 / Cloudflare / Google / Azure) no
  longer advertise `dnssec_online` / `alias_records` capabilities
  the server-side gates already 422 — so the UI stops offering
  cloud DNSSEC sign / ALIAS authoring that then failed (#29,
  partial; real cloud DNSSEC + ALIAS remain a scoped follow-up).

### Fixed

- Dashboard feature-gated queries fired before the module set
  loaded → a one-shot 404 console-spam + scary red cards on hard
  load; `useFeatureModules` now exposes a `ready` flag and the
  AI / ASN / VRF / Conformity surfaces gate on `ready && enabled`,
  with the Conformity tab filtered out when its module is off
  (#364).
- Dashboard widgets: all-zero DHCP / DNS series render the empty
  state instead of a degenerate plot; RPKI "expiring soon" shows
  "just now" not a future "in N d"; the Orphan-services card's
  empty hint shows; the Proxmox panel used a non-existent
  `hostname` column (blank rows) → `name`; UniFi was unreachable
  on the dashboard (missing `integration_unifi_enabled` in the
  settings schemas) (#364).
- #368 was dead on the real agent path — the agent `/config` wire
  payload hand-builds pool / static dicts and dropped the new
  PD / DUID fields, so they never reached `render_kea`; the v6
  pool-overlap check crashed on an IPv4-only `_ip_int`; the agent
  `render_kea` socket-type fallback flipped `udp` → `raw` so a
  control-plane-less render still hears direct clients (#366).
- #374 token binding escaped on every subnet handler not yet
  guarded (a subnet-scoped token could update / delete / resize /
  bulk-edit across any subnet) — `_enforce_subnet_token_scope`
  added to all of them, bulk handlers skipping out-of-scope IPs
  per row.
- #362 adversarial self-review (33-agent pass, 19 confirmed
  findings, all fixed): the SSH-reload port-in-use guard matched
  sshd's own socket and aborted every apply on real hardware;
  maintenance superadmin-bypass skipped the session check (a
  force-logged-out superadmin still bypassed) + ignored token
  scopes; the syslog `config_hash` excluded the CA PEM so CA
  rotation never propagated; OPNsense delete CASCADE-wiped operator
  IP rows on a disappeared interface (now un-claims); the
  `*_expiring` alerts never escalated an already-open event.
- Fleet role-assignment + Subnet-planner group pickers read the
  tuple query keys `['dns','groups']` while group CRUD invalidates
  the hyphenated `['dns-groups']` — React Query matches by prefix,
  so a freshly-created group only appeared after a full reload;
  switched both to the canonical hyphenated keys (#367).

### Security

- Bumped OpenSSL (libcrypto3 / libssl3) to `>= 3.5.7-r0` across the
  Kea / BIND9 / PowerDNS agent images to clear CVE-2026-45447
  (HIGH) surfaced by Trivy (#366).
- Resource-scoped API tokens (#374) limit the blast radius of a
  leaked token — a token bound to one subnet / DNS zone is
  intersected with the owner's RBAC and can only ever narrow it;
  `is_effective_superadmin` is token-aware so a resource-scoped
  admin token is never treated as superadmin.
- Rogue DHCP server detection (#370) surfaces an unauthorized DHCP
  server answering on a managed segment.
- #362 security sweep: `dig` leading-dash option-injection
  (local-file read) closed at the runner + MCP; an SSRF denylist
  (loopback / link-local / `169.254.169.254`) on the socket tools +
  `@server` / resolvers (RFC1918 still allowed); strict rsyslog
  `target` / filter validators on both REST + AI paths; the
  maintenance toggle is superadmin-only and an `sddi_` scoped token
  can no longer bypass it; top-owners now requires `read:customer`,
  not just `read:ip_address`.

### Migrations

Single linear head `c3f7a1d9b486`. Fourteen migrations, all
additive (new columns / tables) except the one `rename_table`,
parented off the 2026.06.04-1 head `a3f1e9c47b20`:

- `c7a3e1f90d24` — `subnet_utilization_history` table (#44).
- `d1b8f4a92c30` — `platform_settings` maintenance-mode columns
  (#57).
- `a3f7c1e92b48` — `decom_date` on subnet + ip_address (#46).
- `d5e9b2c14a07` — `time_bound_grant` table (#65).
- `e7a3f1c0d294` — appliance syslog-forwarding settings (#156).
- `f1c4a90b27d6` — appliance SSH settings (#157).
- `a3e7c9d12f80` — appliance DNS-resolver settings (#158).
- `b6f4d2a91c83` — `opnsense_firewall` table + IPAM provenance FKs
  + `integrations.opnsense` feature module (#31).
- `f3b9c1d6a274` — `dhcp_server_group.dhcp_socket_mode`
  (server_default `direct`, backfills every row) (#365 / #366).
- `c3f1a7e09d52` — `api_token.resource_grants` JSONB (#374).
- `d5b2e8a14c93` — DHCPv6 PD columns on `dhcp_pool` + `duid` on
  `dhcp_static_assignment` (#368).
- `e7a3c0d519f4` — `dhcp_observed_responder` +
  `dhcp_responder_allowlist` for rogue-DHCP detection (#370).
- `f1a4c7b2e9d6` — `platform_settings.dns_vip` / `dhcp_relay_vip` +
  `appliance.etcd_snapshots` / `desired_restore_snapshot` /
  `restore_state` / `restore_reason` (#272 Phase 9b / 10).
- `c3f7a1d9b486` — data-preserving `rename_table`
  `appliance_slot_image` → `appliance_upgrade_image` (#199); the
  rename is baselined in `migrations_lint_baseline.txt` for the
  #296 expand/contract linter (transient superadmin-only table;
  the rolling-upgrade orchestrator runs migrate after the pod
  rollout completes).

### Deprecated

- Legacy `/api/v1/appliance/slot-images/*` paths are kept for one
  release cut as `308` permanent redirects to `/upgrade-images/*`
  so bookmarks / scripts don't hard-break (#199). `308` (not
  `301`) preserves the request method + body, so the air-gap
  upload + delete shims keep working. Update scripts to the new
  paths — the shim is scheduled for removal next cut.

## 2026.06.04-1 — 2026-06-04

The **roadmap-closure + appliance fleet-firewall** release — the
largest single cut since the alpha. It clears a long backlog of
pending feature-roadmap items (Cloud connectors #37, DNS Views
split-horizon #24, IP discovery #23, BIND9 DNSSEC #49, the DHCP
configuration importer #129, DHCPv6 modes #52, reverse-DNS
auto-population #41, the Stale-IP report #45, CGNAT awareness #42)
and lands the full **#285 fleet firewall** for the appliance —
six phases that cut the LAN-wide etcd/kubelet exposure, add a
declarative per-role policy model + merge engine, an operator
Firewall tab with posture presets and a staged-preview diff
viewer, an enforcement master switch gated on "all control-plane
nodes hardened", and source-scopable Web UI access. Same cut wires
the appliance host-config delivery plane so SNMP / NTP / LLDP
settings finally apply on a supervisor-based appliance, ships the
LLDP host-config plane end-to-end (Phases 1–3, including neighbour
discovery), closes the Operator Copilot MCP write-tool catch-up
(17 `propose_*` tools, registry 121 → 138), and folds in a broad
review-sweep of DHCP / DNS / cloud bugs plus a Kea CVE pin. 24 PRs
since 2026.05.26-1.

### Added

Cloud integration (#37) — AWS / Azure / GCP as a unified "Cloud"
integration with a per-provider picker:

- **Read-only infra mirror** (`cloud_endpoint` + AWS / Azure / GCP
  connectors) reconciling VPC → IPBlock, subnet → Subnet, and
  NIC / public / load-balancer IPs → IPAddress with
  `cloud_endpoint_id` ownership, `user_modified_at` locks, and
  prune-on-absence — same read-only-pull shape as the Kubernetes /
  Docker / Proxmox mirrors. Surfaces on both dashboard surfaces +
  a `list_cloud_targets` MCP read tool, gated by the
  `integrations.cloud` feature module.
- **Cloud DNS driver family** — Cloudflare / Route 53 / Azure DNS /
  Google Cloud DNS as agentless first-class drivers, plus the
  token-only tier (DigitalOcean / Hetzner / Linode / Vultr, #327),
  with import-existing-zones for every provider. All do client-side
  multi-value RRset disambiguation (match value + priority, not the
  first row).

DNS Views — end-to-end split-horizon rendering (#24). The storage +
CRUD + record-form picker already shipped; this wires the render:
the BIND9 agent now emits one `view "<name>" { match-clients …; }`
block per view with per-view zone files, `view_id IS NULL` records
are shared across every view, scoped records render only in their
view, RPZ / blocklists replicate into each view block (BIND forbids
top-level zones alongside views), and any record/view change shifts
the structural etag so the full view-correct re-render fires.
PowerDNS keeps its existing 422 view gate.

IP discovery — ping / ARP sweep + reconciliation (#23). Opt-in,
per-subnet scheduled discovery that finds live hosts and folds them
into IPAM (the producer of the #45 / #41 hygiene loop): unprivileged
SOCK_DGRAM ICMP with a TCP-connect fallback, `/proc/net/arp` scan
for ICMP-silent hosts, status=`discovered` rows for live IPs with no
row (dynamic-pool + network/broadcast skipped, operator-locked rows
preserved), a three-bucket reconciliation report, on-demand sweep
endpoint, and a `find_subnet_reconciliation` MCP tool.

BIND9 DNSSEC — inline-signing, policies, DS export, rollover (#49).
`DNSSECPolicy` (reusable dnssec-policy) + `DNSKey` (public per-zone
key state — no private-key custody; BIND owns + auto-rotates keys),
config-driven `dnssec-policy { … }` + per-zone `inline-signing yes;`
render on both the control-plane and agent BIND9 paths, agent
post-reload `rndc dnssec -status` + `dnssec-dsfromkey` reporting, a
DNSSEC Policies page + per-zone key table with a per-key Roll
button, and `find_zone_dnssec_info` / `list_dnssec_policies` MCP
tools.

DHCP configuration importer — Kea / Windows DHCP / ISC dhcpd.conf
(#129). One-shot import-to-evaluate (sister to the DNS
importer #128) behind one canonical IR + preview → commit
pipeline: Kea JSON-with-comments upload, Windows live-pull
(reuses the Path A
WinRM read driver), and a hand-rolled ISC `dhcpd.conf` tokeniser +
recursive-descent walker. Every scope binds to a Subnet (link to an
existing CIDR or auto-create under an operator-chosen space + block);
per-scope savepoints, skip / overwrite conflict actions, and
`import_source` + `imported_at` provenance. Gated by the
`dhcp.import` feature module.

DHCPv6 stateful / stateless / SLAAC mode (#52).
`DHCPScope.v6_address_mode` + `ra_managed_flag` /
`ra_other_flag`; the Kea
driver renders subnet6 by mode (stateful → pools + options +
reservations, stateless → options only, slaac → bare subnet) and the
scope modal shows a DHCPv6-mode picker + RA M/O flags on IPv6 scopes.

Reverse-DNS auto-population (#41). A scheduled, platform-opt-in sweep
that PTR-resolves `hostname IS NULL` rows against configured
resolvers (bounded concurrency, per-run cap), fills the short label
into `hostname` and the FQDN into `description` (only when blank),
and skips integration-owned + lease-mirror rows. Settings → IPAM →
Reverse DNS form + on-demand Run-now.

Stale-IP report + one-click bulk-deprecate (#45). Over the discovery
`last_seen_at` signal: which allocated IPs has nothing answered for
in N days. Paginated report (optional space / block / subnet scope),
bulk-deprecate selected or all-matching (capped, reversible, stamps
`user_modified_at`), a `stale_ip_count` alert rule, a
`find_stale_ips` MCP tool, and a Stale IPs page.

CGNAT (RFC 6598) awareness (#42). An amber "CGNAT" badge on subnet
detail + a New-Subnet advisory hint when a typed network falls in
`100.64.0.0/10` (the one reserved IPv4 range overlays actively carve,
so reaching for it as an on-prem LAN silently overlaps overlay
routes). `is_cgnat_cidr` classifier + `SubnetResponse.is_cgnat`
derivation; framing-only, no allocation change.

SVCB / HTTPS (RFC 9460) + DNAME (RFC 6672) record types (#338). Added
to `VALID_RECORD_TYPES`, gated to `{bind9, powerdns}`; the zone-file
writer FQDN-normalises DNAME and passes SVCB/HTTPS rdata through; the
`dns_io` reconcile parser round-trips all three.

DHCP per-pool occupancy + `dhcp_pool_exhaustion` alert (#339).
Live, control-plane-side occupancy from mirrored `DHCPLease` rows
(driver-agnostic, no agent change), a new alert rule firing on
occupancy ≥ threshold OR free < min-free, and a
`find_dhcp_pool_occupancy` MCP tool.

Functional secondary / stub zones (#336). `DNSZone.masters` (JSONB)
threaded through the driver dataclass + config bundle so a secondary
renders `type slave; masters { … };` and a stub `type stub; …` — the
zone create/update API now 422s a masterless secondary/stub (the
previously-silent broken state), with named.conf-injection guards on
each master entry.

Appliance fleet firewall (#285) — declarative per-role firewall
management for the appliance cluster, landed across Phases 1–6:

- **LAN-wide port cut (Phase 1).** Removed the base
  `/etc/nftables.conf` `k3s-ha` accept that exposed etcd
  (2379/2380) + the kubelet (10250) to the whole LAN; the
  supervisor's peer-scoped drop-in is now the authoritative source
  for those ports (single-node keeps etcd loopback-only). apiserver
  6443 stays LAN-reachable behind a baked bootstrap sentinel that
  auto-retires once the cluster is multi-node, narrowing 6443 to
  peers ∪ pod ∪ service ∪ operator-allowlisted CIDRs.
- **Declarative policy model (Phase 3).** `FirewallPolicy` (fleet /
  per-role / per-appliance scopes) + `FirewallRule` + `FirewallAlias`
  (family-split v4/v6), behind the `appliance.firewall` feature
  module. A control-plane merge engine compiles each node's drop-in
  from the policy model and reproduces the legacy renderer
  byte-for-byte (three-way render identity across the in-pod,
  compile-body, and policy-merge paths).
- **Operator Firewall tab.** Policy / rule / alias editor, posture
  presets (locked / balanced / open), a staged-preview diff viewer
  (per-node line diff + accept↔drop conflict / redundancy advisories),
  an effective-render viewer with a layer breakdown + drift chip, and
  a `firewall_extra` free-text nft escape hatch with an
  allowlist-grammar lint (injection / unbalanced-brace / drop-22
  rejection).
- **Enforcement master switch (Phase 4a).** `firewall_enabled`
  (default off) refuses to ENABLE until every reporting appliance
  node is hardened (base no longer LAN-wide), with an audited
  override; disabling is never gated.
- **Source-scopable Web UI (Phase 6).** Restrict the appliance Web
  UI (frontend :80/:443 + the MetalLB VIP via
  `loadBalancerSourceRanges`) to operator-chosen source CIDRs from
  the Firewall tab, with an anti-lockout guard (rejects a scope that
  doesn't cover the operator's own source IP unless overridden); SSH
  stays in the un-removable base floor so a bad scope is always
  recoverable from the console.
- **Safety + compliance.** A `firewall.apply_stalled` alert (drift
  between control-plane-rendered and host-applied rulesets), reboot-
  survivable test-apply auto-revert machinery, an etcd/control-plane
  peer-drift cross-check (warn-only), a one-time `firewall_extra`
  advisory-lint sweep, a `no_lanwide_control_plane_ports` conformity
  check (PCI-DSS 1.2.1 / HIPAA segmentation), and 4 read + 1
  `propose_*` MCP tools.

LLDP host-config plane (#343 / #347 / #348). Run `lldpd` as a
host-managed OS package configured from a Fleet → Services → LLDP
tab, built to exact parity with the SNMP (#153) / NTP (#154) plane
(`PlatformSettings` → rendered config → ConfigBundle long-poll →
supervisor trigger-file → host runner; no nftables drop-in — LLDP is
raw L2 multicast). Phase 2 adds neighbour discovery
(`ApplianceLldpNeighbour` + `lldpcli show neighbors -f json0` →
heartbeat → `find_lldp_neighbors` MCP tool); Phase 3 adds MED ELIN
location + an AgentX→snmpd bridge so LLDP-MIB is queryable via the
host snmpd.

Operator Copilot MCP write tools (#280 / #304). The deferred
`propose_*` catch-up — 17 new tools (15 writes + 2 import-preview
reads) across conformity, webhooks, DNSSEC, multicast, SNMP / NTP,
and DNS / DHCP import, each an `Operation` (preview + apply) reusing
the existing service path, default-disabled, module-gated, writing a
`via="ai_proposal"` audit row. Registry 121 → 138 tools, 11 → 26
operations.

Sidebar reorg + verbose-boot console (#355). The Core sidebar list
regroups its grown-in items under lightweight `SubNavLabel`
sub-headings (IPAM → NAT Mappings / Stale IPs / Subnet Planner;
DNS → DNS Pools / DNSSEC Policies / Domains) with Dashboard / IPAM /
DHCP / DNS pinned at the top, and Logs moves to a new top-level
**Operations** section (zero route / module-gating changes). The
appliance gains an operator-toggleable **verbose boot console** on
Appliance → Network & Host — OFF (default) keeps today's quiet boot +
Talos dashboard; ON drops the `loglevel=3` cap, sets
`systemd.show_status=1`, and hands tty1 to a normal getty so boot
looks like a standard Linux server. The toggle flips a `grubenv`
variable (survives A/B slot swaps + the `/etc` overlay) via the same
host-config trigger plane; applies on the next reboot, fails closed
to quiet boot on a corrupt grubenv.

### Changed

- **Appliance host-config delivery wired (#346).** The render +
  bundle + host-runner halves of the SNMP / NTP / LLDP host-config
  plane shipped earlier, but the delivery half was unwired under
  #170 — the supervisor heartbeat never carried the settings and
  never called the `maybe_fire_*_reload` writers, so none applied on
  a supervisor-based appliance. The heartbeat now ships
  `snmp_settings` / `ntp_settings` / `lldp_settings` blocks and the
  loop fires each reload trigger on change.
- **Bulk IPAM→DNS sync batched on the create path (#341).** The
  create/update branch of the bulk sync looped one synchronous WinRM
  call per record, defeating the 2026.04.19 WinRM-batching fix on the
  create side. A task-local `_batched_dns_ops` collector now defers +
  flushes grouped by zone (one batched apply for an agentless Windows
  DNS primary); wrapped around the sync, bulk-allocate, and bulk-edit
  loops.
- **Installer disk floor raised 24 → 32 GiB (#312).** The 24 GiB
  floor left /var ~7 GiB and the control-plane first boot thrashed
  the kubelet DiskPressure eviction loop. Raised the global hard
  floor (so /var gets ~14 GiB) + added a soft control-plane-only
  sizing warning (below 40 GiB disk / 8 GiB RAM) at role-pick time.
- **k3s v1.35.4+k3s1 → v1.35.5+k3s1 (#325).** Patch bump (the fetched
  artifacts are gitignored + re-downloaded at build time).
- **CNPG operator resource pins (#315).** Gave the CloudNativePG
  operator `requests`/`limits` (BestEffort → Burstable so it's no
  longer first in the eviction line on the all-in-one node) +
  loosened the webhook startup/liveness probes for a slow webhook-TLS
  bootstrap on a constrained VM.
- **Dependency bumps** — `axios` 1.15.2 → 1.16.0 (#313), `react-router`
  6.30.3 → 6.30.4 (#356).

### Fixed

- **OIDC / LDAP / SAML Superadmin role lockout (#351).** A user
  mapped into a group that grants the built-in Superadmin role has the
  `*/*` wildcard via RBAC but `User.is_superadmin = False` on the row,
  so `GET /auth/me` reported the raw column and every frontend
  superadmin gate (Fleet, Pairing, SNMP/NTP/LLDP, Sessions, Settings)
  locked them out even though the route-level gates admitted them.
  `/auth/me` now reports the EFFECTIVE status (column OR `*/*` role),
  and the same-bug sweep moved ~25 remaining raw-column gates (9 MCP
  superadmin gates, `operations_writes`, `settings` ×6, `api_tokens`,
  `ai/prompts` ×5, agent-keys, `supervisor.py`) to
  `is_effective_superadmin`.
- **Two slot-upgrade blockers (field-tested on a live 3-node
  appliance).** firstboot aborted under `set -u` on every non-first
  boot (a slot upgrade) because `APPLIANCE_HOSTNAME_VAL` /
  `APPLIANCE_HOST_IPS_VAL` were only assigned inside the first-boot
  `.env`-gen block but referenced unconditionally — so no A/B slot
  upgrade ever rolled the control-plane container images. And the
  firewall host runner never deleted its trigger file, so the
  supervisor's presence-guard silently blocked every subsequent
  firewall change. Both fixed (re-derive the vars every boot; `rm` the
  trigger after a successful apply).
- **Firewall schema column mismatch (field-test catch).** The
  firewall migration created `updated_at` but `TimestampMixin` maps
  `modified_at`, so every `select(FirewallPolicy)` 500'd on a
  migration-built DB (the test suite missed it — conftest builds from
  `Base.metadata`). Fixed the migration columns + seed INSERT in place
  (branch unreleased) + added a static migration-parity guard test.
- **Setup Wizard cert button deep-link (#314).** The "Manage
  certificate" CTA navigated to `/appliance` with no tab target,
  restoring the operator's last-active tab instead of Web UI
  Certificate; `AppliancePage` now honours a `?tab=<key>` deep-link.
- **Review-sweep — 10 DHCP / DNS / cloud issues (#342).** Cloudflare
  (#331) + Route 53 (#334) multi-value RRset disambiguation; per-
  endpoint session rollback so one bad cloud/proxmox endpoint can't
  poison the sweep transaction (#333); cloud integration honoured in
  DEMO_MODE (#335); lease-removal IPAM-mirror lookup scoped to the
  owning subnet under overlapping ranges (#329); agent lease-events
  N+1 → 3 bulk loads (#340); Kea HA renders the full 3/5/7-member peer
  list (#332); DHCPv6 agent renderer emits a real `Dhcp6`/`subnet6`
  (#330); per-subnet Kea relay-agent addresses (#337).

### Security

- **LAN-wide etcd / kubelet exposure closed (#285).** The headline
  hardening — etcd (2379/2380) + kubelet (10250) are now peer-scoped,
  never LAN-wide, on control-plane appliance nodes (see Added).
- **Kea CVE-2026-3608.** The Kea agent image shipped 2.6.3-r0 (Trivy
  HIGH); pinned `kea>=2.6.5-r0`.
- **Config-injection guards.** named.conf-injection guards on
  secondary/stub `masters` entries (#336), relay-address-family
  validation on DHCP scopes (#337), and the `firewall_extra`
  allowlist-grammar lint (nft-injection / shell-metachar rejection,
  #285 Phase 3d).

### Migrations

Single linear head `a3f1e9c47b20` (17 migrations, all additive on
`upgrade()`; every `downgrade()` drop is baselined in the migration
linter):

- `a7e3c1f49d20` — subnet IP-discovery columns (#23).
- `d7a3f2b9c1e4` — `platform_settings` reverse-DNS columns (#41).
- `e4c1a8f63b29` — `DHCPScope.v6_address_mode` + RA flags (#52).
- `f2b6d4a91c37` — BIND9 DNSSEC (`dnssec_policy`, `dns_key`,
  `dns_zone.dnssec_policy_id`) (#49).
- `c7f1a3e58b94` — DHCP import provenance columns (#129).
- `d1e7c4a90fb3` — `cloud_endpoint` + `cloud_endpoint_id` FKs +
  feature-module seed (#37).
- `a1c4f7e92b30` — `DHCPScope.relay_addresses` (#337).
- `c9a1f7e0b234` — `DNSZone.masters` (#336).
- `a7f3c1e85d20` — `alert_rule.min_free_addresses` (#339).
- `b8c3f2a9e147` / `c1f4a8e3b29d` — appliance LLDP settings +
  neighbour table (#343 / #347).
- `d7e2a4f9c1b3` / `a3f1d9e07c52` / `e4a7c1f08b9d` / `f5b8d2c91a06`
  — fleet-firewall prereqs, apply-state, policy schema, builtin seed
  (#285).
- `c1e7f3a90b4d` — `platform_settings.web_ui_allowed_cidrs` (#285
  Phase 6).
- `a3f1e9c47b20` — `platform_settings.verbose_boot` (#355).

## 2026.05.26-1 — 2026-05-26

The **multi-node rolling upgrade + cold-boot UX + operator-quality**
release. The headline (#296, landed via #298) closes the last gap
left by 2026.05.22-1's control-plane HA cut: SpatiumDDI can now
walk an OS+app upgrade across every node of a 3/5/7-node appliance
cluster from a single click in `/appliance` → **Rolling Upgrade**.
Both source modes are wired (air-gap mirror PVC + connected URL),
preflight verifies replication lag / disk headroom / quorum before
the lease lock comes out, and the orchestrator handles
cordon → CNPG switchover → drain → A/B slot apply → reboot →
health-gate → uncordon → settle for each node in turn, then
patches the chart's `image.tag` so api / worker / frontend images
follow the OS to the new version. The same cut closes the
operator-visible cold-boot 502 (#299 — schema-aware readiness +
friendly initialising page), lets installers carve non-default k3s
pod / service CIDRs on networks that already use 10.42 / 10.43
(#302), cascades dependent DNS/DHCP server rows when an appliance
is revoked (#197), adds an IANA timezone picker to Settings → NTP
that drives `timedatectl` on the host (#165), and lights up 7 new
Operator Copilot read tools across the conformity / webhooks /
DNSSEC surfaces (#280 — the matching write proposals tracked in
#304). Four PRs since 2026.05.22-2 (#298 #301 #303 #305).

### Added

Multi-node rolling OS+app upgrade (#296 / #298) — the appliance
cluster upgrades itself end-to-end:

- **Rolling Upgrade tab** at `/appliance` → **Rolling Upgrade**.
  Type the target CalVer tag, pick a source (Uploaded image from
  the new in-cluster mirror PVC for air-gap, or a GitHub release
  URL for connected installs), run preflight, plan, start. Per-
  node progress streams as state pills (cordon → switchover →
  drain → apply → reboot → health-gate → uncordon → settle) with
  Halt / Resume / Abort controls.
- **Slot image mirror PVC** (`slot-image-mirror` Deployment + PVC,
  gated by `slotImageMirror.enabled`) and a **Fleet → Slot
  images** upload surface. Bytes stream through the api to the
  mirror once; every node fetches through an authenticated
  control-plane URL with an HMAC token. No node ever talks to
  github.com on an air-gap install.
- **Preflight framework** with six checks
  (`inflight_conflict` / `replication_lag` / `disk_headroom` /
  `mirror_disk_headroom` / `version_path` / `quorum`). Any `fail`
  blocks Plan; `warn` flags the row but lets the operator proceed.
- **Cluster mutex** as a `coordination.k8s.io/v1/Lease`
  (`spatium-upgrade-lock`) — only one rolling-upgrade run holds
  the lease at a time; per-node primitives short-circuit if it
  evaporates mid-walk.
- **CNPG-aware node walk** — if the current primary lands on the
  node being walked, the orchestrator issues a switchover via the
  CNPG `switchover` annotation + waits for the replica to be
  promoted before draining. PDB stays armed for unrelated
  evictions throughout.
- **HelmChartConfig overlay** patches `image.tag` on the umbrella
  chart after every node has the new slot booted + healthy — api /
  worker / frontend / migrate Job all follow the OS image
  forward in one atomic chart reconcile.
- **3 new Operator Copilot read tools** — `find_upgrade_preflight`
  / `find_upgrade_runs` / `find_upgrade_lease` — so the LLM can
  answer "what's the current rolling-upgrade run?" without the
  operator pasting status into chat.

Cold-boot UX (#299 / #301) — first-login no longer 502s on a fresh
appliance:

- **Schema-aware `/health/ready` probe.** The api process reads
  the bundled alembic head once on import and on every readiness
  probe compares against `SELECT version_num FROM
  alembic_version`. Three distinct failure detail strings —
  missing-table, missing-row, version-mismatch — keep operators
  out of "is this a migrate bug or a connectivity bug?" guessing.
  Pods stay out of the Service endpoint set until the migrate Job
  lands the expected head.
- **Friendly initialising page.** The frontend nginx serves a
  self-contained 4 KB `_starting.html` (inlined SVG, dark/light
  via `prefers-color-scheme`, `<meta refresh=5>`) instead of the
  bare nginx 502 HTML on `502` / `504` from the upstream — covers
  the cold-boot 1–2 min window on a fresh multi-node install,
  the mid-rolling-upgrade replica gap, and mid-CNPG-failover
  blips. Real `500` / `503` pass through untouched so demo-mode
  blocks, factory-reset cooldown, and schema-behind detail strings
  still surface to the operator.

K3s install-time customisation (#302 / #303):

- **K3s pod + service CIDR step** in the `spatium-install` wizard
  (between Timezone and Application config). Defaults match k3s
  upstream (`10.42.0.0/16` pods + `10.43.0.0/16` services) so the
  one-press-Enter common case still works. Operators on a LAN
  that already uses one of those ranges (Flannel `host-gw` writes
  routes directly — overlapping pod CIDR masks the LAN) type
  their own; `ipaddress`-based validation enforces ≤ /22 prefix
  per CIDR, pod/service disjoint, and (on static-IP installs) no
  overlap with the just-configured LAN subnet. Choices land in
  `/etc/rancher/k3s/config.yaml.d/spatium-cidrs.yaml` (drop-in
  form so the upstream `config.yaml` stays operator-readable);
  cluster-DNS IP is auto-derived from the service-CIDR base + 10
  per k3s/kube-dns convention.

Appliance delete cascades to managed servers (#197):

- **`GET /appliance/appliances/{id}/dependents`** lists the
  `dns_server` + `dhcp_server` rows that the delete flow will
  sweep — matched on either the new `appliance_id` FK (forward-
  compatible) or `host == hostname` (legacy / pre-FK rows).
- **`DELETE /appliance/appliances/{id}`** sweeps the dependents
  in the same transaction; audit log captures the cascaded server
  names + ids. Re-authorising the appliance re-creates the rows
  via the supervisor's next heartbeat → role-assignment →
  register flow, so an accidental revoke is a single-click undo.
- **Fleet delete-confirm modal** renders the dependents list
  before the operator clicks Revoke so the blast radius is
  visible.

GUI timezone control (#165):

- **`Settings → NTP` gains a Host timezone section** — text input
  backed by a `<datalist>` of `Intl.supportedValuesOf("timeZone")`
  for full IANA autocomplete on modern browsers (curated fallback
  for the old-browser path). Empty = "don't push further changes";
  set the field to an IANA name to apply.
- **Supervisor → host pipeline.** `PlatformSettings.timezone`
  flows through the heartbeat's new `desired_timezone` field;
  `appliance_state.maybe_fire_timezone` writes
  `/var/lib/spatiumddi-host/release-state/tz-pending`; the new
  host-side `spatiumddi-tz-reload.path` unit picks it up, the
  runner validates against `timedatectl list-timezones` + applies
  via `timedatectl set-timezone`. Status sidecar surfaces
  failures (e.g. `unknown_timezone`) back to the api. Survives
  A/B slot swaps via the `/etc` overlay → `/var/persist/etc`.

Operator Copilot read-tool catch-up (#280 / #305) — 7 new tools
across 3 modules (matching `propose_*` writes deferred to #304):

- **conformity** (3 tools, default-enabled) —
  `list_conformity_policies` (per-framework registry filter),
  `find_conformity_results` (append-only evaluation history),
  `get_conformity_summary` (per-framework rollup).
- **webhooks** (3 tools, default-enabled, superadmin-gated —
  webhook URLs may embed SIEM auth tokens, secrets NEVER
  returned, only a `secret_set` boolean) — `list_webhooks`
  (registry), `get_webhook_event_types` (the full typed-event
  vocabulary the publisher emits so the LLM can validate
  `event_types[]` references before proposing a subscription),
  `find_webhook_deliveries` (outbox history with state /
  attempts / last_error / last_status_code).
- **dns** — `find_zone_dnssec_info` (DS records + `dnssec_synced_at`
  timestamp).

Tool registry total moves from 108 (at 2026.05.22-2) → 118 (this
release: +3 from #298's `find_upgrade_*` reads, +7 from #280
above). The README's "tools total" mention was stale at "101"
pre-release; it is bumped to the live count of 118 in the same
commit.

### Changed

- **MetalLB stays pinned at v0.15.3** (carried forward from
  2026.05.22-2 #287) — v0.16.x speaker regression
  ([metallb#3063](https://github.com/metallb/metallb/issues/3063))
  still unresolved upstream as of this cut.
- **Helm chart versioning** — chart `version` and `appVersion`
  remain placeholder (`0.1.0` / `0.0.0-dev`); the release
  workflow overrides both via `helm package --version
  --app-version` using the CalVer release tag, so no manual bump
  per release.

### Fixed

- **Cold-boot 502 on a fresh appliance install (#299).** The api
  pod previously passed `/health/ready` the moment Postgres
  accepted `SELECT 1`, before the migrate Job populated the
  schema; route handlers then 500'd and the frontend nginx
  returned 502 to the operator. Schema-at-head check above closes
  the gap; the friendly initialising page covers the still-no-
  ready-pods sub-window from the operator's perspective.
- **Stale `Apply` shape on the Releases tab** (carried forward
  from 2026.05.22-2 #294) — the Rolling Upgrade tab is now the
  canonical multi-node OS upgrade path; the Releases tab remains
  read-only with a pointer.
- **Class name typo** in the appliance delete-dependents response
  model (`AppliancedDependentServer` → `ApplianceDependentServer`)
  surfaced in Copilot PR review (#305).
- **Webhook MCP tools missing the superadmin gate** — caught in
  Copilot PR review; webhook URLs + delivery history now mirror
  the REST endpoint's superadmin-only access (#305).
- **TimezoneSection saved-state copy** was misleading
  ("clearing falls back to install-time default") — supervisor
  short-circuits on empty so the host stays on whatever was last
  applied. Form copy + saved-note rewritten to match reality
  (#305).

### Migrations

- `b1d4c9e57f02` — `dns_server.appliance_id` +
  `dhcp_server.appliance_id` nullable FKs with `ON DELETE
  CASCADE` (#197).
- `c2e6a89d4b15` — `platform_settings.timezone` (`String(64)`,
  default `''`) (#165).
- Multi-node rolling upgrade tables — `system_upgrade_run`,
  `system_upgrade_node`, `system_upgrade_preflight`,
  `slot_image_mirror` row state (#296 — see `backend/alembic/`
  for the head chain).

### Deferred

- 17 `propose_*` write tools for the conformity / webhooks /
  backup / DNS import / multicast / SNMP / NTP surfaces — each
  needs an `Operation` class with preview + apply, tracked as
  **#304** for a focused follow-up PR.

## 2026.05.22-2 — 2026-05-22

Same-day shake-out cut. Testing the 2026.05.22-1 control-plane HA
release on real 3-node hardware surfaced two appliance bugs that only
appear once DNS/DHCP run across multiple control-plane nodes — both
fixed here. No partition-layout change this time, so the
no-in-place-upgrade caveat from 2026.05.22-1 does not re-apply. Note
that an automated multi-node rolling OS upgrade is still pending
(tracked in #296); upgrading a multi-node cluster today is a
per-node / reinstall operation.

### Fixed

- **Multi-node DNS/DHCP agents couldn't come up (#292).** Enabling the
  DNS/DHCP roles across a 3/5/7-node control plane left the agent
  DaemonSets broken three ways: (1) they pointed at the external
  node-IP URL whose self-signed cert they couldn't verify →
  registration failed; now they use the in-cluster API Service over
  plain HTTP (no intra-cluster TLS to verify), same repoint as the
  supervisor heartbeat; (2) a shared RWO local-path PVC pinned its PV
  to one node, so the other nodes' pods sat Pending forever — switched
  to per-node hostPath; (3) `dns-bind9`/`dns-powerdns` used
  `updateStrategy: OnDelete`, which stranded a pod on its stale
  empty-URL spec — switched to RollingUpdate. The fix also persists the
  agents' last-known-good **config-bundle cache** on per-node hostPath
  (`/var/lib/spatium-{dns,dhcp}-agent`) — it was never mounted, so a pod
  restart during a control-plane outage lost it; now the node keeps
  serving from cache when the control plane is unreachable
  (non-negotiable #5). Chart-only — no agent/image change.
- **Dead "Apply" button on the appliance Releases tab (#294).** It
  triggered a pre-#183 docker-compose updater
  (`spatiumddi-update.path` → `docker-compose pull && up -d`) that
  doesn't exist on the k3s appliance, so it silently did nothing. The
  Releases tab is now read-only; appliance OS upgrades are pointed at
  the Fleet tab's A/B slot-image flow, and docker/k8s control planes
  keep the copy-paste manual-upgrade modal. Removed the dead
  `POST /releases/apply` + `/log` endpoints and the trigger machinery.

## 2026.05.22-1 — 2026-05-22

> ⚠️ **RELEASE NOTE — NO IN-PLACE UPGRADE.** This release changes the appliance partition layout (6-partition GPT + Talos-style `state` partition, #276) to support control-plane HA; A/B slot upgrades **cannot** cross that boundary. Existing appliances **must full-reinstall from the new ISO**. (No production installs exist yet — this affects field-test boxes only.)

The **control-plane HA** release. The headline (#272, landed via #282
+ shake-out #287) takes the SpatiumDDI appliance from a single k3s node
— Postgres / Redis / api / frontend all single-replica — to **N
control-plane nodes (3 / 5 / 7)** with operator-visible promotion,
replicated data, and dead-node replacement. Postgres HA runs on
CloudNativePG (primary + streaming replicas + automatic failover);
Redis HA on Sentinel; a MetalLB L2 control-plane VIP gives operators
and off-cluster agents one stable address that floats across nodes;
embedded-etcd quorum spans the members. Promote / demote is a
multi-select Fleet action that enforces the odd-member rule, and a
dead member can be evicted + replaced with a fresh pairing code. The
same cut also ships the **Network → Sites** hierarchy (tree view with
drag-and-drop re-parenting, default layout), immediate WHOIS/RDAP
population on domain + ASN create, and a security-hardening pass
across the auth surface. Six PRs since 2026.05.18-1 (#273 #275 #282
#287 #288 #289).

### Added

Control-plane HA (#272 / #282) — the appliance becomes a true
multi-node cluster:

- **Multi-node k3s control plane** on embedded etcd (1 / 3 / 5 / 7
  servers; odd-count enforced for quorum). The seed installs as a
  control-plane variant; additional approved appliances are promoted
  into the cluster from the Fleet tab.
- **Postgres HA via CloudNativePG** — the Cluster CR reconciles a
  primary + N-1 streaming replicas with automatic failover; api /
  worker / beat point at the `-rw` service. CNPG is the permanent
  appliance default.
- **Redis HA via Sentinel** — 3-node Sentinel with master election;
  the app talks `sentinel://` and re-resolves the master on failover.
- **MetalLB control-plane VIP** (L2) — a floating HTTPS VIP for the
  frontend Service so browsers + off-cluster DNS/DHCP agents have one
  stable address. Operator sets the pool + VIP in Fleet → Network &
  Host. Ships in its own `metallb-system` namespace.
- **Promote / demote** control-plane members from the Fleet tab
  (multi-select, odd-count guard), plus **dead-node replacement**
  (evict the k8s Node → k3s drops the etcd member → mint a
  replacement pairing code).
- **Singleton-tolerant workloads** — beat / migrate / audit-chain
  stay correct when api/worker scale to N replicas.
- **One shared Web UI TLS cert** across replicas via the cluster
  Secret; regenerates to add the VIP / new-node SANs.

Network → Sites + WHOIS immediacy (#278 / #279 via #288):

- **Sites hierarchy tree view** — indented campus → building → floor
  tree as the default layout, with drag-and-drop re-parenting
  (cycle-blocked targets dimmed) and a searchable **Move** modal for
  keyboard / touch. Flat table stays one toggle away.
- **Immediate RDAP on create** — a new Domain populates registrar /
  expiry / nameservers within seconds (one-shot worker task) instead
  of waiting for the next hourly sweep; the list auto-polls until it
  lands. Same parity for **ASNs** (RDAP holder + RPKI ROAs).

Security (#289):

- **`CORS_ORIGINS`** config knob (comma-separated; default `*`).
- **Per-IP login rate limiting** on `/auth/login` + `/auth/login/mfa`
  and a **single-use MFA challenge** (replay guard), both Redis-backed
  and fail-open.

### Changed

- **MetalLB pinned to the full v0.15.3 release** (chart + images +
  CRDs) and moved into the `metallb-system` namespace (#287). v0.16.0
  regressed the speaker's ServiceL2Status reconciler into an
  apiserver-flooding loop (metallb#3063); an image-only pin doesn't
  work (chart/binary probe skew), so the whole release is pinned.
- **Sites default to the tree view** (#288); the flat table is a
  toggle.
- **`VERSION` env threaded into api / worker / beat** on the Helm
  path so the running version is reported correctly (#275).
- **Per-role node-label gating** documented as non-negotiable #16 —
  every new top-level workload schedules on `spatium.io/role-*`, not
  on chart `enabled` toggles (#273).
- **`task_session` engine pool bounded** to `pool_size=1 /
  max_overflow=0`; **pg_dump / psql / pg_restore** now run with a
  minimal allowlisted env instead of inheriting the full parent
  process environment (#289).
- **User-Agent strings sanitised + truncated** before they land in
  audit / session rows; **email format validated** on local user
  create / update (#289).

### Fixed

- **CNPG never scaled on promote** — the Cluster carries
  `helm.sh/resource-policy: keep` (so a failed-release recovery can't
  wipe the DB), which also makes the helm-controller skip patching its
  spec. The seed supervisor now scales `spec.instances` directly via a
  merge-patch, with the matching RBAC grant — Postgres converges to
  N/N on promote with no manual step (#287).
- **A second code-less top-level Site 409'd** ("a sibling site with
  this code already exists") — the unique index treated NULL/empty
  codes as equal. Now a partial index (`WHERE code IS NOT NULL`) +
  `"" → NULL` normalisation; real codes still unique per parent (#288).
- **Promote / VIP-change left the page dead** — both regenerate the
  API cert and roll the frontend pod, breaking the open TLS session.
  New "wait then reload" progress modals track convergence and surface
  a Reload button (#287).
- **Wildcard CORS with credentials** reflected any Origin with
  credentials; the wildcard path no longer enables credentials (the
  API authenticates via the Bearer header, not cookies) (#289).
- **Malformed `CREDENTIAL_ENCRYPTION_KEY`** silently re-keyed secrets
  from `SECRET_KEY`; it now logs loudly and hard-fails under
  `STRICT_SECRET_KEY` (#289).
- Console vitals were clipped when the cluster VIP line was present
  (#287).

### Security

- Per-IP login rate limiting + single-use MFA challenge replay guard
  (#289).
- User-Agent sanitisation (log-forging vector), local-user email
  validation, minimal pg_dump subprocess env (no parent-secret
  inheritance), and TLS-verification-disabled WARNING logs on the
  proxmox / unifi / ftp clients when an operator enables `tls_insecure`
  (#289).

### Migrations

- `a8d2e91f5c47` — `appliance.appliance_variant` (#272 Phase 1).
- `b9e1c43f7d28` — `pairing_code.auto_approve` (#272 Phase 1).
- `c263ae1d381a` — appliance control-plane cluster columns (#272
  Phase 7).
- `d5f1a37c20e9` — `appliance.node_ip` routable host IP (#272 Phase
  7b).
- `e7a2c91d4f60` — `platform_settings` MetalLB control-plane VIP
  (#272 Phase 7c).
- `f3b8d24a1c70` — `appliance.evict_requested` dead-node replacement
  (#272 Phase 9).
- `a1c7e9f32b84` — Site `(parent_site_id, code)` partial unique index
  (#279).

### Breaking

- **Appliance partition-layout change → full reinstall.** See the
  release note at the top: existing field-test appliances cannot A/B
  upgrade across the new 6-partition layout and must be reinstalled
  from this release's ISO.

## 2026.05.18-1 — 2026-05-18

Bug-check housekeeping cut. After the 2026.05.17 hotfix chain
closed the immediate appliance-boot regressions, a parallel
sub-agent audit pass (`mzac-bug-check`) read every module across
api / worker / supervisor / dns-agent / dhcp-agent and flagged
57 issues by severity (3 critical / 11 high / 25 medium / 18
low). All 57 land in this release across four sequential PRs
(#267 #268 #269 #270). Two of the criticals — the silently-
overwritten beat schedule and the missing Operator Copilot
include — had been live since the relevant features shipped;
the third was a 404-loop against the retired `/pair` endpoint.
The high tier closes five privileged-code supervisor security
holes (TLS verify drop to NONE, predictable `/tmp` cert write,
unvalidated CIDRs, host-env injection vectors, untrusted
heartbeat string interpolation), encrypts the previously-
plaintext `dns_server.api_key`, and adds retry policies where
"safe to retry" had decayed into "no autoretry configured."
Medium tier drops 10 dead `@radix-ui` deps, migrates 8
`datetime.utcnow()` sites + the deprecated `ssl._ssl._test
_decode_cert` call, atomicises TSIG / PowerDNS API-key writes,
and tightens 5 more supervisor hygiene items. Low tier is
cosmetic / preventative — including a `STRICT_SECRET_KEY`
boot-gate, `autoretry_for` on 4 more beat tasks, a DHCPv6 stats
map that finally lights up the metrics row for v6 scopes, and
the `PeerResolveWatcher` closure-over-empty-list footgun
removed. Also rolls up the in-flight appliance shake-out (#209
— full-stack setup-wizard 500 + agent-landing Pending) and the
deps bump (#207 — kube-state-metrics, node-exporter, nginx,
redis).

### Fixed

- **Full-stack appliance setup wizard returned 500 + agent-
  landing stuck Pending (#209).** Three independent regressions
  from #183's k3s migration, caught on the 2026.05.17-6 ISO
  boot of 192.168.0.199. (1) k3s `config.yaml.d/spatium-roles
  .yaml` drop-in REPLACES (not appends) the base config's
  `node-label` list, so the umbrella `spatium.io/role=
  appliance` label disappeared on every fresh full-stack
  install — appliance chart's `global.nodeSelector` then
  matched nothing. Drop-in now re-lists the base label. (2)
  `agent-landing` was deployed on full-stack / frontend-core
  variants too, where it would have port-conflicted on :80
  with the real control-plane frontend once (1) was fixed.
  `_render_appliance_helmchart` grows a fourth
  `agent_landing_enabled` arg, false for non-Application
  variants. (3) The api pod had no host bind mounts to write
  `/var/lib/spatiumddi-host/.setup-complete` through, so
  `mark_setup_complete` raised `PermissionError`. Added
  `api.applianceHostMounts` values block (releaseStateDir rw,
  hostLogDir ro, hostEtcDir ro) gated by the umbrella chart;
  firstboot flips it on for appliance installs only. Existing
  2026.05.17-6 appliances pick all three up via slot upgrade;
  operators who need agent-landing / DNS / DHCP pods to
  schedule TODAY can `sudo kubectl label node $(hostname)
  spatium.io/role=appliance --overwrite`.
- **`dns-agent-stale-sweep` beat task never fired (#217).**
  `celery_app.py:51` set ``celery_app.conf.beat_schedule =
  {"dns-agent-stale-sweep": ...}``, then the very next call
  ``celery_app.conf.update(beat_schedule={...})`` silently
  overwrote the whole schedule. The stale-sweep task is defined
  but never enqueued — DNS agents whose heartbeat goes stale stay
  ``status='active'`` in the UI forever. Moved the entry into the
  ``conf.update(beat_schedule=...)`` dict alongside the other 30+
  beat entries.
- **Operator Copilot daily digest never sent (#218).**
  ``app.tasks.ai_digest`` is referenced in ``task_routes`` (line
  102) and the ``ai-daily-digest`` ``crontab(hour=8, minute=0)``
  entry (line 350) but was missing from the worker's
  ``include=[...]`` list. The 08:00 UTC cron fires every day and
  the worker rejects it as ``Received unregistered task of type
  'app.tasks.ai_digest.send_daily_digest'``. Added to the
  include list.
- **DNS + DHCP agents infinite-retried against removed `/pair`
  endpoint (#246).** The pairing-code → PSK exchange was
  retired under #170 Wave A3 (`POST /api/v1/appliance/pair`
  was removed; pairing now flows through
  `POST /api/v1/appliance/supervisor/register` instead). The
  agent's ``pairing.py`` module still POSTed to ``/pair`` on
  bootstrap when ``BOOTSTRAP_PAIRING_CODE`` was set — got a 404
  + retried forever. Deleted ``pairing.py`` from both
  ``agent/dns/spatium_dns_agent/`` and
  ``agent/dhcp/spatium_dhcp_agent/``, plus their tests; updated
  ``bootstrap.py`` + ``config.py`` to use the long
  ``DNS_AGENT_KEY`` / ``SPATIUM_AGENT_KEY`` directly; tightened
  the three container entrypoint pre-checks back to require the
  PSK. Application appliances are unaffected — the supervisor
  injects the per-role keys via ``role-compose.env`` (#170
  Wave C2). Operators on standalone docker-compose / K8s installs
  who had been pasting a pairing code instead of the long key
  must switch to the long key.
- **11 high-severity audit findings (#268).** Five supervisor
  security holes closed: `k8s_api` + `k8s_proxy`
  `_ssl_context(ca_path=None)` no longer drops to `CERT_NONE`
  silently — it raises unless the dev-only
  `SPATIUM_INSECURE_SKIP_TLS_VERIFY=1` opt-out is set (#233);
  `/tmp/.spatium-k3s-ca.crt` predictable-path write moved
  under `$STATE_DIR` with `os.open(..., O_NOFOLLOW |
  O_CREAT | O_TRUNC, 0o600)` (#235); `kubeapi_expose_cidrs`
  validated through `ipaddress.ip_network(strict=False)` and
  canonicalised before nft render — bad entries logged +
  dropped (#236); `dns_agent_key` / `dhcp_agent_key` /
  `dns_group_name` / `dhcp_group_name` from heartbeat run
  through `_safe_env_value()` with strict regex (`^[a-f0-9]
  {32,128}$` / alphanum+`._-`) so newline / quote / control-
  char injection no longer reaches the rendered env file
  (#237); supervisor entrypoint replaces `set -a; .
  "$HOST_ENV"; set +a` with a busybox-portable `read -r` loop
  that validates each KEY against the POSIX env-var-name
  pattern + strips one layer of surrounding quotes — no
  shell interpretation of operator-managed `.env` (#238).
  Plus `app.tasks.ipam` dead-stub module + its `include` /
  route entries deleted (#220), `refresh_blocklist_feed`
  declares `autoretry_for=(httpx.HTTPError, socket.gaierror)`
  + exp backoff + jitter + `max_retries=3` so transient feed
  failures retry instead of staying offline until the next
  beat (#219), DNS agent `HeartbeatClient.send_once` 401/404
  mirrors `sync.py`'s recovery path (clear cached token + set
  `_stop` → re-bootstrap from PSK) so a token-expired agent
  doesn't 401-loop until container restart (#248), PowerDNS
  driver surfaces `log.warning("powerdns_blocklists_
  unsupported", ...)` when `bundle["blocklists"]` is non-
  empty (pdns auth can't do RPZ; the warning makes the
  silent-no-op visible) (#247), DHCP agent
  `_ShipperState.last_seen_macs` dedupe ledger pruned on
  every flush against `_LAST_SEEN_RETENTION = 300.0` so the
  60 s dedupe window doesn't leak forever (#257), and
  `dns_server.api_key_encrypted` retyped `Text → LargeBinary`
  with both writers calling `encrypt_str()` + new Alembic
  migration `97190c1b0325` flipping the column type +
  scrubbing any pre-existing plaintext to NULL (the column
  was decorative — zero readers existed pre-#210 — so the
  data was never load-bearing) (#210).
- **25 medium-severity audit findings (#269).** Frontend
  drops 10 unused `@radix-ui/*` packages + `class-variance-
  authority` from `package.json` (#223-#232) — all were
  declared but never imported anywhere in `src/`; codebase
  uses hand-rolled shadcn-style primitives in
  `src/components/ui/` instead. `npm uninstall` dropped 19
  transitive packages. Api migrates 7
  `datetime.utcnow()` sites to `datetime.now(UTC)` — worst
  case was `app/api/v1/dns/pool_router.py:447` writing a
  naive datetime into a `DateTime(timezone=True)` column
  (#213); replaces the undocumented `ssl._ssl._test_decode
  _cert` call with supported `cryptography.x509.load_der
  _x509_certificate` (output shape preserved so callers
  don't change) (#212). Supervisor hygiene: dev-only TLS-
  verify-disabled now logs `supervisor.tls_verify_disabled`
  WARNING once per process (#234), `/etc/nftables.d`
  permission tightened from `0775` to `0750` (#239),
  `appliance_state.write_reboot_trigger` migrated to
  `datetime.now(UTC)` (#240), watchdog gains explicit `pod:
  k8s_api.PodStatus | None` annotation (#241),
  `maybe_fire_slot_upgrade` validates `desired_url` against
  `https://` / `file://` schemes before writing the trigger
  file (was accepting unschemed + plain `http://`) (#242).
  DNS-agent: atomic-writes PowerDNS API key + BIND9 TSIG key
  via `os.open(...O_NOFOLLOW|O_CREAT, 0o600)` + `.new`
  rename (#249); ALIAS resolver now overridable via
  `options.alias_resolver` (defaults to `1.1.1.1,8.8.8.8`,
  empty string disables ALIAS) (#250); `DriverBase.apply
  _record_op` ABC widened `-> None` → `-> dict[str, Any] |
  None` to match PowerDNS driver's DNSSEC-state return
  (#251); query log shipper `_fh` annotated `TextIO | None`
  explicitly (drops `# type: ignore`) (#252);
  `_load_or_generate_api_key` refuses to overwrite an
  existing-but-unreadable key file (pre-fix silently re-
  generated, causing 401 mismatch with the running pdns)
  (#253). DHCP-agent: `sync._apply_bundle` + `peer_resolve
  ._peer_hosts` use explicit bundle-shape narrowing instead
  of inline-ternary fallthrough that envelope-defaulted on
  any non-dict (#258 #260); `log_shipper._fh` annotated
  `TextIO | None` (#259).
- **18 low-severity audit findings (#270).** Api:
  `health.py` casts `r.ping()` via
  `cast(Awaitable[bool], ...)` so the redis-py union return
  type narrows cleanly (#211); 8 call-sites switch from
  deprecated `asyncio.get_event_loop().time()` to
  `asyncio.get_running_loop().time()` (#214);
  `backend/pyproject.toml` adds `types-croniter` +
  `types-paramiko` to dev extras (#215); new
  `STRICT_SECRET_KEY=true` toggle that hard-fails the boot
  when `SECRET_KEY` is still the `.env.example` sentinel,
  with a loud stderr warning every boot regardless — opt-in
  to keep first-time `cp .env.example .env` setups bootable
  (#216). Worker: `bind=True` + `autoretry_for=(SQLAlchemy
  Error, ConnectionError, OSError)` + exponential backoff
  added to `event_outbox`, `conformity`, `alerts`, and
  `audit_chain_verify` — were re-raising on transient
  DB/network failures with no retry policy (#221 #222).
  Supervisor: drop unused `import subprocess` (Phase 7
  remnant) + fix E402 on `from .service_lifecycle import
  ...` ordering (#243); persist wall-clock timestamp
  alongside the monotonic anchor in watchdog's
  `_status_history` so a backward host-clock adjustment
  doesn't make `since` land in the past relative to itself
  (#244); `types-PyYAML` in supervisor dev extras (#245).
  DNS-agent: hoist `import hashlib` / `import time` from
  `_render_catalog_zone_payload` / `_write_catalog_zone
  _file` to module top (#254); drop `f` prefix on two
  placeholder-less strings (#255); fix stale `# bind9 (only
  supported backend)` comment now that PowerDNS shipped in
  #127 (#256). DHCP-agent: remove duplicate
  `_sniffer.stop()` call (run() owns sniffer cleanup)
  (#261); drop dead `getattr(self, "pending_acks", None)`
  defensive check (#262); hoist `from .cache import
  save_token` from the 401/404 rebootstrap branch (#263);
  add the long-deferred DHCPv6 stat map — multiple v6
  message types fold into v4-shaped columns by closest
  role-equivalent semantics (SOLICIT≈DISCOVER, ADVERTISE≈
  OFFER, REPLY≈ACK, RENEW+REBIND fold into `request`) so
  operators running v6 finally see per-bucket metrics
  (#264); `PeerResolveWatcher` accepts a deferred
  `apply_fn` and exposes `set_apply_fn()` so the supervisor
  can construct the watcher first and arm it once SyncLoop
  exists — drops the `syncer_holder[0]` closure-over-empty-
  list footgun (#265); hoist `verify=` resolution into
  `AgentConfig.httpx_verify()` — was duplicated verbatim
  across 8 modules (#266).

### Changed

- **Bumped four bundled images.** kube-state-metrics
  ``v2.13.0`` → ``v2.18.0``, prometheus node-exporter ``v1.8.2``
  → ``v1.11.1``, nginx ``1.27-alpine`` → ``1.30.1-alpine``
  (agent-landing + frontend runtime), redis ``7-alpine`` →
  ``8.6-alpine``. All four are in-place safe — three are
  stateless (metrics scrapers + nginx web server, no on-disk
  state to migrate), and Redis 8 reads RDB / AOF files written
  by Redis 5+ so the existing ``redis_data`` volume mounts
  cleanly on first start. Updated in lockstep across
  ``appliance/scripts/bake-images.sh``,
  ``charts/spatiumddi/values.yaml``,
  ``charts/spatiumddi-appliance/values.yaml``,
  ``docker-compose.yml``, ``docker-compose.dev.yml``,
  ``frontend/Dockerfile``, ``k8s/ha/redis-sentinel.yaml``,
  ``.github/workflows/ci.yml`` (test redis container), and the
  matching doc comments in ``charts/spatiumddi/Chart.yaml`` +
  ``charts/spatiumddi-appliance/templates/agent-landing.yaml`` +
  ``docs/deployment/APPLIANCE.md``. Postgres stays on
  ``16-alpine`` pending a dedicated PR — PG18 refuses to start
  against a PG16 data directory, so we need an upfront
  ``pg_upgrade`` migration sidecar before we bump (and the
  ``postgresql-client-16`` pin in ``backend/Dockerfile`` needs
  to move in lockstep with the server major to keep
  ``pg_dump`` / ``pg_restore`` matched).

## 2026.05.17-6 — 2026-05-17

Fifth hotfix on the 2026.05.17 chain. 2026.05.17-5 tried to
``apt-get install helm`` from ``baltocdn.com``'s Helm-stable repo
but the Azure-hosted GitHub Actions runner's network policy doesn't
resolve baltocdn.com (``curl: (6) Could not resolve host:
baltocdn.com``), so the bake step never landed. Swapped to the
upstream ``azure/setup-helm@v4`` action which downloads the helm
static binary directly from ``get.helm.sh`` — same channel
get-helm-3 ships from, reachable from Azure runners (GitHub
Releases CDN). Took the opportunity to bump every helm-action pin
in the repo (release.yml's chart-publish job + agent-e2e.yml were
both on the long-stale v3.14.0) to the current latest v3.20.2 so
all three CI surfaces stay aligned.

### Fixed

- **Release workflow's helm install couldn't reach baltocdn.com.**
  Replaced the apt-repo recipe in ``build-appliance-iso`` with
  ``azure/setup-helm@v4`` pinned to ``v3.20.2`` — same channel
  ``get-helm-3`` ships from, reachable from Azure runners.

### Changed

- **All ``azure/setup-helm`` pins bumped to v3.20.2.**
  ``release.yml``'s Publish-Helm-chart job and ``agent-e2e.yml``'s
  kind-cluster job were both on v3.14.0; aligned with the new
  ``build-appliance-iso`` job's pin so the three CI surfaces
  don't drift.

## 2026.05.17-5 — 2026-05-17

Fourth hotfix on the 2026.05.17 chain. The previous four cuts all
produced a green release artifact and uploaded an appliance ISO, but
once the ISO was booted on a fresh VM (192.168.0.199) it became
clear the slot rootfs was missing two artifacts that #194's k3s
migration introduced: the k3s static binary at `/usr/local/bin/k3s`
and the helm charts at `/usr/lib/spatiumddi/charts/{spatiumddi.tgz,
spatiumddi-appliance.tgz}`. `k3s.service` crash-looped at restart
counter 43, firstboot wedged forever at "Waiting for k3s /readyz",
no api pod ever came up, no web UI ever served. The local `make
appliance-baked-iso` chain runs `appliance-fetch-k3s` +
`appliance-bake-chart` + `appliance-bake-control-chart` before mkosi
— the release workflow's `build-appliance-iso` job inlines its own
steps and never picked them up after #194 added them. Bundled with a
separate `NameError` regression in `spatium-console` that crash-
looped the F-key console dashboard at first frame (restart counter
18 on 192.168.0.199).

### Fixed

- **Release workflow missing k3s + chart bake steps.** Added three
  steps to `.github/workflows/release.yml`'s
  `build-appliance-iso` job ahead of the mkosi build:
  apt-install `helm` from the Helm stable repo (Debian's archive
  doesn't carry a current helm), run `appliance/scripts/fetch-k3s
  .sh` to download the pinned k3s static binary + airgap images
  tarball + LICENSE into `mkosi.extra/`, run `appliance/scripts/
  bake-chart.sh` (appliance chart) and `CHART_NAME=spatiumddi
  appliance/scripts/bake-chart.sh` (umbrella control chart) to
  package both helm charts into `mkosi.extra/usr/lib/spatiumddi/
  charts/`. Mirrors `make appliance-fetch-k3s appliance-bake-chart
  appliance-bake-control-chart` from the local
  `appliance-baked-iso` chain. Without this the slot rootfs has no
  k3s and no charts; the booted appliance is completely non-
  functional. Caught when 192.168.0.199 came up from the
  2026.05.17-4 ISO with k3s.service crash-looping and firstboot
  warning "/usr/lib/spatiumddi/charts/spatiumddi.tgz missing —
  control plane won't auto-deploy".
- **`spatium-console` `NameError: name 'rows' is not defined`** at
  `disk_summary` L619. #194 dropped the docker-overlay skip-prefix
  block and accidentally also dropped the `rows: list[tuple[str,
  float, float, float]] = []` initialization line. The function
  appended to an undefined name on its first iteration → the
  service crash-looped at restart counter 18 within seconds of
  boot. Restored the initialization.

## 2026.05.17-4 — 2026-05-17

Third hotfix in the chain. With #201 + #203 the bake script + sudo
env-passthrough were fixed and the bake itself succeeded on the
2026.05.17-3 release-workflow run: 11 images, 382 MB written into
the rootfs overlay. But the workflow's post-bake verification then
errored at `ls: cannot access 'appliance/mkosi.extra/usr/lib/spatiumddi/docker-overlay.img':
No such file or directory`. The path is from the pre-#183 era when
`bake-images.sh` ran a docker-in-docker sandbox to populate
`/var/lib/docker` and rsynced it into a per-slot overlay image; the
post-#183 path writes per-image `.tar.zst` tarballs to
`var/lib/rancher/k3s/agent/images/` for k3s's containerd to auto-
import at startup. The bake step's verification was never updated
to match. Fixed.

### Fixed

- **`build-appliance-iso` post-bake verification looked for a
  pre-#183 docker-overlay.img.** Replaced with an `ls -lh` +
  `du -sh` against the post-#183 tarball directory the bake script
  actually populates.

### Changed

- **Appliance operator-facing docs caught up to k3s.**
  `appliance/README.md` and `appliance/cloud-init/README.md` still
  described the pre-#183 first-boot flow (`docker compose pull && up
  -d`) + referenced the now-gone `mkosi.extra/usr/local/share/
  spatiumddi/docker-compose.yml` artifact. Rewritten to describe the
  k3s flow: firstboot renders a variant-specific HelmChart manifest
  into `/var/lib/rancher/k3s/server/manifests/`; k3s containerd
  auto-imports the per-image tarballs at `/var/lib/rancher/k3s/
  agent/images/*.tar.zst` so a fresh boot never reaches out to
  ghcr.io; helm-controller installs the chart tarball baked at
  `/usr/lib/spatiumddi/charts/`.
- **`spatium-console` skipped the loop-mounted `/var/lib/spatiumddi/
  docker-overlay/` mount in the disk panel** — dead skip rule from
  the docker-overlay era (the path doesn't exist post-#183). Rule +
  the two comment blocks that referenced it removed.

## 2026.05.17-3 — 2026-05-17

Second hotfix on top of -2. The release-workflow's
`build-appliance-iso` job failed again — different bug. The
workflow's `Bake container images into rootfs overlay` step runs
`sudo appliance/scripts/bake-images.sh` (sudo is required for the
later loop-mount of the docker-overlay.img), but `sudo` strips the
environment by default — so `SPATIUMDDI_VERSION=2026.05.17-2` and
`BAKE_SOURCE=ghcr` (set on the job's `env:` block) never reached
the script. The script fell back to its `SPATIUMDDI_VERSION=dev` +
`BAKE_SOURCE=local` defaults, then errored at
`ERROR: no local image found for ghcr.io/spatiumddi/spatium-supervisor`
because there's no `:dev`-tagged image on a GitHub-hosted runner.

Fix is one line: `sudo -E` instead of bare `sudo` so the env passes
through. Plus a preemptive `DOCKER_CONFIG=$HOME/.docker` to point
root's docker CLI at the runner-user's `~/.docker/config.json`
where the workflow's `docker/login-action` step stamped the GHCR
credentials — our `ghcr.io/spatiumddi/*` container images are
private, so without this redirect the `docker pull` calls inside
the script would 401 anonymously even after the env-var fix.

Same story as -2: container images + Helm chart are already published
from the -1 / -2 cuts; this release only re-runs the workflow against
the env-passthrough fix so the appliance ISO + slot raw.xz finally
land on a GitHub release page. No operator action needed on existing
docker-compose or Kubernetes installs.

### Fixed

- **Release-workflow `Bake container images` step lost
  `SPATIUMDDI_VERSION` + `BAKE_SOURCE` through `sudo`.** Changed
  `sudo appliance/scripts/bake-images.sh` to `sudo
  DOCKER_CONFIG="$HOME/.docker" -E appliance/scripts/bake-images.sh`
  so the env passes through and root inherits the runner-user's
  GHCR auth.

## 2026.05.17-2 — 2026-05-17

Same-day hotfix for the 2026.05.17-1 release pipeline. The
`build-appliance-iso` job in the release workflow failed at 14 s
with exit code 3 because `appliance/scripts/bake-images.sh` tried
to `docker pull` three images that don't exist:
`ghcr.io/spatiumddi/spatiumddi-worker`,
`ghcr.io/spatiumddi/spatiumddi-beat`,
`ghcr.io/spatiumddi/spatiumddi-migrate`. The umbrella chart's
worker / beat / migrate Deployments + Jobs all share the
`spatiumddi-api` image with different `command:` overrides
(confirmed across `docker-compose.yml` and
`charts/spatiumddi/templates/{api,worker,beat,migrate}.yaml`); no
separate Dockerfile, no `build-*` job in the release workflow, no
ghcr tag pushed. They were added to `bake-images.sh` in #183
Phase 11 wave 1 (commit `a6bc553`) without being verified —
straight ghost entries.

2026.05.17-1 published the container images + Helm chart to ghcr
intact (those jobs ran before the bake step failed). docker-compose
+ Kubernetes installs of 2026.05.17-1 already work; this release
only re-runs the workflow against the fixed bake script so the
appliance ISO + slot raw.xz land on the GitHub release page. No
operator action needed on existing installs.

### Fixed

- **Release-workflow `build-appliance-iso` failed on missing
  spatiumddi-worker / -beat / -migrate images.** Dropped the three
  nonexistent entries from `appliance/scripts/bake-images.sh`'s
  `IMAGES` array; the two real control-plane-bundled images
  (`spatiumddi-api` + `spatiumddi-frontend`) stay. Slot rootfs disk
  savings ~250 MB.

## 2026.05.17-1 — 2026-05-17

Big-architecture release — the SpatiumDDI appliance moves end-to-end
from `docker-compose` to **k3s + Helm orchestration (#183)**. Eleven
phases shipped on `issue-183`: bake k3s into the slot rootfs as a
~70 MB static binary, ship per-service helm-controller-driven HelmChart
CRs as the declarative target, rewrite the supervisor to PATCH node
labels for role swaps (single `kubectl label node` call replaces the
old `docker compose down/up` dance), strip docker entirely, AIO + Core
+ Application install variants land in the wizard, frontend on
hostNetwork serves :443 from first boot with a self-signed cert that
operators replace through the in-UI cert manager, and every
`/appliance` management surface (Pods tab, TLS Secret PATCH, Logs &
Diagnostics self-test) gets rewritten to call kubeapi via the api
pod's mounted ServiceAccount instead of the now-dead docker socket.
Role swap latency drops from "tens of seconds + occasional manual
recovery" to "single API call, ~25-35 s end-to-end with no recovery
needed." Off-appliance Helm operators benefit too — flipping
`APPLIANCE_MODE=true` on an umbrella-chart install lights up the same
`/appliance` management surfaces (sans slot-upgrade UI which stays
appliance-ISO-specific). The full design + phase breakdown is in
[`docs/deployment/APPLIANCE.md`](docs/deployment/APPLIANCE.md) under
the new "Current architecture (post-#183)" section. Follow-ups
deferred to [#193](https://github.com/spatiumddi/spatiumddi/issues/193)
(Phase 4 control-plane proxy half, Helm release UI, krew, firstboot
fail-on-missing-tarball, `appliance_mode` split into `k8s_mode` +
`appliance_mode`, plus three carry-overs from #170 Wave E).

Also lands two operator-facing bug closures: **OIDC superadmin gates
(#190)** — eight per-endpoint local `_require_superadmin` helpers
(diagnostics / pairing / slot_images / supervisor approval / alerts /
backup × 2 / factory_reset) each open-coded `user.is_superadmin ==
True`, missing the RBAC wildcard path the canonical `require_superadmin`
in `deps.py` already handled. OIDC users mapped into a Superadmin-role
group passed every `require_permission` gate but hit 403 on every
hand-rolled superadmin surface. New `is_effective_superadmin(user)`
helper in `app/core/permissions.py` unifies both paths; all eight
hand-rolled helpers + the canonical dependency now delegate to it. And
**backend error detail in the UI (#186)** — 40 render sites across 20
files were rendering `(error as Error).message` (which shows axios's
generic `"Request failed with status code 409"`) instead of pulling
the real `response.data.detail` (which carries the operator-readable
message like `"No DNS_AGENT_KEY configured on the control plane..."`).
The `formatApiError(err)` helper that already existed in `lib/api.ts`
from #31 was simply unused at every cast-to-Error site; this release
sweeps all 40 + the two `String((mutation.error as Error).message ??
mutation.error)` variants in PairingTab to use it.

**OIDC auth library migration (#187).** `authlib.jose` is deprecated
and slated for removal in authlib v2.0. The OIDC token-validation
layer in `app/core/auth/oidc.py` migrates to `joserfc` — the library
authlib itself recommends as the replacement. Wire-level behaviour
unchanged (RS256 / `exp` / `iss` / `aud` / `nonce` / tampered-signature
validation all keep the same semantics); the migration is purely
internal. 15 new unit tests at `tests/test_oidc.py` exercise the full
`exchange_code()` flow against real RSA-signed JWTs (not mocked
internals) covering every claim validation path + the
`OIDCServiceError` raise behaviour. Zero `DeprecationWarning` on
import after the swap.

**prune_pairing_codes AttributeError (#189).** The Celery task that
sweeps stale pairing codes referenced `PairingCode.used_at` — a column
that was dropped during #170 Wave A3 (claim accounting moved to a
separate `PairingClaim` child table). Result: every 30-minute beat
tick raised `AttributeError`, the task failed silently in the worker
log, the table never pruned. Replaced the broken column references
with a correlated subquery against `PairingClaim` — claimed codes
prune via `HAVING MAX(claimed_at) < cutoff` (correctly handles
persistent codes with one old + one recent claim), expired codes prune
via `NOT EXISTS` against PairingClaim so a claimed-but-expired code
isn't double-counted. 16 integration tests against live Postgres
cover the three buckets + boundary conditions + persistent multi-claim
semantics + idempotency.

**Frontend nginx upstream + resolver (#176).** The frontend nginx
config previously hardcoded the api host as `api:8000` which worked
for docker-compose but broke on Kubernetes (where the api service is
on `<release>-api.<namespace>.svc.cluster.local` and the cluster
resolver isn't in nginx's default config). The frontend Dockerfile
now ships a templated `default.conf.template` that gets filled in at
container start from `API_UPSTREAM_HOST` + `API_UPSTREAM_PORT` env
vars, plus an `API_DNS_RESOLVER` setting that nginx uses to resolve
upstream hostnames at first request (so pod restarts on the api side
don't strand the frontend on a stale IP). Defaults preserve the
docker-compose shape; the umbrella chart sets the K8s shape; the
appliance bootstrap sets the in-cluster shape with the FQDN form for
the hostNetwork frontend pod's edge case (nginx's resolver doesn't
honour /etc/resolv.conf's search list).

**Proxmox VNet → IPAM subnet matching by CIDR (#177).** The Proxmox
integration's VNet-to-subnet mapping previously matched by name (the
operator-supplied label on the Proxmox SDN VNet). Names drift —
operators rename VNets in Proxmox without telling SpatiumDDI, then
wonder why the integration's "matched subnet" column flips to "no
match" on the next poll. The matcher now keys on **CIDR overlap** —
the IPAM subnet whose CIDR best-matches the VNet's CIDR (longest-
prefix wins on partial overlaps) gets the link, regardless of names.
Existing matched-by-name links migrate to matched-by-CIDR
automatically on the next poll if the CIDR also matches; orphans
surface in the integration's "unmatched" list for operator review.

**Appliance polish (#181 + #182, landed pre-#183).** The DHCP server
detail surface gains a tabbed modal mirroring the DNS side (Overview /
Sync / Events / Logs / Config — Stats deferred to [#195](https://
github.com/spatiumddi/spatiumddi/issues/195)). Three new endpoints
under `/api/v1/dhcp/servers/{id}` (`pending-ops` / `recent-events` /
`rendered-config`); the existing Kea log pipeline drives the Logs
tab. Per-server **maintenance mode** (#182) lets operators pause a
single DNS or DHCP server without removing it from its group —
ConfigBundle long-poll responses get a `paused=true` marker so the
agent stops applying changes, the control plane masks heartbeat
"offline" alerts during the window, and the operator's "reason" + a
"pausing since X ago" chip render across the UI. The fix replaces the
old two-bad-choices situation: delete the row (loses peer + pending
state) vs stop the container (control plane keeps pushing config, red
alerts fire, fleet view shows a degraded server). Maintenance mode is
the deliberate "I'm working on this — don't worry about it" toggle.

Plus a wave of housekeeping closures: **#188 Appliance joins wrong
group on whitespace** marked moot by #170's removal of the installer's
group prompt (groups are now Fleet-UI assigned after admin approval)
and the parallel drop of pairing-code `server_group_id` pre-assignment
in Wave A3 (migration `b5a8d2e9c473`); **#181's missing Stats tab**
carved out as #195 with the open product decision documented (lease-
rate timeseries + active-lease KPI as the recommended core).

### Added

- **Appliance k3s + Helm orchestration (#183).** Eleven-phase
  architecture pivot. Highlights:
  - **k3s static binary baked into the slot rootfs** at
    `/usr/local/bin/k3s` (~70 MB), pinned via the Makefile's
    `K3S_VERSION` env. `kubectl` / `crictl` / `ctr` symlink to it.
  - **Airgap-image preload** — k3s's own images (CoreDNS /
    local-path / pause / metrics-server) + every SpatiumDDI
    container image (api / frontend / worker / beat / migrate /
    dns-bind9 / dns-powerdns / dhcp-kea / supervisor + postgres:16-
    alpine + redis:7-alpine + nginx:1.27-alpine for AIO/Core
    variants) ship as zst-compressed tarballs under
    `/usr/lib/spatiumddi/images/`. firstboot imports them via
    `ctr -n k8s.io images import`. A fresh boot never reaches out
    to ghcr.io.
  - **Two HelmChart CRs** drive the cluster state — `spatium-
    bootstrap` (variant-aware: Application = supervisor + agent-
    landing; AIO + Core = umbrella with control plane pods) and
    `spatiumddi-appliance` (role DaemonSets for dns-bind9 /
    dns-powerdns / dhcp-kea, Application variant only). Both chart
    tarballs baked at `/usr/lib/spatiumddi/charts/`.
  - **Three install variants** in the wizard — Application /
    All-in-One / Core-only. Application = supervisor pairs against
    a remote control plane (current shape pre-#183). AIO = control
    plane + role pods on the same single-node k3s. Core = control
    plane only; remote Applications pair against it. Pairing
    prompts gate on `APPLIANCE_ROLE=application`.
  - **Node-label-based role scheduling** (Phase 10, also lands here).
    The supervisor's role-swap path is now a single
    `kubectl label node <name> spatium.io/role-<role>=true|-` API
    call. Per-role DaemonSets carry a matching `nodeSelector`; the
    k8s scheduler picks up the label and schedules / terminates the
    role pod within ~1-2 s. The HelmChart CR for the appliance
    release is installed once at first boot and never re-PATCHed for
    role swaps — kine (the SQLite-backed k3s datastore) stays small.
    `reconcile_node_labels()` runs on every heartbeat to catch
    out-of-band `kubectl label` drift.
  - **TLS from first boot.** `spatiumddi-firstboot` generates a
    self-signed RSA cert with the host's globally-scoped IPs in the
    SAN list, writes a Secret manifest at
    `/var/lib/rancher/k3s/server/manifests/spatium-appliance-tls.yaml`,
    and the umbrella chart's frontend mounts it via a ConfigMap-
    templated nginx config (`:80 → 301 https`, `:443 TLS` against
    `/etc/nginx/tls/tls.crt` + `tls.key`). Operators browse to
    `https://<appliance-ip>/` immediately; replace the cert through
    `/appliance → Web UI Certificate` (the cert manager now PATCHes
    the Secret in place + bumps a checksum annotation on the
    frontend Deployment to trigger a rollout — replaces the
    pre-#183 SIGHUP-the-frontend-via-docker-sock path).
  - **k3s-aware `/appliance` management surfaces.** New
    `app/services/appliance/k8s.py` stdlib HTTPS kubeapi client
    (mirrors the supervisor's pattern); new
    `charts/spatiumddi/templates/api-rbac.yaml` adds a per-namespace
    ServiceAccount + Role + RoleBinding (`pods` get/list/watch +
    `pods/log` + the specific `spatium-appliance-tls` Secret
    patch + the frontend Deployment annotation patch). Pods tab now
    lists pods via kubeapi instead of docker; "Restart pod" deletes
    the pod (the owning Deployment / DaemonSet recreates it on the
    next reconcile); SSE live logs wrap kubeapi's `?follow=true`
    pod-log endpoint.
  - **Talos-style console dashboard expansion.** Pods panel lists
    all spatium pods with header + state coloring + Age + ports
    column. F3 opens a pod-log viewer. New `Watchdog` header line
    surfacing the external watchdog state. Live-log noise filter
    drops Python tracebacks + systemd restart spam.
  - **Atomic A/B slot upgrades** stay unchanged from #138 — the
    raw.xz slot image now also carries the baked k3s binary + images
    + chart tarballs, so a slot upgrade is **also** a container
    upgrade. Same `/health/live` auto-commit / auto-revert flow.

- **DHCP server detail modal (#181).** Tabbed modal mirroring the
  DNS-side server detail. Five tabs ship — Overview / Sync / Events /
  Logs / Config. Three new endpoints under
  `/api/v1/dhcp/servers/{id}` (`pending-ops` / `recent-events` /
  `rendered-config`). The Logs tab reuses the existing Kea log
  pipeline. Stats tab deferred to #195 (lease-rate timeseries +
  active-lease KPI need a product decision before building).

- **Per-server maintenance mode (#182, DNS + DHCP).** New `paused`
  flag on `dns_server` + `dhcp_server`. ConfigBundle responses to a
  paused server's long-poll carry `paused=true` + an operator-set
  `reason` string; agents stop applying config changes while paused
  (no zone reloads, no Kea reloads — they just heartbeat). Control
  plane masks "offline" / "heartbeat stale" alerts during the
  window. UI: pause button on the server detail modal opens a
  `PauseServerModal` for the reason + a confirmation; resumed by
  clicking the chip. The original problem this closes: pre-fix,
  operators taking a server offline for maintenance had to pick
  between deleting the row (loses peer + pending state) and
  stopping the container (control plane spams 'down' alerts).
  Maintenance mode is the deliberate "I'm working on this, don't
  worry" toggle.

- **Templated frontend nginx upstream + resolver (#176).** The
  frontend Dockerfile ships a `default.conf.template` filled in at
  container start from `API_UPSTREAM_HOST` / `API_UPSTREAM_PORT` /
  `API_DNS_RESOLVER` env vars. Defaults preserve docker-compose's
  `api:8000` shape; the umbrella chart sets the K8s service FQDN
  + the cluster's DNS resolver; the appliance bootstrap sets the
  FQDN form for the hostNetwork frontend pod (nginx's resolver
  doesn't honour `/etc/resolv.conf` search list).

- **Effective-superadmin helper (#190).** New
  `is_effective_superadmin(user)` in `app/core/permissions.py`
  unifies the legacy `User.is_superadmin == True` flag with the
  group → role wildcard `{action: "*", resource_type: "*"}`
  permission path. Six new unit tests cover both paths + the
  inactive-superadmin admission carve-out.

### Changed

- **Appliance: docker-compose → k3s (#183).** The supervisor no
  longer runs `docker compose up/down` against service containers;
  role swaps are `kubectl label node` calls + k8s scheduler does
  the rest. No docker binary on the appliance rootfs (Phase 7).
  The pre-#183 path is gone in every shipped artifact; existing
  installs upgrade by applying a fresh slot image.
- **`/appliance → Containers` tab renamed to `Pods`** to match the
  underlying k8s primitive. UI strings (Docker socket →
  ServiceAccount; container → pod) follow.
- **Eight per-endpoint local `_require_superadmin` helpers**
  (diagnostics / appliance pairing / slot_images / supervisor /
  alerts / backup / backup-targets / factory_reset) now delegate to
  the new `is_effective_superadmin` helper. Error messages + audit-
  log shapes unchanged.
- **40 frontend `(error as Error).message` render sites** (across
  20 files) replaced with `formatApiError(error)` so the backend's
  `response.data.detail` reaches the operator. The
  `formatApiError` helper itself is unchanged (it shipped in #31);
  the change is making every error-surface actually use it.
- **OIDC token validation migrates from `authlib.jose` to `joserfc`**
  (#187). Wire behaviour unchanged; the migration is purely internal
  (`authlib.jose` is deprecated upstream).
- **Proxmox VNet → IPAM subnet matching is now CIDR-based, not
  name-based** (#177). Names drift; CIDRs don't. Existing name-
  matched links migrate to CIDR-matched on the next poll when the
  CIDR also matches.
- **`require_superadmin` dependency in `app/api/deps.py`** drops
  its inline duplication and delegates to
  `is_effective_superadmin` instead.

### Fixed

- **`prune_pairing_codes` `AttributeError` on every beat tick (#189).**
  Task referenced `PairingCode.used_at` which was dropped in #170
  Wave A3; replaced with a `PairingClaim` correlated subquery using
  `HAVING MAX(claimed_at)` for the claimed bucket and `NOT EXISTS`
  for the expired bucket. 16 integration tests against live
  Postgres cover the three buckets + edge cases (persistent multi-
  claim, claimed-but-expired protection, idempotency).
- **OIDC users mapped to Superadmin-role group hit 403 on
  Diagnostics + 7 other surfaces (#190).** Eight hand-rolled
  `_require_superadmin` helpers only checked the legacy
  `User.is_superadmin` column; users with the RBAC wildcard
  permission through a group → role pass `require_permission` gates
  but failed those helpers. Fixed by delegating to
  `is_effective_superadmin` which accepts both paths.
- **UI showed "409" / "422" instead of the backend's actual error
  message (#186).** 40 render sites across 20 files were using
  `(error as Error).message` (axios's generic "Request failed with
  status code N") instead of pulling `response.data.detail` via the
  existing `formatApiError` helper. Pairing-code generation, slot-
  image upload, agent-key reveal, fleet operations, IPAM mutations
  all benefit.
- **DNS record changes didn't propagate to non-primary group
  members.** Pre-fix, `enqueue_record_op` queued one op against
  `is_primary=True` and the agent's pending-op shipper gated on the
  same flag — but under #170 every group member renders its zone as
  `type master` (independent authoritative copy), so secondaries
  stayed frozen at whatever bundle they received on initial
  register. Now fans out one `DNSRecordOp` row per enabled agent-
  based server in the group regardless of `is_primary`.

### Migrations

The k3s migration itself doesn't add any Postgres alembic migrations
(the change is at the orchestration layer, not the schema). Existing
installs upgrade by applying a fresh slot image — the next boot picks
up the baked k3s binary + chart tarballs and the helm-controller
reconciles the bootstrap manifest into a running cluster.

Application appliances that were paired pre-#183 keep working — the
supervisor's identity / cert / approval state lives on
`/var/persist/spatium-supervisor/` which survives the slot swap. The
DNS / DHCP service containers' agent JWTs are similarly preserved.

### Deprecated

- **`/api/v1/appliance/slot-images/*` endpoints** stay functional
  but a rename to `upgrade-images` is queued in [#199](https://
  github.com/spatiumddi/spatiumddi/issues/199) along with a
  GitHub-Releases-driven picker. No removal in this release.

### Security

- **`authlib.jose` deprecation closed before authlib v2.0** (#187).
  Removes the `AuthlibDeprecationWarning` on import + future-proofs
  the OIDC token validation path against the upcoming authlib v2.0
  drop of the `jose` module. Same wire-level semantics; new unit
  test coverage at `tests/test_oidc.py`.

## 2026.05.14-1 — 2026-05-14

Big-feature release closing out two appliance arcs back-to-back.
**Pairing codes (#169)** replace the 64-char hex bootstrap key on the
agent installer with a single-use 8-digit code minted on the control
plane — operators no longer have to read a wall of hex over an IPMI /
serial console. The arc ships in five phases across the same release:
Phase 1 lays the backend (``pairing_code`` table, four endpoints —
create / list / revoke + the unauthenticated ``/api/v1/appliance/pair``
consume — Celery beat reaper, four audit event types, a superadmin-
gated ``find_pairing_codes`` MCP tool, 15 pytest cases); Phase 2 ships
the ``Appliance → Pairing`` tab (card-style radio for kind, generate
modal with copy + live countdown + regenerate loop, table with state
chips and an idempotent revoke action) and adds a third
``deployment_kind="both"`` returning DNS + DHCP keys in one consume
call for combined-agent boxes; Phase 3 wires both DNS + DHCP agents to
accept ``BOOTSTRAP_PAIRING_CODE`` alongside the long PSK env via a
three-tier resolver (explicit env > cached resolved key on disk >
pairing-code exchange) with fatal-on-403 semantics so a dead code
doesn't crash-loop; Phase 4 lands the installer wizard ``Bootstrap
method`` radio (Pairing code recommended / Bootstrap key advanced),
the firstboot env propagation, and a colour-coded ``Pairing`` row on
the console dashboard (Paired ✓ green / Registering… or Pairing in
progress… yellow / Pair failed — regenerate code red); Phase 5 adds
docs + three new typed webhook events (``appliance.pairing_code.created
/.claimed / .revoked``) and switches the Pairing tab to adaptive
polling (2 s when at least one pending code exists, 15 s when idle).
**SNMP support (#153)** lands the second big feature: snmpd runs at
the OS level on every appliance host — local + every registered remote
agent — driven by a ``platform_settings`` singleton that ships through
the ConfigBundle long-poll, with both v2c (community + source-CIDR
allowlist) and v3 USM (per-user auth/priv) modes, a host-side
``spatium-snmp-reload`` runner that validates with ``snmpd -t`` before
atomic install, automatic nftables UDP 161 drop-in management when the
SNMP toggle flips, and an ``A/B persistence contract`` documented in
the runner header so the config survives slot swaps via the ``/etc``
overlay → ``/var/persist/etc``. The Settings → Security page grows a
password-confirm Reveal flow for the SNMP community string mirroring
the agent-bootstrap-keys pattern. The SNMP page lives under
``/appliance`` (not Settings, where it landed first then moved per
operator feedback) so all fleet-rollout config sits together. SNMP is
disabled by default — operators opt in. Also fixes **audit-log
tamper-detection false positive (#73)** that hit every install: the
``before_flush`` event listener was hashing ``id=None`` +
``timestamp=None`` because both columns' SQLAlchemy defaults
(``default=uuid.uuid4`` for id, ``server_default=now()`` for
timestamp) fire AFTER the ``before_flush`` event, not before — so
runtime hashes never matched what Postgres later persisted, and the
verifier reported ``row_hash_mismatch`` on every row. Fixed by pre-
populating both defaults before computing the hash, with a one-shot
data migration (``d4f8c91a2e35``) walking every existing
``audit_log`` row in ``seq`` order and re-hashing with the current
populated values so post-upgrade ``verify_chain`` reports
``ok=True``. Plus polish: the Appliance tabs are now sorted
alphabetically (Containers / Logs / Maintenance / Network / NTP / OS
Versions / Pairing / Releases / SNMP / Web UI Certificate) with a
keep-this-sorted note in the file so future tab additions stay
alphabetical; the **Releases** tab collapses anything older than the
top 3 behind a ``▶ Show N older releases`` disclosure (with a
``installed: <tag>`` chip on the disclosure summary when the
currently-running version sits in the older bucket) so the tab stays
scannable after a project ships dozens of releases; the **README**'s
``Quick start with the OS appliance ISO`` section is rewritten from a
single CLAUDE.md-style mega-paragraph into five skimmable sub-sections
with hard-wrapped lines (~70-80 chars), gaining a ``Joining DNS / DHCP
agents`` sub-section that documents the pairing-code workflow as the
recommended path; and a follow-up fix on the agent container
entrypoints (caught live on a fresh agent appliance install) relaxes
the ``DNS_AGENT_KEY:?`` / ``SPATIUM_AGENT_KEY:?`` pre-check across
bind9 / powerdns / kea so a container booted with only
``BOOTSTRAP_PAIRING_CODE`` doesn't crash-loop before the Python
resolver runs.

### Added

- **Appliance pairing codes (#169).** Short-lived (default 15 min,
  max 1 h) single-use 8-digit codes that replace the long hex
  bootstrap key on the agent installer. End-to-end arc:
  - Backend ``pairing_code`` table (migration ``e2c91d5f7a48``;
    follow-up ``f8a92c1d3b65`` widens the CHECK constraint to accept
    ``deployment_kind='both'``) with sha256 ``code_hash`` (unique
    indexed for O(log n) consume lookup) + ``code_last_two`` (UX
    affordance) + polymorphic ``server_group_id`` (UUID, no FK
    — disambiguated by kind) + ``expires_at`` / ``used_at`` /
    ``revoked_at`` lifecycle columns. Four endpoints under
    ``/api/v1/appliance``: ``POST /pairing-codes`` (create —
    superadmin gate, optional pre-assigned ``server_group_id`` and
    ``note``, refuses if no ``*_AGENT_KEY`` is configured); ``GET
    /pairing-codes`` (list — superadmin, ``include_terminal`` flag);
    ``DELETE /pairing-codes/{id}`` (revoke — idempotent on terminal
    rows); ``POST /pair`` (consume — UNAUTHENTICATED, hash-compare
    lookup, atomic claim, generic 403 + 500 ms friction-sleep on every
    failure mode so timing / response shape don't leak whether a code
    is unknown / expired / claimed / revoked).
  - Four audit event types: ``appliance.pairing_code_created`` /
    ``_claimed`` / ``_revoked`` / ``_consume_denied``. Three of those
    (skipping ``consume_denied`` to avoid scan-noise) map through
    ``event_publisher._SPECIAL_EVENT_MAP`` to the typed webhook
    surface so external integrations can subscribe to
    ``appliance.pairing_code.{created,claimed,revoked}`` (auto-appears
    in the ``/api/v1/webhooks/event-types`` catalog — total event-type
    count went 112 → 115).
  - Superadmin-gated ``find_pairing_codes`` MCP tool with
    ``module="appliance.pairing"``, ``default_enabled=True``. Filters
    by ``deployment_kind`` + ``state``, never surfaces the cleartext
    code or the sha256 hash, only ``code_last_two``. No
    ``propose_create_pairing_code`` write companion by design — the
    create response carries cleartext code we don't want in chat
    transcripts.
  - Celery beat reaper (``app.tasks.prune_pairing_codes``) sweeps stale
    rows every 30 min: 30 d claimed / 7 d revoked / 24 h post-grace
    expired.
  - Frontend ``Appliance → Pairing`` tab — card-style radio buttons for
    agent kind (DNS / DHCP / DNS + DHCP), kind-filtered group dropdown
    (disabled when kind=both with an inline note explaining
    per-service group configuration is post-registration), expiry
    select + free-form note, generate-code button. Success view shows
    the cleartext code in a 3xl monospace box with copy button
    + live countdown + generate-another loop. Table renders codes
    with last-2-digits only (``••••••XX``), kind chip (blue DNS /
    purple DHCP / emerald DNS + DHCP), state chip (pending blue /
    claimed emerald / expired amber / revoked zinc), per-row revoke
    via shared ``ConfirmModal``. Adaptive polling: 2 s while at least
    one pending code exists, 15 s otherwise.
  - Both DNS + DHCP agents accept ``BOOTSTRAP_PAIRING_CODE`` alongside
    ``DNS_AGENT_KEY`` / ``SPATIUM_AGENT_KEY``. Three-tier resolver
    (``pairing.resolve_bootstrap_key``): explicit env key wins; falls
    back to ``<state_dir>/bootstrap.key`` on disk (mode 0600, cached
    from a previous successful pair so re-bootstraps survive ``rm
    agent_token.jwt`` without burning a fresh code); falls back to
    pairing-code exchange. 403 / wrong-kind / no-inputs are fatal —
    ``PairingError`` exits the agent so the supervisor surfaces a
    clear error rather than backoff-looping against a dead code. 7
    pytest cases per agent.
  - ``spatium-install`` whiptail wizard grows a ``Bootstrap method``
    radio (Pairing code recommended / Bootstrap key advanced) with
    ``^[0-9]{8}$`` validation + retry-on-typo loop. Operator's
    chosen value flows through ``/etc/spatiumddi/role-config`` →
    firstboot's ``BOOTSTRAP_PAIRING_CODE_VAL`` per-role propagation →
    ``/etc/spatiumddi/.env`` → ``${BOOTSTRAP_PAIRING_CODE:-}`` in
    the appliance compose's dns-bind9 / dns-powerdns / dhcp-kea env.
  - Console dashboard's ``render_agent_context`` adds a ``Pairing``
    row that reads three signals (``BOOTSTRAP_PAIRING_CODE`` in
    ``.env`` for "was a code in play?"; ``agent_token.jwt`` in the
    agent's docker volume for "registered?"; ``bootstrap.key`` for
    "resolved?"; docker ps state for "failed-exit?") and renders
    ``Paired ✓`` (green) / ``Registering…`` or ``Pairing in
    progress…`` (yellow) / ``Pair failed — regenerate code on control
    plane`` (red). ``AGENT_ROWS`` bumps from 5 → 6 when the row will
    render so the panel doesn't clip.
- **Appliance SNMP support (#153).** snmpd runs at the OS level on
  every appliance host (local + every registered remote agent),
  configured from a single ``platform_settings`` row driven through
  the ConfigBundle long-poll like SNMP / NTP / chrony. v2c mode
  (Fernet-encrypted community at rest + JSONB source-CIDR allowlist
  rendered into ``rocommunity <community> <cidr>`` per-source); v3
  USM mode (per-user table — username + auth_protocol +
  auth_pass_enc + priv_protocol + priv_pass_enc rendered into
  ``createUser`` + ``rouser``). Host-side ``spatium-snmp-reload``
  bash runner validates with ``snmpd -t`` before atomic install,
  manages an nftables drop-in (``/etc/nftables.d/spatium-snmp.nft``)
  that opens UDP 161 when SNMP is enabled and tears it down when
  disabled. A/B-persistence-contract documented in the runner
  header — config + nftables drop-in survive slot swaps via the
  ``/etc`` overlay → ``/var/persist/etc``. ``Settings → Security``
  grows a Reveal flow for the SNMP community (password-confirm,
  audited, local-auth only — mirrors agent-bootstrap-keys reveal).
  SNMP page slotted into ``/appliance`` (moved from Settings per
  operator feedback so all fleet-rollout config lives together).
  10 unit tests for the renderer + bundle. Disabled by default.
- **Adaptive polling on Releases tab** so the tab stays scannable as
  the project ships dozens of releases. Top 3 releases render as the
  existing full cards; the rest collapse behind a ``▶ Show N older
  releases`` disclosure (auto-opens while an apply is in flight,
  consistent with the existing update-log disclosure). Inside the
  disclosure, each release renders as a one-line compact row with
  a per-row release-notes sub-disclosure so the older list stays
  scannable. When the operator's currently-installed version sits in
  the older bucket (they're behind on upgrades), the disclosure label
  carries a primary-coloured ``installed: <tag>`` chip so it's
  findable without expand-and-scan.
- **GUI-configurable NTP / chrony (#154).** Companion to the SNMP
  flow: same ``platform_settings`` → ConfigBundle → host-side
  ``spatium-chrony-reload`` shape. Three source modes — pool /
  servers / mixed — with hygiene-block-always-emitted (driftfile,
  makestep, rtcsync, leapsectz). Optional NTP-serve-to-clients with
  per-CIDR allow list + automatic UDP 123 nftables drop-in. 10 unit
  tests + a banner explaining "chrony is only configured on appliance
  hosts" for non-appliance docker / k8s control planes (where the
  settings still flow to registered appliance agents through the
  bundle).

### Changed

- **Appliance tabs sorted alphabetically.** The ``/appliance`` page's
  ``TABS`` array previously rendered tabs in the order new ones
  shipped (chronological), causing visual reshuffling for operators
  every release. Now alphabetised by label (Containers / Logs &
  Diagnostics / Maintenance / Network & Host / NTP / OS Versions /
  Pairing / Releases / SNMP / Web UI Certificate) with a
  prominent ``IMPORTANT: keep this array sorted alphabetically by
  ``label```` comment at the top so future tab additions slot in
  by position rather than appended.
- **README appliance-ISO section rewrite.** ``### Quick start with
  the OS appliance ISO`` had drifted into a CLAUDE.md-style dense
  single-paragraph format with parenthetical asides about UUID
  collisions and Phase-N citations on every bullet. Rewritten as
  five skimmable sub-sections (Get the ISO / Install / Access /
  Joining DNS / DHCP agents / Managing the appliance) with hard-
  wrapped ~70-80 char lines. Adds the pairing-code workflow as a
  first-class step. Deep technical detail (A/B slot internals,
  NetworkManager migration gotcha, fleet-reboot mechanism) stays in
  ``docs/deployment/APPLIANCE.md`` instead of duplicated in the
  README.
- ``make appliance-dev-iso`` named in the README as the recommended
  one-stop local-build target (vs. the previous ``make appliance &&
  make appliance-iso`` two-step).
- Pairing tab's React Query ``refetchInterval`` flipped from a flat
  5 s to a callback returning 2 000 (pending codes exist) or 15 000
  (idle). Operator watching for an agent to pair gets near-real-time
  feedback; idle tab doesn't burn cycles.

### Fixed

- **Audit-log ``row_hash_mismatch`` on every install (#73).** The
  ``before_flush`` event listener (``app.services.audit_chain.
  compute_audit_hashes``) was hashing ``id=None`` + ``timestamp=None``
  on every row because both columns' SQLAlchemy defaults fire AFTER
  ``before_flush`` — ``id`` uses Python ``default=uuid.uuid4``,
  ``timestamp`` uses ``server_default=now()``, both materialise during
  flush — so the runtime hash never matched what Postgres later
  stored, and every nightly ``verify_chain`` reported tampering.
  Fixed by pre-populating both defaults before hashing
  (``row.id = uuid.uuid4()`` + ``row.timestamp = datetime.now(UTC)``
  if either is None) so the values we hash over are the same ones
  the INSERT carries.
- **Agent container entrypoints crash-loop with only
  ``BOOTSTRAP_PAIRING_CODE``.** The shell entrypoints baked into
  ``ghcr.io/spatiumddi/dns-bind9`` / ``dns-powerdns`` / ``dhcp-kea``
  did ``: ${DNS_AGENT_KEY:?DNS_AGENT_KEY is required}`` (and
  equivalents) before the Python supervisor ran — that fired before
  the new Phase 3 resolver got a chance to look at
  ``BOOTSTRAP_PAIRING_CODE``. Containers booted with only the pairing
  code set crash-looped at the entrypoint with
  ``DNS_AGENT_KEY is required``. Caught live on a fresh appliance
  install at 192.168.0.134. Fix: relax all three entrypoints to
  require AT LEAST ONE of (kind-specific PSK, ``BOOTSTRAP_PAIRING_
  CODE``); the Python resolver still does the real
  resolve-and-cache work.
- **SNMP nftables drop-in syntax.** Initial ``spatium-snmp-reload``
  runner wrote ``add rule inet filter input udp dport 161 accept`` to
  ``/etc/nftables.d/spatium-snmp.nft`` and got
  ``syntax error, unexpected rule, expecting @ or '`` from nft.
  Root cause: ``/etc/nftables.conf``'s ``include "/etc/nftables.d/
  *.nft"`` lives INSIDE the input chain block, so the drop-in must
  be a chain-rule fragment (``udp dport 161 accept``) not a top-level
  ``add rule`` directive. Caught live on the 192.168.0.100 test
  appliance during the SNMP smoke pass.

### Migrations

- ``e2c91d5f7a48_appliance_pairing_codes`` — adds the ``pairing_code``
  table (UUID PK with ``gen_random_uuid()`` server-default, sha256
  ``code_hash`` unique index, ``code_last_two`` for UX, polymorphic
  ``server_group_id``, ``expires_at`` / ``used_at`` / ``revoked_at``
  lifecycle columns, CHECK constraint on ``deployment_kind IN ('dns',
  'dhcp')``, FKs to ``user`` table for created / revoked actors with
  ``ON DELETE SET NULL``).
- ``f8a92c1d3b65_pairing_code_kind_both`` — widens the CHECK
  constraint to also accept ``'both'`` for combined-agent codes.
- ``b95e2d71f042_appliance_snmp_settings`` — adds 7 ``snmp_*``
  columns to ``platform_settings`` (master toggle, version,
  Fernet-encrypted community, v3 users JSONB, allowed-sources JSONB,
  sysContact, sysLocation).
- ``c87a3f29d108_appliance_ntp_settings`` — adds 5 ``ntp_*`` columns
  to ``platform_settings`` (source mode, pool servers JSONB, custom
  servers JSONB, serve-to-clients toggle, allow-client-networks
  JSONB).
- ``d4f8c91a2e35_audit_chain_repair`` — one-shot data migration that
  walks every existing ``audit_log`` row in ``seq`` order and
  re-hashes with the now-correct defaults-pre-populated logic. No-op
  on fresh installs (no rows). Operator-visible columns (action,
  user, timestamp, old_value / new_value, …) preserved verbatim;
  only ``row_hash`` + ``prev_hash`` chain fields rewritten.

## 2026.05.13-1 — 2026-05-13

Fleet management gets its real shape: the per-box ``OS Image`` tab and
the per-fleet ``Fleet`` tab merge into a single unified ``OS Versions``
table where the local appliance pins at the top as a ``SELF`` row + every
registered DNS/DHCP agent sits below, all with the same Upgrade /
Manual-upgrade / per-row Reboot affordances. Phase 8f-8 lands the
operator-triggered fleet reboot button (double-confirm modal with a
required "I understand this agent will go offline for 30-60 s"
checkbox), and three real Phase 8f-4 bugs that kept the fleet-upgrade
trigger from ever writing the trigger file in production get fixed
end-to-end: the agent's bind mount on ``/var/lib/spatiumddi/release-
state`` was ``:ro`` (Phase 8f-2 only had the agent reading the .state
sidecar; the trigger-write half added in 8f-4 needed ``:rw``), the
firstboot-chowned dir was mode 0755 owned by uid 1000 which locked the
agent's unprivileged ``spatium`` user out (now 1777 sticky), and the
sync.py poll-loop only called ``maybe_fire_fleet_upgrade`` inside the
200-response code path so an agent that restarted with a
``desired_appliance_version`` already cached on disk would 304-forever
and never fire (now also evaluated at startup right after
``load_config``). Plus ``spatium-upgrade-slot apply`` now randomises
the freshly-written slot's filesystem UUID via ``tune2fs -U random`` —
the dd-from-slot-image step was leaving both A and B with identical
UUIDs after a re-apply, which broke ``find_slot_partitions()`` and
wedged ``set-next-boot`` with ``ERROR: couldn't detect A/B slots``.
Slot detection in the api + both agents rewrites to parse ``/run/udev/
data/b<maj>:<min>`` files directly instead of shelling out to lsblk
(lsblk can list block topology in a container without ``/dev/sda*``
but returns empty PARTLABEL / UUID because libblkid needs the device
inode — udev publishes the same data into ``/run/udev/data`` which the
existing bind mount covers). The Talos-style console dashboard's
F-keys also get wired up (F2 → htop as the unprivileged ``admin``
user, F3 → ``docker stats``, F4 → nmtui, F9 dropped) and **F12 Shell
is removed entirely** — a console-attached operator shouldn't get an
admin shell that easily; F1 still hands off to agetty for the standard
login path. To unblock F4/nmtui the appliance's networking stack
flips from ``systemd-networkd`` to ``NetworkManager`` (systemd-
resolved still owns ``/etc/resolv.conf`` — NM pushes per-interface
DNS into resolved over D-Bus); the installer's static-IP step writes
a keyfile-format ``.nmconnection`` instead of a ``.network`` file.
Plus a wave of polish: ``OS Versions`` per-row Reboot button, bulk-
select checkboxes + ``Apply to selected`` modal across multiple
appliance agents in one click (each agent does its own dd on its own
inactive slot so there's no fleet-wide outage), slot upgrade ``State``
column renamed ``idle`` → ``ready`` with green chip styling (operator
feedback: ``idle`` reads as "this agent is doing nothing useful"; the
chip should be positive + green so a glance tells you everything's
fine), stale-``failed`` auto-heal on the agent so the chip clears
itself once the failure has been observed instead of sticking forever,
Platform Insights's previously-buried Conformity + Operator Copilot
Usage panels promoted to top-level tabs alongside Postgres +
Containers, the ``/appliance`` sidebar entry is now always visible
(was gated on ``appliance_mode=true``; on docker/k8s control planes
the host-level tabs hide but Releases + OS Versions stay so an
operator with appliance *agents* registered against a docker/k8s
control plane can still drive fleet upgrades), and the Releases tab's
Apply button is replaced with a copy-paste ``docker compose pull`` /
``helm upgrade`` modal on non-appliance hosts (where the host-side
``spatiumddi-update.path`` systemd unit doesn't exist). Backend tests
parallelise via pytest-xdist (~10 min → ~3-4 min on CI). Plus
README's new ``Support the project`` section, the appliance-dev-iso
Makefile drops the container-image bake step so the slot rootfs fits
the 4 GiB partition Phase 8a-1 carved, a Makefile ``appliance-stamp-
dev`` target writes a ``dev-<short-sha>`` stamp into ``/etc/spatiumddi
/appliance-release`` for local-build ISOs so installed_version
populates in the Fleet view, and PR #148's funding-sources update
lands the GitHub Sponsors + BMC links in ``.github/FUNDING.yml``.

### Added

- **Phase 8f-8 — operator-triggered fleet reboot.** Per-row Reboot
  button on the OS Versions table (only on rows whose
  ``deployment_kind=appliance`` — strict gate at every layer so a
  misclick can't reboot the local docker workstation). Confirm modal
  uses the new ``ConfirmModal.requireCheckboxLabel`` prop: the
  Reboot button stays disabled until the operator ticks "I
  understand <agent name> will go offline for ~30–60 s". Backend
  endpoint ``POST /api/v1/appliance/fleet/{kind}/{server_id}/
  reboot`` stamps ``reboot_requested=true`` +
  ``reboot_requested_at=now()``; ConfigBundle long-poll carries it
  to the agent; ``maybe_fire_reboot`` writes
  ``/var/lib/spatiumddi-host/release-state/reboot-pending``; the
  new host-side ``spatiumddi-reboot-agent.{path,service}`` unit
  picks it up + invokes ``systemctl reboot`` after a 5 s grace
  window. Heartbeat handler auto-clears ``reboot_requested`` once a
  heartbeat arrives more than 15 s after the request was stamped —
  by construction post-reboot, since a pre-reboot agent can't
  heartbeat (container's down during shutdown). Migration
  ``a72f4c89e15d`` adds the two columns to both ``dns_server`` +
  ``dhcp_server``.

- **OS Versions unified table.** Merges the per-box ``OS Image`` tab
  + the per-fleet ``Fleet`` tab into one screen: a pinned ``SELF``
  row at the top (left-border accent + ``SELF`` chip) opens the
  full A/B slot detail (per-slot versions, apply log, rollback) in
  a modal via the existing ``SlotUpgradeCard`` component, with
  every registered DNS/DHCP agent below carrying the same Upgrade /
  Manual-upgrade / Reboot affordances. Bulk-select checkboxes on
  appliance rows + an "Apply to selected" modal fires
  ``scheduleUpgrade`` in parallel via ``Promise.allSettled`` so one
  agent's failure doesn't block the others. Docker / k8s rows
  excluded from bulk select (their upgrade path is the manual
  copy-paste modal, which doesn't bulk).

- **Console F-key wiring (#134 Phase 4h follow-up).** F2 →
  ``runuser -u admin -- htop`` (htop reads /proc for all-system
  process display but interactive kill/renice is scoped to admin's
  own processes — operator can't ctrl-K the api container by
  accident from the physical console). F3 → ``docker stats`` (live
  resource view, Ctrl-C returns). F4 → ``nmtui`` (unblocked by the
  systemd-networkd → NetworkManager switch below; runs as root
  because NM polkit needs it). F9 dropped from the footer entirely.
  F12 Shell removed — physical-console operators should not get
  admin shells that easily; F1 still hands off to agetty for the
  traditional login.

- **NetworkManager replaces systemd-networkd as the appliance's
  network stack.** Required to unblock F4/nmtui (nmtui only speaks
  to NetworkManager). systemd-resolved still owns
  ``/etc/resolv.conf`` — NM pushes per-interface DNS over D-Bus via
  the new ``/etc/NetworkManager/conf.d/10-spatiumddi.conf``
  (``dns=systemd-resolved`` + ``plugins=keyfile``). Installer's
  static-IP step writes a keyfile-format
  ``/etc/NetworkManager/system-connections/10-spatium-static.
  nmconnection`` (mode 0600) instead of the old ``10-spatium-
  static.network``. ``NetworkManager-wait-online.service.d/timeout.
  conf`` drop-in preserves the 10-second any-interface boot timeout
  the old networkd drop-in provided. postinst masks
  ``systemd-networkd.{service,socket}`` +
  ``systemd-networkd-wait-online.service`` so the two stacks don't
  fight.

- **OS Versions "ready" state with green chip.** Renames the
  slot-upgrade ``idle`` state to ``ready`` across the agent
  (``slot_state.py``), backend (``UpgradeState`` Literal),
  ConfigBundle field, and frontend chip styling. ``ready`` and
  ``done`` share green styling + CheckCircle2 icon (positive
  states); ``in-flight`` stays blue (Loader2); ``failed`` stays red
  (AlertCircle) until the agent auto-heals it. Fresh appliances
  with no .state file default to ``ready`` instead of None so the
  chip is green from first heartbeat.

- **Stale-failed auto-heal on the agent.** When ``slot_state.
  _last_upgrade_state_from_sidecar()`` sees ``failed`` AND the
  un-suffixed trigger file is gone (host runner already renamed it
  to ``.failed.<ts>``), report ``ready`` instead. Failure history
  stays on disk as ``.failed.<ts>`` sidecars for forensic lookup;
  the heartbeat just stops re-asserting the failure once the
  operator has had time to observe it.

- **Backend test parallelisation (#145).** ``pytest -n auto`` via
  pytest-xdist; each worker carves its own
  ``spatiumddi_test_gw<N>`` Postgres database in conftest. Verified
  locally: 947 s → 285 s, 556/556 pass.

- **Always-visible ``/appliance`` sidebar entry.** Was gated on
  ``versionInfo.appliance_mode``; now visible on every deployment.
  Inside the page, host-level tabs (TLS, Containers, Logs, Network,
  Maintenance) hide when the API host isn't an appliance, leaving
  Releases + OS Versions for operators whose control plane is
  docker/k8s but who have appliance *agents* registered against it
  (hybrid topology). Header gains a ``docker/k8s`` chip on
  non-appliance hosts.

- **Platform Insights tab promotion.** Conformity and Operator
  Copilot Usage panels (previously rendered unconditionally below
  the tab switch and easy to miss on tall screens) promoted to
  their own top-level tabs in the same tab strip as Postgres +
  Containers. Panels grow loading / empty / forbidden fallback
  messages so a clicked-but-empty tab doesn't look broken.

- **Makefile ``appliance-stamp-dev``** writes ``APPLIANCE_VERSION=
  "dev-<short-sha>"`` into ``mkosi.extra/etc/spatiumddi/appliance-
  release`` before each local-build ISO so a freshly-installed dev
  appliance reports a non-empty ``installed_appliance_version`` in
  the Fleet view. The release workflow already does this for CI
  builds with the real CalVer tag; this just covers the local-
  iteration case.

- **README ``Support the project`` section** with a Buy Me a Coffee
  link, an Individuals blurb explaining what tips fund, and an
  Organisations paragraph as a stub for future commercial
  sponsorships. Added to the Contents index near the top.

### Changed

- **Slot detection rewrites to parse ``/run/udev/data`` directly.**
  Three call sites updated identically — DNS agent slot_state.py,
  DHCP agent slot_state.py, backend
  ``services/appliance/slot.py``. lsblk inside a container can list
  block topology from /sys but PARTLABEL + UUID columns come back
  empty because libblkid probes the device inode and containers
  don't have ``/dev/sda*`` bind-mounted. udev publishes the same
  data into ``/run/udev/data/b<maj>:<min>`` with ``S:`` symlink
  lines like ``S:disk/by-partlabel/root_A`` and ``S:disk/by-uuid/
  aa1311ba-…``; parsing those gives a container-friendly lookup
  with no extra mounts beyond the existing ``/run/udev`` bind.

- **Strict ``deployment_kind`` gating on fleet operations.** Upgrade
  + Reboot buttons + bulk-select only render for rows where
  ``deployment_kind === "appliance"`` (previously the lenient
  ``"appliance" || null`` fallback would render Upgrade on rows
  that hadn't checked in yet — could lie about what's possible).
  Backend ``/fleet/{kind}/{id}/upgrade`` + ``/reboot`` endpoints
  return 422 for docker / k8s rows.

- **Releases tab Apply replaced with Manual modal on non-appliance
  hosts.** The host-side ``spatiumddi-update.path`` systemd unit
  the Apply button relies on only exists on a SpatiumDDI appliance;
  on docker/k8s control planes Apply now opens a modal with the
  matching ``SPATIUMDDI_VERSION=<tag> docker compose pull && up
  -d`` or ``helm upgrade spatiumddi --set image.tag=<tag>`` command
  for the operator to copy + run on the control-plane host.

- **``appliance-dev-iso`` Makefile target no longer bakes container
  images.** Phase 8a-1 (#138) carved the disk into ESP + root_A
  4 GiB + root_B 4 GiB + var; the ~480 MiB baked image tarballs
  pushed the slot rootfs past the 4 GiB ceiling. firstboot pulls
  every container image from ghcr.io on first boot exactly as a
  real release does. New ``appliance-clean-baked-images`` target
  wipes leftover tarballs from a prior bake. ``appliance-bake-
  images`` stays as a standalone target for the rare case someone
  needs WIP api+frontend baked in.

- **Agent runtime images (bind9 + powerdns + kea) drop the
  short-lived ``lsblk`` add** that landed in an interim iteration
  of #138 Phase 8f-2. lsblk turned out to be insufficient
  (containers without ``/dev/sda*`` access can't read PARTLABEL /
  UUID even with lsblk installed); the udev-parsing rewrite above
  replaces it entirely.

### Fixed

- **Fleet upgrade trigger never landed in production (Phase 8f-4).**
  Three independent bugs kept the agent from writing the
  ``slot-upgrade-pending`` trigger file the host-side
  ``spatiumddi-slot-upgrade.path`` unit watches: (1) the appliance
  compose mounted ``/var/lib/spatiumddi/release-state`` as ``:ro``
  on the DNS/DHCP agent services (8f-2 only had the agent reading
  the .state sidecar; 8f-4 added the write half but the mount mode
  wasn't updated), (2) firstboot chowned the dir to ``1000:1000``
  with mode ``0755`` so the agent's unprivileged ``spatium`` user
  was locked out of write access (now 1777 sticky — same trade-off
  as /tmp, no secrets stored in this dir), and (3) sync.py only
  called ``maybe_fire_fleet_upgrade`` inside the 200-response code
  path so an agent that restarted with a desired-version already
  cached on disk would 304-forever and the trigger would never be
  re-evaluated (now also fires from the bootstrap-from-cache path
  right after ``load_config``).

- **``spatium-upgrade-slot apply`` left both slots with identical
  filesystem UUIDs after a re-apply.** The slot image's ext4 UUID
  was baked at slot-image build time so two dd's from the same
  source produced the same filesystem UUID. ``find_slot_
  partitions()`` keys off UUID to identify slot_a vs slot_b →
  ``set-next-boot`` + ``status`` both wedged with "couldn't detect
  A/B slots". Now inserts ``tune2fs -U random <target_dev>`` +
  ``udevadm trigger --settle`` between dd and the blkid read so
  each slot gets a fresh random UUID.

- **Stale upgrade state validator rejection.** Backend's heartbeat
  handler preserves the previously-stored ``last_upgrade_state``
  when the agent reports ``None`` (the agent's slot_state
  validator rejected unknown tokens like ``idle`` post-rename so it
  reported None) — would have left rows stuck on ``idle`` forever
  in the DB. Migration not needed; agent + frontend rename handles
  it for new heartbeats and the auto-heal clears stale rows once
  the .state file is cleared.

### Migrations

- ``a72f4c89e15d`` — adds ``reboot_requested`` (bool, default
  ``false``) and ``reboot_requested_at`` (timestamptz, nullable) to
  both ``dns_server`` and ``dhcp_server`` for Phase 8f-8 fleet
  reboot intent.

## 2026.05.12-3 — 2026-05-12

OS Image card polish + per-slot visibility. The big behaviour fix is
that the api container can finally see partition labels through
`lsblk` (a missing `/run/udev` bind mount left the Active column
reading `—` and the Apply confirm modal titled "Apply 2026.05.12-2
to —?" — fixed). On top of that: the card now shows the installed
`APPLIANCE_VERSION` under each slot (sourced from a new
`slot-versions.json` sidecar the upgrade CLI maintains), the GRUB
boot menu labels carry the version too (`SpatiumDDI Appliance
2026.05.12-3 (slot A)`), the three "Active / Durable / Target"
columns get replaced with two stacked slot cards carrying
colour-coded role badges (BOOTED / DEFAULT / TARGET / TRIAL), and
the installer's disk-too-small check fires immediately after the
operator picks a disk instead of burying the failure at the end of
the wizard after they've typed hostname + admin + network +
timezone.

### Added

- **Per-slot installed-version display.** The OS Image card now
  reads `/var/lib/spatiumddi/release-state/slot-versions.json` and
  surfaces `slot_a_version` + `slot_b_version` on the
  `/api/v1/appliance/slot-upgrade` response. Each slot card on the
  redesigned layout (see Changed below) shows the installed
  `APPLIANCE_VERSION` directly under the slot label, falling back
  to `—` when the sidecar is missing / the slot is unstamped /
  unreadable.
- **`slot-versions.json` sidecar with autonomous refresh.** New
  `spatium-upgrade-slot sync-versions` subcommand probes both
  slots (active via `/etc/spatiumddi/appliance-release`, inactive
  via a quick read-only mount + read), writes the JSON atomically
  to `/var/lib/spatiumddi/release-state/slot-versions.json`.
  Called by `spatiumddi-firstboot` on every boot so the sidecar
  stays fresh across power cycles, and by `spatium-upgrade-slot
  apply` at the end of its apply path so the OS Image card
  reflects an upgrade within one polling tick.
- **GRUB menu labels carry the version.** `spatium-install` reads
  `APPLIANCE_VERSION` from the freshly-installed
  `/etc/spatiumddi/appliance-release` and writes grub.cfg
  menuentries as `SpatiumDDI Appliance <ver> (slot A)` instead of
  the bare `(slot A)`. Operator skimming the grub menu sees which
  release lives on each slot — useful for triage when the system
  is half-upgraded or standing at the menu after a failed health
  check. `spatium-upgrade-slot apply` patches just the *inactive*
  slot's menuentry label after each dd via a new
  `_patch_grub_cfg_slot_label` helper that's idempotent across
  both the original "(slot A)" form and the already-stamped
  "<ver> (slot A)" form.
- **Release workflow stamps `appliance-release`.** Every prior
  appliance reported `APPLIANCE_VERSION=0.1.0` because the
  build-time stamp file was never written — the CI workflow now
  writes `/etc/spatiumddi/appliance-release` into the rootfs via
  `mkosi.extra/etc/spatiumddi/appliance-release` before mkosi
  builds, sourcing the CalVer from `GITHUB_REF_NAME`. The file is
  gitignored so dev builds can use placeholder stamps without
  surfacing as pending commits.

### Changed

- **OS Image card redesigned: two stacked slot cards, not three
  columns.** Replaces the "Active / Durable / Target" grid with
  per-slot cards (Slot A on the left, Slot B on the right) each
  carrying the installed version + colour-coded role badges:
  🟢 BOOTED (emerald), 🔵 DEFAULT (blue), 🟠 TARGET (amber),
  🟡 TRIAL (yellow). Card border colour follows the most-relevant
  role so the pair has visual rhythm at a glance. The three-column
  layout duplicated info (Active and Durable are the same slot
  outside of a trial boot) and made operators translate role
  headings back into slot identities — the new shape leads with
  the slot identity. Subtext line on each card explains the
  current role in plain English. Side-by-side on ≥sm viewports,
  stacked on small screens.
- **Disk-too-small check fires after disk pick.** The 16 GiB
  hard-floor check used to live inside `do_install`, which runs
  AFTER role → disk → hostname → admin → network → timezone →
  agent-config → final confirm. An operator with a small disk
  typed all of that out before getting told their disk was too
  small. Moved into `pick_disk` immediately after the menu
  selection; a too-small pick now opens the explainer modal and
  bounces back to the disk-picker menu (operator might have a
  second, larger disk attached they can switch to).
- **CI release pipeline parallelisation.** `build-appliance-iso`
  used to wait on all five container image builds before starting
  — but the ISO doesn't bake them (firstboot pulls from ghcr.io
  at first boot). Dropped the unnecessary `needs:` deps so the
  ISO build runs in parallel with the image builds. Saves
  ~5 min on a typical release cut.

### Fixed

- **`/run/udev` bind mount on api + agent containers so lsblk
  sees PARTLABEL.** lsblk inside a container can read `/sys` +
  `/proc` for partition topology but PARTLABEL + UUID live in
  udev's runtime state under `/run/udev/data/`. Without the bind
  mount, the api container's `_current_slot_from_cmdline` helper
  saw `partlabel: null` for every partition and returned None —
  the OS Image card then rendered "Active (booted) —" and the
  Apply confirm modal titled "Apply 2026.05.12-2 to —?". Same
  root cause hit the DNS + DHCP agents' Phase 8f-2 slot-state
  collector. Fix is one extra read-only mount each on api,
  dns-bind9, dns-powerdns, dhcp-kea services in the appliance
  docker-compose.

### Migrations

- None. All work is appliance-only (installer / host-side
  scripts / appliance docker-compose / OS Image card UI) plus
  read-only API additions on `/api/v1/appliance/slot-upgrade`.
  The control-plane database is untouched.

## 2026.05.12-2 — 2026-05-12

Closes Phase 8 (issue #138, OS appliance atomic A/B upgrade). The
previous release shipped the per-box machinery (Phase 8a-8c); this
one adds the **fleet orchestration** side — control plane drives
slot upgrades for every registered DNS + DHCP agent from a single
Fleet tab. Operator picks a release tag, clicks Upgrade against
N rows, and the agents fire the local slot-upgrade trigger via
the ConfigBundle long-poll within ~60 s. Docker / k8s agents get
copy-paste commands instead, since they have no A/B partition to
dd into. Plus a wave of per-box UX polish around the OS Image
card: rollback button, GitHub release picker dropdown, apply +
reboot confirmation modals, and the card promoted from buried-at-
bottom-of-Releases to its own top-level Appliance tab.

### Added

- **Phase 8f-1 — agent slot state on the server tables.** Nine
  new columns on both `dns_server` and `dhcp_server`:
  `desired_appliance_version`, `desired_slot_image_url`,
  `deployment_kind` (`appliance` / `docker` / `k8s` / `unknown`),
  `installed_appliance_version`, `current_slot`, `durable_default`,
  `is_trial_boot`, `last_upgrade_state`, `last_upgrade_state_at`.
  Migration `f8b1c20d3e72`. All nullable so pre-8f rows and
  docker / k8s deploys keep working; agent fills them in on its
  next heartbeat.
- **Phase 8f-2 — agent heartbeat carries slot state.** New
  `slot_state.py` module on DNS + DHCP agents introspects the
  appliance host via bind-mounted paths (`/etc/spatiumddi-host/
  role-config`, `/etc/spatiumddi-host/appliance-release`,
  `/boot/efi-host/grub/grubenv`, `/var/lib/spatiumddi-host/
  release-state/slot-upgrade-pending.state`). Heartbeat clients
  on both agents merge the collected state into the outbound
  payload. Server-side heartbeat handlers persist whatever the
  agent reported, leaving columns the agent didn't send
  untouched. Appliance docker-compose now mounts the three
  required host paths on dns-bind9, dns-powerdns, dhcp-kea.
- **Phase 8f-3 — ConfigBundle plumb-down.** Both DNS and DHCP
  long-poll responses carry a `fleet_upgrade` block with the
  desired version + URL. DNS bundle includes them in the etag
  directly; DHCP wraps the driver-dataclass etag with a fleet
  marker so a Fleet view change still wakes the agent's long-
  poll even when the driver bundle is unchanged.
- **Phase 8f-4 — agent-side trigger fire.** New
  `maybe_fire_fleet_upgrade()` on each agent's `slot_state`
  module. Gated on four conditions (deployment is appliance,
  desired version + URL both set, desired ≠ installed, trigger
  file not already present). Writes the same
  `/var/lib/spatiumddi-host/release-state/slot-upgrade-pending`
  trigger file the per-appliance OS Image card uses, so the
  host-side `spatiumddi-slot-upgrade.path` unit drives dd +
  grub-reboot identically.
- **Phase 8f-5 — Fleet view UI.** New `/appliance/fleet` tab
  between OS Image and Containers. Single table covering both
  DNS + DHCP agents: kind chip, name + host + last-seen-ip,
  deployment chip, installed version, slot (with "(trial)"
  suffix when current ≠ durable), upgrade-state pill (idle /
  in-flight blue / done green / failed red), relative last-
  seen, pending-upgrade chip with inline clear button, Upgrade
  action. New `/api/v1/appliance/fleet` API surface: `GET /`
  for the list, `POST /{kind}/{server_id}/upgrade` to stamp
  desired version, `POST /{kind}/{server_id}/clear` to drop
  pending intent. Upgrade modal reuses
  `applianceReleasesApi.list` so the operator picks from the
  same GitHub release dropdown as the per-box flow. Audit log
  entry on every fleet write. Auto-refresh every 15 s so the
  pending → done transition lands within one agent long-poll
  cycle (~30–60 s).
- **Phase 8f-6 — manual-upgrade modal for docker / k8s rows.**
  Docker and k8s agents can't take slot upgrades (no A/B
  partition). Clicking Manual upgrade… opens a wide modal with
  the same release picker the appliance flow uses, plus a
  pre-filled copy-paste command tailored to deployment_kind:
  `SPATIUMDDI_VERSION=<tag> docker compose pull && up -d` or
  `helm upgrade spatiumddi-<dns|dhcp> oci://ghcr.io/spatiumddi/
  charts/spatiumddi --set image.tag=<tag> --reuse-values`. One-
  click Copy button. The agent reports the new
  `installed_appliance_version` via its next heartbeat after the
  operator runs the command, so the Fleet row's Installed
  column updates without further input.
- **Phase 8f-7 — auto-clear desired stamp on successful
  upgrade.** Heartbeat handler on both agents clears
  `desired_appliance_version` + `desired_slot_image_url` once
  the agent reports `installed_appliance_version` equal to the
  desired one and `last_upgrade_state` is `done` (or NULL).
  The Fleet view's pending-upgrade chip drops on the next
  refresh.
- **Phase 8c-3 — rollback button in the OS Image card.**
  Operator can now flip the durable default back to the
  previous slot from the UI without SSH'ing in to run
  `spatium-upgrade-slot commit slot_a`. New host-side
  `spatiumddi-slot-rollback.path` + `.service` units watch a
  separate `slot-rollback-pending` trigger file; runner calls
  `spatium-upgrade-slot commit [<slot>]` (grub-set-default).
  Backend endpoint `POST /api/v1/appliance/slot-upgrade/rollback`
  audit-logged via the same pattern as apply. UI button hidden
  during trial boot (where "rollback to inactive" would commit
  the trial slot — opposite of operator intent).
- **GitHub release picker on the OS Image card.** Replaces the
  two URL text inputs with a single dropdown sourced from
  `applianceReleasesApi.list`. Operator picks a CalVer tag
  (e.g. `2026.05.12-1`); both `.raw.xz` and `.sha256` URLs
  derive from the release workflow's stable URL convention.
  "Use custom URL or local path →" toggle keeps the old text-
  input flow available for air-gapped sneakernet, mirrors,
  and local dev builds. Default-selects the newest non-pre-
  release tag.
- **Apply confirmation modal + Reboot-now buttons.** Apply
  now opens a confirm modal naming the release + both URLs +
  target slot before any dd. "Reboot now" button on the green
  "Apply complete" banner triggers the same host-side reboot
  Maintenance tab uses (10 s grace), so the operator doesn't
  navigate tabs between apply and reboot. The amber slot-
  mismatch banner gained the same button + had its copy
  rewritten to accurately describe both apply-trial and
  rollback-pending cases (the old wording assumed apply-trial
  only and was misleading after a rollback).

### Changed

- **OS Image card promoted to its own Appliance tab.** It was
  buried at the bottom of the Releases tab, below a 25-row
  GitHub releases list — operators were missing it entirely.
  Now lives between Releases and Containers with a HardDrive
  icon and a tab summary that's explicit about the distinction
  (Releases = container-stack pull-and-recycle; OS Image =
  atomic A/B host OS upgrade).
- **Slot-mismatch banner copy rewritten.** The old text said
  "Once /health/live confirms, the swap commits automatically.
  A reboot before commit reverts" — true only for apply-trial.
  After a manual rollback (durable was already set explicitly
  via grub-set-default; reboot lands on it and stays there)
  the old copy was actively misleading. New copy is factual
  about the current ≠ durable state plus a clarification that
  the swap dynamics depend on whether the operator got there
  via apply or rollback.
- **README Getting Started Contents reorganised.** Single
  "Getting Started" bullet expanded to three entry-point bullets
  (Codespaces demo, Docker Compose, OS appliance ISO) so first-
  time visitors can jump straight to their path.

### Fixed

- **grub.cfg heredoc backticks escaped.** Phase 8c's
  install-time grub.cfg heredoc is intentionally unquoted so
  `$ROOT_A_UUID` / `$ROOT_B_UUID` expand into the rendered
  file. The comments inside used raw backticks to reference
  `grub-reboot <slot>` / `grub-set-default <slot>` — bash
  parsed those as command substitution, tried to execute
  `grub-reboot <slot>` (where `<slot>` looks like a redirect
  to a file with no name), and emitted "syntax error near
  unexpected token `newline`" on the installer screen. The
  install actually completed (failed substitutions just write
  empty strings into the comment lines, and the real
  menuentries were intact) but the red errors on screen were
  unnerving. Backticks now backslash-escaped in the heredoc
  body so bash treats them as literal — same way
  `\${next_entry}` works on the lines below.

### Migrations

- `f8b1c20d3e72` (Phase 8f-1) — `dns_server` and `dhcp_server`
  gain nine agent-slot-state columns. All nullable except
  `is_trial_boot` (server-default `false`). Reversible.

## 2026.05.12-1 — 2026-05-12

Two appliance pillars land back-to-back. **Phase 4h** rewrites the
appliance's `tty1` from a plain Debian getty into a Talos-style
refresh-on-tick console dashboard: rich + psutil, per-role identity
header, real-time vitals, compose-service health rollup, journalctl
live-log pane, and an F-key strip across the bottom — F1 Login,
F12 Shell, F5 Reboot / F6 Shutdown gated behind an arrow-key
confirm modal because destructive ops shouldn't fire on a careless
Enter. **Phase 8 atomic A/B image upgrades** (\#138) ships the
complete dual-slot upgrade story end-to-end: the installer now
carves five partitions (BIOS boot + ESP + root_A + root_B + var)
with `/etc` rendered through an overlayfs so operator state on
`/var/persist/etc` survives a slot swap, the release pipeline
attaches a 4 GiB slot `.raw.xz` to every cut release at a stable
`/latest/` URL, the `/appliance` UI grows an OS Image card with
**Apply to inactive slot** + live `slot-upgrade.log` tail, and
boot counting + health-gated commit via grub `next_entry` makes
the swap auto-revert if `/health/live` doesn't come up on the new
slot. Operators can now upgrade the appliance with a single click
that's safe to undo.

### Added

- **Phase 4h Talos-style console dashboard.** Replaces
  `getty@tty1` with a new `spatium-console` Python script (rich +
  psutil) that paints a refresh-on-tick dashboard. Same renderer
  on the serial console via a `spatium-console@.service` template
  unit. Header is per-role identity (control AIO / control-only /
  dns-agent-bind9 / dns-agent-powerdns / dhcp-agent) plus vitals
  (load, mem, swap, uptime) plus root + var disk usage, split by
  a horizontal rule with a real-time spinner + wall clock in the
  top-right (spinner ticks at 2 Hz via the 0.25 s render loop;
  vitals cached at 1 s, `docker ps` + env at 2 s). Services pane
  shows compose service health verdicts (missing / healthy /
  running / completed / unhealthy / exited) with the `migrate`
  one-shot renderable as ✓ when it exits 0. Live-log pane tails
  `journalctl`. Footer is a borderless F-key strip — F1 Login /
  F2 Monitor / F3 Containers / F4 Network / F5 Reboot /
  F6 Shutdown / F9 Diag / F12 Shell — bold black on bright blue
  chips for high-contrast readability on the Linux console.
- **Arrow-key confirm modal** for destructive console ops.
  F5 (reboot) and F6 (shutdown) open an arrow-key navigable
  modal (← → / Tab / Y / N shortcuts, Enter confirms, Esc
  cancels; default selection is "No" because reboot/shutdown
  shouldn't fire on a careless Enter). The KeyReader thread
  joins on stop() so the modal doesn't race with the dashboard's
  input loop. F1 hands off to agetty via execvp; F12 shells out
  to `su -l admin`.
- **Active partition slot displayed in the console dashboard.**
  The vitals header on `spatium-console` now reads the active
  slot from `/proc/cmdline` + grubenv (saved_entry +
  next_entry) so operators glancing at the appliance TTY can
  immediately see which slot they're on, the durable default,
  and a "trial boot" amber chip if the durable default doesn't
  match the running slot yet. Closes the visibility gap left
  by Phase 8c — without this, you had to ssh in and run
  `spatium-upgrade-slot status` to know which side was active.
- **Phase 8a — A/B + var partition layout.** Installer carves
  five partitions:
  - p1 BIOS Boot 1 MiB ef02
  - p2 ESP 512 MiB ef00 (`/boot/efi`, FAT32 with relaxed
    `fmask=0133,dmask=0022` so the api container can read
    grubenv through the bind mount)
  - p3 root_A 4 GiB 8304 (active slot at install time)
  - p4 root_B 4 GiB 8304 (inactive slot — staged by
    `spatium-upgrade-slot apply`)
  - p5 var balance 8300 (persistent across slot swaps; carries
    `/var/lib/docker`, `/var/persist/etc`, `/var/home`,
    `/var/root`, operator state)
  Hard floor: 16 GiB target disk. Smaller disks fail with a
  whiptail "Disk too small" modal *before* any destructive
  operation.
- **Phase 8a — `/etc` overlayfs.** Each slot ships an image-
  baseline `/etc` snapshot at `/usr/lib/etc.image/` (taken
  after fstab + hostname write so the snapshot is bootable).
  At every boot, a new `etc.mount` systemd unit mounts an
  overlayfs over `/etc` with lower=`/usr/lib/etc.image`,
  upper=`/var/persist/etc`, work=`/var/persist/etc-work`. All
  operator edits — useradd, chpasswd, fstab, hostname, network
  config, ssh host keys — land in the `/var/persist/etc` upper
  and therefore survive a slot swap unchanged. New
  `spatium-etc-reconcile` runs at every boot to merge system
  uid/gid/shadow entries from lower → upper so a new slot
  baseline can introduce new system users without clobbering
  the operator's.
- **Phase 8b — slot-image build + `spatium-upgrade-slot` CLI.**
  `make appliance-slot-image` extracts the root partition from
  the freshly-built appliance raw, repacks it as a 4 GiB ext4
  `spatiumddi-appliance-slot-amd64.raw.xz` with the kernel +
  initrd baked in + the image-baseline fstab + a snapshotted
  `/usr/lib/etc.image/`. Operator CLI `spatium-upgrade-slot`
  has four subcommands: `status` (per-slot dev + UUID +
  version), `apply <url-or-path> [--checksum]` (streams +
  decompresses to the *inactive* partition via dd, optionally
  verifies SHA-256, re-stamps the slot UUID into the boot
  config — the active slot is never touched), `set-next-boot`
  (one-shot grub-reboot — auto-reverts on next boot if the new
  slot fails to come up), `commit` (durable grub-set-default;
  intended for emergencies — the firstboot service does this
  automatically when `/health/live` passes).
- **Phase 8b-3 — A/B slot upgrade in the /appliance UI.**
  Adds an "Appliance OS Image (atomic A/B upgrade)" card to
  the Releases tab. Shows active slot / durable default /
  inactive target as a three-column grid + a trial-boot amber
  warning when the running slot doesn't match the durable
  default. Operator pastes (or accepts the pre-filled
  `https://github.com/spatiumddi/spatiumddi/releases/latest/`
  URL for) a slot image + optional sha256 sidecar; pressing
  Apply writes a trigger file the host-side
  `spatiumddi-slot-upgrade.path` unit watches, the runner
  invokes `spatium-upgrade-slot apply` + `set-next-boot`, and
  the UI tails `/var/log/spatiumddi/slot-upgrade.log` until
  the host-side state file flips to `done` or `failed`. Active
  slot stays untouched until reboot.
- **Phase 8b-4 — slot `.raw.xz` attached to every GitHub
  release.** The release workflow now adds two artefacts per
  cut: `spatiumddi-appliance-slot-amd64.raw.xz` + its
  `.sha256` sidecar. SlotUpgradeCard pre-fills the
  `releases/latest/download/` URL so a fresh-install
  operator's "Apply" click against an unmodified field just
  works. Older releases stay reachable by replacing `latest`
  with the version tag.
- **Phase 8c — A/B boot counting + health-gated commit.**
  Adds grub `next_entry` one-shot wiring + a
  `spatiumddi-firstboot` commit step that runs
  `grub-set-default <current_slot>` only after
  `/health/live` returns 200. If the new slot kernel-panics,
  initramfs fails, the API stack never comes healthy, or any
  earlier step in firstboot exits non-zero, the next reboot
  reverts to the prior `saved_entry` automatically. Net:
  a bad image can't soft-brick the appliance — the worst case
  is one wasted reboot back to the previous slot.

### Changed

- **Right-size A/B slots — 4 GiB each, 16 GiB disk floor.**
  Earlier Phase 8a draft sized slot_A + slot_B at 8 GiB each
  (24 GiB hard floor) for headroom. Real-world container image
  sizes plus the image-baseline `/etc` snapshot land well
  under 4 GiB, and the goal of the appliance is to be easy to
  drop into a homelab VM with a 16 GiB disk. Net: install
  fits comfortably under a 16 GiB target now, with a clear
  message on smaller disks rather than a partition-table
  surprise.
- **Image-baseline fstab strips operator UUIDs.** Phase 8a-7.
  Earlier draft baked the install-time partition UUIDs into
  `/etc/fstab` on each slot, which broke when a slot image
  cut on machine A was applied on machine B (different UUIDs).
  Switched to `LABEL=var` and `LABEL=ESP` so any host that
  carries the same partition labels boots either slot
  unchanged. The labels are pinned by `mkfs.ext4 -L var` and
  `mkfs.fat -n ESP` at install time.
- **ESP mount masks** — `fmask=0133,dmask=0022` on `/boot/efi`
  so the api container (uid 1000) can read grubenv via the
  read-only bind mount that drives SlotUpgradeCard. No
  secrets live on the ESP (just grub.cfg + grubenv + the
  boot loader binaries) so world-readable is fine.
- **Settings → Security section sorts alphabetically.**
  Account Lockout, Agent bootstrap keys, Audit Event
  Forwarding, Password Policy, Session & Security — the
  order was previously "by-when-it-shipped", which buried
  agent bootstrap at the bottom even though most operators
  on a fresh appliance install need it first.

### Fixed

- **firstboot reloads baked images on slot upgrade (\#138).**
  Phase 4's `bake-images.sh` drops `BAKED_AT` + image tarballs
  into each slot's rootfs so `spatiumddi-firstboot` loads
  operator WIP code without a ghcr.io pull. The load was
  gated on the absence of `/var/lib/spatiumddi/firstboot.done`
  — and that stamp lives on the *shared* `/var` partition, so
  after a slot upgrade the new slot's load was silently
  skipped and docker-compose re-used the previously-cached
  images from `/var/lib/docker`. Now tracked through a
  separate `/var/lib/spatiumddi/images-loaded-at` sidecar; if
  the rootfs's `BAKED_AT` differs from the persisted
  loaded-at, the new tarballs are loaded even on a non-first
  boot.
- **Install wizard writes fstab + hostname BEFORE the overlay
  snapshot** so the baseline `/etc` captured into
  `/usr/lib/etc.image/` is actually bootable. Earlier draft
  snapshotted *after* the overlay mounted, so the snapshot's
  fstab pointed at `/dev/sdaN` UUIDs the rsync had not yet
  set, and slot_B booted with the overlay's live-config
  `overlay / overlay rw 0 0` line — `/var` never mounted,
  `etc.mount` couldn't fire.
- **`/home` + `/root` are now `/var/home` + `/var/root` bind
  mounts.** Slot-swap-safe: operator-created users + the
  root account's history / ssh keys / .gnupg all survive a
  slot upgrade because they live on the persistent `/var`,
  not on the slot rootfs. New `home.mount` + `root.mount`
  systemd units, image-shipped (not fstab).
- **Kernel cmdline `rw` instead of `ro`** since the
  image-baseline fstab has no `/` entry. Without this,
  systemd couldn't remount root rw on a non-overlay-managed
  rootfs.
- **slot-image's `/etc/fstab` rewritten by build-slot-image**
  so a freshly-cut slot ships with the same image-baseline
  fstab the installer writes (mkosi otherwise leaves the
  live-config overlay line, which won't boot once dd'd onto
  a partition).
- **slot-image's `/boot/` kernel + initrd reinstall** inside
  the chroot during `build-slot-image.sh`. mkosi strips
  `/boot/` from its output to keep the image lean; without
  this, dd'ing the resulting raw to a slot partition gave
  grub a working menuentry but a missing vmlinuz on boot.
- **grub.cfg slot UUID re-stamp after dd.** When operator A's
  slot raw.xz is applied to operator B's machine, the slot's
  ext4 filesystem keeps its baked-in UUID and the grub
  menuentry needs to be rewritten to match. `spatium-upgrade-
  slot apply` now reads the live UUID of the freshly-written
  slot via `blkid` and runs an in-place regex replace of the
  slot's UUID line in `/boot/efi/grub/grub.cfg`. Idempotent
  — re-runs with an unchanged UUID are a no-op.
- **`make appliance-slot-image` runs inside the builder
  container.** Earlier draft tried to run the slot-extract +
  ext4-mkfs + xz-compress directly on the host, which broke
  on hosts that didn't have e2fsprogs / xz / qemu-img. Now
  the same builder container that produces the raw also
  produces the slot, so the host only needs Docker.
### Migrations

- None. Phase 8 is appliance-only — touches the installer +
  the host-side `spatium-*` CLIs + grub config + a new
  `spatiumddi-slot-upgrade.path` unit. The control-plane
  database is untouched.

## 2026.05.11-1 — 2026-05-11

Largest release of the project so far — five big feature pillars
land in one cut. **PowerDNS** as a complete second authoritative
DNS driver (\#127, Phases 1 through 5+): ALIAS + LUA records,
online DNSSEC sign/unsign with frontend DS export, RFC 9432 catalog
zones (producer), Helm chart wiring + CI kind smoke test,
Operator-Copilot ``propose_create_dns_zone`` with driver hinting,
backup DNSSEC restore advisory, frontend driver picker, query logs,
group-record colour parity. **SpatiumDDI OS Appliance** (\#134,
Phases 1 / 2 / 3 / 4 / 6): Debian 13 qcow2 + hybrid USB-CD-UEFI ISO
with Proxmox-style installer wizard, dedicated ``/appliance``
management hub (TLS cert upload + CSR-on-server, GitHub release
apply + recycle, container start/stop/restart + live SSE logs,
host log viewer + self-test + diagnostic bundle, maintenance mode
+ host reboot, web first-boot wizard), role-split single ISO with
five roles (control all-in-one / control-only / DNS-BIND9 /
DNS-PowerDNS / DHCP). **Multicast IPAM** (\#126) — full feature:
PIM domain registry, group + domain CRUD, IPAM ``Subnet.kind``
discriminator forking unicast / multicast, bulk allocate + IP
picker, IGMP-state reaper, SNMP IGMP-snooping populator, Copilot
``propose_allocate_multicast_group`` tool. **DNS Import** (\#128)
— BIND9 zonefile and Windows DNS importers behind a new
``dns.import`` feature module, three-tab admin page,
``import_source`` + ``imported_at`` provenance on every imported
zone/record. **GitHub Codespaces public demo** + ``DEMO_MODE``
server-side lockdown (one-click full stack with realistic seeded
data; mutation surfaces locked so visitors can't weaponise the
demo as a scanner / SSRF relay).

### Added

- **PowerDNS driver, Phase 1 (\#127).** Full second authoritative
  driver running side-by-side with BIND9. Backend stack is
  PowerDNS-Authoritative with LMDB embedded zone storage — no
  external database, no shared Postgres credentials, agent-isolated
  the same way the BIND agent is. Phase 1 surface covers zone +
  record CRUD via the local PowerDNS REST API
  (``http://127.0.0.1:8081/api/v1/servers/localhost``), per-bundle
  zone reconciliation on every config sync, and graceful skipping of
  the BIND-specific telemetry threads (``rndc status``, statistics-
  channels XML, query-log shipper) that don't apply on a PowerDNS
  daemon. Per-driver picker UI, ALIAS / LUA records, online DNSSEC,
  catalog zones, and views are deliberately out of Phase 1 — those
  are Phase 2/3 work and the driver's ``capabilities()`` dict makes
  the gaps explicit.
- **``ghcr.io/spatiumddi/dns-powerdns`` container image.** Alpine
  3.22 base + ``pdns`` 4.9.x + ``pdns-backend-lmdb``, multi-arch
  ``linux/amd64`` and ``linux/arm64``. Same agent supervisor + JWT
  bootstrap + long-poll ETag flow as the BIND9 image — the only
  driver-specific bits are the entrypoint (generates the API key
  on first boot, seeds an empty LMDB file, drops to the
  unprivileged ``spatium`` user) and the supervisor's driver
  dispatch. Health check uses ``dig id.server CH TXT`` (PowerDNS's
  CHAOS analogue of BIND's ``version.bind``) so the probe is
  independent of zone state.
- **Multi-arch build matrix.** ``.github/workflows/build-dns-images.yml``
  matrix grew from ``[bind9]`` to ``[bind9, powerdns]``; both
  flavors build, push, and Trivy-scan in parallel. The release-tag
  pipeline adds a parallel ``build-dns-powerdns`` job alongside
  the existing ``build-dns`` (BIND9) so every tag publishes both
  images with ``:<version>`` and ``:latest`` tags.
- **PowerDNS — Phase 5+ second wave: query logs, recreate-from-zero,
  pool-tab deep-link, group-record colours (\#127).** Operator
  testing of the live install surfaced four operator-visible gaps;
  all closed in this drop.

  1. **Server-group records view: record-type badges weren't
     colour-coded.** Per-zone view used a local ``typeBadge`` map;
     the group-level Records tab fell back to a plain grey muted
     badge for every type. Hoisted ``RECORD_TYPE_BADGE`` into a
     dedicated ``frontend/src/pages/dns/recordTypeBadge.ts`` module
     (separate file so the component-only fast-refresh rule stays
     happy) and rewired both consumers. Colours match across the
     two surfaces: A=blue, AAAA=violet, CNAME=amber, ALIAS=fuchsia,
     LUA=rose, MX=emerald, NS=orange, PTR=cyan, SRV=teal, SOA=stone.
  2. **DNS Pools click → zone-pools sub-tab.** ``DNSPoolsPage``
     navigation appended ``&subtab=pools`` to the deep-link target
     (``/dns?group=…&zone=…&subtab=pools``); ``ZoneDetailView``
     reads it on mount via ``useSearchParams`` and pre-selects the
     Pools sub-tab instead of defaulting to Records. The setter
     keeps the URL in sync on tab toggle so refresh / back-nav
     lands on the same surface.
  3. **PowerDNS container destroy + recreate now restores config
     cleanly.** Two real bugs blocked the destroy-then-recreate
     workflow operators expect to be safe:
     - The DNS register endpoint set ``pending_approval=True`` on
       *every* fingerprint mismatch, ignoring the
       ``DNS_REQUIRE_AGENT_APPROVAL`` env gate. A wiped agent
       volume legitimately produces a new fingerprint, but the
       agent already authenticates with the bootstrap PSK, so the
       lockout was hostile to the legitimate redeploy case. Now
       gated on the env flag (default false — same shape DHCP
       has).
     - Cold-boot ordering raced: the agent's ``start_daemon``
       deferred (no pdns.conf yet), the sync loop rendered
       pdns.conf and immediately tried to PATCH zones via REST,
       got Connection refused, and silently swallowed the error
       while still advancing ``_current_structural_etag``. Next
       sync saw matching etag and skipped — leaving zero zones in
       the freshly-wiped LMDB. Fixed by kicking ``start_daemon``
       inside ``swap_and_reload`` after the conf lands, polling
       the local REST API until it answers (10s budget), and
       letting ``_reconcile_zones`` failure propagate so the
       structural_etag doesn't advance on a botched apply.
  4. **PowerDNS query logs surface in the Logs UI.** End-to-end
     query log shipping for the second authoritative driver,
     mirroring the BIND9 flow under the same
     ``DNSServerOptions.query_log_enabled`` gate:
     - Agent renders ``log-dns-queries=yes``,
       ``log-dns-details=yes``, and bumps ``loglevel`` to 6 (Info,
       the threshold pdns 4.9 needs to actually emit query lines)
       when the operator toggles query logging on.
     - ``start_daemon`` redirects ``pdns_server`` stderr into
       ``/var/lib/spatium-dns-agent/pdns.log`` (writable as the
       unprivileged ``spatium`` user; ``/var/log/pdns`` would have
       needed a root-owned mkdir).
     - ``QueryLogShipper`` now spawns for the ``powerdns`` driver
       (previously gated to ``bind9`` only), tails the captured
       stderr file, and POSTs batches to
       ``/api/v1/dns/agents/query-log-entries`` like its BIND9
       sibling.
     - New ``app.services.logs.pdns_parser`` parses pdns's
       ``May 08 02:11:22 Remote 192.0.2.5 wants
       'qname|qtype'`` shape into the shared
       :class:`ParsedQueryLine` dataclass. The optional ``:port``
       suffix (omitted by pdns 4.9 in the basic log format) and
       the bracketed-IPv6 form are both accepted.
     - The query-log ingest endpoint dispatches by
       ``server.driver`` so PowerDNS lines land via the new parser
       and BIND9 lines stay on the existing one — same downstream
       storage row shape.
     - ``DNS_AGENT_DRIVERS`` in ``logs/router.py`` widens from
       ``{"bind9"}`` to ``{"bind9", "powerdns"}`` so PowerDNS
       servers appear in the Logs source picker. ``options_block``
       in the agent config bundle now ships
       ``query_log_enabled`` (it was being computed for the BIND9
       template but never propagated through the bundle, so toggling
       the UI knob never reached the agent).
     - Bonus cleanup: removed the dead per-zone
       ``ENABLE-LUA-RECORDS`` metadata PUT from the bulk-reconcile
       path — it was rejected as "Unsupported metadata kind" by
       pdns 4.9's API filter, generating one warning per
       LUA-bearing zone on every structural reload. Global
       ``enable-lua-records=yes`` in pdns.conf already covers the
       feature.

  Tested end-to-end: destroy-and-recreate against the same PSK
  re-pushes 9 rrsets across pdnstest.local + serves dig answers
  for A/AAAA/CNAME/MX/TXT/ALIAS/LUA/pool. With query logging
  enabled, ``dig www.example.com`` flows through to the
  ``/logs/dns-queries`` endpoint within one batch interval (~5
  seconds default).

- **PowerDNS — Phase 5+ live-test bug fixes (\#127).** End-to-end
  testing of the PowerDNS driver against a fresh local install
  surfaced eight real bugs that the unit tests didn't cover. All
  fixed in this drop:
  1. **``pdns_server`` CLI syntax** — the agent's ``validate()`` and
     ``start_daemon()`` paths invoked ``pdns_server --config-dir
     /path`` (space-separated). PowerDNS rejects this with "perhaps
     a '--setting=123' statement missed the '='?" and treats the
     path as a positional. Switched both to ``--config-dir=/path``
     plus ``--config=check`` for the validate path. The bogus
     ``--no-config`` flag (also non-existent) is gone.
  2. **LMDB pre-seeding on first boot** — the entrypoint called
     ``pdnsutil create-bind-db`` (a BIND-backend SQL helper, not an
     LMDB initialiser); on failure the ``||`` fallthrough
     ``touch``-ed a 0-byte file that pdns then refused to mmap
     (``mdb_env_open failed``). Removed the pre-seed entirely — pdns
     creates the LMDB env on first start. Added an empty-file cleanup
     for stale partial volumes from prior bad starts.
  3. **DNSSEC ZSK creation** — the agent POSTed ``{"keytype":"zsk"}``
     without ``algorithm``; pdns 4.9's default-picker resolved to
     algorithm -1 (Unallocated) and rejected with "Creating an
     algorithm -1 ... key requires the size (in bits) to be passed."
     Pinned both KSK + ZSK to ``algorithm: ecdsa256`` (algo 13,
     RFC 6605) — the current online-signing default per pdns docs.
  4. **DNSSEC ``PRESIGNED`` metadata** — the agent set this on every
     sign + cleared on unsign. PRESIGNED is for *externally-signed*
     zones (operator runs ``dnssec-signzone`` offline + loads the
     output); for online-signing pdns derives signing intent from
     cryptokey presence, and the API filter rejects the kind via
     ``isValidMetadataKind``. Removed both calls + the helper.
  5. **DNSSEC DS-record state sync** — the control-plane handler
     stripped trailing dots from incoming zone names but queried
     ``DNSZone`` with the stripped form, while the DB stores zone
     names *with* the trailing dot. The IN-clause never matched, so
     every ``POST /dns/agents/dnssec-state`` was a 200-OK no-op and
     ``dnssec_ds_records`` stayed empty in the operator UI. Fixed
     by querying both forms.
  6. **LUA records — global flag instead of per-zone metadata** —
     the agent's per-record path tried to PUT
     ``ENABLE-LUA-RECORDS`` zone metadata via the REST API; pdns
     4.9 rejects with "Unsupported metadata kind". Replaced with the
     global ``enable-lua-records=yes`` knob in ``pdns.conf``, which
     is portable across versions and zero-cost for non-LUA zones.
  7. **ALIAS records didn't resolve** — ``pdns.conf`` had
     ``expand-alias=no`` hardcoded. PowerDNS Authoritative needs
     both ``expand-alias=yes`` and a ``resolver=`` upstream to
     synthesise A/AAAA at query time from an ALIAS rrset. Flipped
     to ``expand-alias=yes`` + ``resolver=1.1.1.1,8.8.8.8`` for
     out-of-the-box lab testing.
  8. **Multi-record rrset PATCH (the bug that broke GSLB pools)** —
     the per-record ``apply_record_op`` PATCH used ``changetype:
     REPLACE`` with the single record as the entire rrset. Two
     consecutive ``create www A`` calls collided — the second
     overwrote the first. This silently broke pool fan-out: a
     two-healthy-member pool only ever served the most recently
     added IP. Fixed with a GET-merge-PATCH dance: read the current
     rrset, splice the new content in (or out for delete), and
     PATCH the merged set back. Pool dig now correctly returns the
     full set of healthy members.
  Same drop also pinned every ``docker-compose.dev.yml`` build-able
  service to ``image: spatiumddi-<svc>:dev`` so ``make dev`` always
  builds locally and never silently pulls the registry copy that
  prod's compose declares — a source of half-mixed installs the
  user spotted while testing. ``make up`` (prod) still tags as
  ``ghcr.io/spatiumddi/...:latest`` for the release pipeline.

  Known follow-up: soft-deleted DNS records on PowerDNS-driver
  groups don't propagate to the daemon (only ``?permanent=true``
  deletes enqueue a record op). The "wait until next render" docstring
  on the soft-delete path assumes BIND9 zone-file rendering — for the
  REST-driven PowerDNS path the agent never sees the disappearance.
  Tracked separately; Phase 5+ tail.
- **PowerDNS Phase 5 — operator polish + docs pass (\#127).** Closes
  the documentation surface around the second authoritative driver.
  ``docs/drivers/DNS_DRIVERS.md`` grows a full Section 4 PowerDNS
  driver chapter (REST update strategy, API-key bootstrap, capability
  matrix, LUA / DNSSEC / catalog-zone internals, LMDB cache + recovery
  shape) and a Section 5.2 decision tree for when-to-pick-which-driver;
  ``docs/features/DNS.md`` gains a Section 0 driver-choice subsection
  with the three-driver capability matrix and "pick PowerDNS when…"
  guidance, mentions the ``propose_create_dns_zone`` ``driver_hint``
  argument, and updates the lead paragraph to drop the BIND9-only
  framing; ``docs/deployment/TOPOLOGIES.md`` adds two new sections —
  a "PowerDNS-primary + BIND-secondary hybrid" recipe (catalog-zone-
  driven AXFR crossover, plus per-zone driver placement) and a
  four-step "Migrating a BIND9 group to PowerDNS without DNS
  downtime" walkthrough with rollback notes and a DNSSEC restore
  caveat. ``agent-e2e.yml`` extends the Phase 4c kind smoke test
  with a DNSSEC online-signing pass: ``pdnsutil create-zone`` →
  ``add-record`` → ``secure-zone`` → ``rectify-zone`` → ``dig
  +dnssec`` against the local pdns_server, with RRSIG presence
  asserted in the response so the signing pipeline regression-
  catches in PR review. Frontend record-create modal grows a "Insert
  snippet…" dropdown when LUA is selected — six starter templates
  (``pickrandom`` / ``ifportup`` / ``ifurlup`` / ``createReverse`` /
  ``pickwhashed`` / ``pickclosest``) seed the textarea so operators
  don't have to remember LUA syntax cold. Issue #127 is now
  feature-complete; the only remaining tail is the gpgsql-backend
  variant (operator-choice; LMDB stays default) and pdns 4.10+
  catalog-consumer support.
- **PowerDNS Operator-Copilot ``propose_create_dns_zone``, Phase 4e
  (\#127).** New write-proposal tool + matching ``create_dns_zone``
  operation in ``app.services.ai.operations``. Args carry an
  optional ``driver_hint`` (one of ``bind9`` / ``powerdns`` /
  ``windows_dns``) that lets the LLM express operator intent
  without forcing it to know the exact server-group UUID — when
  ``group_id`` is omitted, the preview picks the first group whose
  servers expose the hinted driver; when ``group_id`` is set, the
  hint is cross-checked against the group's actual driver mix and
  rejects on mismatch with a remediation message. ``dnssec_enabled
  =true`` requires a PowerDNS-driver server in the selected group;
  the preview rejects DNSSEC requests against BIND9-only / Windows-
  only groups before the operator approves the proposal. Apply
  path mirrors the existing zone-create REST shape and writes a
  ``create dns_zone`` audit row tagged ``via=ai_proposal``. Tool
  ships ``default_enabled=False`` per the Tier-5 propose pattern;
  surfaces in Settings → AI → Tool Catalog under category ``dns``.
  Closes Phase 4e of the PowerDNS roadmap.
- **PowerDNS DNSSEC restore advisory, Phase 4d (\#127).** Backup
  restore now scans the post-restore database for DNSSEC-enabled
  zones in PowerDNS groups and surfaces a registrar-republish
  warning on the ``RestoreOutcomeResponse`` (and in the audit-log
  payload). PowerDNS DNSSEC signing keys live in the agent's LMDB
  volume, not in this archive — restoring a signed zone to a
  fresh agent regenerates keys and produces NEW DS records, which
  must be re-published to the parent registrar or external
  validation will fail. The advisory enumerates up to ten zone
  names plus a "(and N more)" suffix so operators know the scope
  of the registrar-handoff work. ``BackupPage`` renders the
  warnings as an amber callout under the success banner. The
  ``dns`` section description in ``backup/sections.py`` now
  documents the LMDB-not-archived caveat so operators see it
  before they rely on cross-install DNSSEC continuity.
- **PowerDNS kind smoke test, Phase 4c (\#127).** ``agent-e2e.yml``
  workflow now installs both DNS-agent flavors side by side in the
  kind cluster — the helm install passes a two-server list (one
  ``flavor: bind9`` in group ``e2e-bind9``, one ``flavor: powerdns``
  in group ``e2e-powerdns``), proving the chart renders + bootstraps
  both StatefulSets coherently. The smoke probe split into two
  steps: ``BIND9 daemon smoke`` runs the existing ``dig
  version.bind CH TXT`` against the bind9 pod, and the new
  ``PowerDNS daemon smoke`` runs ``dig id.server CH TXT`` (pdns's
  CHAOS-class equivalent) against the powerdns pod, with a
  six-attempt retry loop tolerating the slower first-boot LMDB
  seed. The "agents registered" gate now iterates both flavors so a
  powerdns-side regression can't slip past because the bind9 pod
  looks fine. Pod selectors lean on the ``spatiumddi.org/dns-flavor``
  label the chart already emits per Phase 4b. Closes the Phase 1
  "kind-cluster smoke test" item that was deferred until the chart
  had two-flavor support.
- **PowerDNS Helm chart wiring, Phase 4b (\#127).** Umbrella chart
  ``charts/spatiumddi`` learns to render PowerDNS-flavoured DNS
  agents alongside BIND9. The existing ``servers[].flavor`` knob
  (already in the schema, never used) now actually picks the image
  + mount path: ``flavor: powerdns`` pulls
  ``ghcr.io/spatiumddi/dns-powerdns`` (override comes from the new
  ``dnsAgents.flavors.powerdns`` block in ``values.yaml``) and
  mounts the ``dns-state`` PVC at ``/var/lib/powerdns`` for the LMDB
  store; ``flavor: bind9`` (default) keeps the historical
  ``/var/cache/bind`` mount. Both flavors share the same
  ``DNS_AGENT_KEY`` PSK secret, so ``helm install`` with a mixed
  ``servers[]`` list creates two StatefulSets that auto-register
  into different DNSServerGroups. Per-driver feature gating happens
  on the control plane — operators using PowerDNS-only features
  (DNSSEC sign/unsign, ALIAS, LUA, catalog zones) declare a
  PowerDNS-only group; mixing drivers in one group is still
  rejected. ``charts/spatiumddi/README.md`` + ``k8s/README.md``
  show the two-flavor mixed-server example. ``helm lint`` clean;
  ``helm template`` rendered output verified for bind9-only,
  powerdns-only, and both-side-by-side value sets.
- **PowerDNS deployment plumbing, Phase 4a (\#127).** New
  ``docker-compose.agent-dns-powerdns.yml`` standalone-VM compose
  file mirrors the bind9 shape: one ``dns-powerdns`` service against
  the ``ghcr.io/spatiumddi/dns-powerdns`` image with ``LMDB``-backed
  zone storage volumes. Main ``docker-compose.yml`` +
  ``docker-compose.dev.yml`` grow a ``dns-powerdns`` profile (host
  port 5453 so a side-by-side bind9 + powerdns dev setup doesn't
  collide), and the existing ``dns-bind9`` service now lists both
  ``dns`` (back-compat alias) + ``dns-bind9`` profiles for
  symmetry. ``.env.example`` documents the new profile alongside
  ``dns-bind9``; ``DNS_AGENT_KEY`` is shared by both drivers
  (single bootstrap PSK, image-baked driver). README + DOCKER.md +
  GETTING_STARTED.md + TOPOLOGIES.md updated to surface the
  driver choice everywhere ``--profile dns`` previously implied
  bind9. **Breaking rename:**
  ``docker-compose.agent-dns.yml`` → ``docker-compose.agent-dns-bind9.yml``
  for symmetric naming with the new powerdns file. The legacy
  ``--profile dns`` Compose alias is preserved so most operators
  don't need to edit anything.

### Changed

- **DNS host-port default flipped from 5353 → 1053.** The original
  5353 was chosen to dodge the port-53 / systemd-resolved collision
  but landed on the well-known mDNS port that avahi (default-on in
  Ubuntu desktop, Fedora, and most lab distros) already binds —
  operators were hitting the conflict on first run and reporting
  it. 1053 is the conventional alternate (Kubernetes / CoreDNS use
  it for the same reason) and has no IANA collision. The
  ``DNS_HOST_PORT`` env var still overrides; existing deployments
  that want to keep the old port can pin ``DNS_HOST_PORT=5353``
  in ``.env`` and recreate the container. Updated across
  ``docker-compose.yml``, ``docker-compose.dev.yml``, the two
  standalone-agent compose files, ``.env.example``, the
  ``GETTING_STARTED`` block in README, and ``DOCKER.md``.

### Migrations

- ``e7f94b21c8d5_dns_zone_dnssec_ds_records`` — adds
  ``dns_zone.dnssec_ds_records`` (JSONB, nullable) and
  ``dns_zone.dnssec_synced_at`` (timestamptz, nullable) so the
  agent can cache PowerDNS DS rrsets after a successful sign and
  the operator-facing zone-edit page renders them without round-
  tripping the agent on every page load.
- ``f1e7a3c92b40_multicast_groups`` — adds the ``multicast_group``
  table (\#126, Phase 1 Wave 1): UUID PK, owning ``ip_block`` FK
  (so groups inherit space/block hierarchy), address + scope +
  rendezvous-point + IGMP-version columns plus the standard
  per-row custom-fields JSONB. Indexed on ``(block_id, address)``.
- ``c8d2f47a90b3_multicast_domain`` — adds the ``multicast_domain``
  table (\#126, Phase 2 Wave 1) for PIM domain registry: name +
  description + scope-boundary CIDR list + per-domain RP-set + RP
  assignment policy enum + per-row custom fields. Group rows gain
  ``domain_id`` FK ON DELETE SET NULL so reassigning a group out
  of a domain doesn't cascade-delete it.
- ``d3a9c5b71e84_subnet_kind`` — adds ``subnet.kind`` column
  (\#126) discriminating ``unicast`` (default, backfilled for
  every existing row) from ``multicast``. The IPAM tree splits
  rendering by kind: multicast subnets hand off to the new
  multicast-group / domain surfaces; unicast continues using the
  IP / DHCP machinery. Indexed for the per-block "list multicast
  subnets" query.
- ``b7e2d9a5f314_dns_import_source`` — adds ``import_source`` +
  ``imported_at`` columns to ``dns_zone`` and ``dns_record``
  (\#128 Phase 1 Wave 1) so imported entities are distinguishable
  from operator-created ones. Both nullable; existing rows
  backfill ``NULL`` (operator-created).
- ``a1f4d97c8e25_network_device_poll_igmp`` — adds the
  ``network_device_poll_igmp`` table backing the multicast Phase 3
  SNMP IGMP-snooping populator. Per-poll: ``network_device_id``
  FK, polled_at, ``rows_added`` / ``rows_updated`` / ``rows_seen``
  counters, error column. Driven from the existing per-device
  poll scheduler — same pattern as the ARP + FDB pollers.
- ``c9f2a83b04d7_appliance_certificate`` — adds the
  ``appliance_certificate`` table (\#134 Phase 4b.1) for the
  appliance Web UI cert manager: name + source enum
  (uploaded/csr/letsencrypt/self-signed) + cert PEM + Fernet-
  encrypted private key + ``is_active`` + identity columns
  (subject_cn / issuer_cn / sans_json / fingerprint /
  valid_from / valid_to) + creator audit FK + reserved CSR
  columns (cert_pem nullable, populated on import).
- ``d8f3a92e0c47_appliance_csr_pending`` — relaxes NOT NULL on the
  cert-derived columns (\#134 Phase 4b.3) so a CSR-pending
  ``appliance_certificate`` row (generated CSR + stored private
  key, no cert yet) can exist. ``cert_pem IS NULL`` is the
  canonical pending sentinel.
- ``e4a7f10b2c39_agent_last_seen_ip`` — adds ``last_seen_ip``
  (VARCHAR 45) to both ``dns_server`` and ``dhcp_server``,
  populated from ``request.client.host`` on every agent
  heartbeat. Surfaces in the UI as a chip next to the
  operator-set host name so operators can identify which
  physical machine an agent is on in NAT / distributed
  deployments — the operator-set ``host`` field is just a
  label.

### Added (continued)

- **PowerDNS catalog zones, Phase 3d (\#127).** RFC 9432 catalog
  zones light up as **producer-only** for PowerDNS groups (BIND9
  already shipped). When ``DNSServerGroup.catalog_zones_enabled`` is
  on and this server is the group primary, the agent renders the
  catalog zone alongside regular zones via the existing PowerDNS
  REST reconciler — apex SOA + NS + ``version`` TXT pinned to
  ``"2"`` + one PTR per primary zone under ``<sha1>.zones.<catalog>.``
  using the same RFC 9432 §4.1 wire-format hash BIND9 emits.
  Membership re-renders on every config sync; the ``int(time.time())``
  serial guarantees AXFR consumers always pull on member changes.
  Consumer mode logs a warning explaining that PowerDNS 4.9 (the
  shipped image) doesn't auto-consume catalogs — operators stand
  up secondaries via plain AXFR against the producer instead;
  full consumer-side support waits for pdns 4.10+. Driver capability
  reads ``catalog_zones: "producer-only"`` (string, not bool, to
  carry the asymmetry in capabilities introspection). The
  control-plane ``build_config_bundle`` gate flipped from
  ``server.driver == "bind9"`` to ``server.driver in ("bind9",
  "powerdns")``; producer detection now uses
  ``DNSServer.driver == server.driver`` so each driver picks its
  own primary independently. 16 / 16 backend driver tests still
  pass; 5 / 5 new agent-side ``test_powerdns_catalog`` tests cover
  the apex records, the SHA-1-hashed PTR labels, empty-member
  skipping, trailing-dot normalisation, and serial monotonicity.
- **PowerDNS online DNSSEC, Phase 3c.fe (\#127, frontend).** Zone
  edit modal grows a dedicated DNSSEC card on every existing zone:
  status indicator (signed / unsigned + last-sync timestamp), a
  green "Sign zone" button (or amber "Re-sign" + red "Unsign" pair
  when signed), and a copy-to-clipboard list of DS rrset strings
  to paste into the parent registrar. Status auto-refreshes every
  10 s while signed so DS records appear within one config-sync
  cycle of clicking Sign. Driver-aware errors surface inline (the
  API's 422 on non-PowerDNS groups renders as a destructive banner
  inside the card, not a vanished button). Migration adds
  ``dnssec_ds_records`` (JSONB) + ``dnssec_synced_at`` (timestamp)
  columns to ``dns_zone`` (revision ``e7f94b21c8d5``); the agent
  populates them via a new ``POST /api/v1/dns/agents/dnssec-state``
  endpoint after every sign / unsign. New
  ``GET /dns/groups/{g}/zones/{z}/dnssec/info`` operator-facing
  endpoint returns the cached state for the FE without round-
  tripping the agent on every page load. PowerDNS's DS rrset
  extraction walks the cryptokeys response — KSK entries carry a
  ``ds`` array with one entry per supported digest algorithm,
  all of which the operator should publish. Catalog zones
  (Phase 3d) still pending.
- **PowerDNS online DNSSEC, Phase 3c (\#127, backend).** Operators
  can sign / unsign a PowerDNS zone via two new endpoints:
  ``POST /dns/groups/{g}/zones/{z}/dnssec/sign`` and
  ``/unsign``. Driver-aware via the new
  ``_DRIVER_GATED_OPERATIONS`` map (PowerDNS-only — BIND9's manual
  ``dnssec-keygen`` flow stays in the \#49 umbrella's scope). On
  sign, the agent generates a KSK + ZSK via PowerDNS's
  ``POST /zones/{z}/cryptokeys`` REST endpoint, sets
  ``PRESIGNED=1`` zone metadata, and rectifies — pdns then serves
  signed answers and handles automatic key rollover internally.
  On unsign, every cryptokey is deleted and ``PRESIGNED`` cleared.
  Both operations are idempotent (re-signing a signed zone or
  re-unsigning an unsigned zone converges without error).
  ``DNSZone.dnssec_enabled`` (already on the model) flips
  synchronously with the API call so the UI reflects intent
  immediately. PowerDNS driver's ``capabilities()
  ["dnssec_inline_signing"]`` flips to True.
  Frontend DNSSEC card with sign/unsign button + DS-record export
  + NSEC3 settings come in Phase 3c.fe (next commit). Catalog
  zones (Phase 3d) still pending.
- **PowerDNS LUA records, Phase 3b (\#127).** LUA lands as a
  PowerDNS-only computed-record type for zones served by a
  PowerDNS-only server group. Operators write a snippet like
  ``A 'pickrandom({"10.0.0.1","10.0.0.2"})'`` and pdns evaluates
  it at query time to produce the response — useful for
  weighted-random load distribution, ``ifportup`` health-checked
  failover, and geo-routing patterns. The agent automatically
  sets ``ENABLE-LUA-RECORDS=1`` zone metadata via PUT
  ``/zones/{zone}/metadata/ENABLE-LUA-RECORDS`` when a zone has
  any LUA record (PowerDNS only evaluates LUA records when this
  metadata is set; otherwise the snippet is served as a literal
  string). Same driver-aware gate as ALIAS — API returns 422 if
  any server in the zone's group runs a non-PowerDNS driver.
  Frontend swaps the value ``<input>`` for a monospace
  ``<textarea>`` when the type is LUA, and shows a violet info
  banner explaining the format and the
  server-side-code-execution caveat. Online DNSSEC (Phase 3c) and
  catalog zones (Phase 3d) still pending.
- **PowerDNS ALIAS records, Phase 3a (\#127).** ALIAS lands as a
  first-class record type for zones served by a PowerDNS-only
  server group. Resolves CNAME-at-apex (which RFC 1034 §3.6.2
  forbids for CNAME) by having PowerDNS resolve the target at
  query time and serve the resulting A / AAAA. Driver-aware gate
  in the API: ``POST/PUT /dns/groups/{g}/zones/{z}/records`` and
  the template-instantiation path return 422 if any server in the
  group runs a driver other than ``powerdns`` — operators get a
  clear error up front pointing at the move-to-powerdns-group
  remediation, not a confusing per-server apply failure later.
  PowerDNS driver's ``capabilities()["alias_records"]`` flips to
  True; ``_SUPPORTED_RECORD_TYPES`` gains ALIAS and the validator
  no longer rejects it. Frontend record-create modal includes
  ALIAS in the type dropdown with a contextual violet info banner
  that surfaces only when ALIAS is selected. LUA records (Phase
  3b) and online DNSSEC (Phase 3c-or-later) still pending.
- **PowerDNS frontend driver picker, Phase 2 (\#127).** Server
  create/edit modal grows ``PowerDNS (agent-managed)`` as a third
  option in the Driver dropdown alongside BIND9 and Windows DNS.
  Selecting it shows a violet info banner explaining that operators
  run the new ``ghcr.io/spatiumddi/dns-powerdns`` container alongside
  the server, that records apply via the local PowerDNS REST API on
  port 8081 (loopback only), and that the API key is generated +
  rotated automatically by the agent. The API Key input is hidden
  for PowerDNS (replaced with a disabled "(generated by agent on
  first boot)" placeholder) so operators don't accidentally try to
  set it. The API Port placeholder switches per-driver: ``953
  (rndc)`` for BIND9, ``8081 (PowerDNS REST, loopback)`` for
  PowerDNS, ``953 / 8081`` for the legacy mixed default. Server
  Detail Modal's existing ``isBind9`` gate continues to hide the
  Logs / Stats / Config tabs and the rndc-status panel for
  PowerDNS — those need PowerDNS-specific implementations (Phase 3:
  pdns query log, ``pdns_control`` stats, rendered config push).

### Added — Multicast IPAM (\#126)

- **Registry data model + REST CRUD (Phase 1 Wave 1).** New
  ``multicast_group`` table backed by IPAM's existing IP-block
  hierarchy (every group lives under an IP block, inheriting
  custom-fields and ownership). Backend exposes a full ``/multicast/
  groups`` CRUD surface; the address validator rejects anything
  outside 224.0.0.0/4 (IPv4) or ff00::/8 (IPv6). Group rows carry
  scope (link-local / admin-scoped / global), RP, IGMP version,
  description, and the standard custom-fields JSONB.
- **Operator UI — list page + tabbed editor (Phase 1 Wave 2).** New
  ``/ipam/multicast`` page; per-group editor renders three tabs
  (Properties, Custom Fields, Active Listeners — Phase 3 populates
  the last one from SNMP IGMP-snooping).
- **Bulk allocate + IP picker (Phase 1 Wave 3).** Multicast subnets
  reuse the same bulk-allocate / IP-picker UX as unicast subnets,
  with the validator gated to the multicast-only range. Picker
  shows next-available addresses + a "skip to N" jump for sparse
  group allocations.
- **IP-detail tab + collision conformity (Phase 1 Wave 4).** The
  IP-detail modal grows a Multicast tab listing every group that
  carries this address (typically zero or one — collisions are an
  operator misconfiguration). Conformity check
  ``multicast_address_uniqueness`` lights up when two groups share
  a multicast address.
- **Bulk-delete on the groups page (Phase 1 follow-up).** Checkbox
  column + "Delete N selected" toolbar mirroring the DHCP-MAC and
  IPAM bulk-delete patterns.
- **PIM domain registry (Phase 2 Wave 1).** New ``multicast_domain``
  table + page; operators model their PIM-SM topology (or other
  multicast routing protocol) here. Each domain pins a scope
  boundary (the CIDRs at the domain edge that drop multicast),
  a static RP set, and an RP assignment policy. Group rows now
  carry an optional ``domain_id`` FK.
- **Domains UI + group-domain picker (Phase 2 Wave 2).**
  ``/ipam/multicast/domains`` lists PIM domains; the group editor
  gets a domain dropdown so operators reassign groups across
  domains without API tinkering.
- **Subnet kind discriminator (\#126).** ``Subnet.kind`` column
  forks unicast (default, all existing rows backfilled) from
  multicast. The IPAM tree branches rendering by kind: multicast
  rows hand off to the multicast-group / domain surfaces; unicast
  continues using the IP / DHCP machinery. Means a single block
  hierarchy can mix unicast + multicast subnets cleanly without
  the type confusion that haunts other IPAMs.
- **SNMP IGMP-snooping populator (Phase 3 Wave 1).** Per-device
  poll scheduler pulls the L2 switch's IGMP-snooping table on each
  cycle, materialising "which clients joined which multicast
  groups" into an Active Listeners view on every group. Driven by
  the same scheduler as the ARP + FDB pollers (Phase 2 of network
  discovery); per-device toggles plus a per-poll telemetry table
  (``network_device_poll_igmp``).
- **Operator Copilot multicast tools (Phase 4 Wave 1).** Three
  read tools (``find_multicast_groups``, ``find_multicast_domain``,
  ``count_multicast_groups``) and one write proposal
  (``propose_allocate_multicast_group`` — preview includes the
  collision conformity result so the assistant warns about
  duplicates before the operator clicks Apply). Default-enabled
  per the MCP-coverage-for-new-features non-negotiable.
- **Close-out: IGMP reaper (Phase 4).** Stale Active-Listener
  entries (last-heard > 30 min) drop out via a per-evaluator
  sweep, so the dashboard reflects current state instead of
  growing forever. Configurable window in
  ``multicast.listener_staleness_minutes``.
- **Page rework: merged groups + domains into one tabbed page.**
  Two-page UX was thrash; merged into a single ``/ipam/multicast``
  with Groups / Domains / Listeners sub-tabs (same pattern as
  Network and Compliance).

### Added — DNS Import (\#128)

- **import_source + imported_at provenance (Phase 1 Wave 1).** Every
  ``dns_zone`` and ``dns_record`` gains ``import_source``
  (free-form string, e.g. ``"bind9-zonefile"`` /
  ``"windows-dns:dc1.contoso.local"``) and ``imported_at``
  (timestamptz). Lets the operator (and audit) tell apart
  operator-created records from imported ones, and answer
  questions like "which zone came from which migration source?"
- **``dns.import`` feature module.** Gated by the new
  ``ModuleSpec(id="dns.import", default_enabled=False)`` —
  appears as a togglable Feature in Settings → Features &
  Integrations. Off by default since most ops never import; on,
  it unlocks the import admin page + the import endpoints.
- **BIND9 archive parser + canonical IR (Phase 1 Wave 2).** Pure-
  Python parser walks a BIND9 zonefile (or a tarball /
  ``named.conf``-style index) and renders every zone + record
  into a canonical intermediate representation. Handles ``$TTL``,
  ``$ORIGIN``, ``$INCLUDE``, IDN labels, every common record
  type. Refuses unsigned-DNSSEC data with a clear error.
- **``/dns/import/bind9/preview`` + ``/dns/import/bind9/commit``
  endpoints (Phase 1 Wave 3).** Two-phase apply: preview returns
  the IR + a per-zone diff against existing zones (would-add /
  would-update / would-skip-collision counts) without touching
  the DB; commit applies it inside a single transaction with
  ``on_collision: skip|update|abort`` per-zone policy. Audit
  rows tag every imported entity with the source archive's
  filename + checksum.
- **DNS Import admin page (Phase 1 Wave 4).** New
  ``/admin/dns-import`` route with three tabs:
  BIND9 / Windows DNS / Other (placeholder for future ISC + Knot
  importers). BIND9 tab is fully wired — upload, preview, commit.
  Per-row "Cancel zone" affordance on the preview table for
  operators who want to skip just one of N zones from a big
  zonefile.
- **Windows DNS live-pull importer (Phase 2).** ``/dns/import/
  windows-dns/{preview,commit}`` endpoints + frontend hook. Uses
  the existing Windows DNS WinRM driver to enumerate zones via
  ``Get-DnsServerZone`` + records via ``Get-DnsServerResourceRecord``,
  flattens the PowerShell shape into the same canonical IR, and
  runs through the same commit path. Operator picks a Windows DNS
  server from the existing server-group surface; no new credential
  storage needed.
- **BIND9 import tests + lint pass.** Backend coverage on the
  parser corpus + a CI lint pass.

### Added — DEMO_MODE + Codespaces public demo

- **``DEMO_MODE`` server-side lockdown.** New ``settings.demo_mode``
  flag (env-driven) — when on, the api refuses every "abusable"
  mutation: nmap scan creation, AI provider creates / chats,
  outbound webhook subscriptions, integration target creates (every
  read-only mirror — Kubernetes / Docker / Proxmox / Tailscale /
  UniFi), audit-forward target creates, SMTP / backup target
  creates, factory reset, password change. IPAM / DNS / DHCP CRUD
  on the seeded data stays open so visitors can poke at the
  product. Refusals 403 with a structured ``DEMO_MODE_ENABLED`` body
  so a frontend tooltip can explain the block.
- **GitHub Codespaces demo.** ``.devcontainer/`` + a Codespaces
  launch badge in the README. One click brings up a full
  SpatiumDDI stack on a fresh Codespace: builds images from
  ``main``, runs migrations, seeds a realistic IPAM / DNS / DHCP /
  network-modeling dataset so every screen has something to look
  at, lands the operator on the dashboard signed in as
  ``admin / admin``. Cold-start ~5-8 min on a 4-core Codespace;
  free-tier hours come from the visitor's own GitHub account.

### Added — SpatiumDDI OS Appliance (\#134)

This release lands Phases 1 / 2 / 3 / 4 (a-g) / 6 in one cut. The
appliance is alpha-ready: bootable ISO, web-managed lifecycle,
distributed-role-split installer.

- **Phase 1 — Debian 13 qcow2 + builder container.** ``make
  appliance`` produces a self-contained Debian-trixie qcow2 with
  the full SpatiumDDI stack pre-installed. Builder runs in a
  published ghcr.io container so the only host requirement is
  Docker. Hybrid BIOS + UEFI grub. Includes ``cloud-init`` +
  ``cloud-initramfs-growroot`` (so the root partition expands to
  fill the host disk on first boot), ``chrony``, ``ssh``,
  ``docker.io``, the bundled DNS/DHCP agents, ``qemu-guest-agent``
  + ``open-vm-tools`` (for graceful shutdown + IP reporting on
  any major hypervisor), ``nftables`` default-deny inbound
  firewall, ``unattended-upgrades`` (security pocket only),
  persistent ``systemd-journald`` + ``logrotate``, a first-boot
  2 GiB swapfile, ``NOTICE`` / ``LICENSES``, custom ``/etc/issue``
  banner with the appliance's IP + URL.
- **Phase 2 — hybrid USB/CD live ISO via xorriso.** ``make
  appliance-iso`` wraps the qcow2 into a 437 MB hybrid ISO that
  boots three ways: BIOS-CD, UEFI-CD, USB-dd. Uses
  ``grub-mkrescue`` for the tri-mode hybrid; ``live-boot`` /
  ``live-config`` / ``live-tools`` packages installed so the same
  initrd boots both disk-installed and from the ISO. Initrd
  regenerated via chroot in ``wrap-iso.sh`` so the live-boot
  scripts are actually active. ``live-tools`` divert of
  ``update-initramfs`` undone properly during the installer step.
- **Phase 3 — Proxmox-style installer wizard.** Boot the live ISO
  → drops to whiptail wizard on tty1. Walks through five questions
  (target disk, hostname, admin user + password, network DHCP/
  static, timezone) plus the Phase 6 role question; partitions GPT
  (1 MiB BIOS Boot Partition + 512 MiB ESP + ext4 root); rsyncs
  the live rootfs onto the target; copies the live kernel into
  ``/boot``; regenerates the initrd inside a chroot; writes
  ``/boot/grub/grub.cfg`` directly (bypassing ``update-grub``'s
  chroot-confused ``grub-probe``); installs GRUB for both
  i386-pc (BIOS) and x86_64-efi (UEFI ``--removable``); injects
  the operator's answers; ejects the install media (best-effort
  ATA EJECT) + reboots. Drop-to-shell ``on_failure`` handler
  prints the install log + the SSH credentials for live-ISO log
  capture before handing the operator a root prompt.
- **Phase 4a — Visibility gating + management hub frame.**
  ``settings.appliance_mode`` env flag + ``/api/v1/version``
  fields (``appliance_mode``, ``appliance_version``,
  ``appliance_hostname``) so the frontend can gate the
  "Appliance" sidebar entry before login. New ``appliance``
  permission family + "Appliance Operator" built-in role. New
  ``/appliance`` route with tabbed hub for the sub-phases.
- **Phase 4b — TLS / certificates.**
  - 4b.1 cert storage model + upload / list / activate / delete
    (Fernet-encrypted private key at rest, one-active-at-a-time
    invariant in a single transaction, audit on every mutation).
  - 4b.2 nginx HTTPS deployment — appliance frontend serves :443
    with the active cert from a shared docker volume; :80 → 301
    redirect with ``/.well-known/acme-challenge/*`` carve-out for
    Phase 4b.4's Let's Encrypt; ``docker kill --signal=HUP`` on
    the frontend container reloads nginx without dropping
    in-flight connections.
  - 4b.3 generate CSR on the server + accept signed cert
    paste-back (RSA-2048/3072/4096 + EC P-256/P-384, full
    subject + SANs); CSR-pending rows render with amber rings +
    distinct affordances; mismatched cert returns 422.
  - 4b.5 self-signed cert auto-generated on first boot when no
    cert exists; SAN list pulls from the host's globally-scoped
    IPs (not the docker bridge IP, which would mismatch every
    browser warning).
- **Phase 4c — SpatiumDDI release management.** ``/appliance/
  releases`` page lists recent GitHub releases (60 s server-side
  cached). One-click apply writes a host-side trigger file;
  ``spatiumddi-update.path`` + ``.service`` units pick up the
  trigger, run ``docker-compose pull && up -d`` so the api
  recycles itself cleanly. UI tails the host-side update log on
  3 s while an apply is in flight.
- **Phase 4d — Container management.** ``/appliance`` Containers
  tab lists every spatium container with health + state, lets
  the operator start / stop / restart spatium-prefixed containers,
  and streams logs over SSE (fetch + reader, token auth — works
  through nginx without a separate websocket).
- **Phase 4e — Logs + diagnostics.** Host log viewer (reads from
  the bind-mounted firstboot.log / update.log), self-test runner
  (DNS resolution / every spatium container healthy / internal
  API health / DHCP daemon / DNS daemon), one-click diagnostic
  bundle download — zip with redacted env + container logs +
  system info.
- **Phase 4f — Network/host info + maintenance + reboot.** Read-
  only system info card (hostname / host IPs / uptime / version /
  reboot-pending), maintenance-mode flag (file-backed; global
  amber banner above the header on every page until disabled),
  host reboot via the same systemd Path-unit pattern as 4c (10 s
  grace so the 202 reaches the browser before the box goes
  down). Hostname rename / static-IP toggle / nftables editor /
  SSH key upload deferred behind a host-side writer service.
- **Phase 4g — Web first-boot wizard + recovery banner.** Single-
  page checklist at ``/appliance/setup`` with file-backed
  completion flag; amber ``SetupBanner`` above the header until
  the operator hits "Finish setup".
- **Phase 6 — Role-split single ISO (five roles).** Installer
  wizard adds a Role radio: ``control`` (all-in-one default with
  bundled BIND9 + Kea) / ``control-only`` (control plane without
  bundled agents — for distributed deployments) /
  ``dns-agent-bind9`` / ``dns-agent-powerdns`` / ``dhcp-agent``.
  For agent roles, the wizard collects ``CONTROL_PLANE_URL``,
  agent key, and target agent group; firstboot wires the right
  ``COMPOSE_PROFILES`` and writes the per-role env. Control-plane
  services (postgres / redis / migrate / api / worker / beat /
  frontend) all gain ``profiles: ["control"]`` so they don't run
  on agent appliances; new ``agent-landing`` ``nginx:alpine``
  service serves a templated static status page on agent roles
  (role / hostname / control-plane URL / version).
- **Agent bootstrap key reveal in Settings.** New
  Settings → Security → "Agent bootstrap keys" section. Password-
  confirm + local-auth-only gate (external-auth admins get a
  clear "log in as a local admin" 403). Reveals DNS_AGENT_KEY +
  DHCP_AGENT_KEY (with show/hide + copy buttons) so operators
  can hand them to agent installs across every topology
  (appliance role-split installer, docker-compose, Helm, bare
  metal). Both successful + denied reveals audited.
- **DNS/DHCP servers show source IP.** New ``last_seen_ip``
  column on ``dns_server`` + ``dhcp_server``, populated from
  ``request.client.host`` on every agent heartbeat. Surfaces as
  a chip next to the operator-set host name on the Servers tab
  cards so operators can identify which physical machine an
  agent is on in NAT / distributed deployments — the operator-set
  host field is just a label.
- **README + Project Status updates.** Appliance moves from
  "roadmap" to "🔄 Alpha" everywhere it's mentioned (deployment
  table, project status row, full-feature detail, docs index,
  table-of-contents).

### Changed

- **DNS server-group detail page — Servers tab first.** The
  group detail view defaulted to the Zones tab; reordered so
  Servers is the first tab and the URL-fallback default. Matches
  the DHCP group detail page; operators land on the servers list
  (more frequently edited than zones for ops work) when they
  open a group.

### Security

- **CodeQL alerts \#1 and \#2 closed.** Two long-open static-
  analysis findings cleared: a TLS pinning gap on the SNMP /
  WinRM client path (server-cert verification was off in
  contexts where pinning was always intended), and a BGP
  community-import error-message sanitisation pass (operator-
  supplied input was reaching the error string verbatim,
  enabling a stored-XSS surface in the audit-log viewer
  rendering). Both flagged by GitHub's CodeQL workflow; both
  closed in this drop.
- **pdns_parser separator regex ReDoS.** The PowerDNS-config
  parser's record-separator regex backtracked on certain
  malformed input + degraded to quadratic time on big inputs.
  Replaced the regex with a str-split + token-walk so worst-case
  is linear. Lit up the same CodeQL alert as the BIND parser's
  earlier ReDoS finding.
- **Live-mode SSH only on the appliance live ISO.** The
  installer's debug-SSH affordance (random root password,
  visible on the console banner) only runs when
  ``boot=live`` is in the kernel cmdline; the installed system's
  ssh stays on the install-time admin user + the
  ``PermitRootLogin no`` baseline.

### Fixed

- **BIND9 query logs end-to-end + CHAOS-class noise filter.**
  The bind9-driver query-log shipper was double-quoting + missing
  the CHAOS-class filter, so the Logs tab was showing the agent's
  own ``version.bind`` probe spam every 30 s. Closed both.
- **/logs — reorder tabs, add Event Log empty state.** Tab order
  matched a stale design; reordered + added an empty-state card
  for the Event Log tab so first-time operators see a hint
  instead of a blank table.
- **Hide all Ask AI affordances when no provider configured.**
  Page-level "Ask AI" buttons were visible even when zero AI
  providers were enabled, leading to confusing "no provider"
  errors. Gated on the AI providers list being non-empty.
- **UI three UX fixes.** UniFi added to README, Features sidebar
  rename, ConfirmModal pattern applied everywhere (replacing the
  last few raw ``window.confirm`` calls).
- **HTTP_PORT default flipped to 8077** in environment + compose
  files. 80 was colliding with random other services on
  developer laptops; 8077 has no IANA collision + matches the
  docs.
- **Alerts evaluator: silence audit_chain_broken spam.** The
  evaluator was emitting "audit chain broken" warnings every
  60 s on a healthy install. Closed the false-positive path.

### Migrations

(See bullets earlier — eight new migrations land in this release.)

---

## 2026.05.07-1 — 2026-05-07

The **backup + factory-reset** release. Issue \#117 (full system
backup with remote destinations) closed end-to-end through Phase
3, and issue \#116 (per-section factory reset back to defaults)
shipped in a single commit alongside it. Backup now ships eight
destination kinds — local volume, AWS S3 (and every S3-compatible
endpoint), SCP/SFTP, Azure Blob, SMB/CIFS, FTP/FTPS, Google
Cloud Storage, WebDAV — plus selective per-section restore,
cross-install secret rewrap so cross-install operators no longer
hand-copy `SECRET_KEY` to the destination's `.env`, automatic
`alembic upgrade head` on restore (with drift auto-recovery
when the dump's alembic_version row is stale relative to its
schema), exclude-secrets diagnostic mode for shareable
debug snapshots, scheduled cron + retention, restore-from-
destination, archive proxy-download, "download latest" per-
target, friendly cron presets, and `system.backup_*` /
`system.restore_performed` / `system.factory_reset` typed
events through the existing webhook event-outbox. Three
read-only MCP tools (`list_backup_targets` /
`list_backup_archives_at_target` / `find_backup_audit_history`)
surface the backup state to the Operator Copilot. Factory
reset runs per-section across 12 sections plus an
"Everything" target, every guardrail enforced server-side
(superadmin gate + fresh password re-check + per-section
confirm phrase + in-flight-backup mutex + 6h cooldown +
audit anchor that survives `audit_log` wipes). The page
lives as a third tab on the Backup admin surface — backup
snapshots state, factory reset wipes it, they're the two
opposite ends of the same lifecycle.

The release also closes out the **operator-toggleable platform**
wave that started before backup landed: feature-module toggles
+ Settings → Features page that let admins hide whole sidebar /
REST / MCP surfaces (Network ownership entities, AI Copilot,
Conformity, Tools, plus the four integrations); the Tool
Catalog rewrite that mirrors the Features page layout (3-col
adaptive grid + pill toggle + auto-save on flip); and the
Operator Copilot's Tier 2-5 tool wave from issue \#101 — read
tools surfacing customers / sites / providers / users / groups
/ roles plus DNS/DHCP sub-resource depth, integration mirrors,
observability, and Apply-gated write proposals. The Copilot now
ships **91 tools** total. Plus release-note formatting got a
complete rework: the GitHub release body now reads as flowing
prose with emoji section headings instead of a wall of forced
`<br>`s.

A new `docs/deployment/TOPOLOGIES.md` documents six reference
production topologies — single VM through HA cloud + on-prem
hybrid — with hand-authored SVG diagrams + sizing notes.

Also in this release: a **BGP enrichment** surface
(\#122) wiring RIPEstat (announced prefixes / prefix-overview /
routing-history / as-overview, 6 h cache) and PeeringDB (`net`
/ `netixlan`, 24 h cache) into REST + 5 MCP tools + an ASN-
detail "BGP Footprint" tab; a **diagnostics** surface (\#123)
that captures every uncaught Python exception from API + Celery
into a new `internal_error` table with fingerprint dedup +
Acknowledge / Suppress / Submit-bug flow; the **tags wave**
(\#104) shipping `?tag=` filtering on every REST list endpoint
+ autocomplete + tag chips across IPAM / DNS / DHCP / Network
modeling pages; **three new dashboard tabs** — Network /
Integrations / Security (\#107 / \#108 / \#109) — plus
Compliance + Conformity sub-tabs front-of-house; the
**security wave continuation** with password policy (\#70),
account lockout after N failed logins (\#71), active session
viewer + force-logout (\#72), and tamper-evident audit chain
hash + nightly verifier (\#73); two more Operator Copilot
features — `ask_yes_no` tool with chat-suspend semantics
(\#120) and the visible context chip + per-call default prompt
prefill (\#119); plus a Gmail-style draggable + resizable
Copilot bubble that lets operators keep the chat open while
clicking around the rest of the app.

### Added

- **Factory reset — every guardrail server-side (issue #116).**
  Per-section "wipe back to defaults" surface for superadmins.
  12 sections mapped from the issue body — IPAM, DNS, DHCP,
  Network modeling, Integrations, AI / Copilot, Compliance,
  Tools, Observability logs, Auth + RBAC, Settings + branding,
  plus an "Everything" target. Three dispatch kinds: `truncate`
  (9 sections, straight `TRUNCATE … RESTART IDENTITY CASCADE`
  over the section's tables), `auth_rbac` (partial wipe that
  preserves the calling user, every other superadmin, and
  built-in roles), `settings_reset` (DELETE the singleton
  platform_settings row; recreated with model defaults on next
  request). Tables intentionally untouchable: `alembic_version`,
  `oui_vendor`, `backup_target`, `feature_module`,
  `event_outbox`, `internal_error`, `audit_forward_target` —
  platform-housekeeping or safety-net state. Hard guardrails
  enforced server-side: superadmin gate, password re-verification
  (fresh bcrypt compare, NOT bearer-token check), exact-match
  per-section confirm phrase (`DESTROY-IPAM`, `DESTROY-DNS`, …,
  `FACTORY-RESET-ALL`), in-flight backup mutex, Redis lock
  against concurrent resets, 6-hour cooldown, audit anchor
  written via fresh `AsyncSessionLocal` so the event-publisher
  hook fires `system.factory_reset` automatically. Pre-flight
  backup as warn-only with `acknowledge_no_backup` override —
  no enabled backup target → 412 unless explicitly acknowledged.
  Endpoints at `/system/factory-reset/{sections,preview,execute}`.
  UI lives as a third tab on the Backup admin page (after Manual
  + Destinations) with red-bordered "Reset everything" card +
  draggable modal that gates the password field on a green-
  border phrase match. Closes \#116.

- **Backup & restore — Phase 3 quick wins (issue #117).** Five
  follow-ups landed in one commit: (1) **cron presets dropdown**
  on the target form — 7 UTC presets (hourly / every 6h / 12h /
  daily 02:00 / 04:00 / weekly Sun 03:00 / monthly 1st 03:00) +
  custom-text fallback; (2) **`GET /backup/targets/{id}/archives/
  latest/download`** — proxies the newest archive at a target
  through the existing per-driver `download(filename)` method
  for one-shot automation; (3) **typed-event fan-out** via the
  existing event-outbox — `system.backup_completed` /
  `system.backup_failed` / `system.restore_performed` plus
  `backup.target.{created,updated,deleted}` lifecycle events
  ride the existing HMAC-signed POST + retry infrastructure,
  exposed via `GET /webhooks/event-types`; (4) **exclude-secrets
  diagnostic mode** — checkbox on the build-and-download form
  that switches to plain-format dump and post-processes the SQL
  text in memory to scrub every Fernet-encrypted column +
  `__enc__:`-prefixed JSONB field. The live database is never
  touched (initial design used `pg_export_snapshot` with a side
  transaction NULLing columns — but Postgres exports the
  *committed* state, so plain-format dump + text post-processing
  is the correct simpler approach); (5) **WebDAV destination
  driver** — Tier 3, closes the Nextcloud / ownCloud / mod_dav /
  IIS WebDAV market. Driven directly via `httpx` (existing
  platform dep) using PUT / GET / PROPFIND / DELETE — no SDK
  added. Form labels Nextcloud's "app password" requirement
  explicitly so 2FA-enabled users don't fail mysteriously.

- **Backup & restore — alembic upgrade-on-restore + drift
  auto-recovery (issue #117 Phase 2 + Phase 3 follow-up).**
  After the data replay, `alembic upgrade head` runs against
  the freshly-restored database when the archive's
  `schema_version` is older than the local install's expected
  head. State machine surfaces `up_to_date` / `upgraded` /
  `incompatible_newer` / `unknown` / `failed` /
  `auto_recovered` in the response + audit row. The
  `auto_recovered` state catches the real-world drift case: if
  the backup was taken when alembic_version had drifted relative
  to its schema (operator manually stamped head to fix earlier
  inconsistency, then later restored from a backup taken before
  the fix), the restore brings back BOTH the up-to-date schema
  AND the stale alembic_version, and `alembic upgrade head` then
  fails with "table already exists" on the first migration. The
  migration step now detects that signature
  (`DuplicateTableError` / `DuplicateColumnError` / "already
  exists") and recovers by running `alembic stamp head` to align
  — the schema is already correct, only the version row needed
  catching up. Operator-facing note explains what happened in
  plain English. Replaces the "currently version-skew is rejected
  outright" Phase 1 behaviour from the issue body.

- **Backup & restore — cross-install secret rewrap (issue #117
  Phase 2).** After the data replay, `rewrap_secrets` walks every
  Fernet-encrypted column (22 columns across 16 tables) plus the
  `__enc__:`-prefixed fields inside `backup_target.config`
  JSONB, decrypts each with the source key recovered from
  `secrets.enc`, and re-encrypts with the destination's local
  key. Same-install restores short-circuit with
  `same_install=true` (counters all zero). Idempotent re-runs
  (a row already encrypted with the dest key) count as
  `skipped_idempotent` rather than failed. Failures are counted +
  surfaced in the response + audit row, never raised — one bad
  row mustn't kill an otherwise-clean restore. Fernet derivation
  matches `app.core.crypto._fernet` exactly: explicit
  `credential_encryption_key` wins when set; SHA-256 over
  `secret_key` is the fallback. The connection lifecycle is
  important: the restore phase intentionally disposes the
  SQLAlchemy engine (so pg_restore / psql can run without pool
  contention), so rewrap opens its own short-lived asyncpg
  connection rather than reviving the pool prematurely. The
  operator-facing note adapts to the rewrap state ("no key
  rewrap was needed" / "re-encrypted N secret values" /
  "re-encrypted N, but K rows could not be decrypted with either
  key"). Closes the manual-`SECRET_KEY`-copy step from Phase 1's
  cross-install restore caveat.

- **Backup & restore — Tier 2 destinations (issue #117 Phase 2).**
  Three new destination drivers slot into the same registry +
  `BackupDestination` ABC the Tier 1 set used: (1) **SMB / CIFS**
  — Windows / Samba shares via `smbprotocol`'s smbclient shim;
  NTLM auth with optional NTLM domain + SMB3 encryption toggle;
  atomic `.tmp → rename` writes; `delete_session` on
  test-connection so probes don't pollute the cached session
  pool. (2) **FTP / FTPS** via stdlib `ftplib` (no new dep) —
  three modes: plain `ftp`, `ftps_explicit` (AUTH TLS),
  `ftps_implicit` (port 990 wrapped before the first FTP
  command). Passive + active; `verify_tls` toggle for self-
  signed labs. MLSD listing with a LIST + SIZE + MDTM fallback
  for legacy NAS. (3) **Google Cloud Storage** via
  `google-cloud-storage` — auth via service-account JSON key the
  operator pastes in (Fernet-wrapped at rest); ADC intentionally
  not exposed because the api/worker containers don't carry GCP
  metadata identity. Drive-by: replaced the hardcoded
  `{local_volume, s3, scp, azure_blob}` allowlist on
  `BackupTargetCreate.kind` with a registry-derived
  `_valid_kinds()` so future drivers don't need a third-place
  edit.

- **Backup & restore — selective restore (issue #117 Phase 2b).**
  Operators tick which sections to restore (IPAM only, DNS only,
  etc.); the rest of the install stays untouched. Available on
  both the upload-restore + restore-from-destination flows.
  Section catalog (17 sections, 110 tables) doubles as the
  factory-reset section catalog. Per-section flow: `TRUNCATE
  TABLE … RESTART IDENTITY CASCADE` for the selected sections'
  tables, then `pg_restore --data-only --disable-triggers
  --table=<each>` to re-load just those tables. `platform_internal`
  (alembic_version + oui_vendor) always rides along — the
  schema-head pin + OUI cache are install-state, not user-data.
  CASCADE is documented in the UI warning: cross-section FK
  rows in non-selected sections that reference wiped data also
  get cleared. Volatile sections (DHCP leases, DNS query log,
  DHCP activity log, nmap scan history, metric samples, Celery
  scratch) stay unticked by default but operators can opt in for
  a diagnostic restore. The shared `BackupSectionsPicker`
  component drives both the upload-restore modal and the
  restore-from-destination modal. UI auto-ticks every
  non-volatile selectable section the first time the operator
  flips into selective mode.

- **Backup & restore — Phase 2a foundation (issue #117).**
  Section catalog primitive + custom-format dumps. Switches
  `_run_pg_dump` from `--format=plain` to `--format=custom`
  (the format `pg_restore --table=` knows how to walk
  selectively); manifest carries `format_version: 2` +
  `dump_format: "custom"` so Phase 1 plain-format archives stay
  restorable through the auto-detection path. Member name
  changes from `database.sql` → `database.dump`; restore-side
  reader does `manifest.dump_format` first, falls back on
  member-name sniffing for older archives. Section catalog at
  `app/services/backup/sections.py` maps all 110 schema tables
  to 17 logical sections (auth, audit, settings, ownership,
  network_modeling, ipam, dns, dhcp, integrations, ai,
  backup_self, diagnostics, logs, metrics, leases, nmap_history,
  platform_internal). `volatile=True` flag on diagnostic
  sections (default-excluded from selective backups);
  `selectable=False` on `platform_internal` (always rides
  along). Startup verification:
  `assert_catalog_covers_models` — 110 tables in catalog, 0
  missing.

- **Backup & restore — proxy archive download from any
  destination (issue #117).** `GET /backup/targets/{id}/archives/
  {filename}/download` streams `driver.download(filename)`
  straight back to the operator's browser as a zip download.
  Works the same way for every destination kind (S3 / SCP /
  Azure / SMB / FTP / GCS / WebDAV / local volume) — the
  driver's existing `download(filename)` method does the heavy
  lifting; the endpoint wraps the bytes in a `StreamingResponse`
  with `Content-Disposition: attachment`. Operators can pull a
  remote archive to a laptop without giving them the
  destination's credentials.

- **Operator Copilot — backup + factory-reset read tools (issues
  #117 + #116).** Three read-only MCP tools, all superadmin-only:
  `list_backup_targets` (every configured target with last-run
  state, schedule, retention; `config` blob omitted from output
  so destination credentials stay out of the LLM context),
  `list_backup_archives_at_target` (calls the driver's
  `list_archives` so the result matches the Backup admin
  Archives drawer; resolves target by id-or-name; destination
  errors returned in-band), `find_backup_audit_history`
  (windowed timeline of `backup_created` /
  `backup_target_run_success` / `-run_failed` / `backup_restored`
  / `factory_reset_performed` audit rows, default 7d). No
  `propose_*` write tools — restore + factory-reset are
  password-gated + confirm-phrase-gated by design; inserting an
  LLM intermediary into "should I restore?" adds friction
  without value, and `propose_create_backup_target` involves
  pasting destination credentials, which doesn't fit a
  chat-driven flow. Operators reach for the Backup admin page
  when they're about to mutate state.

- **Deployment topologies guide.** New `docs/deployment/
  TOPOLOGIES.md` walks six production topologies with
  hand-authored SVG diagrams + sizing notes: (1) single VM
  (homelab); (2) control
  plane + separated DNS/DHCP appliances via the existing
  `docker-compose.agent-{dns,dhcp}.yml` files; (3) DNS + DHCP
  HA pairs (server groups + Kea HA); (4) HA control plane
  (Patroni + Redis Sentinel + multi-replica api/worker); (5)
  hybrid cloud (control plane in cloud, on-prem agents reaching
  back over HTTPS via VPN/WireGuard/Tailscale, with cloud-
  component matrix for AWS/Azure/GCP); (6) Kubernetes via the
  umbrella Helm chart. Plus closing sections on backup +
  factory-reset semantics across topologies and an honest
  "what's NOT supported yet" list (active-active across
  regions, multi-tenant control plane, read replicas as query
  routes). Cross-linked from `README.md`,
  `docs/deployment/DOCKER.md`, and `CLAUDE.md`'s doc map.

- **Diagnostics — captured uncaught exceptions + admin viewer
  + Submit-bug button (issue #123).** Replaces "tail docker
  compose logs and hope you catch the traceback" with a
  queryable surface. New `internal_error` table — 16 columns
  with a fingerprint (sha256 of class + top-2 frames) for
  dedup, `occurrence_count` + `last_seen_at` to bump noisy
  crashes instead of inserting fresh rows, `acknowledged_by`
  / `acknowledged_at` for ack state, `suppressed_until` for
  the "silence this for a day" flow, plus indexes on
  timestamp / service / fingerprint and a partial unacked
  index. `app.services.diagnostics.capture` walks request
  headers (`Authorization` / `Cookie` / `X-API-Token`
  stripped) + payload bodies (anything matching
  `password|secret|token|key|credential` redacted),
  truncates bodies > 4 KB and the whole `context_json` blob
  > 16 KB, then either bumps the matching fingerprint row or
  inserts fresh. Async + sync entry points share the same
  shape — async for FastAPI, sync for Celery. New
  **Admin → Diagnostics → Errors** page lists rows
  newest-first with per-row Acknowledge / Suppress (1h / 1d
  / 1w) / Delete actions; **Submit-bug** button collects the
  redacted traceback + context into a pre-filled GitHub-issue
  template URL operators copy-paste into the tracker. Daily
  prune sweep (`internal_error_prune` Celery beat task) drops
  rows older than the configured retention window so the
  table doesn't grow unbounded.

- **BGP enrichment — RIPEstat + PeeringDB (issue #122).** The
  control plane already tracked the *registry* side of an ASN
  (WHOIS / RDAP holder + RPKI ROAs); this adds the
  *routing-table* side. Three pieces:

  * **RIPEstat client** — async httpx fetch for
    `announced-prefixes` / `prefix-overview` / `routing-history`
    / `as-overview`. 6 h in-process TTL cache. Soft-failure
    shape (`{available: false, error: "..."}`) so operators
    behind tight egress get a useful message instead of a
    crashed page. PeeringDB 404 mapped to "not registered" not
    "unreachable" (only ~30 k of ~80 k allocated 16-bit ASNs
    have PeeringDB records).
  * **PeeringDB client** — async httpx fetch for `net`
    (registered network record) and `netixlan` (IXP
    membership). 24 h TTL.
  * **REST surface** — six endpoints under `/api/v1/bgp/*`
    surfacing the normalised shapes (`announced-prefixes`,
    `prefix-overview`, `routing-history`, `as-overview`,
    `peeringdb-net`, `peeringdb-ixps`). Authenticated, not
    RBAC-gated — the underlying data is public; the auth
    requirement just keeps the cache from being abused by
    anonymous traffic.
  * **Five new MCP tools** for the Operator Copilot —
    `bgp_announced_prefixes`, `bgp_prefix_overview`,
    `bgp_routing_history`, `bgp_as_overview`,
    `bgp_peeringdb_summary`. Default-enabled.
  * **BGP Footprint tab** on the ASN detail page — three
    stacked sections (announced prefixes filterable + capped
    at 1000 with refine-the-filter hint, peering profile from
    PeeringDB `net` with policy badge + IRR AS-set +
    looking-glass + website, IXP presence from `netixlan`
    grouped by IX + city with humanised speed). Private ASNs
    (`kind=private`) skip the queries with a clear "no public
    BGP footprint" empty state.

- **Operator Copilot — `ask_yes_no` tool with chat-suspend
  semantics (issue #120).** When the model needs a binary
  answer ("continue with the deletion?", "include disabled
  scopes?"), it calls a new `ask_yes_no` tool. The chat drawer
  renders Yes / No buttons in place of the raw tool result;
  the operator clicks one and the answer feeds the next user
  turn — no typing, no extra round-trip burned on parsing
  "yes please". Implemented via the existing `propose_*`
  short-circuit pattern: tool returns a structured
  `kind: "yes_no_question"` payload, orchestrator's round
  loop short-circuits the moment that result lands (no
  further LLM call this turn), frontend pattern-matches on
  `kind` and renders a `YesNoCard`, click fires a normal user
  message which resumes the loop on the next turn with the
  answer in context.

- **Operator Copilot — visible context chip + per-call
  default prompt prefill (issue #119).** Operators reported
  clicking "Ask AI about this …" affordances and seeing the
  drawer open with a blank empty state. Two changes close the
  perception gap: (a) **context chip** above the composer —
  amber-tinted strip "Asking about: <one-line context>" with
  a × button to drop the seed, so the operator sees what the
  model is being told and can dismiss before sending; (b)
  optional `prompt` prop on `AskAIButton` forwards into the
  existing `askAI({ context, prompt })` plumbing so the
  composer textarea pre-fills with a tailored default
  question (operator can edit before sending — never
  auto-sent). Wired per-resource at every call site (IPAM
  subnet → "Tell me about this subnet — utilisation, recent
  changes, …", DNS zone → records summary, DHCP scope →
  pool / static / lease summary, alerts row → "Why did this
  fire?", audit row → "What changed and who did it?", etc.).

- **Tag-filter system + autocomplete + chips across the UI
  (issue #104, multi-phase).** A platform-wide tag surface
  that turns the existing `tags JSONB` column on every
  resource into something operators can actually filter and
  navigate by. Phases:

  * **Phase 1 — `?tag=` filter on every REST list endpoint.**
    Multi-tag AND/OR semantics via repeated `?tag=`
    parameters. Applies across IPAM (spaces / blocks / subnets
    / IPs), Network modeling (ASNs / VRFs / circuits /
    services / overlays / customers / sites / providers),
    plus the DNS / DHCP surfaces in Phase 4.
  * **Phase 2 — autocomplete endpoints** at
    `/api/v1/tags/autocomplete?prefix=…&kinds=…` returning the
    union of distinct tags across the requested resource
    kinds, ranked by occurrence count. Drives the chip
    autocomplete in modals + filter bars.
  * **Phase 3a — ASNs page** gets the tag-filter chip bar +
    autocomplete first as the reference implementation.
  * **Phase 3b — six remaining network-modeling pages**
    (VRFs / circuits / services / overlays / customers /
    sites / providers) gain the same chip-bar pattern.
  * **Phase 4 — DNS + DHCP tags.** `tags JSONB` columns added
    to `dns_zone` / `dns_record` / `dhcp_scope` / `dhcp_pool`
    / `dhcp_static_assignment`; `?tag=` filter wired through
    every DNS + DHCP REST list endpoint.
  * **Phase 4b — DNS zones list + DHCP scopes list** render
    tag chips inline.
  * **Phase 5a — IPAM block-detail subnet list** renders tag
    chips on every subnet row.
  * **Phase 5b — SubnetDetail address table** renders tag
    chips on every IP row.
  * **Phase 5c — clickable tag pills on IPDetailModal** —
    click any tag to navigate to a filtered IPAM view of every
    resource carrying that tag.

  Migration `b7e29c4f5d18_dns_dhcp_tags.py` adds the JSONB
  columns + GIN indexes for fast `?tag=` lookups.

- **Dashboard tabs — Network + Integrations + Security
  (issues #107 / #108 / #109).** Three new dashboard tabs
  alongside the existing Overview / IPAM / DNS / DHCP set,
  each backed by a single rollup endpoint under a new
  `/api/v1/dashboards/` package — one query per tab, one
  refresh tick.

  * **Network (#107)** — `/dashboards/network/summary`
    aggregates ASN drift count + top-N drifted, RPKI ROA
    expiring + expired, circuits past `term_end_date` +
    suspended/decom (deduped into one alerts panel),
    service-catalog rows with at least one orphan resource,
    overlay networks impacted by any down circuit. 6 KPI
    cards + 4 detail-list cards laid out 2×2, refresh every
    60 s, click-throughs to the canonical pages.
  * **Integrations (#108)** —
    `/dashboards/integrations/summary` shows per-mirror
    counts (K8s clusters / Docker hosts / Proxmox nodes /
    Tailscale tenants / UniFi controllers) with last-sync
    staleness + any reconciler error counts surfaced from
    the audit log.
  * **Security (#109)** — `/dashboards/security/summary`
    surfaces account lockout state (locked accounts +
    failures-in-window), active session count + sessions by
    auth source, pending API token expirations, audit-chain
    verification status (last verifier run + result), MFA
    enrolment ratio across local users.

- **Compliance + Conformity dashboard tabs.** Two more
  sub-tabs on the main dashboard surfacing the existing
  `compliance_change` (#105) + conformity-evaluations (#106)
  work front-of-house instead of buried under `/admin`.
  Compliance tab: three KPI cards for the classification
  flag counts (PCI / HIPAA / internet-facing) +
  click-through to `/admin/compliance`. Conformity tab:
  per-framework status cards (PCI-DSS / HIPAA / SOC2) with
  pass / warn / fail tallies + the latest auditor PDF
  download link.

- **Password policy enforcement (issue #70).** Configurable
  complexity / history / max-age rules applied to every
  local-auth password set. Seven new `platform_settings.
  password_*` knobs (min length, per-class requirements
  upper / lower / digit / symbol, history depth, max age in
  days). Defaults are deliberately permissive so an upgrade
  doesn't suddenly invalidate working passwords; operators
  tighten in **Settings → Security → Password Policy**. Two
  new `user.*` columns: `password_changed_at` (backfilled
  to `created_at` for existing users) and
  `password_history_encrypted` (Fernet over a JSON list of
  prior bcrypt hashes, capped at the configured depth).
  New `app.services.password_policy` module with pure
  validate / history / max-age helpers — same code path on
  self-service change, admin reset, create-user. Public
  `GET /auth/password-policy` so the change-password form
  renders rule hints client-side and shows ✓ as the user
  types. Login flow flips `force_password_change` when an
  existing user's password is older than
  `password_max_age_days` (0 = off). Server returns
  `{detail: {reason, errors[]}}` on policy / history
  violation so the UI surfaces every failed rule in one
  pass.

- **Account lockout after N failed logins (issue #71).**
  Windowed-counter lockout for local-auth users.
  `user.failed_login_count` + `failed_login_locked_until` +
  `last_failed_login_at` track rolling state; reset on any
  successful login or via superadmin **POST
  /users/{id}/unlock**. Three new
  `platform_settings.lockout_*` knobs: threshold (0 disables
  — default 0 so an upgrade never locks anyone out),
  duration in minutes, rolling-window reset minutes. So 5
  fails inside 15 min trips the lock; 1 fail every 16 min
  never accumulates. Login flow short-circuits with HTTP 403
  before checking the password while the lock is live, so an
  attacker hitting a locked account doesn't learn whether
  the candidate password would have worked.
  `account.locked` + `account.unlocked` audit actions land
  as distinct rows from the underlying failures.
  External-IdP / RADIUS / TACACS+ users are unaffected —
  rate-limited at the upstream provider, not here. Settings
  surface: **Security → Account Lockout**; Users page shows
  a "locked" pill + 🔓 unlock button on rows.

- **Active session viewer + force-logout (issue #72).**
  Every login + refresh already created a `UserSession`
  row; this lands the live JWT registry the operator can
  browse and revoke from. Access tokens now carry a `jti`
  claim equal to the session row's UUID;
  `get_current_user` looks up the session by `jti` on every
  request and rejects when `revoked=True` or `expires_at`
  has passed — so flipping `UserSession.revoked = True` 401s
  the in-flight access token on its next call. Tokens
  minted before this landing carry no `jti` and stay valid
  until their short TTL expires (rolling-deploy compat).
  `user_session.auth_source` mirrors the provider this
  session was minted against (local / OIDC name / SAML name
  / etc.) so the viewer shows "Logged in via Okta" without a
  join. `last_seen_at` is bumped on each authenticated
  request, throttled to 60 s per session. Composite
  `(revoked, expires_at)` index covers the hot lookup path.
  New `/api/v1/sessions` router: `GET /sessions/me` (current
  user's sessions, any user), `GET /sessions` (all sessions,
  superadmin), `POST /sessions/{id}/revoke` (any user
  revoking their own; superadmin revoking anyone's). New
  **Admin → Sessions** page lists active sessions with
  per-row Force-logout button.

- **Tamper-evident audit chain hash + nightly verifier
  (issue #73).** Every `audit_log` row now carries `seq` +
  `row_hash` + `prev_hash` so the audit trail forms a
  verifiable chain — any post-hoc edit / insert / delete
  leaves a detectable break. `seq` is `bigserial`; the
  runtime hasher orders rows by `(timestamp, id)` and looks
  up `prev_hash = row WHERE seq = MAX(seq)` inside a
  Postgres transaction-scoped advisory lock so concurrent
  inserts can't fork the chain. `row_hash = sha256(prev_hash
  || canonical_json(row))` over every content column (id,
  timestamp, who, what, state-change, correlation, result);
  `seq` itself is NOT in the payload — chain position, not
  content. Hooked into the global `before_flush` event so
  every audit row goes through the same path (async or
  Celery `task_session()`). DB-level `BEFORE DELETE`
  trigger raises an exception so even a superuser can't
  silently snip a row to "fix" a break. The migration
  backfills the entire existing table in `(timestamp, id)`
  order so the chain is contiguous from row 1 — operators
  don't start over with a fresh chain on upgrade.
  `app.services.audit_chain.verify_chain` walks the table
  in `seq` order, recomputes each row's hash, and returns
  the first break with `reason=row_hash_mismatch` (someone
  edited content) or `reason=prev_hash_mismatch` (someone
  deleted or inserted a row mid-stream). Beat-driven nightly
  verifier writes the result to a metric for the new
  `audit_log_immutable` conformity check kind to surface in
  the auditor's PDF.

- **Operator Copilot — Tool Catalog infrastructure + 10 new
  tools (issue #101).** The catalog framework that the
  later Tier 2-5 waves layered onto. New
  `Tool.default_enabled: bool` flag on every registered tool
  — niche / network-egress / write tools ship as `False`,
  so operators opt in. New
  `platform_settings.ai_tools_enabled: list[str] | None`
  (migration `e5a18c40729b`): `NULL` = use registry
  defaults; non-NULL = exactly these tools regardless of
  declared default. Per-provider `AIProvider.enabled_tools`
  continues to narrow further — both layers compose.
  `effective_tool_names()` helper centralises the layering
  so every code path (system-prompt builder, tool-schema
  list to the LLM, registry dispatch gate) reads from one
  source of truth. `ToolDisabled` exception + registry-side
  gate so a hallucinating model that calls a disabled tool
  gets a clear "ask your admin to enable this tool" error.
  Plus 10 new tools landing alongside the catalog: `find_ip`
  vendor enrichment from `oui_vendor`, `find_dhcp_leases`
  vendor enrichment, `find_switchport`, `ping_host`,
  `tls_cert_check`, `lookup_whois_*` trio, `query_logs`
  inventory, plus a `help_write_permission` self-service
  helper. **Admin → AI → Tools** page renders the catalog
  with per-tool toggle + category grouping + write/read
  badges.

- **Operator Copilot — MAC vendor rollup tools + composer
  history walk.** Two new read-only tools:
  `count_devices_by_vendor` (vendor → count buckets with
  optional substring filter) and `find_devices_by_vendor`
  (the actual rows behind those counts), pulling from IPAM,
  active DHCP leases, or the deduplicated union. Short-
  circuit cleanly when OUI lookup is disabled in platform
  settings. Default-enabled in the registry; show up
  disabled for operators with an explicit allowlist saved
  (the catalog never auto-adds new tools to a curated list
  — that's the upgrade-safety contract). Plus chat composer
  ↑/↓ now walks the full user-message history of the active
  session, not just the last one — ↑ from an empty textarea
  recalls the newest, subsequent ↑ steps older, ↓ steps
  newer, ↓ past the newest returns to draft mode.

- **Operator Copilot — resizable + draggable Gmail-style
  bubble.** Replaced the right-edge full-height drawer with
  a Gmail-compose-style bubble that doesn't block the page
  behind it. Operators can keep the chat open while
  clicking around IPAM / DNS / DHCP and drag it out of the
  way. Defaults to 440 × 680 anchored to the lower-right
  corner; position + size persist across close/reopen via
  sessionStorage. Title bar is the drag handle (clicks on
  buttons / inputs don't drag). Top-left corner carries a
  resize handle. Min 320 × 320, max `viewport - 24px`,
  clamped on every render so a stale geometry from a
  different monitor never paints off-screen. Empty-state
  prompt list switched from a flat 6×4-5 enumeration to a
  category dropdown so the bubble doesn't have to scroll
  forever.

- **UniFi mirror — VLANs + Router on top of Phase 1.** The
  reconciler now creates one `router` row per controller
  and one `vlan` row per UniFi network with an 802.1Q tag,
  then points each mirrored Subnet's `vlan_ref_id` (and the
  denormalised integer `vlan_id`) at the matching VLAN. The
  IPAM page's VLAN column lights up automatically; the
  VLAN page shows each UniFi tag under a `<controller-name>`
  Router. Lifecycle: Router is keyed by
  `unifi_controller_id` (new cascade FK) — deleting a
  controller cascades the Router and any VLANs that were
  only ever attached via it. Migration
  `c3e8b57a2f14_unifi_router_link.py` carries the new FKs.

- **UniFi per-controller detail page.** Clicking a
  controller name on the dashboard or the UniFi list page
  now opens `/unifi/:id` — a focused dashboard for that one
  controller. Reads from a new single-shot endpoint `GET
  /unifi/controllers/{id}/dashboard` that joins the
  controller header + every IPAM row owned by the
  controller (subnets / addresses) + every VLAN under its
  auto-created Router + the latest discovery snapshot in
  one round-trip. Three tabs: Subnets / VLANs / Clients.

- **UniFi panel on both dashboard surfaces.** Per
  CLAUDE.md non-negotiable \#15 (new integrations show up
  on the Dashboard — both surfaces): the
  `IntegrationsPanel` inside the IPAM tab on the main
  dashboard now lists UniFi as the fifth column alongside
  Kubernetes / Docker / Proxmox / Tailscale (controller
  name, mode-aware endpoint hint, "N sites · N nets" meta,
  last-sync staleness dot); and the dedicated Integrations
  dashboard tab's `/dashboards/integrations/summary` rollup
  + `_INTEGRATION_RESOURCE_TYPES` registry now include
  UniFi so reconciler error-audit rows surface in the
  recent-errors list there too.

- **Build agents emit BIND9 + Kea versions.** Both DNS and
  DHCP agent Dockerfiles now write
  `/etc/spatiumddi-versions` after `apk add` — a small
  key=value file with `alpine_release` plus every installed
  bind* / kea* package version. Format is grep-friendly +
  sortable; the file ships in the image so operators can
  `docker run --rm --entrypoint cat … /etc/spatiumddi-versions`
  to confirm what's actually inside a given image tag (the
  same image tag built three months apart could carry
  different package minor versions depending on Alpine's
  patch cycle). Release workflow's "Create GitHub Release"
  job pulls the just-published images, reads the versions
  file, and renders a "Bundled BIND9 / Kea versions"
  section into the release body so operators chasing a CVE
  can pin to the exact upstream patch level.

- **Backup & restore — Phase 1 finale (issue #117).** Two
  end-of-Phase-1 polish items: tabbed Backup admin page +
  restore-from-destination. The page splits into **Manual** (one-off
  download + restore-from-uploaded-file) and **Destinations**
  (configure local volumes / S3 / SCP / Azure Blob, run them on
  cron, restore from any archive at any destination). The
  per-archive drawer in the destinations tab gets a Restore icon
  alongside Delete: click → confirmation modal → server fetches
  the archive bytes from the destination via a new
  ``download(config, filename)`` method on every driver,
  decrypts ``secrets.enc`` with the operator-typed passphrase,
  takes the standard pre-restore safety dump, replays via
  ``psql --single-transaction``, writes the post-replay
  ``backup_restored`` audit row in the freshly restored DB. New
  endpoint: ``POST /backup/targets/{id}/archives/restore``
  (``filename`` / ``passphrase`` / ``confirmation_phrase``;
  superadmin-gated; same wrong-passphrase + wrong-confirmation
  rejection semantics as the Phase 1a upload-restore endpoint).
  Verified end-to-end against a local-volume target — wrong
  passphrase + wrong confirmation both reject cleanly, happy
  path restored a 2.7 MB archive in 2.8 s, api stays healthy
  post-replay.

- **Backup & restore — Phase 1d (issue #117).** Two more
  destination drivers slot into the Phase 1c scaffolding,
  closing out the Tier 1 destination set called for in the
  issue body. ``scp`` writes archives via SFTP using
  paramiko — supports password OR PEM private-key auth (with
  optional key passphrase), three host-key check modes
  (``strict`` / ``known_hosts`` / ``insecure_skip``), and
  always uses a temp-then-rename pattern so a crashed
  transfer never leaves a half-archive the listing pass picks
  up. ``azure_blob`` writes to a container via
  azure-storage-blob — supports both classic shared-key auth
  (account name + key) and connection-string auth. Both
  drivers reuse the same archive-name regex + secret-field
  machinery as ``s3``, so the operator UX is consistent
  across destinations: every secret field
  Fernet-wrapped at rest, redacted to ``<set>`` on read,
  preserved across PATCH-without-rotate. Verified end-to-end
  against linuxserver/openssh-server (SCP) and Azurite (Azure
  Blob Storage) one-shot containers; both build, write, and
  list a real archive in under a second on a LAN. paramiko
  4.0 + azure-storage-blob 12.28 added to ``pyproject.toml``
  runtime deps. Total destination kinds now four:
  local_volume / s3 / scp / azure_blob.

- **Backup & restore — Phase 1c (issue #117).** Adds the
  ``s3`` destination kind. One driver covers AWS S3 plus
  every S3-compatible service via the optional ``endpoint_url``
  config field — MinIO, Wasabi, Backblaze B2, Cloudflare R2,
  DigitalOcean Spaces, Linode Object Storage. Config carries
  bucket / region / optional key prefix / endpoint URL /
  addressing style (virtual / path / auto) / access key ID /
  Fernet-wrapped secret access key. boto3 joins the dep set;
  every driver method wraps the sync client calls in
  ``asyncio.to_thread`` so a slow upstream can't block the
  event loop. Test connection writes + heads + deletes a probe
  object so it distinguishes auth failure from no-such-bucket
  cleanly. Implements per-driver secret-field handling via a
  new helper module
  (:mod:`app.services.backup.targets.secrets_config`) — fields
  declared ``secret=True`` get Fernet-wrapped before storage
  with an ``__enc__:`` prefix, redacted to ``<set>`` on every
  read, and merged on PATCH so an operator updating bucket
  metadata doesn't have to retype the secret. The frontend
  recognises secret fields by their flag, never pre-fills them
  on edit, and shows ``(set — leave empty to keep, type to
  replace)`` as the placeholder. Verified end-to-end against
  a MinIO container: create target, test connection, run-now
  writes a real archive, PATCH-without-secret leaves the
  existing wrap intact and the next run succeeds.

- **Backup & restore — Phase 1b (issue #117).** Builds on
  Phase 1a with scheduled, retention-managed backups to
  configurable destinations. New `backup_target` table backs
  one row per destination (Phase 1b ships `local_volume`; the
  same row + driver registry serves S3 / SCP / Azure in 1c /
  1d without schema changes). Each target carries: a
  Fernet-encrypted backup passphrase (so scheduled runs don't
  re-prompt), an optional 5-field UTC cron expression
  (`croniter`-parsed; manual-only when omitted), retention
  (mutually-exclusive `keep_last_n` or `keep_days`), and full
  last-run telemetry (status / filename / bytes / duration_ms
  / error). A 60 s celery-beat sweep
  (`app.tasks.backup_sweep.sweep_backup_targets`) walks every
  enabled target whose `next_run_at` is now in the past, fires
  `run_backup_for_target`, recomputes `next_run_at`, and
  records an audit row — `last_run_status = "in_progress"`
  acts as the per-target mutex so a slow run can't double up
  on the next tick. New REST surface at
  `/api/v1/backup/targets/*`: list / get / create / patch /
  delete / `run-now` (synchronous; same path the schedule
  uses) / `test` (write+verify+delete probe) / list-archives /
  delete-archive. New `BackupTargetsSection` on the Backup
  admin page renders configured destinations with last-run
  state + per-row Run / Test / Edit / Delete + an expandable
  archives drawer. The api / worker images now mount a named
  `spatium_backups` docker volume at
  `/var/lib/spatiumddi/backups` (the default `local_volume`
  config path) — the Dockerfile pre-creates + chowns the path
  so the volume inherits app-user ownership on first mount.
  `croniter>=3.0.0` joins the dep set. New migration
  `d2a8e417b9f3` adds the `backup_target` table + a partial
  index on `next_run_at WHERE enabled AND schedule_cron IS NOT
  NULL` so the beat sweep query stays cheap regardless of row
  count.

- **Backup & restore — Phase 1a (issue #117).** The "download a
  snapshot of my install" surface operators have been asking for.
  New page at `Administration → Backup`. Two cards:
  *Create + download* runs `pg_dump --format=plain --no-owner
  --no-privileges --clean --if-exists` against the live database
  and bundles the SQL dump alongside `manifest.json` (app version,
  schema head, hostname, created_at) and `secrets.enc` (the source
  install's SECRET_KEY, passphrase-wrapped via PBKDF2-HMAC-SHA256
  600 k iterations + AES-256-GCM with a fresh per-backup salt + nonce)
  into a single zip archive that streams back via
  `Content-Disposition: attachment` (same shape as the conformity
  PDF export). *Restore from file* takes an uploaded zip + the
  passphrase + a typed `RESTORE-FROM-BACKUP` confirmation, takes
  a pre-restore safety dump under
  `/var/lib/spatiumddi/backups/pre-restore-{ts}.zip`, replays the
  archive's SQL via `psql --single-transaction` so failures roll
  back atomically, and writes a fresh audit row in the *restored*
  database so the trail of evidence survives the wipe. Manifest
  version + format checks reject archives newer than the running
  build with a clear "upgrade SpatiumDDI before restoring this
  archive." Wrong-passphrase yields `BackupCryptoError` (raised
  from cryptography's `InvalidTag`) before any destructive step
  runs, so you can never lose data to a typo. Both endpoints are
  superadmin-gated; the entire flow is audited. The README upgrade
  section now leads with "Take a backup before upgrading." Phase
  1a deliberately does **not** ship remote destinations
  (S3 / SCP / Azure / SMB / FTP / GCS) or scheduled backups —
  those are tracked in follow-up issues so the local-only path
  could ship cleanly. The api image gains `postgresql-client` so
  `pg_dump` + `psql` are available at runtime.

- **VoIP OUI vendor enrichment — Phase 3 (issue #112).** Closes
  the #112 phasing. The existing IEEE OUI lookup now flips an
  `is_voip_phone` boolean on every IPAddress / DHCPLease /
  switchport response when the vendor name matches the curated
  VoIP-phone vendor list (Polycom / Yealink / Mitel / Aastra /
  Avaya / Snom / Grandstream / Cisco SPA / Cisco-Linksys /
  AudioCodes / Sangoma / Digium / Spectralink / Fanvil /
  Obihai / Htek / Panasonic Communications). Substring,
  case-insensitive — handles registry-string drift like
  ``Polycom`` vs ``Polycom, Inc.`` and rebrands like
  Aastra → Mitel. Generic Cisco strings are deliberately *not*
  matched (Cisco's OUIs span both routers and phones; operators
  on CallManager use option-150 fences in the phone profile
  instead). Frontend renders a sky-blue Phone icon next to the
  MAC in the IP detail modal, the IPAM IP table, and the DHCP
  lease table — operators spot "16 Polycoms, 4 Yealinks, 2
  random laptops" without scanning OUI strings. MCP `find_ip`
  / `find_dhcp_leases` / `find_switchport` tools now carry
  `is_voip_phone` on every match so the Operator Copilot answer
  can prefix with a phone glyph too. 31 new tests lock the
  matcher behaviour for every curated vendor + a representative
  set of non-phone vendors that must not trip the flag.

- **VoIP voice-segment metadata — Phase 2 (issue #112).** Flips
  voice-VLAN tagging from passive UI labelling into a real audit
  signal. Five additive surfaces:
  - **`Subnet.subnet_role` enum column** — `data` / `voice` /
    `management` / `guest` (NULL = unspecified). Pure metadata
    — no Kea / BIND driver behaviour change. Migration
    `f1a3b8c52d04` with a partial index over the column for
    role-filter queries.
  - **`voice_segment_not_internet_facing` conformity check** —
    fails when a voice-tagged subnet is also flagged
    `internet_facing` (almost always a misconfiguration —
    phones should be inside a private VRF / NAT'd through the
    SBC). Plus a built-in disabled seed policy. The conformity
    target-filter resolver now also recognises
    `subnet_role: <role>` so future policies can scope to any
    network role.
  - **`voice_lease_count_below` alert rule type** — counts
    active DHCP leases on every voice-tagged subnet and fires
    when the count drops below the operator's threshold (set
    to ~50% of the expected fleet size to catch
    mass-disconnect events without firing on routine reboots).
    Reuses the existing `threshold_percent` column as a raw
    count threshold for this rule type.
  - **IPAM page role filter chips** — multi-select chips next
    to the existing tag filter narrow the subnet tree to
    selected roles. Empty selection = all roles (no filter).
  - **VLAN page chip rendering** — the per-VLAN subnet table
    grows a Role column with a sky-blue Voice chip,
    violet Management chip, and amber Guest chip so an operator
    can spot voice VLANs at a glance.
  - Subnet edit/create modals get a Network role select inside
    the existing Compliance classification section. 6 new
    tests cover the conformity check (pass / fail /
    not_applicable) and the alert evaluator (fires below
    threshold / silent at threshold / ignores non-voice subnets).

- **VoIP phone profiles — Phase 1 (issue #112).** Reusable
  vendor-class-fenced DHCP option recipes for VoIP phones. Three
  pieces ship together:
  - **Curated VoIP options catalog** (`dhcp_voip_options.json`,
    `GET /dhcp/voip-options`) — 9 vendors (Polycom / Yealink /
    Cisco SPA / Cisco IP Phone / Mitel / Avaya / Snom /
    Grandstream / Aastra) with the DHCP options each vendor's
    phones look for at boot (option 66 / 150 / 132 / 160 / 161 /
    176 / 242 / 43 sub-options).
  - **`DHCPPhoneProfile` + M:N scope attachment** — a profile is
    a `vendor_class_match` (option-60 substring) plus an
    `option_set` JSONB list of `{code, name, value}`. The same
    profile attaches to multiple voice VLANs without duplication
    via `dhcp_phone_profile_scope`. CRUD at
    `/dhcp/server-groups/{group_id}/phone-profiles` with
    starter-pack seeding at `…/seed-starter-pack` (creates
    disabled profiles for all 9 vendors with placeholder
    `CHANGE-ME` values; idempotent).
  - **Kea driver render path** — the assembler walks every
    profile attached to any of the bundle's scopes and emits one
    `client-class` per enabled profile with the vendor-class
    fence as the `test` expression. Vendor options Kea doesn't
    know by name (160 / 161 / 242 / etc) render with `"code": NN`
    form rather than tripping a load-time error. Phone classes
    sit in Dhcp4 client-classes alongside regular + PXE classes.
  - **Frontend Phone Profiles tab** on the Kea-managed group
    page (added in #113), with vendor-grouped picker that auto-
    fills the option set when the operator selects a curated
    vendor, plus M:N scope-attachment checklist showing each
    scope's CIDR + name. Only renders on Kea-managed groups —
    Windows-DHCP groups don't see the tab.
  - **MCP `list_phone_profiles`** for the Operator Copilot —
    rolls up scope-attachment counts in one query rather than
    walking per-row. Migration `e4d8c2a91f7b`. Phase 2 (subnet
    voice-segment metadata + filter + alert rule + conformity
    check) and Phase 3 (OUI vendor enrichment for phones) stay
    deferred per the issue's phasing.

- **DHCP group-centric UI — Kea servers only (issue #113).** The
  scopes / pools / static assignments / client classes / option
  templates / MAC blocks tabs now live on the **group detail
  page**, not the per-server detail page, for groups with at
  least one Kea member. The data model has been group-scoped
  since `2026.04.21-2`; this aligns the UI with that shape so
  operators stop being misled by "edit scope on kea-1 → why did
  it change on kea-2 too" (it always did, the UI just looked
  per-server). Kea server detail page narrows to per-instance
  surfaces (Leases / History / Server Options) plus a banner
  pointing back to the group: `Configuration is managed at the
  group level — Open group →`. Tab state on the group page is
  per-group sessionStorage so navigating between groups doesn't
  bounce off the active tab. Server list inside the group's
  Servers tab is now click-through-able to drill into a peer.
  **Windows DHCP servers are explicitly out of scope** — every
  Windows-DHCP server detail page keeps all of its existing
  tabs exactly as before; the gate is `kea_member_count > 0`
  on the parent group, and groups are single-vendor today.

- **Aggregation candidates — passive badge with per-candidate
  snooze (issue #114).** The inline "Aggregation suggestions"
  banner that crowded the IPAM page on every load is replaced
  with a small badge button in the block header — click expands
  a popover showing the same candidate set with two new
  per-row actions: **Snooze 30 days** (re-appears after the
  timer) and **Don't suggest again** (permanent, operator-flagged
  "I know, leave me alone"). Snooze entries persist in
  `platform_settings.aggregation_snooze` JSONB keyed on a stable
  hash of parent block + sorted child CIDRs, so a snooze still
  matches the same candidate even if `collapse_addresses` returns
  the children in a different order on a later pass. Filtered
  server-side by default; the popover surfaces a "Show snoozed"
  toggle so operators can revisit and Re-enable. Per-session
  expand/collapse keyed on block id, so an operator working
  through suggestions doesn't re-click on every nav. Migration
  `d9e4c12a7f85`.
- **Dashboard IPAM-tab IP-space filter (issue #115).** Multi-select
  pill on the Dashboard's IPAM tab that scopes every space-aware
  card to the selected spaces — IPv4/v6 split, capacity headroom,
  utilization KPIs, subnet heatmap, top-subnets list, and the
  shared KPI grid all narrow to the selection. Default "All
  spaces"; persisted per session in `sessionStorage` keyed on
  `spatium.dashboard.ipam.space_filter` so a refresh / drawer
  toggle / nav-away-and-back keeps it. Pill is IPAM-tab-only —
  switching to Overview / DNS / DHCP shows global numbers
  unfiltered, which sidesteps the "the filter looks broken on
  other tabs" trap. Non-IPAM-shaped surfaces on the IPAM tab
  (the Integrations panel) carry a small "(not space-scoped)"
  annotation when the filter is active so operators understand
  the scope. Backend: `/ipam/subnets` `space_id` query param now
  accepts repeated values (`?space_id=A&space_id=B`) — single-id
  callers still work since FastAPI's repeated-key parsing returns
  a 1-element list. axios's `paramsSerializer: { indexes: null }`
  serialises arrays as repeated keys with no brackets.
- **UniFi Network integration — Phase 1.** Issue #30. Read-only
  mirror of UniFi networks + active clients into IPAM. Per
  controller `unifi_controller` row, dual-transport (local +
  cloud connector via api.ui.com), dual-API (public Integration
  v1 surface for site enumeration + version probe; legacy
  controller API for the actual rich data — `rest/networkconf`,
  `stat/sta`, `rest/user` — which is the only place UniFi
  exposes MAC, hostname, network_id, oui, fixed_ip, and CIDR
  fields). Single `UnifiClient` parameterised by mode; cloud
  mode prepends the `connector/consoles/<host_id>` segment.
  Auth flavours: `api_key` (modern UniFi OS, required for
  cloud) or `user_password` (legacy local-only via cookie +
  CSRF login). Networks land as Subnets with gateway from
  `ip_subnet`, VLAN tag preserved; clients land as IPAddress
  rows keyed on MAC with hostname / OUI / `is_wired` carried
  through. DHCP fixed-IP reservations from `rest/user` mirror
  as `status="reserved"` so the UI surfaces them as static.
  `mirror_networks` / `mirror_clients` / `mirror_fixed_ips`
  per-row toggles, `site_allowlist` (empty = all sites),
  `network_allowlist` per-site VLAN filter, `include_wired` /
  `include_wireless` / `include_vpn` (default false) for the
  client mirror. Same Celery beat shape as the other
  integrations — 30 s tick, per-row interval gate, 60 s floor
  in cloud mode (api.ui.com rate-limits). Sidebar entry,
  admin CRUD page with mode-aware form (host vs. cloud_host_id),
  per-row test-connection probe, MCP `list_unifi_targets` tool,
  feature module `integrations.unifi` (default off — operator
  must opt in via Settings → Features → Integrations).
  Migration `b2c84f7a91d3` adds `unifi_controller` table +
  `unifi_controller_id` cascade FK on `subnet` / `ip_block` /
  `ip_address` and seeds the feature_module row. Phase 2 (DHCP
  reservation surfacing in the controller detail view + WiFi
  broadcast roster) and Phase 3 (write surface — propose-only
  subnet/VLAN renames pushed back via the integration API) are
  deferred per the issue's phasing.
- **Operator feature toggles + Settings → Features page.** New
  `feature_module` table seeded from a 17-entry catalog covering
  Network (customer / provider / site / service / asn / circuit /
  device / overlay / vlan / vrf), AI (copilot), Compliance
  (conformity), Tools (nmap), and Integrations (kubernetes /
  docker / proxmox / tailscale). Network / AI / Compliance / Tools
  default-on for discovery; Integrations default-off (each one
  needs operator-supplied credentials anyway). New
  `require_module(...)` FastAPI dependency 404s when a module is
  disabled, applied to every togglable router. The AI tool
  registry gains a `module` attribute and `effective_tool_names`
  strips disabled-module tools regardless of catalog overrides —
  disabling `network.customer` removes the customer find / count
  tools end-to-end. Toggle endpoint mirrors integration toggles
  into the existing `PlatformSettings.integration_*_enabled`
  columns in the same transaction so reconciler tasks
  (`kubernetes_sync` / `docker_sync` / `proxmox_sync` /
  `tailscale_sync`) keep gating on the settings column without
  migration churn. Migration `d8b5e4a91f27` backfills the
  feature_module rows from existing settings so on-toggles stay on
  across the upgrade. Settings → Features page lays out modules
  in a 3-column adaptive grid (wide groups full-width, narrow
  groups cluster three-up so AI / Compliance / Tools sit
  side-by-side), tab split between "Features" and "Integrations",
  pill toggle that auto-saves on flip. Integration toggles moved
  out of Settings → Integrations in the same wave — single home
  for the on/off switch is now Features → Integrations. CLAUDE.md
  Non-Negotiable \#14 documents the five-step checklist for
  adding new togglable feature modules in future PRs.
- **AI Tool Catalog page rewrite — auto-save + matching layout.**
  Same 3-column adaptive grid as Features, same shared
  `Toggle` pill, every flip fires `PUT /ai/tools/catalog` with
  the recomputed enabled list — no more batch Save button. React
  Query optimistic update via `setQueryData` so the toggle moves
  instantly; reverts on error, refetches on settle. Per-category
  "Enable all" / "Disable all" link in the section header still
  fires a single batch PUT for bulk operations. "Reset to
  defaults" sends NULL to revert to registry per-tool defaults.
  Search filter + "registry defaults" badge preserved.
- **Operator Copilot — Tier 2 tool wave (issue \#101).** Six new
  read-only tools register on import via two new modules. New
  `tools/ownership.py`: `list_customers` (filterable by status /
  contact substring), `get_customer_summary` (deep roll-up
  counting subnets / blocks / spaces / circuits / services / ASNs
  / DNS zones / domains / overlays for one customer; accepts UUID
  or exact name), `list_sites` (kind + region filters, surfaces
  parent-site nesting), `list_providers` (kind + contact filters,
  surfaces default-ASN linkage). New `tools/admin.py`: `list_users`
  (auth source + active flag + superadmin filter, returns groups
  / MFA / lockout state), `list_groups` (role assignments +
  member counts + has-role filter), `list_roles` (built-in vs
  custom filter, returns the full permission grants JSON +
  groups holding the role). All three admin tools are
  superadmin-gated inline — non-superadmin callers get a clear
  "ask your platform admin" error rather than silent empty
  results. Ownership tools tagged with the matching feature_module
  (`network.customer` / `network.site` / `network.provider`) so
  disabling a module removes the corresponding tools from the AI
  surface in lock-step with the sidebar. Operators who pinned an
  explicit `platform_override` won't auto-get the new tools — by
  design; they'll appear as disabled rows in the Tool Catalog
  page so the operator can opt in.
- **Operator Copilot — Tier 4 tool wave (issue \#101).** Ten new
  read-only tools across two modules. New `tools/integrations.py`
  with `list_kubernetes_targets` / `list_docker_targets` /
  `list_proxmox_targets` / `list_tailscale_targets` — each gated
  by the matching `integrations.*` feature_module so disabling
  the integration in Settings → Features removes the tool from
  the AI surface. Output is intentionally narrow — credentials,
  CA bundles, encrypted keys never appear; just name / endpoint /
  enabled flag / last-sync timestamp + error / IPAM space binding.
  New `tools/observability.py` with `query_dns_query_log` (BIND9
  query log; qname / qtype / client_ip / view / since-window
  filters), `query_dhcp_activity_log` (Kea activity log; severity
  / log code / MAC / IP filters), `query_logs` (inventory of which
  log sources are populated in the last N hours — operators run
  this once per chat to learn what's available), `get_dns_query_rate`
  + `get_dhcp_lease_rate` (timeseries roll-ups from the
  `dns_metric_sample` / `dhcp_metric_sample` tables, capped at 24
  buckets), and `global_search` (cross-resource lookup that
  reuses the same internal helpers as the Cmd-K palette via lazy
  import to avoid pulling FastAPI router glue at boot).
- **Operator Copilot — Tier 3 tool wave (issue \#101).** Ten new
  read-only sub-resource tools that drill into the rows inside a
  zone / scope. DNS side: `list_dns_records` (cross-zone with
  name / fqdn / type / value substring filters — distinct from
  the existing per-zone `query_dns_records`), `list_dns_blocklists`
  (RPZ rows with category / source / sync state), `list_dns_pools`
  (GSLB pools with eager-loaded members + per-member health
  state), `list_dns_views` (split-horizon views). DHCP side:
  `list_dhcp_pools` (dynamic / excluded / reserved ranges within
  a scope), `list_dhcp_statics` (MAC → IP reservations with
  hostname filter), `list_dhcp_client_classes` (group-scoped
  conditional option-delivery), `list_dhcp_option_templates`
  (named option bundles), `list_pxe_profiles` (PXE / iPXE
  provisioning profiles with per-arch boot-file matches),
  `list_dhcp_mac_blocks` (group-global blocked MACs with reason +
  expiry). All default-enabled per the discovery argument; all
  appended to the existing `tools/dns.py` and `tools/dhcp.py`
  modules. Total tool count: 67 → 77.
- **Operator Copilot — Tier 5 write proposals (issue \#101).**
  Four new `propose_*` tools that stage write actions for the
  operator to Approve / Reject in the chat drawer:
  `propose_create_dns_record` (zone_id + name + record_type +
  value + optional ttl/priority; preview probes for an identical
  existing record and surfaces it as a hint without rejecting),
  `propose_create_dhcp_static` (scope_id + ip_address + mac_address
  + optional hostname/description; preview rejects on out-of-scope
  IPs and warns on conflicting IP/MAC reservations),
  `propose_create_alert_rule` (subnet-utilization rule_type only —
  the simplest case; other rule_types keep their UI authoring
  path), `propose_archive_session` (sets
  `AIChatSession.archived_at = now()`; preview rejects cross-user
  attempts so operators can only archive their own sessions). All
  four ship default-disabled — operators opt in per-tool via
  Settings → AI → Tool Catalog. Each underlying mutation lives
  as a registered `Operation` in `services/ai/operations.py` with
  a preview / apply pair and writes an audit row tagged
  `via=ai_proposal` at apply time. **Double validation to enable**:
  the Tool Catalog page now detects the `propose_` name prefix
  and shows a confirm modal before turning on any such tool —
  even bulk "Enable all" on a category routes write proposals
  through the modal so the AI can't be silently armed with write
  capability. Bonus visual cue: each propose_ tool now renders a
  yellow "proposal" badge in the catalog so they read as distinct
  from read-only tools at a glance. Total tool count: 47 → 67
  (10 of those default-disabled, including the 4 propose_ tools
  and the 4 integration list_targets gated behind their default-
  off feature_module). Deferred from Tier 5 per the issue's
  "needs UX thought" note: `propose_create_subnet` (too many edge
  cases — auto-allocate network/broadcast rows, parent-block
  overlap checks, allocation policy) and the `propose_delete_*`
  family (cascade-impact preview is a separate design pass).
- **Release-note formatting — flowing prose + emoji section
  headings.** New `scripts/format_release_notes.py` runs between
  the changelog awk-extract and the GitHub release body in
  `.github/workflows/release.yml`. The transformer joins
  consecutive prose lines into single-line paragraphs (so GFM's
  `breaks: true` mode renders them as one paragraph instead of
  forced `<br>`s), preserves blank-line paragraph breaks + fenced
  code blocks + bullet lists, and emoji-prefixes the
  Keep-a-Changelog headings — `🚀 Highlights` (the top summary),
  `✨ Added`, `🔧 Changed`, `🐛 Fixed`, `🔒 Security`,
  `🗃️ Migrations`, `⚠️ Deprecated`, `💥 Breaking`. Soft-hyphen
  edge case handled (`per-\nframework` joins to `per-framework`,
  not `per- framework`). Idempotent — safe to re-run on
  already-transformed input. Backfilled the last three releases
  on GitHub (`2026.05.05-2`, `2026.05.05-1`, `2026.05.03-1`) via
  `gh release edit` so they pick up the new format immediately.
  CHANGELOG.md keeps its terminal-friendly hard-wrap; the
  formatter handles release-time cleanup.
- **Shared `Toggle` pill component.** Extracted from
  `SettingsPage.tsx` into `components/ui/toggle.tsx` so the
  Settings page, Features page, and Tool Catalog page all use
  the same on/off pill with identical look + hit area.
- **CLAUDE.md — Non-Negotiable \#13 (MCP coverage) + \#14 (feature
  module gating).** Two new project-wide rules: when adding a new
  resource / feature with REST endpoints, also expose matching
  MCP tools (with an explicit per-tool default-enabled decision);
  when adding a new top-level resource family, evaluate whether
  it should be a togglable feature module and follow the
  five-step checklist (catalog entry, seed migration, route gate,
  MCP module attribute, sidebar nav module tag).

### Changed

- **Factory reset surface moved from a sidebar entry to a Backup
  page tab.** Backup snapshots state, factory reset wipes it —
  they're the two opposite ends of the same lifecycle, so both
  live on the Backup admin page now. Third tab after Manual +
  Destinations. Removed the standalone `/admin/factory-reset`
  route + sidebar entry; the section component renders only the
  inner content (intro panel, section card grid, modal) and
  the BackupPage owns the page chrome.
- **Backup MCP coverage clarified.** Per CLAUDE.md non-negotiable
  \#13, every new feature with REST endpoints gets matching MCP
  tools — but for backup we deliberately ship READ-only tools
  (`list_backup_targets`, `list_backup_archives_at_target`,
  `find_backup_audit_history`) and SKIP write proposals. Restore
  + factory-reset are password-gated + confirm-phrase-gated by
  design; an LLM intermediary in "should I restore?" adds
  friction without value. Documented with a clear rationale in
  the backup tools module.
- **Sidebar integration visibility moved to feature_module.** The
  sidebar now reads `useFeatureModules().enabled("integrations.*")`
  instead of `platformSettings.integration_*_enabled`. Behaviour
  is identical (the toggle endpoint mirrors both columns) — but
  the source of truth is the new feature_module catalog so future
  toggles stay consistent.

### Migrations

- `d2a8e417b9f3_backup_target.py` — new `backup_target` table for
  scheduled backups (Phase 1b of issue #117). Single-row-style
  configurable destinations with Fernet-wrapped passphrase, cron
  schedule, retention policy, last-run state.
- `c4f7a1d3e589_feature_module_table.py` — new `feature_module`
  table seeded with 13 togglable ids (network.\* / ai.copilot /
  compliance.conformity / tools.nmap).
- `d8b5e4a91f27_integration_feature_modules.py` — adds the four
  `integrations.*` rows + backfills `enabled` from existing
  `PlatformSettings.integration_*_enabled` so existing on-toggles
  stay on.
- `b2c84f7a91d3_unifi_integration.py` — new `unifi_controller`
  table; `unifi_controller_id` cascade FK on `subnet` /
  `ip_block` / `ip_address`; `integration_unifi_enabled` column
  on `platform_settings`; seeds the `integrations.unifi`
  feature_module row at `enabled=False`.
- `c3e8b57a2f14_unifi_router_link.py` — `unifi_controller_id`
  cascade FK on `router` and `vlan` so the Phase 1+ UniFi mirror
  can keep its VLAN + Router rows in lock-step with the
  controller they came from.
- `f3a8c2d491e7_password_policy.py` — seven
  `platform_settings.password_*` columns + two `user.*` columns
  (`password_changed_at` backfilled to `created_at`,
  `password_history_encrypted` Fernet-wrapped JSON). Issue #70.
- `a7b3c8d92e14_account_lockout.py` — three `user.*` columns
  (`failed_login_count`, `failed_login_locked_until`,
  `last_failed_login_at`) + three
  `platform_settings.lockout_*` columns. Defaults to lockout
  disabled (threshold=0). Issue #71.
- `c8e4f7a91d36_session_viewer.py` — `auth_source`,
  `last_seen_at`, `revoked` columns on `user_session` + composite
  `(revoked, expires_at)` index. Issue #72.
- `d92f4a18c763_audit_chain_hash.py` — `seq` (bigserial) +
  `prev_hash` + `row_hash` columns on `audit_log` plus the
  `BEFORE DELETE` trigger that raises an exception. Backfills
  the entire existing table in `(timestamp, id)` order so the
  chain is contiguous from row 1. Issue #73.
- `c7e9d4f81a26_internal_error_table.py` — new `internal_error`
  table for captured uncaught exceptions (issue #123). 16
  columns including `fingerprint` for dedup,
  `occurrence_count` for noisy-crash bumping, ack + suppress
  state.
- `b7e29c4f5d18_dns_dhcp_tags.py` — `tags JSONB` columns on
  `dns_zone` / `dns_record` / `dhcp_scope` / `dhcp_pool` /
  `dhcp_static_assignment` + GIN indexes for fast `?tag=`
  filter lookups. Issue #104 Phase 4.
- `e5a18c40729b_ai_tool_catalog.py` — `ai_tools_enabled
  list[str] | None` column on `platform_settings`. NULL = use
  registry defaults; non-NULL = exactly these tools. Drives the
  Tool Catalog page. Issue #101.

### Fixed

- **BGP Footprint sticky header bleed-through.** The Announced
  Prefixes table on the ASN detail page used `bg-muted/30` on
  the sticky `<thead>`, and the 30% alpha let scrolled rows
  show through the header — a "text overlay" appearance.
  Switched to a solid `bg-card` (matches the surrounding card
  chrome) and added `z-10` so the header floats above the
  scrolled rows reliably.
- **PeeringDB 404 → "not registered" not "unreachable".**
  PeeringDB returns HTTP 404 when an ASN simply isn't
  registered there — only ~30 k of the ~80 k allocated 16-bit
  ASNs have PeeringDB records, so a 404 is the common case for
  any AS that isn't a content provider, transit, or large
  eyeball network. The previous client treated every
  `HTTPError` as "upstream unreachable", surfacing this
  misleading message in the BGP Footprint tab on AS15318 (and
  any other AS without a PeeringDB record). 404 is now its
  own branch with a clear "no PeeringDB record" empty state.
  Cached at the same TTL as the success path so we don't
  re-query on every page load.
- **Frontend BGP query staleTime — 24 h → 60 s.** The backend
  already caches RIPEstat for 6 h and PeeringDB for 24 h
  in-process, so the source of truth for "how stale is this
  data" lives there. Mirroring those windows on the frontend
  meant a manual page refresh after a ROA change wouldn't
  re-fetch for hours. Frontend stale window dropped to 60 s
  so refresh-on-tab-focus does the right thing; the backend's
  TTL cache still absorbs the volume.
- **Conformity PDF export — fetch as blob, not
  `window.open`.** `window.open` made a fresh browser
  navigation that didn't carry the JWT (axios holds it in
  memory, not as a cookie), so the API correctly replied
  HTTP 401 and the operator saw a blank tab. Mirrored the
  IPAM `exportFile` pattern: fetch the PDF via the
  authenticated axios client with `responseType: "blob"`,
  parse the backend's `Content-Disposition` for the UTC-
  timestamped filename, trigger a synthetic anchor click for
  the download. Both call sites updated — the top-of-page
  "Export PDF" button and the per-framework download icon
  inside `FrameworkCard`.
- **`IPSpacePicker` quick-create no longer closes the parent
  modal.** Two stacked bugs made the "+ New" IP space shortcut
  inside the integration endpoint modals (UniFi / Proxmox /
  Docker / Kubernetes / Tailscale / DeviceFormModal) close the
  parent modal AND silently fail to create the space:
  (1) the shared `Modal` rendered inline in the DOM so the
  inner `QuickCreateSpaceModal` `<form>` ended up nested
  inside the parent modal's `<form>`, the HTML parser silently
  drops nested form tags, the inner submit button got
  re-associated with the OUTER form; (2) even with the inner
  form re-established via a portal, React's synthetic submit
  event bubbles through the React component tree regardless
  of DOM hierarchy. Fix: portal the Modal to `<body>` AND
  `stopPropagation` on the inner submit handler.
- **Operator Copilot — second message vanishes from drawer
  on stream error (issue #121).** Operators reported their
  second-and-later chat turn occasionally disappearing from
  the drawer with no error message. The user message *is*
  committed to the DB before the LLM stream starts so nothing
  was actually lost — the disappearance was purely a display
  race in the drawer. Two issues compounded: (1)
  `sendMut.onSettled` cleared `pendingUserMessage` on every
  completion, including stream errors; the optimistic echo
  vanished before the React Query refetch could land the
  persisted version. (2) `StreamingBubble` was gated on
  `sendMut.isPending`; once the mutation settled, the bubble
  unmounted, taking the inline error message with it. Fix
  splits the lifecycle — `mutationFn` returns
  `{ sessionId, error }` so `onSuccess` can preserve the
  optimistic echo until the real refetch lands; the inline
  error sticks on a separate state that isn't gated on
  `isPending`.
- **UniFi reconciler — three crash-loop fixes.** Phase-0
  cleanup pass restored after a container-sync round-trip
  overwrote it (the dedup logic in `_apply_addresses` got
  silently lost when black-in-container synced an older copy
  back to the host). Made `_UnifiSite` a frozen dataclass so
  it gains `__hash__` — without it, every reconcile pass
  crashed with `unhashable type: '_UnifiSite'` because the
  reconciler keys `site_to_networks` + `site_to_clients` by
  it, and zero IPAM rows were ever created. Cross-subnet IP
  collision: `_apply_addresses` blew up the entire UniFi
  sweep with `uq_ip_address_subnet_address` when a
  unifi-owned row at IP X had to be moved into a subnet that
  already held a non-unifi-owned row at the same IP (typical
  source: SNMP discovery wrote the IP in whatever subnet
  currently enclosed it before a route shifted) — added two
  pre-checks (move path + insert path) that drop the
  unifi-owned source row + append a warning to the summary
  instead of crashing the sweep.
- **VoIP phone icon tooltip on IPAM table + DHCP lease
  list.** Hover-text was already on the IP detail modal but
  missing from the two table renderings. Wrap the Phone icon
  in a span with a title that reads "VoIP phone — <vendor>"
  (or just "VoIP phone" when the vendor is null) so operators
  can confirm at a glance which curated vendor lit up the
  flag without opening the row.
- **Network role select moved onto the subnet General tab.**
  Operators routinely tag subnets with a network role (most
  subnets get one), unlike PCI / HIPAA / internet-facing
  flags which are infrequent. Burying the select inside the
  Compliance section on the Advanced tab made it
  discoverable mainly to operators who already knew it was
  there. Lifted out of `ClassificationSection` into a
  standalone `NetworkRoleField` rendered on the General tab.
- **Backup restore — alembic upgrade fails when alembic_version
  drifts inside the dump.** Real-world failure mode: a backup
  taken at a moment when the live install's alembic_version was
  drifted relative to its schema (operator stamped head later,
  then restored an earlier backup) would land both the up-to-date
  schema AND the stale alembic_version, then `alembic upgrade
  head` would fail on the first migration with "table already
  exists". The migration step now detects that error pattern
  and recovers by running `alembic stamp head` instead — see
  the Phase 3 entry above for the new `auto_recovered` state.
- **CodeQL alert \#25 — explicit TLS minimum version on the
  copilot's `tls_cert_check` tool.** `_fetch_cert_sync` in
  `app/services/ai/tools/ops.py` now sets
  `ctx.minimum_version = ssl.TLSVersion.TLSv1_2` explicitly so
  TLSv1.0 / 1.1 servers fail handshake with a clear error and
  the contract is obvious to readers (modern OpenSSL already
  disables them by default — explicit beats implicit, and CodeQL
  no longer flags the call site).
- **CI was red since `5d577fd` (~11h before backup work began)
  and stayed red through Backup Phase 2/3.** Six mypy errors
  surfaced during the release-prep `make ci` audit: 5 narrowing
  fixes in the SCP/SMB/FTP destination drivers + the rewrap
  module + ftplib socket type (`port not in (None, "")` →
  `port is not None and port != ""` for mypy narrowing; `assert
  client.sock is not None` before `wrap_socket`; `assert
  new_value is not None` inside the rewrap success branch); 1
  untyped variable annotation in `bgp/peeringdb.py:57`. Plus 2
  prettier-formatting warnings on `BackupPage.tsx` +
  `BackupTargetsSection.tsx` after the Phase 3 commits.

---

## 2026.05.05-2 — 2026-05-05

The **Operator Copilot polish + compliance loop** release. Three
threads land together. **Operator Copilot polish** turns the chat
into something operators can actually rely on — especially against
self-hosted Ollama: the OpenAI-compat driver now forwards
`options.num_ctx` / `num_predict` / `extra_body` so Ollama respects
the configured context window (the silent 2048-token default was
truncating the 8K-token prompt + tool schemas and caused every
small model to hallucinate tool names from a half-cut list); a
reasoning-channel fallback captures `delta.reasoning` from
qwen3.5 / DeepSeek-R1 / o1-style models so post-tool answers don't
disappear; a trailing-usage-chunk handler picks up Ollama's
separate empty-choices `usage` chunk so per-message token counts
are accurate. The tool registry expands from 22 to 35 read-only
tools with new modules covering ASNs / domains / VRFs / circuits /
services / overlays / applications, plus `find_switchport` (joins
IP→MAC→FDB→interface), `ping_host`, nmap inspection +
`propose_run_nmap_scan` write proposal, OUI vendor enrichment
inline on `find_ip` / `find_dhcp_leases` / `find_switchport`, and
name-or-UUID resolution on `space_id` / `block_id`. Per-provider
**system prompt override** + per-provider **tool allowlist** ship
as new tabs on the AI Provider modal — narrow Ollama down to the 8
tools you actually use, restrict a kiosk provider to read-only,
fork the prompt without losing the baked-in default. The chat
drawer renders markdown via react-markdown / remark-gfm, persists
the active session + composer draft to sessionStorage, surfaces
per-message tokens / copy / info popover under every assistant
reply, and grows multi-select bulk delete in the History panel.
**Compliance change alerts (#105)** add a new `compliance_change`
rule type plus three disabled seed rules (PCI / HIPAA /
internet-facing): the alert evaluator scans the audit log on the
existing 60 s tick, opens one event per mutation against a
classification-flagged subnet (or descendant IP / DHCP scope), and
auto-resolves after 24 h. **Conformity evaluations (#106)** are
the proactive companion: declarative `ConformityPolicy` rows pin a
`check_kind` against a target set; a beat-driven engine runs every
enabled policy on its `eval_interval_hours` cadence, writes
append-only `ConformityResult` rows, and emits AlertEvent on
pass→fail transitions. Six starter check kinds cover the common
shapes (`has_field`, `in_separate_vrf`, `no_open_ports`,
`alert_rule_covers`, `last_seen_within`, `audit_log_immutable`),
eight seed policies span PCI-DSS / HIPAA / SOC2, and a synchronous
**reportlab PDF export** is the auditor-facing artifact (per-
framework section, failing-row enumeration with diagnostic JSON,
SHA-256 integrity hash over (id, status) tuples in the trailer for
tamper detection). Two new built-in roles — **Auditor**
(read-only) and **Compliance Editor** (admin) — drop into the
RBAC seeder. #105 + #106 form the complete compliance loop:
alerts catch the change in real time, evaluations prove steady
state and produce the document auditors actually file. Plus
ancillaries: nmap `quick` preset bumped from top-100 to top-1000
(`udp_top100` preset renamed to `udp_top1000` with migration), a
`PXE Profiles` button added to the DHCP server-group view, README
gains a top-level table of contents, and a mypy fix on
`network_modeling.py` unblocks the dependabot axios PR (#102).

### Added

- **Operator Copilot — Ollama context-window forwarding.**
  `openai_compat` driver now forwards `provider.options.num_ctx` /
  `num_predict` / `extra_body` via the OpenAI SDK's `extra_body`
  parameter so Ollama respects the configured context. Without
  this Ollama silently truncated to its 2048-token default and
  cut the system prompt + tool schemas mid-stream, which caused
  every small model (gemma4 / qwen2.5 / qwen3.5 / gpt-oss) to
  hallucinate tool names. README documents the
  `OLLAMA_CONTEXT_LENGTH` env-var route as the recommended
  server-side default.
- **Operator Copilot — reasoning-channel fallback in the streaming
  driver.** qwen3.5 / DeepSeek-R1 / o1-style models route their
  post-tool answer to `delta.reasoning` instead of
  `delta.content`. The driver now captures both, flushes
  `reasoning_buf` as content when the turn emitted no content and
  no tool calls, and falls back to `model_extra["reasoning"]`
  when the field isn't a first-class delta attribute.
- **Operator Copilot — trailing usage-chunk handler.** Ollama
  emits a separate empty-choices chunk carrying `usage` after the
  `finish_reason` chunk. Driver now branches on `not choices and
  chunk.usage`, the orchestrator captures token counts independent
  of `finish_reason`, and the per-message footer renders accurate
  prompt / completion totals.
- **Operator Copilot — per-provider system prompt override.**
  Migration `d6a39e84c512` adds `ai_provider.system_prompt_override`
  TEXT column. New "System prompt" tab in the AI Provider
  create/edit modal carries a textarea, "Reset to default", "Start
  from default" copy-and-edit, and a collapsible inline view of
  the baked-in default. Snapshotted into the chat session at
  creation time so a mid-conversation provider edit doesn't break
  in-flight chats. Baked-in default expanded ~10× — persona, DDI
  domain primer, full tool taxonomy, response-style rules,
  write-action gating, "no LaTeX" + "not a general-purpose coding
  assistant" scope rules, three worked examples for the canonical
  question shapes.
- **Operator Copilot — per-provider tool allowlist.** Migration
  `c4e8b71f0d23` adds `ai_provider.enabled_tools` JSONB column
  (NULL = "all enabled" default; empty list = no tools at all;
  non-empty list = exactly those names). New "Tools" tab in the
  AI Provider modal renders a category-grouped checkbox list with
  tool descriptions and "write" badges on `propose_*` rows.
  Saving with every box checked writes back NULL so the provider
  stays on "use whatever the registry has" rather than pinning a
  stale snapshot. Orchestrator filters
  `REGISTRY.read_only()` to `provider.enabled_tools` when set;
  unknown names silently skipped at request build time so a tool
  rename doesn't break a saved allowlist. The system prompt's
  "Tools available: N" line mirrors the filtered count. New
  `GET /api/v1/ai/providers/tools` returns the catalog
  (name / description / category / writes flag), registered ahead
  of `/{provider_id}` so Starlette matches the literal first.
  Three use cases this addresses: small local Ollama models that
  struggle with 35 tools, read-only kiosk providers, compliance
  posture restricting `propose_*` writes per provider.
- **Operator Copilot — tool registry expansion (22 → 35).** New
  module `tools/network_modeling.py` covering recently-shipped
  entities: `list_asns`, `get_asn`, `list_domains`, `list_vrfs`,
  `list_circuits`, `list_network_services`,
  `get_network_service_summary`, `list_overlay_networks`,
  `get_overlay_topology`, `list_application_categories`. New
  module `tools/nmap.py` with `list_nmap_scans` +
  `get_nmap_scan_results` read tools, plus `propose_run_nmap_scan`
  in `tools/proposals.py` wired through the existing
  preview/apply operation pattern with audit hooks. New
  `find_switchport` joins IP→MAC (IPAM or ARP) → FDB → interface
  to answer "what port is X plugged into" — flags trunk uplinks
  via `interpretation_hint` when the MAC matches multiple
  interfaces. New `ping_host` runs argv-validated ICMP from the
  SpatiumDDI host (read tool, no proposal — liveness checks
  shouldn't need a confirm prompt). Name-or-UUID resolution on
  `space_id` / `block_id` lets the model pass `"home"` instead of
  a UUID. OUI vendor enrichment surfaced inline on `find_ip` /
  `find_dhcp_leases` / `find_switchport` so no extra tool call
  is needed. Tool-not-found errors echo the full registry list
  + a `hint` so smaller models that hallucinate names can
  self-correct.
- **Operator Copilot — chat drawer markdown rendering.**
  Assistant messages now render through `react-markdown` +
  `remark-gfm` (tables, code fences, headings, links). Curated
  override map keeps inline code distinct from block code via the
  `language-` className regex. Streaming-compatible — partial
  render on every chunk, blinking caret preserved.
- **Operator Copilot — sessionStorage persistence.** Active
  session ID + composer draft persist via `useSessionState` so
  closing and reopening the drawer lands on the same conversation
  with the half-typed message intact. Stale-id guard
  (`detailQ.isError → setActiveSessionId(null)`) drops the
  reference cleanly when the underlying session is deleted in
  another tab.
- **Operator Copilot — per-message footer + bulk delete.**
  OpenWebUI-style footer under every assistant reply renders
  prompt / completion tokens, a copy button, and an info popover
  with provider / model / latency. History panel grows a checkbox
  column + "Select all" + "Delete N" / "Delete all" toolbar
  driven by a single `bulkDeleteMut` Promise.all fan-out. Both
  delete paths invalidate `["ai-usage-me"]` so the daily token
  chip drops live as messages cascade.
- **Operator Copilot — anti-loop dedup guard.** Per-turn
  `seen_calls: set[tuple[str, str]]` in the orchestrator catches
  small models that loop on a successful tool call (qwen2.5:7b
  reproducibly did this); the loop emits a synthetic warning
  telling the model the result is already in context, breaking
  the cycle without crashing the conversation.
- **Compliance change alerts (#105) — `compliance_change` rule
  type.** Migration `e3f1c92a4d68` adds three columns to
  `alert_rule`: `classification` (one of `pci_scope` /
  `hipaa_scope` / `internet_facing`), `change_scope` (one of
  `any_change` / `create` / `delete`), and `last_scanned_audit_at`
  watermark. The evaluator scans `audit_log` on the existing 60 s
  alert tick, opens one event per mutation against a
  classification-flagged subnet (or descendant IP / DHCP scope
  via the subnet FK), and auto-resolves after 24 h. Watermark
  baselines to `now()` on first run so historical audit rows
  don't retro-page operators. Resource resolution falls back to
  `audit_log.old_value.subnet_id` for delete actions where the
  live row no longer exists. Per-pass scan capped at 1000 audit
  rows so a long-disabled rule flipping on doesn't pause the
  evaluator. Three disabled seed rules (PCI / HIPAA /
  internet-facing) ship via the existing main.py role/seed
  pipeline. Frontend `AlertsPage` rule-type picker + form gain a
  Compliance optgroup with classification + change-scope fields.
- **Conformity evaluations (#106) — declarative policies +
  scheduled evaluator + auditor PDF.** Migration `b5d8a3f12c91`
  adds `conformity_policy` (declarative check definitions —
  framework / reference / severity / target_kind / target_filter
  / check_kind / check_args / enabled / eval_interval_hours /
  fail_alert_rule_id) and `conformity_result` (append-only
  history, indexed twice on `(policy_id, evaluated_at)` and
  `(resource_kind, resource_id, evaluated_at)` so both natural
  drilldowns hit an index). Beat task `app.tasks.conformity`
  ticks every 60 s; per-policy `eval_interval_hours` gating
  (default 24 h) keeps the work cheap. On-demand re-evaluation
  via `POST /conformity/policies/{id}/evaluate`. Six starter
  `check_kind` evaluators in `services/conformity/checks.py`:
  `has_field` (non-empty named field), `in_separate_vrf`
  (subnet's effective VRF holds only classification-matched
  siblings), `no_open_ports` (latest nmap scan didn't expose
  forbidden ports — warn when no recent scan, never silent-pass),
  `alert_rule_covers` (≥1 enabled alert rule of named rule_type
  exists), `last_seen_within` (IP / subnet recency check), and
  `audit_log_immutable` (platform-level positive-presence
  signal). Eight seed policies covering PCI-DSS / HIPAA /
  internet-facing / SOC2, all `is_builtin=True` and
  `enabled=False` so the operator opts in. Built-in rows accept
  narrow updates only (enabled / interval / severity /
  fail_alert_rule_id / description) — clone first to author a
  variant. pass→fail transitions emit `AlertEvent` rows against
  the policy's wired alert rule when set, surfacing conformity
  drift in the existing alerts dashboard. Permission resource
  type `conformity` plus two new built-in roles seeded:
  **Auditor** (read-only on conformity + audit + the underlying
  resources, suitable for an external auditor account that can
  pull the PDF and verify evidence without changes) and
  **Compliance Editor** (admin on conformity + read on the
  underlying resources, for the team that authors and tunes
  policies). Frontend `/admin/conformity` page renders a
  per-framework summary card row, policies table with inline
  toggle / re-evaluate / edit / delete, and a filterable results
  panel where each row expands to show the diagnostic JSON
  inline. Platform Insights gains a Conformity card with deep-
  link. Sidebar entry under the Auditing divider.
- **Conformity — auditor-facing PDF export.** New `reportlab>=4.2`
  dependency. `services/conformity/pdf.py` renders the latest
  result per (policy, resource) tuple as a single PDF organised
  by framework: per-framework summary table, per-policy section
  with pass / warn / fail / not_applicable tally and enumerated
  failing rows with the diagnostic JSON pretty-printed beneath,
  trailer with a SHA-256 hash over (result_id, status) tuples
  so the auditor can verify the underlying rows haven't been
  edited post-generation. `GET /conformity/export.pdf` endpoint
  with an optional `?framework=` filter. Per-framework "download
  PDF" deep-link icon in each summary card.
- **DHCP — PXE Profiles button on the server-group view.**
  `GroupDetailView` header now carries a PXE Profiles
  `HeaderButton` that navigates to `/dhcp/groups/:gid/pxe`.
  Previously the page was only reachable from inside the scope
  edit modal.
- **README — top-level table of contents.** New `## Contents`
  section between the alpha warning and the elevator pitch with
  one anchor per top-level heading so the 700-line README is
  scannable from the top.

### Changed

- **Operator Copilot — system prompt + safety rules.** Baked-in
  default expanded ~10× to include the persona, DDI domain
  primer, full tool taxonomy, write-action gating, formatting
  conventions (no LaTeX), and three worked examples. Explicit
  "you are not a general-purpose coding assistant" scope rule
  (decline code generation outside narrow platform-config
  snippets). Reads the per-provider override when set, falls
  back to the baked-in default otherwise.
- **nmap — `quick` preset bumped to top 1000.** Was `-T4 -F`
  (top 100); now `-T4 --top-ports 1000`. The `udp_top100` preset
  renamed to `udp_top1000` (`--top-ports 1000`) with migration
  `a8d6e10f3b59` backfilling existing scan rows so historical
  history doesn't silently mislabel its preset. Frontend
  `NmapScanForm` + IPAM auto-profile preset enum updated.
- **Compliance change alerts (#105) — fields surface on
  AlertRuleResponse.** `classification` / `change_scope` /
  `last_scanned_audit_at` flow through the REST surface; the
  rule-list cell renders `pci_scope · any_change` instead of
  the generic dash for compliance_change rows.
- **README — Operator Copilot section rewritten.** ~13 lines →
  ~60 lines covering accurate tool names, the Ollama 5-minute
  self-host recipe, and the `OLLAMA_CONTEXT_LENGTH` gotcha
  called out so operators don't repeat the truncation
  diagnosis.

### Fixed

- **mypy — `network_modeling.py` `dict(rows.all())` typing.**
  SQLAlchemy returns `Sequence[Row[tuple[UUID, int]]]` which
  mypy can't narrow to `Iterable[tuple]` for `dict()`'s
  constructor. Three count-rollup queries rewritten as
  `{row[0]: row[1] for row in rows.all()}` dict comprehensions.
  Same fix unblocks the dependabot axios 1.15.0 → 1.15.2 PR
  (#102) — its CI was failing on the same three errors. After
  this lands the PR can be rebased (`@dependabot rebase`) and
  merged.

### Migrations

- `a8d6e10f3b59_nmap_udp_top100_to_top1000` — backfills
  `nmap_scan.preset` from `udp_top100` → `udp_top1000`.
- `c4e8b71f0d23_ai_provider_enabled_tools` — adds
  `ai_provider.enabled_tools` JSONB column (NULL-default).
- `d6a39e84c512_ai_provider_system_prompt_override` — adds
  `ai_provider.system_prompt_override` TEXT column (NULL-default).
- `e3f1c92a4d68_compliance_change_alerts` — adds three columns
  to `alert_rule` (`classification` / `change_scope` /
  `last_scanned_audit_at`) for the new rule type.
- `b5d8a3f12c91_conformity_evaluations` — creates
  `conformity_policy` + `conformity_result` tables with their
  partial indexes.

---

## 2026.05.05-1 — 2026-05-05

The **Operator Copilot + network modeling** release. Two big themes
land together: **Operator Copilot (#90)** ships in two phases —
Phase 1 lays the LLM provider foundation (config, MCP HTTP endpoint,
tool registry with 18 read-only tools, chat orchestrator + SSE chat
endpoint, floating chat drawer, token / cost observability with
per-user daily caps); Phase 2 widens it to a full multi-vendor
copilot with Anthropic (Claude) + Azure OpenAI + Google Gemini
drivers alongside the existing OpenAI-compat driver, automatic
failover chain across enabled providers, "Ask AI about this"
affordances on subnets / IPs / DNS zones / records / alerts / audit
rows / DHCP / network devices, custom prompts library, Cmd-K palette
"Ask AI" entry, daily Operator Copilot digest, write tools with
preview / apply flow, and richer dynamic context in the system
prompt. **Network modeling (#91 / #93 / #94 / #95)** lands a
four-issue umbrella: **Customer / Site / Provider** logical
ownership entities cross-cutting IPAM / DNS / DHCP, **WAN circuits**
with transport classes + endpoints + term + cost (foundation for
the SD-WAN routing layer), **service catalog** (`network_service`
+ polymorphic `network_service_resource` join row that bundles VRF /
Subnet / IPBlock / DNSZone / DHCPScope / Circuit / Site /
OverlayNetwork into a customer-deliverable, with kind-aware
`/summary` endpoint — L3VPN view returns canonical VRF + edge
sites + edge circuits + edge subnets shape with warnings), and
**SD-WAN overlay topology** (`overlay_network` + `overlay_site` m2m
with role + edge device + ordered preferred-circuit chain +
`routing_policy` + curated `application_category` catalog seeded
with 33 well-known SaaS apps; `/topology` endpoint returns
nodes/edges by shared preferred circuits; `/simulate` endpoint runs
pure read-only what-if when circuits go down; SVG circular-layout
topology visualization). Also lands the **security wave** (#69 TOTP
MFA for local users, #74 API-token scopes, #75 subnet classification
tags), **#26 IPAM template classes** (reusable stamp templates with
child layouts), **#27 block move across IP spaces** (typed-name
confirm + dependent-row validation), **#25 split-horizon DNS
publishing** at the IPAM layer (block-level `dns_split_horizon` flag
inheritable to descendant subnets), **#51 PXE / iPXE provisioning
profiles** for DHCP, **#96 API docs link** in sidebar + header, plus
a wave of UX polish: tabbed IP Space modals (shared `ModalTabs`
helper), pinned identity header above tabs on every IPAM modal, the
Network sidebar section sub-grouped (Logical: Customers / Providers
/ Services / Sites; Infrastructure: ASNs / Circuits / Devices /
Overlays / VLANs / VRFs), Administration sidebar split, and an
extended `seed_demo.py` that now covers every shipped entity for a
realistic demo dataset.

### Added

- **Operator Copilot — provider config + LLM driver foundation
  (#90 Wave 1).** Migration `a4b8c2d619e7` adds `ai_provider`
  table (Fernet-encrypted api_key, kind discriminator with CHECK
  constraint covering `openai_compat` / `anthropic` / `google` /
  `azure_openai`, ordered priority for failover, JSONB options
  bag, indexed on `(is_enabled, priority)`). LLM driver ABC at
  `app/drivers/llm/base.py` defines neutral request / chunk / tool
  dataclasses modeled on the OpenAI Chat Completions schema;
  concrete drivers translate at the SDK boundary so the
  orchestrator only speaks the neutral interface. OpenAI-compat
  driver covers OpenAI + Ollama + OpenWebUI + vLLM + LM Studio +
  llama.cpp server + LocalAI + Together + Groq + Fireworks with
  streaming + tool-call delta reassembly per `index`.
  `/api/v1/ai/providers` CRUD with unsaved test-connection probe.
  Admin → AI Providers page wires it all up.
- **Operator Copilot — tool registry + 18 read-only tools + MCP
  HTTP endpoint (#90 Wave 2).** Tool registry shape mirrors the
  driver registry. 18 read-only tools cover the common operator
  asks: `list_subnets`, `get_subnet`, `list_ips`, `get_ip`,
  `list_zones`, `list_records`, `list_dhcp_scopes`, `list_leases`,
  `list_alerts`, `list_audit`, `list_devices`, `list_circuits`,
  `list_customers`, `list_sites`, `list_providers`, `list_asns`,
  `list_vrfs`, `list_overlays` (added later in Phase 2). MCP-shaped
  HTTP endpoint at `/api/v1/ai/mcp` exposes the same tool set so
  external MCP clients (Claude Desktop, Cursor, Cline) can connect
  directly without going through the chat drawer.
- **Operator Copilot — chat orchestrator + sessions API + SSE
  chat endpoint (#90 Wave 3a).** Migration `b5d9c41e2f80` adds
  `ai_chat_session` + `ai_chat_message` tables. Orchestrator at
  `services/ai/chat.py` runs the iterative tool-calling loop:
  selects highest-priority enabled provider, sends the message
  history + available tools, streams chunks back, dispatches tool
  calls, appends results to the history, repeats until the model
  emits a non-tool-call response. SSE endpoint at `POST
  /api/v1/ai/chat/{session_id}/messages` streams chunks to the
  frontend with `text-delta` / `tool-call-delta` / `tool-result`
  events.
- **Operator Copilot — floating chat drawer (#90 Wave 3b).**
  Right-side slide-in drawer (`ChatDrawer.tsx`) keyed on a global
  toggle. Streams responses live via EventSource, renders Markdown
  + code blocks + tool-call collapsed-by-default cards. Empty
  state shows clickable example prompts that auto-start the chat.
  SSE endpoint sets `X-Accel-Buffering: no` so nginx doesn't
  buffer the stream.
- **Operator Copilot — token / cost observability + per-user daily
  caps (#90 Wave 4a).** Migration `c8e3a7f10b54` adds
  `ai_usage_event` table tracking input + output token counts,
  computed cost per pricing table, model + provider, request kind
  (chat / mcp), and user. `services/ai/pricing.py` ships a curated
  pricing table for the major hosted models (gpt-4o / gpt-4o-mini
  / claude-3-5-sonnet / claude-3-5-haiku / gemini-1.5-pro /
  gemini-1.5-flash / common Ollama models) with cost-per-Mtoken
  rates. Per-user daily cap (`AIChatSetting.daily_token_cap_per_user`)
  enforced in the orchestrator.
- **Operator Copilot — chat drawer usage chip + platform-insights
  AI usage card (#90 Wave 4b).** Live token chip in the drawer
  header shows today's usage / daily cap. New "AI usage" card on
  Platform Insights aggregates the last 7 days by provider + model
  with a stacked bar chart (Recharts).
- **Operator Copilot — Anthropic (Claude) driver (#90 Phase 2).**
  Driver translates the neutral request shape into Anthropic's
  Messages API format — system prompt as a top-level field
  (vs. system role in OpenAI), tool-use blocks vs. tool-calls.
  Streaming via `messages.stream` event types.
- **Operator Copilot — Azure OpenAI + Google Gemini drivers
  (#90 Phase 2).** Azure OpenAI driver adapts the existing
  OpenAI-compat shape to Azure's per-deployment URL pattern
  (`https://{resource}.openai.azure.com/openai/deployments/{deploy}`
  + `?api-version=` query param). Google Gemini driver translates
  to Gemini's `generateContent` API and reassembles streamed
  function calls.
- **Operator Copilot — failover chain across enabled providers
  (#90 Phase 2).** Orchestrator now walks providers in priority
  order on transient failures (5xx / timeout / rate-limit) — first
  successful chunk wins. Permanent errors (4xx / auth) surface
  immediately. Failover events recorded in `ai_usage_event` with
  `request_kind="failover"`.
- **Operator Copilot — "Ask AI about this" affordances
  (#90 Phase 2).** Compact icon button on resource detail
  contexts that pre-fills the chat drawer with a templated prompt
  and the resource UUID for tool calls. Wired across subnets / IP
  rows / DNS zones / DNS records / DHCP scopes / leases / alerts
  / audit rows / network devices.
- **Operator Copilot — custom prompts library (#90 Phase 2).**
  New `ai_custom_prompt` table — operator-curated prompt templates
  stored per-org. Surfaces in the chat drawer as a "Prompts ▾"
  dropdown above the input. Built-in starter pack (Find unused
  IPs, Audit recent changes, Summarize subnet utilization, Triage
  open alerts).
- **Operator Copilot — Cmd-K palette "Ask AI" entry
  (#90 Phase 2).** Cmd-K (Ctrl-K on Linux/Windows) opens a global
  palette; "Ask AI" is the top entry and pre-fills the prompt
  with the current page's context. Fixed a shortcut conflict
  with the existing search hotkey.
- **Operator Copilot — daily digest (#90 Phase 2).** Optional
  daily 0900 local digest — Celery beat fires
  `tasks.ai_daily_digest`, the orchestrator calls a fixed prompt
  ("summarise interesting changes since yesterday: alerts /
  pending IPs / DNS drift / circuits expiring soon"), result lands
  in operator inbox via the existing audit-forward / SMTP /
  webhook channels. Off by default; `AIChatSetting.daily_digest_enabled`
  toggles it on per platform.
- **Operator Copilot — write tools with preview / apply flow
  (#90 Phase 2).** Two-phase write contract: model proposes via
  `propose_*` tool variants (returns the planned diff in a
  `proposed_change` envelope), operator reviews + clicks "Apply"
  in the chat drawer, frontend sends an `apply_change` follow-up
  that hits the real CRUD endpoint. Three pilot tools:
  `propose_create_ip`, `propose_update_ip_status`,
  `propose_create_dns_record`. Audit log captures both the
  proposal and the apply.
- **Operator Copilot — richer dynamic context in system prompt
  (#90 Phase 2).** System prompt now interpolates platform stats
  (subnet count, alert count, recent audit summary), the operator's
  role + scoped permissions, and "today's interesting things"
  (services with terms expiring < 30 d, alerts opened in the last
  hour, deviced last seen > 7 d ago).
- **TOTP MFA for local users (#69).** New `user_mfa_secret` table
  with Fernet-encrypted TOTP shared secret + backup-codes JSONB
  list. Enrolment flow: Settings → Security → "Enable MFA" →
  scan QR (`pyotp` + `qrcode` libraries) → enter 6-digit code to
  confirm → backup codes shown once. Login flow gains a second
  step when MFA is enabled — JWT pre-token issued on
  username+password, exchanged for full token after TOTP code or
  backup code accepted. Backup codes are single-use and persisted
  hashed. Admin can force-disable MFA per user (audit-logged).
  Migration `f8d4e29b1c75_user_modified_at` (rename) +
  `c4e7d28f1059_totp_mfa`.
- **API-token scopes (#74).** `api_token` rows gain a `scopes`
  JSONB column listing the resource_types the token is allowed to
  touch (vs. inheriting all of the user's permissions). Scope set
  is permission-name granularity (`subnet:read`, `subnet:admin`,
  `*` for full inheritance). Token create modal lets the operator
  pick scopes via a chip selector grouped by resource family.
  Authorization enforces scope intersection: token can do at most
  what the scope set allows AND what the user has permission for.
- **Subnet classification tags (#75).** `subnet.pci_scope`,
  `subnet.hipaa_scope`, `subnet.internet_facing`,
  `subnet.contains_pii` boolean columns. List filters across the
  IPAM page + the API. Compliance card on Platform Insights shows
  rolled-up counts. Tags inherit through the IP block tree (set on
  a parent block → all descendant subnets get the tag) with an
  explicit override toggle.
- **#26 IPAM template classes — reusable stamp templates with
  child layouts.** Migration `f9c1a7e25b83`. `ipam_template`
  captures default tags / custom-fields / DNS / DHCP / DDNS
  settings (plus optional sub-subnet `child_layout`) and stamps
  them onto blocks or subnets at apply time. `applies_to` locks
  each template to one of the two carriers. `force=False` fills
  only empty / null target columns;`force=True` overwrites and is
  the path `/reapply-all` uses to refresh drift across every
  recorded instance (cap 200). `IPBlockCreate.template_id` /
  `SubnetCreate.template_id` add optional pre-fill on the create
  paths; carrier rows now carry an `applied_template_id` SET-NULL
  FK so a "reapply across instances" sweep can find every row
  touched. `/admin/ipam/templates` page (list + tabbed editor:
  General / Tags + CFs + DNS-DHCP / DDNS / Child layout). New
  `manage_ipam_templates` permission + IPAM Editor seed.
- **#27 block move across IP spaces.** New `POST
  /ipam/blocks/{id}/move` accepts a target `space_id` + a
  typed-name confirmation. Pre-flight validates: target space
  exists, no CIDR overlap in the target tree, every dependent row
  (DNS records, DHCP scopes, addresses with custom-field
  inheritance) survives the move. `MoveBlockModal` walks the
  operator through the consequences with a chevron-revealed list
  of affected resources before the typed-name confirm unlocks
  Move.
- **#25 split-horizon DNS publishing at the IPAM layer.**
  `IPBlock.dns_split_horizon` boolean + the existing
  `dns_inherit_settings` walk. When set, descendant subnets
  publish records to `dns_zone_id` (internal) AND every entry in
  `dns_additional_zone_ids` (DMZ / external). Per-record routing
  is decided by the new `IPAddress.dns_zone_overrides` JSONB list
  (`[{zone_id, record_type}]`) so an operator can pin one address
  to publish only into the internal zone. Auto-sync task respects
  the split.
- **#51 DHCP — PXE / iPXE provisioning profiles.** New
  `pxe_profile` table — operator-curated profiles per
  architecture (`bios_x86` / `efi_x86_64` / `efi_arm64` /
  `efi_x86`). Each profile binds to a TFTP `next-server` + a
  `boot-filename` per arch, plus an optional iPXE script body.
  `DHCPScope.pxe_profile_id` SET-NULL FK; on render, the Kea
  driver emits one `client-class` per arch-match guarded by
  `option dhcp.user-class` matching the iPXE signature so legacy
  PXE clients see the BIOS bootfile and iPXE clients see the iPXE
  script. New `/dhcp/groups/{id}/pxe` admin page with profile CRUD
  and a per-scope "PXE profile" picker on the scope editor.
- **#91 Customer / Site / Provider logical ownership entities.**
  Three first-class rows that cross-cut IPAM / DNS / DHCP /
  Network so operators can answer "who owns this?", "what's at
  NYC?", and "which circuits does Cogent supply us?" without
  resorting to free-form tags. `Customer` is soft-deletable;
  `Site` is hierarchical (`parent_site_id`) with a unique-per-
  parent `code` (NULLS NOT DISTINCT for top-level deduping);
  `Provider` carries an optional `default_asn_id` FK. Cross-
  reference columns added on `subnet` / `ip_block` / `ip_space` /
  `vrf` / `dns_zone` / `asn` / `network_device` / `domain` /
  `circuit` / `network_service` / `overlay_network` with
  `ON DELETE SET NULL` so a customer/site/provider deletion never
  cascades into core IPAM / DNS / DHCP rows. Three new admin
  pages (Customers / Sites / Providers) with bulk-action tables +
  draggable modals. Shared `CustomerPicker` / `SitePicker` /
  `ProviderPicker` + matching Chip components plug into every
  IPAM / DNS / circuit / overlay create / edit modal. RBAC seeded
  into Network Editor + IPAM Editor. Migration
  `c2a7e4f81b69_logical_ownership_entities`.
- **#93 WAN circuits + transport classes.** New `circuit` table —
  carrier-supplied logical pipe (the contract + transport class +
  bandwidth + endpoints + term + cost), distinct from the
  equipment lighting it up. `provider_id` is `ON DELETE RESTRICT`
  (carrier relationship too load-bearing to silently null);
  `customer_id` and the four endpoint refs (a/z-end site +
  subnet) are `ON DELETE SET NULL`. Nine transport classes (mpls
  / internet_broadband / fiber_direct / wavelength / lte /
  satellite + three cloud cross-connects: direct_connect_aws /
  express_route_azure / interconnect_gcp). Soft-deletable so
  `status='decom'` is the operator-visible end-of-life flag while
  the row stays restorable. CRUD under `/api/v1/circuits` with
  filters (provider_id / customer_id / site_id matching either
  end / subnet_id / transport_class / status / expiring_within_days
  / search) and `/by-site/{site_id}` convenience endpoint. New
  `/network/circuits` page with bulk-action table + tabbed editor
  modal (General / Endpoints / Term + cost / Notes) + colour-
  coded term-end badge + asymmetric bandwidth display. Migration
  `d9f3b21e8c54_wan_circuits`.
- **#93 alert rules — `circuit_term_expiring` +
  `circuit_status_changed`.** First mirrors `domain_expiring`
  (severity escalation per `threshold/4` / `threshold/12`).
  Second is transition-style: router stamps `previous_status` +
  `last_status_change_at` on every status update; evaluator keys
  events on `last_status_change_at` and latches `(from, to,
  changed_at)` into `last_observed_value` so a single transition
  fires exactly one event, auto-resolved after 7 d. Routine
  `active` ↔ `pending` flips during commissioning are
  intentionally excluded — only `suspended` / `decom`
  transitions surface.
- **#94 service catalog — `network_service` + polymorphic
  resources + L3VPN summary.** First-class customer-deliverable
  bundle. `NetworkService` is one row per thing the operator
  delivers (`mpls_l3vpn` is the v1 concrete kind, `custom` is
  catch-all; `sdwan` lit up alongside #95; future: DIA, hosted
  DNS / DHCP, MPLS L2VPN, VPLS, EVPN). `NetworkServiceResource`
  is the polymorphic m2m that binds to VRF / Subnet / IPBlock /
  DNSZone / DHCPScope / Circuit / Site / OverlayNetwork. Hard
  rule: `mpls_l3vpn` services may have at most one VRF attached
  (422 on second VRF, 422 on kind-flip-to-L3VPN if >1 VRF
  already linked). Soft rules surfaced as warnings on
  `GET /summary`: missing VRF, fewer than 2 edge sites, edge
  subnet's enclosing block in a different VRF than the service.
  Endpoints: standard CRUD + bulk-delete, `POST/DELETE
  /{id}/resources` for attach / detach, `GET /{id}/summary`
  with kind-aware shape (L3VPN view returns canonical
  VRF + edge sites + edge circuits + edge subnets + warnings),
  `GET /by-resource/{kind}/{id}` reverse lookup powering the
  upcoming "show services using this resource" entry points
  (#99). New `/network/services` page (bulk-action table) +
  tabbed editor modal (General / Resources / Term + cost / Notes
  / Summary) with per-kind resource pickers (cross-group
  fan-out for DNS zones + DHCP scopes). Migration
  `e1d8c92a4f73_network_service_catalog`. RBAC into Network
  Editor + IPAM Editor.
- **#94 alert rules — `service_term_expiring` +
  `service_resource_orphaned`.** First mirrors
  `circuit_term_expiring`. Second is sweep-style: walks every
  active service's join rows and surfaces any link whose target
  row no longer exists or is soft-deleted. Subject is the join
  row's PK so detaching the orphan resolves the alert via the
  standard "subject no longer matches" branch in `evaluate_all`.
  Migration `f2c8d49a1e76` widens `alert_event.subject_type`
  from VARCHAR(20) → VARCHAR(40) to fit `network_service_resource`.
- **#95 SD-WAN overlay — overlays + routing policies + apps +
  topology + simulate.** Vendor-neutral source of truth for
  overlay topology + routing-policy intent. Four new tables
  landing together: `overlay_network` (soft-deletable; six kinds
  — sdwan / ipsec_mesh / wireguard_mesh / dmvpn / vxlan_evpn /
  gre_mesh; free-form vendor + encryption_profile so non-curated
  vendors plug in without enum migration), `overlay_site` (m2m
  binding sites with role hub / spoke / transit / gateway, edge
  device, loopback subnet, ordered `preferred_circuits` jsonb —
  first wins, fall through on outage), `routing_policy`
  (declarative per-overlay policy with priority + match-kind +
  match-value + action + action-target + enabled), and
  `application_category` (curated SaaS catalog used by
  `match_kind=application`, seeded at startup with 33 apps —
  Office365 / Teams / Zoom / Slack / Salesforce / GitHub / AWS /
  Azure / GCP / SIP voice / OpenAI / Anthropic / …). CRUD under
  `/api/v1/overlays` (with sites + policies sub-resources) and
  `/api/v1/applications`. `GET /overlays/{id}/topology` returns
  nodes (sites + roles + device + loopback + preferred-circuits)
  + edges (site pairs whose `preferred_circuits` lists overlap —
  `shared_circuits` is the intersection so the UI can colour by
  transport class) + policies. `POST /overlays/{id}/simulate` —
  pure read-only what-if; body specifies `down_circuits`,
  response shows per-site fallback resolution + per-policy
  effective-target with `impacted` flag and human-readable note.
  Three new RBAC resource types (`overlay_network` /
  `routing_policy` / `application_category`) into Network Editor.
  Service-catalog (#94) integration unlocked: `sdwan` added to
  `SERVICE_KINDS_V1`, `overlay_network` lit up as a real attach
  target, `service_resource_orphaned` alert sweep covers deleted
  overlays. New `/network/overlays` list page + detail page at
  `/network/overlays/{id}` with five tabs — Overview / Topology
  (SVG circular layout, role-coloured nodes, transport-coloured
  edges with solid for single-class and dashed for mixed) /
  Sites (table + editor with up/down circuit-reorder) / Policies
  (priority-ordered with up/down reorder + per-kind editors) /
  Simulate (toggle circuits down + see per-site fallback +
  per-policy impact with amber-tinted impacted rows). Migration
  `c4f7e92d3a18_sdwan_overlay`.
- **#96 API docs link — sidebar + header.** Surface the existing
  Swagger UI / ReDoc at `/docs` and `/redoc` from the navigation
  itself instead of expecting operators to know the URL. New
  "API Docs" entry under Help in the sidebar; new external-link
  icon in the header next to the user menu.
- **Tabbed IP Space modals + shared `ModalTabs` helper.** The
  Create / Edit IPSpace modal grew enough fields that a single
  long form was hard to scan. Split into General / DNS Defaults
  / DHCP Defaults / DDNS Defaults / Custom Fields tabs via a new
  shared `ModalTabs` helper at `frontend/src/components/ui/
  modal.tsx` that any modal can opt into.
- **Pinned identity header above tabs on every IPAM modal.** The
  identity row (Name + CIDR / colour swatch / breadcrumbs) now
  pins above the tab bar in Create / Edit modals across IPSpace
  / IPBlock / Subnet so tab switches don't lose context.
- **Network sidebar sub-grouping.** The Network section grew to
  8 entries (Customers / Providers / Services / Sites under
  "Logical"; ASNs / Circuits / Devices / Overlays / VLANs / VRFs
  under "Infrastructure") and got hard to scan flat. Two
  `SubNavLabel` rows split the contents the same way
  Administration handles its 18 items. Same collapse behaviour
  preserved.
- **Administration sidebar split.** Identity / Platform /
  Auditing / Tools dividers added so the 18 admin items aren't
  one flat scroll.
- **Demo seeder coverage.** `scripts/seed_demo.py` extended to
  cover every shipped entity (customers / sites / providers /
  circuits / services / overlays + routing policies +
  application catalog / VRFs / ASNs + RPKI / domains) so
  `make seed-demo` produces a realistic dataset for a fresh
  install. README updated to mention it.

### Changed

- **#90 Phase 1 — multiple polish landings.** SSE chat streaming
  disabled nginx buffering on `/api/v1/ai/chat` so chunks flush
  to the browser without 5–10 s batched delays. Chat drawer
  optimistically renders the user's just-sent message
  (previously the message popped in only when the assistant
  reply started). Empty-state example prompts in the chat
  drawer made clickable so first-time users have a single-click
  path to a working chat.
- **AI Providers + IPAM Templates pages — admin-page overflow
  fixes.** Both pages now wrap in `h-full overflow-auto p-6`
  matching the rest of the admin surface; AI Providers + IPAM
  Templates pages also picked up the narrow-viewport overflow
  rules from the admin-page memory (flex-wrap header,
  `min-w-0/flex-1/shrink-0`, `break-all` on URL / UUID cells,
  wide modals for tabs + 2-column grids).
- **Domains list — chevron expander column dropped.** The per-row
  expander never carried information that wasn't in the row
  itself; removing it widens the actually-useful columns
  (registrar, expiry, NS state).

### Fixed

- **`#90` Phase 2 follow-ups — CI failures + 4 CodeQL alerts.**
  Mostly minor: missing imports surfaced when the AI tool
  registry was lifted out of Phase 1 wave 2; CodeQL flagged
  three uncontrolled-format-string false positives in the system-
  prompt builder that became real risks once we started
  interpolating tenant-supplied strings into the prompt — fixed
  with explicit `str.format(safe_field=…)` calls. Fourth alert
  was an unused parameter in `_load_proposed_change` flagged by
  the tighter Phase 2 ruff config.
- **"Ask AI" button visual weight.** The first iteration used
  muted-gray styling and looked greyed-out next to the active
  HeaderButton family. Bumped to match HeaderButton's normal
  weight + added a visible "Ask AI" label next to the icon.
- **Black reformat on `alerts.py`.** Drift carried through from
  the #93 alert-types commit; clean-up.

---

## 2026.05.03-1 — 2026-05-03

Network-layer release. Closes the four-issue umbrella roadmap
(#84–#87) plus two follow-ups (#88, #89): the standalone "VLANs"
+ "Network" sidebar entries get rolled into a new **Network**
section that groups Devices / VLANs / VRFs / ASNs, and three
brand-new first-class entities land underneath. **ASNs** become a
real table with RDAP holder refresh (per-RIR routing through the
IANA bootstrap), RPKI ROA pull (Cloudflare or RIPE source) with
expiry tracking, holder-drift detection with a side-by-side diff
viewer, four ASN/RPKI alert rule types, BGP peering relationships
(`peer | customer | provider | sibling`) with directional listing,
and a BGP communities catalog (RFC 1997 / 7611 / 7999 well-knowns
seeded as platform rows + per-AS extensions, large communities per
RFC 8092). **VRFs** replace the freeform `vrf_name` /
`route_distinguisher` / `route_targets` text fields on IPSpace
with a proper relational entity carrying optional `asn_id`, with a
cross-cutting validator that warns (or 422s under
`vrf_strict_rd_validation`) when the ASN portion of an `ASN:N` RD
or RT doesn't match the VRF's linked ASN — the migration backfills
existing freeform values into VRF rows so nothing is lost.
**Domains** track the registry side of a name (registrar, expiry,
nameservers, DNSSEC status) distinct from DNSZone, with RDAP
refresh through a TLD → RDAP-base lookup driven by the IANA
bootstrap registry, four `domain_*` alert rule types
(expiring / NS drift / registrar changed / DNSSEC status changed),
and explicit `dns_zone.domain_id` linkage that follows the
sub-zone tree (so `test.example.com` shows up under
`example.com`'s linked-zones tab). Plus a wave of UX easy-wins:
shared `RdapPanel` that flattens RDAP wire shape into operator-
friendly UI on both ASN and Domain WHOIS tabs, ASN + VRF pickers
on IPSpace / IPBlock modals, the dashboard Platform Health card
moved up next to the KPI ribbon, alphabetised API tag ordering,
and IPAM gap rows (`.11 – .13 · 3 free`) are now clickable to
launch AddAddressModal pre-filled with First / Last / Random
quick-pick buttons over the gap.

### Added

- **Network sidebar section.** New non-clickable "Network" header
  (mirrors the Administration shape) groups Devices / VLANs /
  VRFs / ASNs. Devices replaces the old top-level Network entry;
  VLANs lifts up from its own slot. Routes move to
  `/network/devices`, `/network/vlans`, `/network/vrfs`,
  `/network/asns`; the old `/network` and `/vlans` paths redirect
  so existing bookmarks keep working, and the legacy
  `/network/:id` device-detail URL is preserved alongside the new
  `/network/devices/:id` canonical form. Closes #84.
- **ASN management — first-class entity.** New `asn` table:
  BigInteger `number` to fit the full 32-bit range; `kind`
  (public / private) auto-derived from RFC 6996 + RFC 7300;
  `registry` (RIR — arin / ripe / apnic / lacnic / afrinic) auto-
  derived from a hand-curated IANA delegation snapshot at
  `app/data/asn_registry_delegations.json`; WHOIS columns for the
  RDAP refresh task. Sibling `asn_rpki_roa` table tracks prefix +
  max_length + validity window + trust_anchor + state, ON DELETE
  CASCADE from `asn`. CRUD at `/api/v1/asns` with kind / registry
  / whois_state / search filters, bulk-delete capped at 500,
  audit-logged. New `manage_asns` permission seeded into the
  Network Editor builtin role. List page at `/network/asns` with
  sticky thead, multi-select bulk delete, kind / registry / WHOIS
  filter chips, draggable create / edit modal. Detail page at
  `/network/asns/:id` with WHOIS / RPKI ROAs / Linked IPAM /
  BGP Peering / Communities / Alerts tabs; per-row Refresh WHOIS +
  Refresh RPKI buttons in the header. Migrations
  `f59a5371bdfb_asn_management` + `4a7c8e3d51b9_asn_phase2`. Refs
  #85.
- **ASN — RDAP holder refresh.** `app/services/rdap_asn.py`
  derives the RIR via the existing `derive_registry()` classifier
  and queries the RIR's RDAP base directly (`rdap.arin.net`,
  `rdap.db.ripe.net`, `rdap.apnic.net`, `rdap.lacnic.net`,
  `rdap.afrinic.net`) — `rdap.iana.org/autnum/<n>` is a bootstrap
  registry, not a query proxy, and returns HTTP 501 for every
  real query, so the routing-by-RIR layer is mandatory.
  `app/tasks/asn_whois_refresh.refresh_due_asns` ticks hourly,
  walks every `asn` row whose `next_check_at` has elapsed, parses
  holder + last-modified out of the response, derives
  `whois_state` (`ok` / `drift` / `unreachable` / `n/a`), and
  audit-logs every state transition.
  `POST /api/v1/asns/{id}/refresh-whois` drives the same code
  path synchronously for the operator. Operator-tunable cadence
  via new `PlatformSettings.asn_whois_interval_hours` (default
  24, range 1–168). Settings → Network → ASN Refresh surfaces
  the knob.
- **ASN — RPKI ROA pull.** `app/services/rpki_roa.py` fetches the
  global ROA dump from Cloudflare (`rpki.cloudflare.com/rpki.json`
  ~80 MB JSON, ~850k ROAs) or RIPE NCC's validator JSON, filters
  by AS number, and caches the multi-MB payload in-memory for 5
  min via a `_get_cached_roas` so a beat sweep refreshing 50 ASNs
  makes a single HTTP call instead of 50.
  `app/tasks/rpki_roa_refresh.refresh_due_roas` ticks hourly,
  reconciles `asn_rpki_roa` rows additively + with deletes,
  derives state (`valid` / `expiring_soon` / `expired` /
  `not_found`) off `valid_to`, and audit-logs adds / removes /
  state transitions. `valid_from` and `valid_to` parsing accept
  Cloudflare's `expires` (Unix epoch) and RIPE's `notBefore` /
  `notAfter` (ISO 8601) on the same row.
  `POST /api/v1/asns/{id}/refresh-rpki` reuses `_refresh_one_asn`
  for the synchronous per-AS button — same reconcile shape (added
  / updated / removed / transitions) as the hourly beat tick.
  Two new `PlatformSettings` knobs: `rpki_roa_source`
  (cloudflare | ripe) and `rpki_roa_refresh_interval_hours`
  (default 4, range 1–168), surfaced through Settings → Network →
  ASN Refresh.
- **ASN — alert rules.** Four new rule types wired into
  `services.alerts`: `asn_holder_drift` (single-event latch via
  `alert_event.last_observed_value` JSONB so a single flip fires
  exactly one event, auto-resolves after 7 d),
  `asn_whois_unreachable`, `rpki_roa_expiring` (severity
  escalation at threshold/4 + threshold/12 around the operator-
  set `threshold_days`, default 30 d), and `rpki_roa_expired`.
  Frontend AlertsPage type-picker and AlertRuleType union
  extended.
- **ASN — holder-drift diff viewer.** `asn_whois_refresh` now
  persists `previous_holder` into `whois_data` on every successful
  RDAP refresh — drift or not — so the detail page can render a
  side-by-side without consulting the audit log. WHOIS tab on ASN
  detail renders a rose-tinted diff card when
  `whois_state === "drift"`: previous holder vs current holder
  plus the timestamp drift was detected.
- **ASN — BGP peering relationships (#89).** New `bgp_peering`
  table — operator-curated graph of BGP relationships between
  tracked ASNs (`peer | customer | provider | sibling`). Both
  endpoints are FK ON DELETE CASCADE because a peering row is
  meaningless once one endpoint is gone. Unique on
  `(local, peer, relationship_type)`. Column named
  `relationship_type` (not `relationship`) so it doesn't shadow
  the imported `sqlalchemy.relationship` function in the model
  body. New `router.local_asn_id` FK ON DELETE SET NULL stamps
  which AS a router originates routes from. CRUD endpoints under
  `/api/v1/asns/peerings` (router-level `manage_asns` gate
  inherited). New `PeeringsTab` on the ASN detail page with a
  directional listing (`→ outbound` / `← inbound` from this AS's
  POV) and clickable counter-AS that links to the peer's detail
  page. `PeeringFormModal` lets operators pick the counterparty
  (filtered to exclude self), pick whether "this AS is the local
  side" or "the counterparty is the local side" (modal normalises
  to canonical `(local, peer, relationship)` shape on submit),
  pick the relationship with inline copy explaining each, plus a
  free-form description. Edit limits the editable fields to
  relationship + description (the (local, peer) pair is the row's
  natural key). Migration `d3f2a51c8e76_bgp_peering`.
- **ASN — BGP communities catalog (#88).** New `bgp_community`
  table; `asn_id` is nullable so platform-level rows (RFC 1997 /
  7611 / 7999 well-knowns) can be shared across all ASes. `kind`
  denormalises which on-the-wire shape `value` carries:
  `standard` / `regular` (`ASN:N` per RFC 1997) / `large`
  (`ASN:N:M` per RFC 8092). `inbound_action` /
  `outbound_action` capture free-form policy hints.
  `app.services.bgp_communities` owns the well-known catalog
  (no-export, no-advertise, no-export-subconfed, local-as,
  graceful-shutdown, blackhole, accept-own) and seeds it on first
  boot via a hook in `main.py`'s lifespan; subsequent boots
  refresh the description text so upgrades that reword a row land
  without an admin edit. CRUD: `GET /asns/communities/standard`
  (read-only catalog), `GET|POST /asns/{asn_id:uuid}/communities`,
  `PATCH|DELETE /asns/communities/{community_id:uuid}`. Format
  validators per kind: standard must be one of the seven seeded
  names; regular matches `\d+:\d+`; large matches
  `\d+:\d+:\d+`. Standard catalog rows refuse PATCH / DELETE
  with a 400 explaining they're platform-owned. New
  `CommunitiesTab` on the ASN detail page with a collapsible
  standard-catalog table at the top with "Use on this AS" buttons
  per row that pre-fill the form, plus the per-AS list grouped by
  kind. Migration `f4a6c8b2e571_bgp_communities`.
- **VRFs as first-class entities (#86).** New `vrf` table carries
  name, description, `asn_id` FK, `route_distinguisher` with
  RD-format validation, split import / export RT lists, tags,
  custom_fields. `ip_space` and `ip_block` both gain a nullable
  `vrf_id` FK ON DELETE SET NULL. Migration backfills new VRF
  rows from every distinct (vrf_name, rd, rt-list) triple on
  existing IPSpace rows and stamps each space's `vrf_id` at the
  matching new row; the freeform columns stay in place for one
  release cycle so operators can verify the mapping landed
  correctly before they get dropped. CRUD at `/api/v1/vrfs`
  (list with asn_id + search filters and pagination, create / get
  / update / delete, bulk-delete with force-detach semantics),
  audit-logged on every mutation. `manage_vrfs` permission seeded
  into the Network Editor builtin role. List page at
  `/network/vrfs`; detail page with linked IP spaces / IP blocks
  tabs and an Edit button (Pencil HeaderButton wired to the
  shared `VRFEditorModal`). Phase 2 lights up the cross-cutting
  RD / RT validation: each `ASN:N` entry whose ASN portion does
  not match `vrf.asn.number` produces a non-blocking warning on
  the response; flipping `PlatformSettings.vrf_strict_rd_validation`
  to true escalates the same mismatch to 422. Second warning
  fires when `vrf.asn_id` is null but the RD is in `ASN:N` form,
  reminding the operator to either link an ASN row or move to
  `IP:N` flavour. IPBlock responses also carry a `vrf_warning`
  field that flags when a block's pinned VRF differs from its
  parent space's VRF — intentional in hub-and-spoke designs but
  worth a heads-up. Migrations `2c4e9d1a7f63_vrf_first_class` +
  `b7e2a4f91d35_vrf_phase2`.
- **VRF picker on IPSpace + IPBlock modals.** The four routing-
  context modals (New / Edit IPSpace + Create / Edit IPBlock)
  use a new `VrfPicker` component bound to the `vrf_id` FK. The
  freeform `vrf_name` / `route_distinguisher` / `route_targets`
  text inputs are gone from the IPSpace form — RD + import /
  export RTs live on the VRF row now and are surfaced read-only
  via the picker label. Backend `vrf_id` added to `IPSpaceCreate`,
  `IPSpaceUpdate`, `IPBlockCreate`, `IPBlockUpdate`. Space-detail
  header shows the linked VRF's name + RD + import / export RTs
  (resolved against the cached VRF list) instead of the
  deprecated freeform fields. Legacy rows that still have
  `space.vrf_name` set without `vrf_id` get a "(legacy)" suffix
  and an in-line nudge to migrate.
- **Domain registration tracking (#87).** Distinct from DNSZone —
  tracks the registry side of a name (registrar, registrant,
  expiry, the nameservers the registry advertises) versus the
  records SpatiumDDI serves. New `domain` table with the spec'd
  fields, an httpx-based RDAP client at `app.services.rdap` (10 s
  per-call / 15 s total budget). The TLD → RDAP-base lookup is
  driven by the IANA bootstrap registry at
  `data.iana.org/rdap/dns.json`, cached in-process for 6 h with
  an asyncio lock against thundering-herd refetch + a stale-cache
  fallback if the bootstrap fetch fails — `rdap.iana.org/domain/<n>`
  returns 404 for any non-test domain (only `example.net` etc.
  happen to work), so per-TLD routing is mandatory. CRUD +
  synchronous `POST /domains/{id}/refresh-whois` endpoints under
  the new `manage_domains` permission gate. Refresh writes the
  parsed fields back, recomputes `nameserver_drift` against the
  operator-pinned expected list, and stamps `whois_state` via the
  pure `derive_whois_state` decision tree (unreachable → expired
  → expiring < 30 d → drift → ok). Beat-fired
  `app.tasks.domain_whois_refresh.refresh_due_domains` ticks
  hourly, gates per-row on `Domain.next_check_at`, and self-paces
  via the new `PlatformSettings.domain_whois_interval_hours` knob
  (default 24 h, 1–168 h range). Detail page at
  `/admin/domains/:id` with registration card, expected-vs-actual
  NS diff panel with drift badge, raw WHOIS / Linked DNS Zones /
  Alert History tabs. List page at `/admin/domains` with a sticky
  table, expiry countdown badges (green > 90 d / amber 30–90 d /
  red < 30 d / dark-red expired), per-row Refresh + Edit + Delete,
  multi-select bulk refresh / bulk delete. Domains nav lives in
  the core sidebar (between DNS Pools and Logs) — registration
  tracking is core operational data, not platform admin.
  Migrations `3124d540d74f_domain_registration` +
  `4a9e7c2d18b3_domain_phase2`.
- **Domain — alert rules.** Four new rule types: `domain_expiring`
  (severity escalation at threshold/4 + threshold/12 around the
  operator-set `threshold_days`, default 30 d),
  `domain_nameserver_drift`, `domain_registrar_changed`,
  `domain_dnssec_status_changed`. The two transition-once rules
  latch the observed value into `alert_event.last_observed_value`
  JSONB so a single flip fires exactly one event, auto-resolves
  after 7 d. `alert_rule.threshold_days` is the new params column.
  Frontend AlertsPage exposes the new types in a grouped picker
  with rule-type-specific form fields and help text.
- **DNSZone ↔ Domain explicit linkage.** New
  `dns_zone.domain_id` nullable FK ON DELETE SET NULL. Picker on
  the DNS zone create / edit modal — "Auto-match by zone name"
  remains the default for backward-compat. Domain detail page's
  "Linked DNS Zones" tab prefers the explicit FK and falls back
  to a left-anchored suffix match (`zone === domain || zone.
  endsWith("." + domain)`) when `domain_id` is unset, so child
  zones inherit (`test.example.com` shows up under `example.com`)
  but `example.com.au` correctly does NOT match `example.com`.
  Sub-zones get a small "sub-zone" badge so the operator can tell
  parent vs descendant at a glance. Migration
  `e7b8c4f96a12_dns_zone_domain_fk`.
- **BGP FK on IPSpace / IPBlock.** New optional `asn_id` UUID
  column on both, FK to `asn.id` ON DELETE SET NULL, indexed.
  Schema surfaces it on Create / Update / Response so the API is
  ready, with a new shared `AsnPicker` component at
  `components/ipam/asn-picker.tsx` wired into the New IPSpace,
  Edit IPSpace, Create IPBlock, and Edit IPBlock modals as an
  optional "Origin ASN (BGP)" field. Migration
  `c9f1e47d2a83_bgp_asn_fk`.
- **Dashboard — three new network summary cards under the KPI
  row.** ASNs (public / private count + WHOIS health + ROA expiry
  warnings), VRFs (count + missing-RD / unlinked-ASN warnings),
  Domains (count + expiry buckets + NS-drift indicator). Platform
  Health card moved up to immediately below the KPI ribbon,
  where the colour-coded ribbon belongs.
- **Settings → Network section.** Three new sections: ASN Refresh
  (asn_whois_interval_hours, rpki_roa_source,
  rpki_roa_refresh_interval_hours), Domain Refresh
  (domain_whois_interval_hours), and VRF Validation
  (vrf_strict_rd_validation toggle). Backs the platform_settings
  columns added by the phase 2 migrations.
- **Shared `RdapPanel` for WHOIS rendering.** New component that
  flattens the wire shape into operator-friendly UI: handle / name
  / DNSSEC / port43 headlines, status flags, nameserver chip list
  (domain side), event timeline, and entities flattened from
  nested vCard arrays into per-role org / email / phone / address
  blocks. Raw JSON is still available behind a "Show raw RDAP
  JSON" toggle for ops debugging. Wired into both the ASN detail
  WHOIS tab and the Domain detail WHOIS tab.
- **DNS sub-zone shortcut.** New "Sub-zone" header button on the
  zone detail page pre-fills the New Zone modal with `.<parent>`
  so the operator just types the leading label. Saves a
  back-trip to the group level.
- **IPAM gap-row click → AddAddressModal.** The
  `192.168.0.112 – 192.168.0.120 · 9 free` rows that interleave
  the IP table are now clickable. Click → AddAddressModal opens
  locked to manual mode with the range banner shown above the IP
  input plus First / Last / Random quick-pick buttons.
- **API tag ordering.** `/api/v1` router includes are now
  alphabetised by tag name so the ReDoc / Swagger UI lists
  sections A → Z. Comment at the top reminds future contributors
  to insert in sort order.

### Changed

- **Sticky table headers.** RPKI ROAs / Communities / Peerings
  tables had `sticky top-0` thead rows with `bg-muted/30` (30%
  opaque) so scrolled rows showed through and the headers looked
  visually merged with the data. Switched to `bg-card` (fully
  opaque) plus a `shadow-[inset_0_-1px_0]` trick for the bottom
  divider so the rule stays attached to the sticky header
  instead of getting clipped against the scroll edge.
- **CLAUDE.md roadmap trimmed to GitHub issue links.** Each
  pending roadmap entry (Major roadmap items, Integration
  roadmap, Future ideas — categorised) is now a single-line
  markdown link to the GitHub issue that holds the full design
  body. Section headings, intro paragraphs, and h4 subsection
  headings are preserved so the categorical browse view still
  works — only the multi-paragraph item bodies move out. CLAUDE.md
  drops from 1029 to 427 lines (~58% smaller in the roadmap
  region); the canonical design context lives on GitHub where it
  can be assigned, commented on, milestoned, and linked from PRs.

### Fixed

- **RDAP lookups silently broken end-to-end.** ASN side:
  `rdap.iana.org/autnum/<n>` returns HTTP 501 Not Implemented for
  every real query — IANA's RDAP service is a bootstrap registry,
  not a query proxy. Switched `app.services.rdap_asn` to derive
  the RIR via `derive_registry()` and query the RIR's RDAP base
  directly. Domain side: same story — `rdap.iana.org/domain/<n>`
  returns 404 for any non-test domain. Added a TLD → RDAP-base
  lookup driven by the IANA bootstrap registry at
  `data.iana.org/rdap/dns.json`. Routes `.net` →
  `rdap.verisign.com/net/v1/`, `.com` → same, etc.
- **RPKI ROA pull was permanently locked out for fresh ASNs.**
  `ASN.next_check_at` is owned by the WHOIS refresh task — it
  bumps the column ~24 h forward on every successful RDAP pull.
  The RPKI ROA refresh task was *also* gating its first-time-pull
  SELECT on that same column, which meant the first WHOIS refresh
  after an ASN was created (typically within minutes) would push
  `next_check_at` past `now()`, and the RPKI sweep would never
  see the ASN as eligible. Net effect: zero ROAs ever landed for
  any ASN that had at least one WHOIS refresh — which is every
  public ASN. Dropped the gate on the first-time SELECT entirely;
  the source-side service caches the global ROA dump in-memory
  for 5 min so back-to-back sweeps don't fan out to N network
  calls.
- **RPKI ROA validity windows were always empty.** The service
  docstring said the public mirrors don't surface `valid_from` /
  `valid_to`, but Cloudflare's `rpki.json` actually ships
  `expires` (Unix epoch) on every ROA and RIPE's validator emits
  `notBefore` / `notAfter` (ISO 8601). Added `_parse_validity`
  that accepts either shape; `valid_to` now lands on every row,
  `valid_from` lands on RIPE rows.
- **`GET /asns/{id}/rpki-roas` route never existed.** Frontend
  client was calling the endpoint since the wave 2 detail-page
  landed but the backend never implemented it. Every call 404'd
  silently and the React Query result stayed empty. Added the
  route alongside `refresh-rpki` (same router, same gate). Also
  aligned `ASNRpkiRoaState` frontend union to what the task
  actually emits (`valid | expiring_soon | expired | not_found`).
- **`GET /asns/peerings` was 422'ing.** Earlier route registration
  order put `GET /{asn_id}` before `GET /peerings`, so `peerings`
  was being fed to the UUID coercer and rejected. Constrained
  every `{asn_id}` path to Starlette's `:uuid` converter so
  non-UUID strings fall through to the literal `/peerings` and
  `/bulk-delete` matches further down.
- **`prefix` came back as `IPv4Network` not `str`.** asyncpg
  returns CIDR columns as `ipaddress.IPv4Network` / `IPv6Network`
  instances; Pydantic's `str` field type refused to coerce them
  and 500'd every list call. Added a `mode="before"`
  field_validator that round-trips through `str(...)`.
- **Raw RDAP payload rendered as "No raw WHOIS data".** Frontend
  was checking `typeof asn.whois_data?.raw === "string"` but the
  RIRs all serve JSON, so the refresh task stores `raw` as a
  nested object. Switched to a defensive
  `JSON.stringify(raw, null, 2)` pretty-print that also still
  accepts string `raw` values from older snapshots (no migration
  needed). Heading retitled "Raw RDAP response".
- **VRF migration's source-row scan crash.** `route_targets`
  filter was `jsonb_array_length(route_targets) > 0`, which throws
  `cannot get array length of a scalar` on existing rows whose
  `route_targets` JSONB happens to be a string rather than an
  array. Added a `jsonb_typeof(route_targets) = 'array'` guard so
  non-array values just don't match the filter (no VRF row is
  created for them).
- **VRF detail page Edit button.** Hoisted `VRFEditorModal` to
  `export` from `VRFsPage.tsx` and wired a Pencil HeaderButton in
  the `VRFDetailPage` header.

---

## 2026.04.30-1 — 2026-04-30

Notifications-and-automation release. The headline work closes
out the Notifications & external integrations bucket on the
roadmap: SMTP email delivery for the alerts framework + audit
forward, Slack / Teams / Discord chat-channel webhook flavors
that render mrkdwn / MessageCard / embed bodies natively, and a
new typed-event webhook surface — 96 curated events
(`subnet.created`, `dns.zone.updated`, `ip.allocated`, …)
delivered with HMAC-SHA256 signatures via an outbox-backed retry
queue (exponential backoff 2 / 4 / 8 … 600 s, dead-letter on
permanent failure, manual retry from the UI). Bundled with two
DNS deliverables (GSLB pools — priority + weight + health-checked
A/AAAA record sets that auto-render rendered-record sets the
BIND9 driver applies, and a server-detail modal with
logs / stats / config tabs) plus three IPAM deliverables (device
profiling — passive DHCP fingerprinting + active auto-nmap on
fresh leases, IPAM bulk allocate — contiguous IP range stamping
with name templates, and the post-bulk-allocate IPAM table polish
wave: sticky thead, shift-click range select, dashed gap-marker
rows for missing IPs between contiguous allocations, plus a
revamped subnet Tools dropdown). Plus a DNS pool reconciliation
fix (member IP edits + removed-member cleanup + zone-detail
refresh).

### Added

- **Typed-event webhooks** (Phase 2 of notifications-and-
  external-integrations). New `/admin/webhooks` admin surface
  + `POST/GET/PUT/DELETE /api/v1/webhooks` CRUD. 96 typed event
  types derived from a `resource_namespace × verb` cross-product
  (e.g. `space.created`, `subnet.bulk_allocate`,
  `dns.zone.updated`, `dhcp.scope.deleted`, `auth.user.created`,
  `integration.kubernetes.created`); subscribers with empty
  `event_types` match everything. SQLAlchemy
  `after_flush` + `after_commit` listeners snapshot committed
  `AuditLog` rows and write one `EventOutbox` row per matching
  `EventSubscription`. Celery beat (`event-outbox-drain`, 10 s)
  drains the outbox via `SELECT … FOR UPDATE SKIP LOCKED`, signs
  each POST with `hmac(secret, ts + "." + body, sha256)`, and
  retries with exponential backoff (2 / 4 / 8 … 600 s capped) up
  to `max_attempts` (default 8 ≈ 8.5 min cumulative). Permanent
  failures flip to `state="dead"` for operator review. Reserved
  `X-SpatiumDDI-*` headers (Event / Delivery / Timestamp /
  Signature) protected from operator override. Migration
  `0f83a227b16d_event_subscription_outbox_tables`.
- **Webhook admin UI.** Subscriptions page with one-time secret
  reveal modal on create (auto-generated 32-byte hex unless an
  operator supplies their own), event-type multi-select with
  filter box, custom-headers editor, timeout + max-attempts
  inputs. Per-row test button synthesizes a `test.ping` event
  through the live pipeline with an inline success / failure
  flash. Per-row expandable deliveries panel auto-refreshes
  every 8 s and shows state / attempts / last-status / next-
  retry; **Retry now** on failed/dead rows resets attempts and
  re-queues. Edit form supports secret rotation toggle.
- **SMTP delivery for alerts + audit forward** (Phase 1 of
  notifications-and-external-integrations). New SMTP target
  type with host / port / username / encrypted password / TLS
  mode (`starttls` / `ssl` / `none`) / from-address / to-list
  fields. stdlib `smtplib` driven through `asyncio.to_thread`
  (no extra dep). Subject + body rendered from the audit row
  (or the alert event for the alerts framework). Audit-forward
  targets gain `kind="smtp"`; alert rules gain a `notify_smtp`
  toggle alongside the existing syslog + webhook channels.
  Migration `30cda233dce9_add_smtp_chat_flavor_to_audit_forward`.
- **Chat-flavored webhooks** — Slack / Teams / Discord. New
  `webhook_flavor` column on `audit_forward_target` selects
  between generic JSON (default), Slack `mrkdwn` block, Teams
  `MessageCard`, and Discord `embed`. Single payload renderer
  per flavor; no extra dep. Configured by pasting the
  platform's incoming-webhook URL into a webhook target.
- **DNS GSLB pools.** New `DNSPool` model — priority + weight +
  health-checked record sets that render to a rotating set of
  A / AAAA records. The pool has members (each with an
  address, weight, health-check policy) and an enabled/disabled
  flag; on each beat tick (`dns_pool_healthcheck.dispatch_due_pools`
  → per-pool `run_pool_check`) the worker runs every member's
  TCP / HTTP(S) / ICMP probe with configurable
  unhealthy / healthy thresholds, transitions states based on
  consecutive successes / failures, and `apply_pool_state`
  reconciles the rendered records — DELETE the rows for
  members that are now unhealthy or removed; CREATE / UPDATE
  for members whose IP changed or who newly went healthy. All
  changes flow through the existing `enqueue_record_op`
  pipeline so the zone serial bumps once per reconciliation
  pass and the agent applies in driver-native order. UI: new
  Pools tab on the zone detail with members CRUD, weight + IP
  edit, and per-member last-check state badge.
- **DNS server detail modal.** Click a server row in the
  group's Servers tab to open a draggable detail modal with
  three tabs — **Logs** (filtered live tail of the agent's
  query log + structured filters: substring / qtype / client
  IP / since), **Stats** (push-driven 5 m / 15 m / 1 h
  rolling agent metrics: queries-per-second, NXDOMAIN rate,
  cache hit ratio when reported), and **Config** (read-only
  rendered server-options snapshot from the latest applied
  ConfigBundle).
- **IPAM device profiling.** Two new sub-systems converging on
  a unified "Device profile" panel inside the read-only IP
  detail modal.
  - **Active auto-nmap on fresh DHCP leases** (Phase 1).
    Subnet-level opt-in (`Subnet.auto_profile_on_lease: bool`,
    default false). On a fresh lease event the agent posts to
    `/api/v1/dhcp/agents/lease-events`; the API enqueues a
    `service_and_os` nmap run against the new IP, capped at 4
    in-flight scans per subnet with a refresh-window dedupe so
    a flapping lease can't fire-hose the scanner. Per-IP
    re-profile-now button (`POST /ipam/addresses/{id}/profile`)
    on the IP detail modal lets operators kick a fresh scan
    on demand.
  - **Passive DHCP fingerprinting** (Phase 2, default off,
    needs `cap_add: NET_RAW`). DHCP agent gains a scapy
    `AsyncSniffer` thread that captures DHCP DISCOVER /
    REQUEST option lists and posts them to a fingerbank lookup
    task. Results land in the IP row's profile panel as
    Type / Class / Manufacturer (e.g.
    `Phone / VoIP / Polycom`).
  - Same-day follow-ups: `setcap cap_net_raw+eip` on
    `/usr/bin/nmap` plus `NMAP_PRIVILEGED=1` so non-root
    operator OS scans actually work (Debian's nmap does an
    early `getuid()==0` check that ignores file caps),
    `securityContext.capabilities.add: [NET_RAW]` on the K8s
    worker + `worker.netRawCapability` Helm gate for
    restricted PSA / OpenShift-SCC / GKE Autopilot, and a
    Settings → IPAM → Device Profiling form for the
    fingerbank API key (Fernet-encrypted at rest; response
    only exposes a boolean `fingerbank_api_key_set`).
- **IPAM bulk allocate.** New `POST /ipam/subnets/{id}/bulk-
  allocate/{preview,commit}` stamps a contiguous IP range plus
  a name template (`{n}` / `{n:03d}` / `{n:x}` /
  `{oct1}`–`{oct4}` octet fragments) in one shot. Per-row
  conflict detection (already-allocated, dynamic-pool overlap,
  FQDN collision) with `on_collision: skip|abort` policy,
  capped at 1024 IPs per call. New `BulkAllocateModal` lives
  under the subnet Tools menu with a three-phase form →
  preview → committed flow and live client-side template
  rendering as the operator types.
- **Nmap subnet sweep + bulk operations.** Two new presets —
  `subnet_sweep` (`-sn` ping-sweep, capped at /16 worth of
  hosts) and `service_and_os` (`-sV -O --version-light`, the
  device-profiling default). CIDR-aware target validation +
  multi-host XML parsing — the runner walks every `<host>`
  element and emits a `hosts[]` summary when more than one
  responds. New `POST /nmap/scans/bulk-delete` (cap 500,
  mixes cancel + delete based on per-row state) and
  `POST /nmap/scans/{id}/stamp-discovered` (claim alive hosts
  as `discovered` IPAM rows + stamp `last_seen_at`;
  integration-owned rows just bump the timestamp). The
  `NmapToolsPage` is rewritten as a 3-tab right panel
  (Live / History / Last result) with a checkbox column +
  bulk-delete toolbar on history.
- **Seen recency column on IPAM IP table.** New "Seen" column
  backed by a 4-state `SeenDot` (alive < 24 h green / stale
  24 h–7 d amber / cold > 7 d red / never grey, source method
  in the tooltip — orthogonal to lifecycle status).
- **Tools dropdown on subnet header.** IPAM subnet header
  collapsed from 9 buttons to 6 via a Tools dropdown
  (alphabetised: Bulk allocate…, Clean Orphans, Merge…,
  Resize…, Scan with nmap, Split…). New `discovered` status
  added to `IP_STATUSES_INTEGRATION_OWNED` so nmap-stamped
  rows show up correctly across the integration colour-coding.

### Changed

- **IPAM table polish — sticky `<thead>` finally holds in
  Chrome.** The inner `<div className="overflow-x-auto">`
  wrapper was establishing a Y-scroll context per CSS spec —
  `overflow-x: auto` with `overflow-y: visible` computes to
  `overflow-y: auto` automatically, defeating sticky
  positioning by anchoring the head to a non-scrolling
  intermediate parent. Removed the wrapper so sticky resolves
  to the outer `flex-1 overflow-auto`.
- **Shift-click range select on IPAM IP checkboxes.** Capture
  `e.shiftKey` in `onClick` (which fires before `onChange`),
  walk the IP-only `tableRows` order between the previous
  click and the new one, and toggle every selectable row to
  the new state.
- **Subtle dashed-emerald gap-marker rows in the IP table.**
  Between non-adjacent IPAM entries (e.g. `.11 · 1 free` or
  `.11 – .13 · 3 free`) a heads-up row makes deleted /
  missing IPs visible — humans tend to miss single-row gaps
  scrolling a long table. Suppressed inside dynamic DHCP
  pools where slots are owned by the DHCP server.

### Fixed

- **DNS pool member IP edits silently dropped.** The
  `PoolMemberUpdate` Pydantic schema only declared `weight` +
  `enabled`, so a frontend PUT carrying a new `address` was
  filtered out before the handler saw it; the diff loop in
  `PoolsView` was also only checking `enabled` / `weight`.
  Added `address` to both the schema (with an IP validator +
  uniqueness guard returning 409 on collision) and the
  frontend diff. Address change resets the member's health
  stats (`last_check_state="unknown"`, counters → 0) so the
  new IP re-proves health.
- **DNS pool member removal didn't clean up rendered records.**
  `apply_pool_state` only iterated `pool.members` (the in-
  memory list), so records whose member had just been deleted
  were missed before the FK CASCADE stripped the row at SQL
  commit. Added an orphan sweep using a JOIN through
  `DNSPoolMember.pool_id` that catches records whose member
  is no longer attached to the pool, deleting them as part of
  the same reconciliation pass.
- **Pools tab Refresh button on zone detail.** The header
  Refresh on the zone view only invalidated `["dns-records"]`,
  so the Pools tab + per-zone server-state pill stayed
  stale. Now invalidates `["dns-records"]`, `["dns-pools"]`,
  and `["dns-zone-server-state"]`.
- **Reconciliation gate widened on pool member edit.** Was
  `enabled_changed` only — now `member_changed` covers any of
  address / enabled / weight, so a pure address change
  triggers reconciliation (was previously a no-op).
- **CodeQL alerts #19 + #20 — polynomial-redos in the bulk-
  allocate hostname-template parser.** The hand-rolled regex
  driving the `{n}` / `{oct1-4}` substitution
  (`\{(n|oct[1-4])(?::([^}]+))?\}`) was flagged as polynomial
  under `re.sub`: for adversarial inputs starting with `{{n:`
  and many repetitions, every `{` starting position triggered
  an O(n) backtrack scan of the inner `[^}]+`, taking the
  whole substitution to O(n²). Same shape as the BIND9 query
  log parser fixes #16 / #18. Replaced with the stdlib
  `string.Formatter` parser — a C-implemented linear-time
  tokenizer that already understands the exact grammar we
  want and handles escaped `{{` / `}}` correctly. 250 kB
  adversarial input (`{{n:|` × 50000) now renders in ~5 ms.
  Companion `_bulk_template_has_token` warning detector
  uses the same parser, so escaped tokens like `a{{n}}b` no
  longer trip a false-positive warning.
- **`event_outbox` Celery task corrupted asyncpg in prefork
  workers.** First implementation imported `AsyncSessionLocal`
  from `app.db`, which binds asyncpg connections to the loop
  that first checked them out. Celery's prefork pool reuses
  processes across tasks, so the second `asyncio.run`
  re-entered with a different loop and surfaced as `Future
  attached to a different loop` followed by cascading
  `cannot perform operation: another operation is in
  progress` errors. Replaced with a per-tick `NullPool`
  ephemeral engine — same pattern `audit_forward.
  _ephemeral_session` and `event_publisher._ephemeral_session`
  use.

---

## 2026.04.28-2 — 2026-04-28

DNS finish-line + IPAM subnet planning + DHCP option authoring
release. The headline work closes most of the remaining
DNS-specific roadmap items in CLAUDE.md: a multi-resolver
propagation check that fans out to Cloudflare / Google / Quad9 /
OpenDNS in parallel, conditional forwarders as a first-class
zone type, a curated catalog of 14 well-known public RPZ
blocklist sources, a zone-delegation wizard that finds the
parent zone in the same group and auto-stamps the NS + glue
records, four starter zone-template wizards (Email with
MX/SPF/DMARC, Active Directory with the standard SRV records,
Web with apex A + www CNAME, k8s external-dns target), full
TSIG key management with Fernet-encrypted secrets and a
"copy this secret now" reveal modal, a clickable DNS query
analytics strip on the Logs page (top qnames + top clients +
qtype distribution), and BIND9 catalog zones (RFC 9432) with
producer / consumer roles auto-derived from the group's
primary. On the IPAM side the subnet planner lands as a
draggable multi-level CIDR design surface with transactional
apply, the block detail tooling gains a CIDR calculator,
address planner, aggregation suggestion, and free-space
treemap, and bulk-select on block detail reaches parity with
the space view. Plus DHCP scope authoring gets an option-code
library lookup (95-entry RFC 2132 + IANA catalog with
autocomplete on the custom-options row) and named option
templates that can be applied to a scope in one click.
Also: vendor-neutral LLDP neighbour collection on network
devices, hostname targets for the nmap scanner, and a
follow-up linear-time fix for CodeQL alert #18 in the BIND9
query log parser (same shape as the #16 fix that landed in
2026.04.28-1).

### Added

- **DNS multi-resolver propagation check.** New `/dns/tools/
  propagation-check` POST endpoint fires the same query against
  Cloudflare / Google / Quad9 / OpenDNS in parallel via
  `dnspython`'s `AsyncResolver` and returns per-resolver
  `{resolver, status, rtt_ms, answers, error}`. Each query
  carries its own timeout so a slow resolver can't poison the
  others. UI surfaces as a Radar button on every record row in
  the records table; modal lets the operator switch record
  type and re-check. Driver-agnostic — queries are made from
  the API process, doesn't touch the BIND9 / Windows drivers.
- **Conditional forwarders.** `DNSZone` carries `forwarders`
  (JSONB list of IPs) + `forward_only` (true → `forward only;`,
  false → `forward first;`). When `zone_type = "forward"` the
  BIND9 driver renders `zone "X" { type forward; forward only;
  forwarders { ... }; }` in `zone.stanza.j2` and the agent's
  wire-format renderer (no zone file written, no allow-update);
  the form gates the forwarders/policy fields on the type
  selector and refuses submit when no upstreams are listed.
  `ZoneDetailView` swaps the records table for a forwarders +
  policy panel for forward zones — record management never
  applied there. Migration `a07f6c12e5d3_dns_zone_forwarders`.
- **Curated RPZ blocklist source catalog.** Static JSON shipped
  at `backend/app/data/dns_blocklist_catalog.json` with 14
  well-known public blocklists drawn from AdGuard's
  HostlistsRegistry + Pi-hole defaults + Hagezi / OISD
  (AdGuard DNS Filter, StevenBlack Unified, OISD Small/Big,
  Hagezi Pro / Pro+, 1Hosts Lite, Phishing Army Extended,
  URLhaus, DigitalSide Threat-Intel, EasyPrivacy, plus
  StevenBlack fakenews / gambling / adult). New
  `GET /dns/blocklists/catalog` returns the snapshot
  (in-process cached); `POST /dns/blocklists/from-catalog`
  creates a normal `DNSBlockList` row with `source_type="url"`
  prefilled and immediately enqueues `refresh_blocklist_feed`
  so the list populates without a manual click. Frontend
  "Browse Catalog" button on the Blocklists tab opens a
  filterable picker (category + free-text), with already-
  subscribed entries flagged.
- **Zone delegation wizard.** `services/dns/delegation.py`
  finds the longest-suffix-matching parent zone in the same
  group (forward zones excluded), reads the child's apex NS
  records, and computes the NS records the parent needs to
  delegate the child plus glue (A / AAAA) for any
  in-bailiwick NS hostnames. Diffs against existing parent
  records so a second run is a no-op, surfaces warnings
  ("ns1 is in-bailiwick but has no A/AAAA in child"), and
  applies through the normal `enqueue_record_op` pipeline so
  the parent zone's serial bumps once. Endpoints
  `GET /dns/groups/{gid}/zones/{zid}/delegation-preview` +
  `POST /dns/groups/{gid}/zones/{zid}/delegate-from-parent`.
  Frontend: contextual "Delegate" button appears in the zone
  header only when an eligible parent has missing records;
  `DelegationModal` shows the exact records that would land
  in the parent before commit.
- **DNS template wizards.** Static catalog at
  `backend/app/data/dns_zone_templates.json` with four starter
  shapes (Email zone with MX + SPF + DMARC + optional DKIM
  selector, Active Directory zone with the standard LDAP /
  Kerberos / GC SRV records + optional `_sites` entries, Web
  zone with apex A + optional AAAA + `www CNAME`, Kubernetes
  external-dns target — empty zone). `services/dns/
  zone_templates.py` validates required parameters and
  substitutes `{{key}}` placeholders (plus a built-in
  `{{__zone__}}`) at materialise time; records can declare
  `skip_if_empty: ["param"]` so optional fields drop out
  cleanly. Endpoints `GET /dns/zone-templates` +
  `POST /dns/groups/{gid}/zones/from-template`. Frontend
  `ZoneTemplateModal` mounted as a "From Template" button on
  the ZonesTab header alongside "Add Zone".
- **TSIG key management UI.** New `DNSTSIGKey` model with
  Fernet-encrypted `secret_encrypted`, `algorithm` enum
  (hmac-sha1 / 224 / 256 / 384 / 512), `name`, `purpose`,
  `notes`, `last_rotated_at`. CRUD at `/api/v1/dns/groups/
  {gid}/tsig-keys` with a side `/generate-secret` helper that
  returns a fresh random base64 secret of the right size for
  the chosen algorithm, and a `/{kid}/rotate` endpoint that
  re-randomises the secret. Plaintext is returned **once** on
  the create / rotate response — list / get never expose it.
  Operator-managed rows distribute to every BIND9 agent in
  the group via the existing `tsig_keys` block in the
  `ConfigBundle` (alongside the legacy auto-generated agent
  loopback key); named.conf renders one
  `key { algorithm …; secret …; };` stanza per row. UI: new
  "TSIG Keys" tab on the DNS server group view, with create /
  edit / rotate / delete plus a one-shot "Copy this secret
  now" modal after each create / rotate. Migration
  `7c299e8a5490_dns_tsig_keys`.
- **DNS query analytics.** `POST /api/v1/logs/dns-queries/
  analytics` returns top-10 qnames + top-10 clients + complete
  qtype distribution in a single round trip. Computed
  on-demand via `GROUP BY` against the existing
  `dns_query_log_entry` rows (24 h retention) — no new schema,
  no new beat task. The Logs → DNS Queries tab renders an
  Analytics strip above the raw event grid: three cards each
  showing key + count + percentage of total, with every row
  clickable to seed the corresponding filter (qname /
  client_ip / qtype). The strip refetches only when
  `(server_id, since)` changes, so per-keystroke filter edits
  on the events grid don't pay for a re-aggregation.
- **BIND9 catalog zones (RFC 9432).** Opt-in per group via
  `DNSServerGroup.catalog_zones_enabled` +
  `catalog_zone_name` (defaults to `catalog.spatium.invalid.`).
  The producer is the group's `is_primary=True` bind9 server;
  every other bind9 member joins as a consumer. Bundle
  assembly emits a `catalog` block per server: `mode=producer`
  ships the member zone list, `mode=consumer` ships the
  producer's IP. The agent renders the catalog zone file per
  RFC 9432 §4.1 — SOA + NS at apex,
  `version IN TXT "2"`, and one `<sha1-of-wire-name>.zones IN
  PTR <member>` per primary zone — and on consumers injects a
  single `catalog-zones { zone "<catalog>." default-masters {
  <producer-ip>; } in-memory yes; };` directive into the
  options block. The catalog block is part of the structural
  ETag so membership changes trigger a daemon reload, and
  SHA-1 hashing uses the proper wire format (length-prefixed
  labels + null terminator). Frontend toggle lives in the
  server-group create / edit modal alongside the recursion
  checkbox. Migration `d8e4a73f12c5_dns_catalog_zones`.
- **IPAM subnet planner.** New `/ipam/plans` page where the
  operator designs a multi-level CIDR hierarchy as a
  draggable tree (one root + nested children, arbitrary
  depth), saves it as a `SubnetPlan` row, validates against
  current state, then one-click applies — every block +
  subnet created in a single transaction. `kind` is
  explicit per node (`block` or `subnet`); root must be a
  block (subnets need a block parent), and a subnet may not
  have children. Resource bindings (DNS group, DHCP group,
  gateway) are optional per-node — `null` = inherit, explicit
  value sets the field on the materialised row and flips the
  corresponding `*_inherit_settings=False`. Two root modes:
  new top-level CIDR (creates a fresh block at the space
  root) OR anchor to an existing `IPBlock` (descendants land
  as children of the existing block). Validation
  (`/plans/{id}/validate` + `/plans/validate-tree` for
  in-flight trees) checks duplicate node ids, kind rules,
  parent-containment, sibling-overlap, and overlap against
  current IPAM state. Live validation runs every 300 ms; the
  apply confirmation modal surfaces block + subnet counts;
  any conflict mid-apply → 409 with the full conflict list
  and nothing is written. `/plans/{id}/reopen` flips an
  applied plan back to draft state when the materialised
  resources have all been deleted, so operators can iterate
  without starting fresh. Frontend uses `@dnd-kit/core` for
  drag-to-reparent; drops onto descendants OR onto subnet
  targets are refused. Sidebar entry "Subnet Planner"
  alongside NAT Mappings. Migration
  `c8e1f04a932d_subnet_plan`.
- **IPAM subnet planning + calculation tools.** Four
  related additions on the block detail surface:
  - **CIDR calculator** at `/tools/cidr` — pure client-side
    breakdown of any IPv4 or IPv6 prefix
    (network / netmask / wildcard / broadcast / range / total
    addresses / decimal + hex / binary breakdown for v4 and
    compressed + expanded forms for v6). Quick-paste preset
    buttons for the common RFC 1918 / CGNAT / ULA blocks.
    BigInt math throughout so v6 prefixes work cleanly.
    Sidebar entry under Tools.
  - **Address planner** —
    `POST /api/v1/ipam/blocks/{id}/plan-allocation` accepts
    a list of `{count, prefix_len}` requests (e.g. `4 × /24,
    2 × /26, 1 × /22`) and packs them into the block's free
    space using largest-prefix-first ordering with first-
    fit-by-address placement (so sequential same-size
    requests pack contiguously from low addresses). Returns
    the planned allocations + any unfulfilled rows + the
    remaining free space after the plan. Reuses the same
    `address_exclude` walk that powers `/free-space`. UI:
    "Plan allocation…" button next to the Allocation map.
  - **Aggregation suggestion** —
    `GET /api/v1/ipam/blocks/{id}/aggregation-suggestions`
    runs `ipaddress.collapse_addresses` on the block's
    direct-child subnets; any output that subsumes more than
    one input is a clean merge opportunity. Read-only banner
    on the block detail surfaces them when present
    (e.g. `10.0.0.0/24 + 10.0.1.0/24 → /23`).
  - **Free-space treemap** — Recharts squarified Treemap on
    the block detail, toggled via a Band / Treemap selector
    next to the Allocation map header (selection persisted
    in sessionStorage per block). Cells coloured by kind
    (violet child blocks, blue subnets, hashed-zinc free)
    and sized by raw address count. Pixel-thin slices on the
    1-D band become visible squares here.
- **Block-detail bulk-select parity with space view.** The
  block-detail table can now select child blocks (not just
  subnets), so the bulk-action toolbar that's been there at
  the top level is accessible inside any block. Selection
  state moved to the same `subnet:<id>` / `block:<id>` keyed
  Set the space view uses; a single bulk delete cascades a
  mixed set (subnets first, then leaf blocks, allSettled on
  both phases). Subnet-only actions (Bulk Edit, Split, Merge)
  gate on the absence of selected blocks.
- **Vendor-neutral LLDP neighbour collection.** Adds an
  LLDP-MIB (IEEE 802.1AB) walk as a 5th poll step on every
  network device, gated by per-device `poll_lldp` toggle
  (default on). Captures remote chassis ID + port ID
  (subtype-aware decoding — MAC addresses formatted, interface
  names left raw), system name + description, port
  description, and decoded capabilities bitmask (Bridge /
  Router / WLAN AP / Phone / Repeater / Other / Station /
  DocsisCableDevice). Stored in
  `network_neighbour(device_id, interface_id,
  remote_chassis_id, remote_port_id)` with absence-delete
  every poll so stale neighbours fall off cleanly. New API
  `GET /network-devices/{id}/neighbours` with `sys_name` /
  `chassis_id` / `interface_id` filters; new "Neighbours" tab
  on the network device detail page with vendor-aware enable
  hints (Cisco IOS / NX-OS, Junos, Arista EOS, ProCurve /
  Aruba, MikroTik RouterOS, OPNsense / pfSense) when no rows
  are present. Migration `b9e4d2a17c83_network_neighbour`.
- **Nmap accepts hostname targets.** Operators routinely
  scan `router1.lan` without first looking up its IP, and
  nmap does its own DNS resolution at scan time. New
  `_HOSTNAME_RE` validates RFC 1123 labels (rejecting shell
  metachars, spaces, slashes, anything that isn't a valid
  DNS character). The `target_ip` column is widened from
  `INET` to `VARCHAR(255)` (DNS hard upper bound) via
  migration `f4a83cb15920_nmap_target_text`; the column name
  stays for audit / API continuity. Form input relabelled
  "Target" with hostname examples + helper text.
- **DHCP option-code library lookup.** Static catalog of 95
  RFC 2132 + IANA `bootp-dhcp-parameters` v4 entries shipped
  at `backend/app/data/dhcp_option_codes.json` (each entry:
  `code`, `name`, `kind`, `description`, `rfc`). Loaded once
  per process via `services/dhcp/option_codes.py`
  (lru_cache); `search()` helper does case-insensitive
  name/description matching with numeric-prefix code lookup.
  `GET /api/v1/dhcp/option-codes` returns the catalog (with
  optional `q=` substring filter + `limit`). Frontend wires
  it into `DHCPOptionsEditor`'s custom-options row — the
  bare numeric code input is now a combobox that searches by
  code or name, surfaces the description as a hint under the
  row, and auto-fills `name` on pick. Catalog is fetched once
  per session (`staleTime: Infinity`) and filtered
  client-side, so per-keystroke search has no server round-
  trip. v6 catalog deferred until v6-specific UI lands.
- **DHCP option templates.** New `DHCPOptionTemplate` model,
  group-scoped, holds named bundles of option-code → value
  pairs (e.g. "VoIP phones", "PXE BIOS clients"). CRUD at
  `/api/v1/dhcp/server-groups/{gid}/option-templates` +
  `/api/v1/dhcp/option-templates/{id}` plus a server-side
  `POST /scopes/{id}/apply-option-template` for programmatic
  apply (mode `merge` = template wins, mode `replace` = drop
  existing). UI ships a new "Option Templates" tab on the
  DHCP server-group view (mirrors Client Classes / MAC
  Blocks) with the shared `DHCPOptionsEditor` for authoring,
  plus an "Apply template…" picker above the options editor
  on the scope create / edit modal that does a client-side
  merge into the editor's local state — operator still hits
  Save to persist; conflict-key list surfaces inline so the
  operator knows what was overwritten. Apply is a stamp,
  not a binding — later template edits do not propagate back
  to scopes that already used it. Permission gate
  `dhcp_option_template`, seeded into the existing
  "DHCP Editor" role. Migration
  `e7f218ac4d9b_dhcp_option_templates`.
- **DNS Blocklists multi-select.** Conditional bulk-action
  toolbar (Apply / Detach / Refresh / Delete) on the
  Blocklists tab — each button counts only the rows where
  the action makes sense (Refresh skips manual lists,
  Detach skips not-applied, etc.).
- **IP space VRF / RD / RT fields on Create.** The IPAM
  Create IP Space modal gains the same VRF /
  Route-distinguisher / Route-targets fields the Edit modal
  already had — the backend schema accepted them; only the
  create form was the gap. Collapsed by default since most
  homelab deployments don't run multi-VRF.

### Changed

- **Subnet / block utilization is now visible in the
  Allocation map.** Each subnet/block cell carries a
  mid-saturation tint (the slice exists) plus a saturated
  fill sized by `utilization_percent`, so you can see at a
  glance which subnets are nearly full vs nearly empty
  without scrolling to read the table below. Applies to
  both the Band view and the new Treemap view.
- **Per-row Refresh button on the Blocklists tab now shows
  a spinner + auto-polls** until `last_synced_at` advances
  (was silently no-op-feeling).
- **Blocklist per-row action buttons no longer hide on
  hover** — operator complaint after the bulk-actions push
  obscured the per-row affordances.
- **Sidebar nav alphabetised within each section** and the
  audit-log group merged into platform admin (one less
  divider to scan past).
- **Adding a new IPAM block now reparents existing subnets
  too**, not just sibling blocks. Operator intent on adding
  e.g. a /16 inside a /12 that already holds /24 subnets is
  for the /24s to land under the new /16 — matching the
  existing block-reparenting story we already had on
  `create_block`. Audit row carries `reparented_subnets`
  listing what moved.
- **DNS group selection / expand-collapse UX** — clicking
  the name of an already-expanded group no longer collapses
  it (operator complaint after returning from a zone
  drilldown caused unwanted collapse). Click is now
  expand-only; explicit chevron click still toggles.

### Fixed

- **CodeQL alert #18 — polynomial-redos in BIND9 query log
  view-name regex.** The previous shape was
  `\(\s*view\s+(?P<view_paren>[^)]+?)\s*\)` — both `\s+` and
  the lazy `[^)]+?` could match whitespace, so on
  operator-supplied inputs like `(view ` followed by many
  spaces with no closing paren the engine enumerated every
  split between the two and backtracked quadratically
  (~3.4 s on 50k chars in the lab). Same shape as alert #16
  that landed at the start of release 2026.04.28-1. Fix is
  to give every segment a disjoint character class:
  `\(view\s+(?P<view_paren>[^)\s]+)\s*\)`. Adversarial
  200k-char input now parses in ~4 ms; existing parser tests
  still pass. Taint source is the agent-posted query log
  `raw` field, so this is a real DoS surface.
- **Live nmap SSE viewer now clears the output buffer when
  the scan ID changes.** The parent reuses the same
  component instance across scans (just swaps the prop), so
  React's `useState` initial value never reset and old
  lines lingered until the first new `data:` frame painted
  over them. Reset moved into the `useEffect` that opens the
  EventSource so it fires on every scan switch.

---

## 2026.04.28-1 — 2026-04-28

Network discovery + nmap release. The headline work is the SNMP
polling surface that walks standard MIBs (IF-MIB, IP-MIB,
Q-BRIDGE-MIB, RFC1213/BRIDGE-MIB fallbacks) on routers + switches
to populate ARP / FDB / interface tables and cross-references the
results back into IPAM (last-seen timestamps, optional auto-create
of discovered IPs, switch-port + VLAN visibility on every IP that's
been observed). Bundled with an on-demand nmap scanner — preset
or custom scans launched from a per-IP "Scan with Nmap" button or
the standalone `/tools/nmap` page, with live SSE output streaming
to the browser and structured XML parsed into a results panel. The
IPAM IP table now opens a read-only detail modal on row click
(replacing the previous edit-form-on-click behaviour); the sidebar
is regrouped (core flattened, Tools section added, Administration
items separated by dividers); the Settings → Discovery section that
toggled a never-implemented stub task is removed; and the BIND9
query log parser is reworked to be linear-time (CodeQL alert #16
closed).

### Added

- **SNMP-based network discovery.** New `/network` top-level page
  for managing routers + switches with read-only SNMP polling.
  Vendor-neutral — works on Cisco / Juniper / Arista / Aruba /
  MikroTik / OPNsense / pfSense / FortiNet / Cumulus / SONiC /
  FS.com / Ubiquiti out of the box because everything walks
  standard MIBs.
  - **Data model** (migration `c4e7a2f813b9_network_devices`):
    `network_device` row carries SNMP credentials Fernet-encrypted
    at rest (v1 / v2c community OR v3 USM with auth + priv
    protocol enums + context name); `network_interface`,
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
  - **Polling pipeline.** `pysnmp` 6.x async with `bulkWalkCmd`
    (one OID column per walk to avoid GETBULK PDU bloat that
    timed out on UniFi switches). `app.tasks.snmp_poll.poll_device`
    runs sysinfo → interfaces → ARP → FDB sequentially under a
    per-device `SELECT FOR UPDATE SKIP LOCKED` so concurrent
    dispatches can't double-poll the same row.
    `dispatch_due_devices` beat-fires every 60 s and queues every
    active device whose `next_poll_at <= now`. Per-device interval
    default 300 s, minimum 60 s. Status: `success | partial |
    failed | timeout`, with `last_poll_error` populated for ops
    triage. Stale ARP entries are kept with `state='stale'` (no
    delete); `purge_stale_arp_entries` daily beat task removes
    rows older than 30 days.
  - **IPAM cross-reference.** After every successful ARP poll,
    `cross_reference_arp` finds matching `IPAddress` rows in the
    device's bound `IPSpace` and updates `last_seen_at` (max-merge),
    `last_seen_method='snmp'`, and fills `mac_address` only when
    currently NULL — operator-set MACs are never overwritten. When
    the per-device `auto_create_discovered=True` toggle is on
    (off by default), inserts new `status='discovered'` rows for
    ARP IPs that fall inside a known `Subnet`. Returns counts
    (`updated`, `created`, `skipped_no_subnet`).
  - **Switch-port column in IPAM.** The IPAM IP table carries a
    "Network" column showing `<device> · <port> [VLAN N]` for the
    most-recent FDB hit on each IP's MAC, with a `+N more` badge
    + hover tooltip listing every (device, port, VLAN) tuple when
    the MAC is learned in multiple places. Backed by a batched
    `GET /api/v1/ipam/subnets/{id}/network-context` endpoint that
    returns `{ip_address_id: NetworkContextEntry[]}` in one round
    trip — no N+1 fan-out per page-of-IPs. Per-IP detail modal
    keeps the deeper "Network" tab for the full per-MAC drilldown.
  - **API.** Full CRUD at `/api/v1/network-devices` plus
    `POST /test` (synchronous SNMP probe, ≤10 s, returns
    `TestConnectionResult` with sysDescr + classified
    `error_kind`: `timeout | auth_failure | no_response |
    transport_error | internal`), `POST /poll-now` (queues
    immediate Celery task, returns 202 + task_id), and per-device
    list endpoints `/interfaces`, `/arp` (filter by ip / mac /
    vrf / state), `/fdb` (filter by mac / vlan / interface_id).
    All paginated `{items, total, page, page_size}`.
  - **Frontend.** Top-level `/network` page in the core sidebar.
    Per-device detail at `/network/:id` with Overview / Interfaces
    / ARP / FDB tabs, each filterable + paginated. Add/edit modal
    with SNMP-version-conditional credential fields plus inline
    Test Connection (saves first on create, then probes against
    the saved row). New "Network" tab on the IP detail modal
    showing per-IP switch/port table sorted by `last_seen DESC`.
  - **Bulk operations + import/export.** Network page supports
    multi-select with bulk Test / Poll Now / Activate / Deactivate
    / Delete actions, plus per-row Edit pencil. CSV export
    (deliberately no credentials) and CSV import with default-
    community fallback for v1/v2c rows missing the column. Live
    preview validates each row (resolves `ip_space_name` → id,
    checks enums + port range), shows ready/error per row, then
    commits via `Promise.allSettled` of per-row creates with
    per-row outcome reporting.
  - **Permissions.** Single `manage_network_devices` permission
    gates all endpoints (read + write); new "Network Editor"
    builtin role gets it. Superadmin always bypasses.
  - **Tests.** 35 backend tests covering pysnmp wrapper paths
    (mocked: v1 / v2c / v3 auth construction, OID resolution,
    `ipNetToPhysical → ipNetToMedia` fallback,
    `Q-BRIDGE → BRIDGE` fallback, error classification), API CRUD
    + `/test` + `/poll-now` + the four list endpoints +
    `/network-context`, and three cross-reference paths.

- **Nmap scan integration.** On-demand nmap scans against any
  IPv4/IPv6 host from the SpatiumDDI host perspective. Two entry
  points: a per-IP "Scan with Nmap" button on the IPAM detail
  modal, and a standalone `/tools/nmap` page for ad-hoc targets
  (including IPs that aren't in IPAM yet).
  - **Data model** (migration `d2f7a91e4c8b_nmap_scans`):
    `nmap_scan` table carries the target IP + optional FK to the
    matching `IPAddress` row, the operator's preset choice, the
    sanitised port-spec + extra-args, full status / exit-code /
    duration metadata, and the parsed summary JSON. The actual
    XML artefact lands in `raw_xml`; the line-buffered human
    output lands in `raw_stdout` (so the SSE stream has something
    to replay if an operator opens a viewer mid-scan).
  - **Presets.** `quick` (`-T4 -F`), `service_version` (`-T4 -sV
    --version-light`), `os_fingerprint` (`-T4 -O`),
    `default_scripts` (`-T4 -sC`), `udp_top100` (`-T4 -sU
    --top-ports 100`), `aggressive` (`-T4 -A`), and `custom`
    (everything from `extra_args`).
  - **Argv hardening.** `build_argv` validates target IPs via
    `ipaddress.ip_address`, port-specs against `^[0-9,\-,UTSI:]+$`,
    and shlex-tokenises operator-supplied extra args, rejecting
    any token containing shell metacharacters
    (`;|&$\`<>()`) or path traversal in `--script` values. The
    subprocess is spawned via `create_subprocess_exec` — never a
    shell. nmap runs as the API container's non-root user, so
    privileged scan modes (raw SYN, OS detection without
    privilege) silently degrade to TCP-connect.
  - **Dual output for live UX.** `nmap -oN -` streams human-
    readable output to stdout (what the operator sees scrolling
    in the live viewer); `-oX <tmpfile>` writes structured XML
    to a per-scan tempfile in parallel. After process exit, the
    runner reads the XML, parses it into `summary_json`, and
    unlinks the file. No XML wall-of-text in the live view.
  - **API.** `POST /api/v1/nmap/scans` (queues a celery task,
    returns 202 + the row), `GET /scans` (paginated list, filter
    by ip_address_id / target_ip / status), `GET /scans/{id}`
    (full record), `GET /scans/{id}/stream` (SSE — emits one
    `data:` frame per nmap stdout line, then a final `event:done`
    on terminal status), and `DELETE /scans/{id}` (cancels
    queued/running scans, hard-deletes terminal ones — both
    paths share the trash button in the UI).
  - **SSE auth.** `EventSource` can't set Authorization headers,
    so the stream endpoint accepts `?token=<jwt-or-api-token>`.
    A dedicated `_resolve_user_from_query_token` helper validates
    the token against the same JWT / API-token paths that the
    Bearer dep uses; the router has no global
    `Depends(get_current_user)` because that would 401 the SSE
    request before the query-token resolver could run (each
    non-SSE endpoint declares its own permission dep instead).
  - **Frontend.** `NmapScanModal` flips between the form view and
    the live output viewer (Cmd+K-style). `NmapScanForm` carries
    the preset radio group + port-spec + extra-args + lockable
    target. `NmapScanLiveViewer` opens an `EventSource`,
    appends each line to a `<pre>` with auto-scroll, and renders
    the parsed summary panel (open ports table + OS guess) on
    `done`. `NmapToolsPage` reuses the same form + viewer
    components, plus a Recent Scans table with row-click to
    open and per-row delete (both with a custom
    `ConfirmDeleteScanModal` instead of `window.confirm`).
  - **Permissions.** Single `manage_nmap_scans` permission gates
    all endpoints, seeded into the existing "Network Editor"
    builtin role.
  - **Image.** `nmap` added to the api Dockerfile's apt-get
    install list.

- **IP detail modal.** Clicking an IP row in the IPAM table now
  opens a read-only detail surface (`IPDetailModal`) with status /
  role / DHCP-mirror badges, hostname + FQDN + MAC + OUI vendor,
  forward / reverse DNS zone references, DNS / DHCP linkage
  flags, tags, custom-fields table, and the per-IP SNMP network-
  context inline. Action buttons in the modal header: **Scan with
  Nmap**, **Edit** (hops into the existing form), **Delete**
  (routes through the existing orphan-vs-purge confirm).
  Read-only rows (network / broadcast / DHCP-mirror / orphan /
  read-only statuses) stay inspectable but hide the Edit /
  Delete actions. The pencil + trash icons in the row's right-
  edge cell still behave as before — the detail modal is purely
  additive.

- **Network device CSV import / export.** Import accepts CSV via
  file picker or paste, validates each row pre-commit (resolves
  `ip_space_name` → id, checks enums + port range), and shows
  per-row status. Export downloads
  `network-devices-<utc>.csv` with name / hostname / ip_address /
  device_type / description / vendor / snmp_version / snmp_port /
  ip_space_name / is_active / last_poll_status — deliberately no
  community / v3 keys since exports must not leak credentials.
  An import-time "default community" field fills v1/v2c rows
  missing the column so round-trip exports + edits + re-imports
  work without re-typing communities per device.

### Changed

- **Sidebar regroup.** Core nav reordered for data-flow logic
  (Dashboard → IPAM → VLANs → NAT → DNS → DHCP → Network →
  Logs). New **Tools** section between core and Integrations
  (always visible, default-open) holds the Nmap entry. The
  Administration section's 11 items are now grouped into
  Identity & Access (Users, Groups, Roles, Auth Providers, API
  Tokens) → Platform (Settings, Custom Fields, Alerts, Platform
  Insights, Trash) → Audit (Audit Log), separated by horizontal
  dividers within the same collapsible parent — no nested
  collapsibles. Collapsed-rail mode flattens cleanly.

- **README "What's in the box".** The 17 dense paragraph-bullets
  are replaced with five category-grouped tables (Core DDI /
  Discovery & visibility / Integrations / Identity & ops /
  Deployment) plus a one-line tagline above. Each row carries
  an emoji + bold feature name + 6-10 word detail — eyes scan
  in seconds. The original long-form prose is preserved verbatim
  under a `<details>` disclosure for evaluators who want the
  full spec.

- **BIND9 query log parser.** Reworked to drop the polynomial-
  ReDoS regex shape that CodeQL alert #16 flagged. The previous
  iteration's three independent `\s+`-anchored optional groups
  (parenthesised view, bare view, qname/qclass/qtype/flags
  chain) gave the engine room to try multiple alignments of
  whitespace runs on adversarial input. Replaced with a hard
  split on the unambiguous `: query: ` literal: a tiny linear
  `_HEAD_RE` matches client + port and emits the remainder,
  `_VIEW_RE` extracts an optional view name from that remainder,
  and `_BODY_RE` matches qname/qclass/qtype/flags anchored at
  the start of the post-separator slice. Each regex is now
  clearly linear; the existing 13 parser tests pass unchanged.

- **CI workflow on docs-only pushes.** `ci.yml` now uses
  `paths-ignore` to skip the lint / typecheck / test pipeline
  when a push only touches `**/*.md`, `docs/**`, `LICENSE`,
  `NOTICE`, `.gitignore`, or issue/PR templates.

### Removed

- **Settings → Discovery section.** The two toggles
  (`discovery_scan_enabled`, `discovery_scan_interval_minutes`)
  shipped in 2026.04.16-1 with a Celery task stub that never
  did anything — no beat schedule, no production code reading
  the flags. Real discovery is the SNMP polling surface above
  (with its own per-device `auto_create_discovered` toggle).
  Migration `a4d92f61c08b_drop_discovery_scan_settings` drops
  both columns; the Settings page section is gone; the stub
  Celery task is deleted. Destructive but safe — the columns
  held no operational data.

### Fixed

- **Nmap task dispatch.** `app.tasks.nmap.*` had no entry in
  `task_routes` so dispatched scans landed in celery's default
  `celery` queue, which the worker doesn't subscribe to (worker
  consumes only `ipam` / `dns` / `dhcp` / `default`). Added
  the route. Without this, scan rows stayed `queued` forever
  and the SSE stream just polled an empty `raw_stdout`.

- **Nmap SSE 401.** The router-level `Depends(get_current_user)`
  fired the Bearer extractor before the per-endpoint query-token
  resolver could run, so EventSource (which can't set Authorization
  headers) always 401'd on `/scans/{id}/stream`. Removed the
  router-level dep; every other endpoint already enforces auth
  via its own permission dep.

- **Confirm-delete dialogs in nmap.** The first cut used the
  browser's `window.confirm()` which doesn't match the rest of
  the app's modal patterns. Replaced with a shared
  `ConfirmDeleteScanModal` showing the target IP, preset, and
  status; verb flips between "Cancel" (running) and "Delete"
  (terminal).

### Notes

- The SSE stream is implemented as a 500 ms-poll over the DB-
  persisted `raw_stdout` column (one `db.get(NmapScan, …)` +
  `expire_all()` per tick per active stream). For nmap that's
  fine — the tool emits lines at human cadence — but it's a hot
  loop per concurrent viewer. If many operators end up watching
  live scans simultaneously (more than ~20-30) it'll show up as
  measurable Postgres load; the natural follow-up is a Redis
  pub/sub fanout or `LISTEN/NOTIFY` behind the same HTTP shape.
  Tracked under deferred follow-ups in `CLAUDE.md`.

- SNMP polling lives in the existing Celery worker pool. That's
  fine to ~100 devices on a 5-min interval. Splitting into a
  dedicated `snmp-poller` container becomes interesting once
  SNMP traffic competes with the worker's other tasks or when
  the operator wants different network reachability for the
  poller (different VLAN, jumphost, etc) — also tracked as a
  deferred follow-up.

- Nmap runs as a non-root user inside the api container. That's
  the right default for a containerised service, but it means
  raw SYN scans (`-sS`) and unprivileged OS detection silently
  fall back to TCP-connect. Operators running on bare metal can
  give the API process `CAP_NET_RAW` to unlock those modes;
  containerised deployments can't and shouldn't.

---

## 2026.04.26-1 — 2026-04-26

IPAM operations + observability release. The headline work is the
soft-delete + Trash recovery surface for accidental deletions, three
preview-then-commit subnet operations (find-free / split / merge),
NAT mapping cross-reference into IPAM, the new
`/admin/platform-insights` page surfacing Postgres + container stats
without a Prometheus pipeline, and dashboard sub-tabs that split
the home page into Overview / IPAM / DNS / DHCP. Also bundles per-IP
role + reservation TTL + MAC observation history, DHCP lease history
forensics, IPSpace VRF metadata, VXLAN UI surface, the `task_session`
helper that fixes a long-standing Celery loop-leak across seven
tasks, the DNS-agent 404 → re-bootstrap recovery path, k8s + Helm
worker / beat liveness probes, ReDoS hardening on the BIND9 + Kea
log-line parsers, and the f963137 fix (status-validator +
`user_modified_at` lock + Proxmox bridge gateway) that was pushed
in the previous cycle but never made the changelog.

### Added

- **Dashboard sub-tabs.** Home page now sits under four tabs:
  Overview / IPAM / DNS / DHCP. Selection persists to
  ``localStorage`` so reload lands on the last-viewed tab; the KPI
  strip stays visible across all tabs (subnet count + utilisation +
  zones + servers as the always-present inventory). Per tab:
  - **Overview** — heatmap, Top Subnets (compact, top 6), Live
    Activity feed, Platform Health card, empty-state for fresh
    installs. The "everything-at-a-glance" page.
  - **IPAM** — heatmap (also shown), three new summary cards (IPv4
    vs IPv6 subnet-count split, total NAT mappings, IPv4 capacity
    headroom), Top Subnets extended list (top 20), and the
    Integrations panel (Kubernetes / Docker / Proxmox / Tailscale)
    moved here since they all populate IPAM rows.
  - **DNS** — DNS query rate chart full-width (previously cramped
    into a half-width slot on the home page) + DNS server list
    with status / driver / group / last-seen columns. Empty-state
    explains how to register a server.
  - **DHCP** — DHCP traffic chart full-width + DHCP server list
    + HA Pairs section listing groups with ≥ 2 Kea members.
    Same empty-state pattern.
  Refresh button now also invalidates the new
  ``["nat-mappings", "count"]`` and ``["platform-health"]`` keys
  alongside the existing dashboard query keys.

- **Platform Insights admin page.** New `/admin/platform-insights`
  surface with two tabs covering the bits operators usually need a
  separate Prometheus / pgwatch / Grafana pipeline for:
  - **Postgres** — version + DB size, cache hit ratio, current WAL
    position, active vs max connections, longest-running
    transaction (PID / age / state / query / app / client). Tables
    by total size (heap + indexes + TOAST, live + dead rows, last
    autovacuum) for catching unbounded growth in audit / metrics
    / log tables. Connections grouped by state with "idle in
    transaction" tinted amber — the canonical signal for a stuck
    pool. Slow queries from `pg_stat_statements` if the extension
    is enabled, with a friendly hint when it isn't (we don't
    install it ourselves; needs `shared_preload_libraries` +
    restart).
  - **Containers** — per-container CPU% (computed the same way
    `docker stats` does), memory used / limit / %, network rx /
    tx, block-IO read / write. Default-filtered to the
    `spatiumddi-*` prefix; pass empty `prefix=` to see every
    container on the host. Tone-coded (red >80% CPU, amber >50%;
    red >90% memory, amber >75%) and auto-refreshes every 5 s.
    Endpoint reports `available=false` with a one-line hint when
    `/var/run/docker.sock` isn't mounted into the api container —
    operator opt-in via the same compose toggle the Docker
    integration uses. K8s side covered the same way via a
    hostPath mount.
  Backend at `app/api/v1/admin/postgres.py` (4 endpoints) +
  `app/api/v1/admin/containers.py` (1 endpoint). Sidebar entry
  under Admin → Platform Insights with a Cpu icon.

- **NAT mapping ↔ IPAM tighter integration.** NAT records used
  to be loose strings; an `IPAddress` row showed only a count
  badge with no way to drill in. Now:
  - **FK columns on `nat_mapping`** — `internal_ip_address_id`
    and `external_ip_address_id` (nullable, ``ON DELETE SET
    NULL``) auto-resolved on create / update by looking up the
    typed string in `ip_address`. Strings stay authoritative for
    addresses outside IPAM (a public WAN IP, a peer's NAT
    endpoint), so existing operator workflows keep working.
    Migration `f5b9c1e8d472` adds + backfills the columns.
  - **Conflict detection** — create / update reject 409 when the
    requested external IP+ports is already claimed by another
    `1to1` / `pat` rule on the same protocol. Port-overlap
    aware; protocol-aware (an `any`-protocol pat collides with
    everything on its IP).
  - **Per-IP and per-subnet listing endpoints** —
    `GET /ipam/nat-mappings/by-ip/{id}` returns every mapping
    touching an IPAM row (FK match + INET-string match on either
    side); `GET /ipam/nat-mappings/by-subnet/{id}` uses Postgres
    `inet <<= cidr` containment to find every mapping whose
    internal IP falls inside a subnet's CIDR.
  - **UI** — clicking the NAT badge on an IP row opens a modal
    listing every mapping for that IP (formatted as
    `internal:port → external:port` with kind / protocol /
    device pills). A new "NAT" tab on the subnet detail page
    shows every mapping touching that subnet.

- **VXLAN ID surface in the IPAM UI.** `subnet.vxlan_id` already
  existed in the schema (Integer, nullable, range 1–16 777 214)
  and the frontend type, but no UI ever read or wrote it.
  Numeric input added to Create + Edit subnet modals next to the
  VLAN picker; chip on the subnet detail header next to the
  existing VLAN chip when set.

- **Per-IP role + reservation TTL + MAC observation history.**
  `IPAddress` gains `role` (host / loopback / anycast / vip /
  vrrp / secondary / gateway — orthogonal to status) and
  `reserved_until` (datetime, nullable). New beat task
  `app.tasks.ipam_reservation_sweep.sweep_expired_reservations`
  flips reserved rows past their TTL back to `available`, on a
  5-minute cadence. Roles in `IP_ROLES_SHARED` (anycast / vip /
  vrrp) bypass MAC-collision warnings — the same MAC legitimately
  appears on multiple IPs in a load-balancer or HSRP/VRRP pair.
  New `ip_mac_history` table tracks every distinct MAC ever
  observed against an IP, keyed `(ip_address_id, mac_address)`
  with `first_seen` + `last_seen` timestamps; written on every IP
  create / update where a MAC is present, surfaced via
  `GET /ipam/addresses/{id}/mac-history` (newest-first, OUI
  vendor lookup attached). Migration
  `f1c9a4d2b8e6_ip_role_reserved_mac_history`. Test coverage in
  `tests/test_ip_role.py`, `tests/test_mac_history.py`,
  `tests/test_reservation_sweep.py`.

- **IPAM subnet operations — find-free + split + merge.** Three
  preview-then-commit endpoints under `/ipam/spaces/{id}/find-
  free`, `/ipam/subnets/{id}/split/preview` + `/commit`,
  `/ipam/subnets/{id}/merge/preview` + `/commit`. Find-free
  walks the IPBlock tree for unallocated CIDRs of a requested
  prefix length (with optional `parent_block_id` scope and a
  minimum-free-addresses filter). Split breaks a subnet into
  2^k aligned children at a longer prefix; merge collapses
  contiguous siblings back into one supernet via
  `ipaddress.collapse_addresses`. Both gate non-trivial
  operations on a typed-CIDR confirmation, hold a pg advisory
  lock through commit, and re-validate every constraint pre-
  mutation. Surfaced in the UI via three header buttons on
  the subnet detail (Find Free… / Split… / Merge…) **and** via
  the bulk-action toolbar on the block- and space-level subnet
  tables: select 1 subnet to split, select 2+ to merge.
  Free-space finder also lives on the block detail header
  pre-scoped to that block.

- **`IPSpace` VRF / route-domain annotation.** Three new
  optional columns on `ip_space`: `vrf_name` (≤ 64 chars),
  `route_distinguisher` (ASN:idx or IPv4:idx, no validation —
  vendors disagree), `route_targets` (JSONB list of RT strings).
  Pure metadata — address allocation already supports
  overlapping ranges via separate IPSpace rows; these columns
  give operators somewhere to put the routing identity for
  reporting / export / future BGP-EVPN integration. Migration
  `f1c8b2a945d3_subnet_ops_ipspace_vrf`. Surfaced as badges on
  the IPSpace detail header when set, plus a "VRF / Routing"
  section in the Edit Space modal (open by default since
  operators kept missing it under a collapsed toggle).

- **Soft-delete + 30-day recovery + Trash admin page.**
  `IPSpace`, `IPBlock`, `Subnet`, `DNSZone`, `DNSRecord`, and
  `DHCPScope` rows now inherit a `SoftDeleteMixin` (`deleted_at`,
  `deleted_by_user_id`, `deletion_batch_id`). A global
  `do_orm_execute` event listener injects
  `Model.deleted_at IS NULL` into every SELECT touching one of
  these models — callers that need to see soft-deleted rows opt
  in via `execution_options(include_deleted=True)`. Cascade-
  stamping under one `deletion_batch_id` means restoring a
  subnet brings its DHCP scopes back atomically; restoring a
  zone brings its records back. New endpoints under `/admin/`:
  - `GET /admin/trash` — paginated list across every in-scope
    model, with type / since / `q` substring filters and
    deleted-by user resolution.
  - `POST /admin/trash/{type}/{id}/restore` — atomic batch
    restore with `default_conflict_check` (rejects 409 when a
    live row would clash on the same uniqueness key).
  - `DELETE /admin/trash/{type}/{id}` — hard-delete a row
    that's already soft-deleted.

  Frontend page at `/admin/trash` lists soft-deleted rows
  newest-first, per-row Restore (with confirmation modal +
  conflict-detail rendering) and Delete-permanently buttons.
  Sidebar entry under Admin. Nightly `trash_purge` Celery beat
  task (`app.tasks.trash_purge.purge_expired_soft_deletes`)
  hard-deletes rows past `PlatformSettings.soft_delete_purge_days`
  (default 30; set to 0 to disable purging). Subnet / block /
  space delete confirmation text updated from "permanently
  delete" → "move to Trash. You can restore from Admin → Trash
  within 30 days." since the actual behaviour is soft-delete,
  not hard-delete. IP addresses are intentionally NOT
  soft-deletable — they cascade-delete with their parent
  subnet, and the parent subnet is the recoverable unit.
  Migration `c1f4a8b27d09_soft_delete`.

- **DHCP lease history + NAT mapping table.** New
  `dhcp_lease_history` table records every lease that ever
  expired, was reassigned to a different MAC, or disappeared
  from an absence-delete sweep — gives operators a forensic
  trail when "who had this IP last week" comes up. Written from
  three sites: the `dhcp_lease_cleanup` expiry sweep, the agent
  lease-event ingest path on MAC change, and `pull_leases` on
  absence-delete. Surfaced on the DHCP server detail as a new
  "Lease History" tab with filtering by MAC / IP / time window.
  Daily prune task (`app.tasks.dhcp_lease_history_prune`)
  honours `PlatformSettings.dhcp_lease_history_retention_days`
  (default 90; set to 0 to keep forever).

  New `nat_mapping` table is operator-curated metadata
  describing 1:1 NAT, PAT, or hide-NAT bindings between
  internal and external IPs. SpatiumDDI doesn't render or push
  these rules anywhere — purely IPAM cross-reference: an IP row
  gets a `nat_mapping_count` badge and the dedicated
  `/ipam/nat` page lists / creates / edits / deletes mappings.
  Migration `f4e1d2a09b75_lease_history_and_nat`.

- **Tailscale integration — Phase 2: synthetic tailnet DNS surface.**
  When a `TailscaleTenant` has `dns_group_id` bound, the reconciler
  now also materialises a `<tailnet>.ts.net` `DNSZone` in that
  group and one A / AAAA record per device address. Tailnet domain
  is auto-derived from the first device FQDN (same as Phase 1's
  `tailnet_domain`). Records carry `auto_generated=True` plus a
  new `tailscale_tenant_id` FK on both `dns_zone` and `dns_record`
  (CASCADE on tenant delete) — so deleting the tenant sweeps the
  whole synthetic zone in one shot.
  - **Read-only enforcement.** API blocks `PUT /zones/{id}`,
    `DELETE /zones/{id}`, record CRUD on synthesised zones with a
    422 + explanatory message ("delete the Tailscale tenant or
    unbind its DNS group to release the zone"). UI shows a cyan
    "Tailscale (read-only)" badge near the zone title and disables
    the Edit / Delete / Add Record header buttons. The per-record
    lock badge in the records table now branches on
    `tailscale_tenant_id` to read "Tailscale" instead of "IPAM"
    when the record was synthesised by Tailscale (rather than
    DDNS / IPAM auto-sync).
  - **Diff semantics.** Reconciler compares desired vs. current
    on every pass keyed by `(name, record_type, value)`: new
    records are inserted, removed devices have their records
    deleted. Idempotent — a second sync with the same device list
    creates / deletes nothing.
  - **Conflict safety.** If an operator-managed zone with the
    same name already exists in the bound DNS group, the
    reconciler refuses to claim it (would silently overwrite
    operator records every sync); the collision lands as a summary
    warning, the operator-managed zone is left untouched, and the
    sync still succeeds for the IPAM mirror.
  - **Filtering.** Devices with expired keys (and
    `keyExpiryDisabled=false`) are skipped, matching Phase 1's
    IPAM mirror semantics. Devices with no FQDN, or whose FQDN
    doesn't end in the derived tailnet domain (different tailnet,
    truncated name during onboarding), are skipped without error.
  - **Bonus.** Because we land actual `DNSRecord` rows, the
    existing BIND9 render path picks them up automatically — non-
    Tailscale LAN clients can resolve `<host>.<tailnet>.ts.net`
    through SpatiumDDI's BIND9 with no extra forwarder plumbing.
  - **TTL.** Synthesised records are stamped at 300 s — short
    enough that a stale entry (device reauthed with a different
    IP) falls out of resolver caches within five minutes of the
    next sync.
  - Migration `e6f12b9a3c84_tailscale_phase2_dns`. 5 new reconciler
    tests cover the synthesis happy path + idempotency, diff on
    device disappearance, no-DNS-group skip, operator-zone
    collision refusal, and foreign-FQDN filtering.

- **Query / activity log surface for BIND9 + Kea agents.** Two new
  tabs on the Logs page (`/logs`):
  - **DNS Queries** — BIND9 `query-log` channel content, parsed into
    timestamp / client IP+port / qname / qclass / qtype / flags /
    view columns with the original raw line preserved. Filters: `q`
    (substring match on qname or raw), `qtype`, `client_ip`, time
    `since`, max events. Requires `query_log_enabled` on the
    `DNSServerOptions` row (already a UI toggle); the BIND9 template
    has rendered the channel since the `f8a3c1e7d925` migration —
    this wave just plugs in the read side.
  - **DHCP Activity** — Kea `kea-dhcp4` log content, parsed into
    timestamp / severity / log code / MAC / IP / transaction id
    columns. Filters: severity, log code (`DHCP4_LEASE_ALLOC` etc),
    MAC, IP, time `since`, raw substring search.
  Each tab has its own server picker drawing from the new
  `GET /logs/agent-sources` endpoint (lists `bind9` DNS + `kea`
  DHCP servers).
- **Agent push pipeline.** The DNS agent gains a `QueryLogShipper`
  thread that tails `/var/log/named/queries.log` (override via
  `DNS_QUERY_LOG_PATH`), batches up to 200 lines or 5 s of activity
  (whichever first), and POSTs to `POST /api/v1/dns/agents/query-
  log-entries`. The DHCP agent gains a `LogShipper` thread doing the
  same against `/var/log/kea/kea-dhcp4.log` (override via
  `DHCP_LOG_PATH`) → `POST /api/v1/dhcp/agents/log-entries`. Kea's
  rendered config now writes to *both* stdout (existing
  `docker logs` workflow) and the new file with in-process rotation
  (`maxsize=50MB`, `maxver=5`). Both shippers handle file-not-yet-
  present (sleep + retry), inode-change rotation (re-open), and
  transient control-plane errors (drop the batch, never block the
  daemon). Memory cap at 5000 buffered lines per shipper trims the
  oldest half if the control plane is unreachable.
- **Storage + retention.** Two narrow tables `dns_query_log_entry`
  and `dhcp_log_entry` hold the parsed lines (composite indexes on
  `(server_id, ts)`); FK cascade drops a server's entries when the
  server row is removed. Nightly `prune_log_entries` Celery task
  drops rows older than 24 h — query logs are *operator triage*, not
  analytics; longer retention belongs in Loki / a SIEM. Migration
  `d8c5f12a47b9_query_log_entries`. Parser unit tests in
  `tests/test_log_parsers.py` cover IPv4 / IPv6 / view-tagged BIND9
  lines plus stock Kea lease-alloc / decline / packet-trace shapes,
  including the "unparseable line still preserves raw text" path.
  Retention task tests in `tests/test_prune_logs.py`.

- **Tailscale integration (read-only tenant mirror) — Phase 1.**
  Settings → Integrations → Tailscale toggle
  (`integration_tailscale_enabled`) lights up a Tailscale nav item
  in the sidebar. `TailscaleTenant` rows bind per-tenant to one
  IPAM space + optional DNS server group, with a Fernet-encrypted
  PAT (`tskey-api-…`) and the tailnet slug (or `-` for the API
  key's default tailnet). Same 30 s beat sweep + per-tenant
  `sync_interval_seconds` gating (60 s default, 30 s floor) + Sync
  Now button as Proxmox / Docker / Kubernetes; FK cascade on
  tenant delete. The reconciler hits `GET
  /api/v2/tailnet/{tn}/devices?fields=all` and:
  - Auto-creates the CGNAT IPv4 block (`100.64.0.0/10` by default,
    operator can override per tenant for non-default slices) and
    the IPv6 ULA block (`fd7a:115c:a1e0::/48`) under the bound
    space on first sync, plus one subnet per block. Idempotent —
    subsequent reconciles don't duplicate.
  - Mirrors every device's `addresses[]` (both IPv4 + IPv6) as
    `IPAddress` rows with `status="tailscale-node"`, hostname =
    device FQDN (`<host>.<tailnet>.ts.net`), description carrying
    OS + client version + user, and `custom_fields` for tags,
    authorized flag, last seen, expires, advertised + enabled
    routes, key-expiry-disabled, update-available, plus the stable
    Tailscale device + node IDs.
  - Skips devices whose Tailscale node-key has expired by default
    (`skip_expired=True`); devices with `keyExpiryDisabled=true`
    (long-lived servers / appliances where the operator has turned
    expiry off) are kept regardless of what the `expires`
    timestamp says, since Tailscale leaves a frozen / ignored
    value on the field. Tailscale's `0001-01-01T00:00:00Z`
    sentinel for "never expires" is correctly interpreted as
    not-expired.
  - Auto-derives the tailnet domain (e.g. `rooster-trout.ts.net`)
    from the first device FQDN — no separate config field.
  - Claim-on-existing + `user_modified_at` lock semantics match
    the Proxmox path: pre-existing operator rows in the CGNAT
    block get adopted (FK stamped) with the lock set, so operator
    edits to hostname / description / status / mac survive every
    subsequent reconcile. Custom fields stay reconciler-owned
    because the tailnet metadata (last_seen, version, route list)
    is most useful when fresh.
  - Un-claim-on-disappear preserves operator-edited rows when the
    upstream device goes away — releases the FK rather than
    deleting the row, mirroring Proxmox/Docker/K8s behaviour.
  - Setup guide in the admin page walks the operator through PAT
    generation in the Tailscale admin console and explains the
    `-` shorthand for default tailnet. Test Connection probe hits
    `/devices?fields=default` for a cheap reachability + auth
    check before save.
  - Migration `c4e1a87b3920_tailscale_integration` adds the
    `tailscale_tenant` table + `tailscale_tenant_id` provenance
    FK on `ip_address` / `ip_block` / `subnet` (CASCADE on tenant
    delete) + `integration_tailscale_enabled` on
    `platform_settings`.
  - 14 unit tests covering tailnet-domain derivation edge cases
    plus the full reconciler diff: block/subnet idempotency,
    multi-address mirroring, expired-device skip + the
    `0001-01-01` sentinel guard, claim-on-existing with the
    `user_modified_at` lock, and the lock-vs-unlock branches in
    the un-claim-on-disappear path.

### Changed

- **Dashboard headline KPIs restricted to IPv4.** "Allocated IPs"
  and "Utilization %" now compute over IPv4 subnets only. A single
  IPv6 /64 carries 2^64 hosts and was swamping the totals across
  every IPv4 subnet combined, making the headline numbers
  meaningless. Per-subnet utilisation, the heatmap, and the IPv6
  subnet count remain — IPv6 stays first-class everywhere it's
  meaningful, just not in capacity-planning rollups. KPI labels
  updated to "Allocated IPs (IPv4)" / "Utilization (IPv4)" so the
  scope is explicit.

- **VRF / Routing section open by default in Edit Space modal.**
  Previously collapsed under a toggle that operators kept missing;
  now expanded by default so the fields are visible the first time
  you open the modal. Operators who don't run multiple VRFs can
  collapse it with the toggle.

- **VLAN page "New VLAN" button promoted to page header.**
  Previously a small inline button above the VLANs sub-table that
  was easy to miss; now lives in the router page header alongside
  Edit / Delete as a `HeaderButton variant="primary" icon={Plus}`,
  consistent with the create-button placement on every other page.

- **Trash page wrapped in standard admin container** (`h-full
  overflow-auto p-6` + `mx-auto max-w-5xl`) so the table no longer
  spans the entire viewport and truncates the rightmost columns
  on widescreen layouts. Restore now opens a confirmation modal
  with conflict-detail rendering instead of firing the mutation
  directly from the table row.

- **Soft-delete confirmation copy** — every "Delete" dialog whose
  underlying API path soft-deletes (Subnet bulk, Block, Space,
  Subnet single) now reads "move to Trash. You can restore from
  Admin → Trash within 30 days" instead of the misleading
  "permanently delete" wording carried over from the pre-trash
  era. Hard-delete (operator picks Permanent in the Trash modal)
  still says "cannot be restored".

- **K8s + Helm worker / beat liveness probes.** Both manifests
  shipped with no probes for the Celery worker + beat
  Deployments — k8s couldn't detect a hung worker. Worker now
  runs `celery -A app.celery_app inspect ping -d
  celery@${HOSTNAME}` (scoped by hostname so the probe matches
  this specific pod, not any random worker on the broker); beat
  runs `grep -q celery /proc/1/cmdline` (matches the
  docker-compose pattern). API readinessProbe in both k8s/base
  and the Helm chart switched from `/health/live` to
  `/health/ready` (which actually checks DB + Redis
  connectivity) — a pod that can't reach its dependencies is now
  removed from LB rotation instead of returning 5xx. Liveness
  stays on `/health/live` so a transient Postgres blip doesn't
  trigger a pod restart.

- **Dev compose worker / beat healthchecks.** Same overrides the
  prod `docker-compose.yml` had — without them, both services
  inherited the Dockerfile's `/health/live` HTTP probe and
  reported `unhealthy` because they don't run an HTTP listener.

### Security

- **ReDoS hardening on agent log parsers.** CodeQL flagged the
  BIND9 query-line regex (`_QUERY_RE` in
  `app/services/logs/bind9_parser.py`) as polynomial — the
  whitespace + optional view-group repetitions could be coerced
  into quadratic-time matching by a malicious agent shipping a
  crafted line through `POST /api/v1/dns/agents/query-log-entries`.
  Added a 4 KiB length cap (`_MAX_LINE_LEN`) at the top of
  `parse_query_line` before any regex execution; same cap added
  to `parse_kea_line` for parity. A real BIND9 query line is
  bounded by qname (≤ 255 chars per RFC 1035) plus timestamp /
  client / view metadata, so 4 KiB is well above any legitimate
  line. Verified — a 10 KiB pathological input now caps at 4 KiB
  and parses in under 50 ms instead of degrading.

### Fixed

- **Multiple Celery tasks broke under "Future attached to a
  different loop".** `asyncio.run(...)` creates a fresh event
  loop per task invocation; the shared `engine` /
  `AsyncSessionLocal` from `app/db.py` were binding asyncpg
  connections to whichever loop first checked them out, so a
  later task using a pooled connection would crash with a stale
  loop reference. Manifested most visibly in the alerts
  evaluator (fires every 60 s) but lurked across
  `dhcp_lease_cleanup`, `dhcp_lease_history_prune`,
  `ipam_reservation_sweep`, `trash_purge`, `prune_metrics`, and
  `prune_logs` — every newer task that imported
  `AsyncSessionLocal` directly. Added a `task_session()`
  context-manager helper in `app/db.py` that builds a throwaway
  `create_async_engine` + `async_sessionmaker` per call and
  disposes the engine on exit (connection lifecycle now matches
  loop lifecycle). Migrated all seven affected tasks. Existing
  `dhcp_health` and `dhcp_pull_leases` already followed this
  pattern with their own per-task engine; we now have one
  canonical helper.

- **DNS agent stuck in 404 loop after stale server row.** Sync
  loop handled 401 by dropping cached token + signalling stop,
  but treated 404 ("server row deleted on the control plane")
  as a generic "unexpected status" and just logged + retried
  forever. CLAUDE.md non-negotiable was clear that both 401 and
  404 should re-bootstrap from PSK; the DHCP agent already had
  it right. Mirrored the DHCP pattern. Even after sync stopped
  itself, the DNS supervisor only watched `daemon_running()` and
  signal events — heartbeat / metrics / query-log threads kept
  hammering the API with the stale token. Supervisor now adds
  the DHCP-agent-style "die if any thread dies" check; container
  exits with code 2, orchestrator restarts, `ensure_token` sees
  the empty cache, and the agent re-bootstraps from PSK.

- **NAT mappings sidebar nav also lit up the IPAM nav item.**
  React Router's `NavLink` does prefix matching by default;
  `/ipam/nat` matched both the `/ipam` IPAM entry and the
  `/ipam/nat` NAT Mappings entry. Added an `end` prop to
  `NavItem` and set `end: true` on the IPAM nav config so it
  only matches `/ipam` exactly.

- **Free space finder now scoped per block.** `FindFreeModal`
  takes an optional `defaultBlockId` prop that flows into the
  request body's `parent_block_id`; the block-detail toolbar
  passes the current block so search results are pre-restricted
  to candidates inside it. Without this, opening Find Free from
  inside a block searched the whole space and surfaced
  candidates the operator probably didn't want.

- **Integration mirror reconcilers preserve operator edits +
  accept integration-owned statuses on update + don't fake
  bridge gateways.** Three closely-related fixes around the
  Proxmox / Kubernetes / Docker reconcilers, all hitting the
  same scenario (operator has IPAM rows, enables an integration,
  syncs run, things either error or get clobbered):
  - **Status validator now accepts integration values on
    update.** `IPAddressUpdate` hardcoded
    `{available, allocated, reserved, static_dhcp,
    deprecated}` and 422'd anything else, making every
    Proxmox-mirrored row (`status="proxmox-vm"`) un-editable
    from the API/UI. Lifted the sets to module-level constants
    in `app.models.ipam` (`IP_STATUSES_OPERATOR_SETTABLE`,
    `IP_STATUSES_INTEGRATION_OWNED`, `IP_STATUSES`); update
    path now accepts ALL statuses, create + next-IP paths are
    unchanged in spirit (operators shouldn't be hand-creating
    `proxmox-vm` rows).
  - **Operator edits sticky across reconciles.** Added
    `ip_address.user_modified_at` (timestamp, nullable) —
    stamped by the API write path when an operator changes
    hostname / description / status / mac_address. All three
    integration reconcilers consult the column: claim-on-
    existing adopts an operator-owned row at a desired
    (subnet, address) tuple by stamping the FK + `user_modified
    _at = now()`; subsequent edit-skip protects the operator's
    fields; preserve-on-disappear releases the FK rather than
    deleting the row when a guest goes away. Migration
    `f8d4e29b1c75`.
  - **Proxmox bridge stops faking a gateway.** The reconciler
    was treating the PVE host's bridge IP (e.g. `192.168.0.94`
    on `vmbr0.20`) as the network gateway — wrong: in plain-
    bridge deployments PVE is a peer on the LAN, not the
    router. Bridge subnets now land with `gateway=None`; the
    bridge IP becomes a per-PVE-host placeholder row labelled
    with the node name. SDN subnets keep their declared
    gateway (PVE owns L3 there, so the value is real). Subnet
    gateway updates are now no-clobber: integrations only set
    the field when they know a real value, so an operator who
    fixes the upstream gateway on a Proxmox-mirrored subnet
    doesn't see it cleared on every sync.

### Notes

- Phase 2 of the Tailscale integration (synthetic
  `<tailnet>.ts.net` DNS surface) shipped in this cycle (entry
  above). The optional BIND9 forwarder zone for
  `100.100.100.100` remains a roadmap item — see `CLAUDE.md`
  "Future Phases" for the deferred follow-ups.

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
