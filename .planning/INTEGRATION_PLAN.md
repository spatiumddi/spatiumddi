# roadmap-batch-2 — integration plan (durable; resume from here)

Branch: `roadmap-batch-2` off `main`. **No push** until the user says "merge".
Commit **each issue separately** as it lands (durability — survive token exhaustion).
Full per-issue blueprints: `.planning/specs.json` (137KB, 9 specs). Raw issues: `.planning/issues_raw.md`.

## Migration rule (avoid head-forks)
For every migration: set `down_revision = <live alembic head>` (run
`docker compose -f docker-compose.yml -f docker-compose.dev.yml exec -T api alembic heads`),
create it, **apply immediately** (`alembic upgrade head`), confirm single head, then move on.
Starting head: `c7a3e1f90d24`. Never pre-assign a chain — always chain off the live head.

## Pre-commit checks (per issue)
- Backend: `.venv/bin/ruff check --fix`, `.venv/bin/black --line-length 100`, `.venv/bin/mypy <files>`
- Migrations: `python3 scripts/lint_migrations.py` (baseline new downgrade drops via `--baseline`)
- Frontend: `npx prettier --write`, `npx eslint`, `npm run build`
- Rebuild dev containers (`build api|frontend && up -d --force-recreate`), run the new tests
- `make ci`-equivalent before any eventual push

## Implementation order (easy/independent → XL), with status
1. [ ] **#57 Maintenance mode** (M, migration) — 3 platform_settings cols + MaintenanceModeMiddleware (503+Retry-After, superadmin bypass; mirror DEMO_MODE block) + settings GET/PUT + global banner + MCP. Precedent: DEMO_MODE write-block, `a3f1e9c47b20` migration shape.
2. [ ] **#47 Top-N reports** (M, no model migration; feature_module seed only) — `api/v1/reports/router.py` (top subnets by util / owners by IP / most-modified via audit_log / noisiest DNS clients) + ReportsPage + sidebar/route + feature module `reports.top_n` + 4 MCP. NOTE: owners FKs live on Subnet/IPBlock, NOT IPAddress.
3. [ ] **#46 Decom-date** (L, migration) — `decom_date` Date col on subnet + ip_address (+indexes) + IPAM schemas (6 sites) + `decom_expiring` alert rule (reuses threshold_days, NO extra col) + dashboard SubnetDecomCard + 1 MCP. Precedent: #76 secret_expiring rule, network_service term_end_date Date col.
4. [ ] **#58 Network tools** (L, feature_module seed migration) — `api/v1/tools/router.py` (ping/traceroute/mtr/dig/whois subprocess; port-test socket; TLS cert ssl/cryptography; dns-propagation; mac-vendor via services/oui) server-perspective v1 (agent-perspective deferred), rate-limited (Redis like auth throttle), permission `use_network_tools` + module `tools.network` + NetworkToolsPage + 7 MCP. Precedent: NmapToolsPage, dns_tools.py, ping_host MCP.
5. [ ] **#65 Time-bound permissions** (L, migration) — `time_bound_grant` table consulted live by `user_has_permission` + beat sweep revoking expired (audited) + grants router (mount under /groups or /time-bound-grants) + frontend panel on Groups page + 2 MCP. Precedent: core/permissions.py, models/auth.py.
6. [ ] **#156+#157+#158 Appliance host-config trio** (L×3, migrations) — ONE combined effort (shares settings model/router, dhcp/agents.py ConfigBundle, appliance/supervisor.py heartbeat, supervisor appliance_state.py + heartbeat.py, FleetTab.tsx, SettingsPage.tsx, api.ts). #156 syslog/rsyslog, #157 ssh authorized_keys (+lockout safety), #158 resolver/systemd-resolved. Each: platform_settings cols + GET/PUT /settings/{syslog,ssh,resolver} + ConfigBundle block + host runner `spatiumddi-{syslog,sshkeys,resolved}-reload.{path,service}` + appliance_state.maybe_fire_* + Fleet chip + Settings tab + MCP. Precedent: SNMP #153 / NTP #154 / LLDP / timezone #165 — grep `spatium-snmp-reload`, `maybe_fire`. Can be 3 commits.
7. [ ] **#31 OPNsense** (XL, migration) — full read-only mirror cloned from Proxmox: OPNsenseRouter model + httpx client (4 REST endpoints + ARP) + reconciler (LAN/VLAN/OPT*→Subnet, leases→dhcp, reservations→reserved, arp→opnsense-arp) + 30s beat sweep + CRUD/test/sync router + feature module `integrations.opnsense` + BOTH dashboard surfaces (NN #15: DashboardPage IntegrationsPanel + dashboards/integrations.py) + sidebar/route + OpnsensePage + MCP. Precedent: proxmox/tailscale integration end-to-end.

## High-contention shared files (edit additively, re-verify build each time)
- `frontend/src/lib/api.ts` — ALL issues
- `backend/app/models/settings.py` (PlatformSettings) — #57 #156 #157 #158 #31
- `backend/app/api/v1/settings/router.py` (SettingsResponse/Update) — #57 #156 #157 #158 #31
- `backend/app/api/v1/dhcp/agents.py` (ConfigBundle) + `appliance/supervisor.py` (heartbeat) — #156 #157 #158
- `agent/supervisor/spatium_supervisor/{appliance_state,heartbeat}.py` — #156 #157 #158
- `backend/app/api/v1/router.py` (includes) — #31 #47 #58 (#65 maybe)
- `backend/app/services/feature_modules.py` (MODULES) — #31 #47 #58
- `frontend/src/components/layout/Sidebar.tsx` + `frontend/src/App.tsx` — #31 #47 #58
- `frontend/src/pages/appliance/FleetTab.tsx` — #156 #157 #158
- `frontend/src/pages/DashboardPage.tsx` — #31 #46
- `backend/app/services/ai/tools/__init__.py` — #57 #58 #65 #156 #157 #158
- `backend/app/celery_app.py` — #31 #65 (+ beat for #46, #156/7/8 host-config has no beat)

## Note
Remove `.planning/` from the branch before the eventual PR (scratch/design artifacts).
