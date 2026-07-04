# Issue #524 — IPv6 Router Advertisement management (radvd) + rogue-RA detection

Branch: `issues-523-524-527-528-530`. No commits/pushes made.

## Design decision (delivery home)
RA config is **per-subnet DHCPv6 data**, which the host-global supervisor
heartbeat (SNMP/NTP/chrony plane) does not scope. So RA rides the **DHCP
ConfigBundle** (correct per-group scoping) and is applied by the DHCP agent:
the control plane renders the full `radvd.conf` text (fully unit-testable in the
backend), ships it in the bundle (`radvd_conf`, in the ETag), and the agent
writes it + runs radvd as an opt-in co-process in the Kea container. This
honours "reuse the DHCP driver/ConfigBundle path" + non-negotiable #5 (radvd
config rides the on-disk bundle cache).

## Migration
- **File:** `backend/alembic/versions/b7e4d1a92c30_ipv6_ra_management.py`
- **revision:** `b7e4d1a92c30`  **down_revision:** `a3d7f1c9e6b2` (verified head)
- Adds 9 `dhcp_scope` columns; creates `ra_observed_router` + `ra_router_allowlist`; seeds `feature_module ('ipv6.router_advertisements', TRUE)`.
- Baseline updated: `backend/alembic/migrations_lint_baseline.txt` regenerated via `--baseline` (added my 11 drop-op lines + 3 pre-existing a3d7f1c9e6b2 lines that were unbaselined; no removals). `lint_migrations.py` passes.

## Models (`backend/app/models/dhcp.py`)
- `DHCPScope` new columns: `ra_enabled`, `ra_mo_override`, `ra_router_lifetime` (1800), `ra_max_interval` (600), `ra_prefix_valid_lifetime` (86400), `ra_prefix_preferred_lifetime` (14400), `ra_prefix_on_link` (T), `ra_prefix_autonomous` (T), `ra_interface` (''). Reuses existing `v6_address_mode`/`ra_managed_flag`/`ra_other_flag`.
- New `RAObservedRouter` table `ra_observed_router` (group-scoped; source_ip/mac, prefixes JSONB, M/O flags, router_lifetime, iface, classification expected|acknowledged|rogue, first/last_seen). Unique `(group_id, source_ip)`.
- New `RARouterAllowlist` table `ra_router_allowlist` (group-scoped; source_ip/mac/note).

## Feature module (#14)
- `ModuleSpec(id="ipv6.router_advertisements", group="Network", default_enabled=True)` in `feature_modules.py`.
- New RA router gated with `Depends(require_module("ipv6.router_advertisements"))` + `wake_publishing` in `router.py`.
- Agent ingest endpoint self-gates via `is_module_enabled(...)` (like mac-sightings).
- MCP tools tagged `module="ipv6.router_advertisements"`.

## radvd rendering + assembly (backend)
- `backend/app/services/dhcp/radvd.py` (NEW): pure functions `derive_mo_flags` (stateful→1/1, stateless→0/1, slaac→0/0, override→literal), `resolve_rdnss` (scope `dns-servers` IPv6-only → subnet `dns_servers`), `resolve_dnssl` (`domain-search`→`domain-name`→subnet `domain_name`), `build_ra_config(scope, subnet)`, `render_radvd_conf(ra_configs)` (groups by interface).
- `backend/app/drivers/dhcp/base.py`: new frozen `RAConfigDef`; `ConfigBundle` gains `ra_configs` + `radvd_conf`, both in `compute_etag()`.
- `backend/app/services/dhcp/config_bundle.py`: assembles `ra_configs` + renders `radvd_conf` into the bundle.
- `backend/app/api/v1/dhcp/agents.py`: bundle wire dict gains `radvd_conf`; new `POST /dhcp/agents/ra-observations` (module-gated, cap 200) + `RAObservationEntry`/`RAObservationBatch` schemas.
- `backend/app/api/v1/dhcp/scopes.py`: RA fields added to `ScopeCreate`/`ScopeUpdate`/`ScopeResponse` + create-assignment + `_scope_to_response`.

## Rogue-RA (Part B, backend)
- `backend/app/services/dhcp/ra_detection.py` (NEW): `ObservedRA`, `classify_router` (allowlist → expected else rogue), `record_observations` (upsert, don't downgrade acknowledged).
- `backend/app/api/v1/dhcp/ra_routers.py` (NEW): `/dhcp/ra/groups/{id}/ra-config` (preview), `/observed-routers` (+`/{id}/acknowledge`), `/ra-allowlist` GET/POST/DELETE. Audited.
- Alert rule `rogue_ra` (`RULE_TYPE_ROGUE_RA`) in `alerts.py`: constant, in `RULE_TYPES`, `_matching_rogue_ra_subjects`, evaluate_all branch (`subject_type="ra_router"`), `seed_rogue_ra_alert_rule` (disabled by default). Wired into `main.py` startup seed. Rides existing AlertEvent fan-out automatically.

## Agent (`agent/dhcp/spatium_dhcp_agent/`)
- `radvd_apply.py` (NEW): `apply_radvd(radvd_conf)` — no-op unless `RADVD_MANAGED=1`; atomic write to `RADVD_CONFIG_PATH`, validate `radvd -c -C`, SIGHUP via `RADVD_PIDFILE`. Best-effort.
- `ra_sniffer.py` (NEW): `RASnifferShipper` — scapy AsyncSniffer `icmp6 and ip6[40] == 134`, opt-in `DHCP_RA_SNIFFER_ENABLED=1`, ships to `/dhcp/agents/ra-observations`. Mirrors fingerprint shipper.
- `sync.py`: `_apply_bundle` calls `apply_radvd(inner.get("radvd_conf"))` (best-effort, after Kea write).
- `supervisor.py`: wires the RA sniffer thread (gated, stop-handled).
- `images/kea/Dockerfile`: `apk add radvd`, `setcap cap_net_raw,cap_net_admin=+ep`, `/etc/radvd` + `/run/radvd` dirs, `RADVD_*` env (default MANAGED=0).
- `images/kea/entrypoint.sh`: `supervise_radvd` loop (waits for config, runs `radvd -C … -p … -n`), launched only when `RADVD_MANAGED=1`, added to `_term`.

## MCP tools (`backend/app/services/ai/tools/radvd.py` NEW, added to tools `__init__`)
- `find_ra_subnets`, `find_observed_ra_routers`, `count_rogue_ra_routers` — reads, `default_enabled=True`.
- `propose_allowlist_ra_router` — write proposal, `default_enabled=True`; backed by new `allowlist_ra_router` Operation in `operations.py` (required_permission `("write","dhcp_server")`, reclassifies rogue rows).

## Frontend
- `lib/api.ts`: `DHCPScope` interface RA fields; new `RAObservedRouter`/`RARouterAllowlist`/`RAScopeConfig`/`RAConfigPreview` types; `dhcpApi` methods `raConfigPreview`, `listObservedRARouters`, `acknowledgeRARouter`, `listRAAllowlist`, `createRAAllowlist`, `deleteRAAllowlist`.
- `pages/dhcp/DHCPPage.tsx`: new "Router Adverts" tab (gated on module via `useFeatureModules`) + `RouterAdvertisementsTab` (radvd preview table + rendered-conf disclosure + observed-routers table with Acknowledge, mirrors RespondersTab).
- `pages/dhcp/CreateScopeModal.tsx`: RA management section (ra_enabled toggle, M/O override, router/prefix lifetimes, on-link/autonomous, interface) shown for v6 scopes; state + payload wired.

## Docs / env / NOTICE
- `docs/features/DHCP.md`: new §19 (RA management + rogue-RA); DHCPv6 note updated.
- `.env.example`: `DHCP_RA_SNIFFER_ENABLED=0` + `RADVD_MANAGED=0` documented.
- `docker-compose.yml`: NET_ADMIN comment on dhcp-kea for radvd.
- `NOTICE`: radvd (BSD-style) added.

## Tests (all pass against a real Postgres)
- `backend/tests/test_radvd_render.py` — 11 tests: M/O derivation, override, RDNSS/DNSSL resolution + fallback, build_ra_config gating, lifetimes, render stanza, empty, per-interface grouping.
- `backend/tests/test_rogue_ra.py` — 6 tests: classify (allowlist ip/mac vs rogue), record_observations upsert/dedupe, groupless skip, alert matcher, ignores old/acknowledged.
- Ran `test_radvd_render` + `test_rogue_ra` (17 passed), plus existing `test_dhcp_v6_mode`, `test_rogue_dhcp`, `test_dhcp_relay_addresses`, `test_dhcp_heartbeat_wire_contract`, `test_dhcp_socket_mode` (35 passed) — no regressions.

## Verification run
- ruff clean + venv black clean on all changed files.
- `lint_migrations.py` passes.
- Backend imports + tool/operation registration + feature-module presence + ETag-shifts-on-ra-change all verified via a live import check.
- Frontend `npm run build` passes.
- Agent modules `py_compile` + entrypoint `sh -n` clean.

## Unverified / notes
- radvd container path not exercised end-to-end at runtime (no live IPv6 lab here) — logic reviewed; entrypoint waits for config before launch, agent SIGHUPs on updates.
- Host `mypy` (Docker) not run per instructions; types kept consistent with surroundings.
- Disabling RA on all scopes leaves radvd on its last config until container restart (empty `radvd_conf` is intentionally not written, since radvd refuses a zero-interface config) — documented behaviour.
- Appliance host-runner (spatium-radvd-reload) path intentionally NOT built: RA's per-group scoping doesn't fit the host-global supervisor heartbeat; DHCP-agent-co-process is the delivery. Docker/K8s/appliance-agent all use the same agent path.
