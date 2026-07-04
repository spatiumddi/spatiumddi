# Issue #527 — BGP prefix-hijack detection via RIS Live for tracked ASNs/prefixes

## Summary
Adds route-origin monitoring to the ASN subsystem: detects when an **unexpected
origin AS announces one of your tracked prefixes** (exact-prefix hijack) or a
**more-specific sub-prefix** of it, classifies the announcement against RPKI
(invalid vs unknown → critical vs warning), latches detections, and fires two new
alert rules through the standard AlertEvent fan-out. The reliable delivery path is
a beat-driven RIPEstat poll; an optional RIS Live WebSocket consumer is scaffolded
behind an env flag (default OFF) and never load-bearing.

## Feature-module decision
**Folded into the existing `network.asn` module — no new module.** BGP hijack
monitoring is part of the ASN feature surface. The endpoints mount on the existing
`asns_router` (already `require_module("network.asn")` + `require_resource_permission("manage_asns")`);
MCP tools are tagged `module="network.asn"`. The RIS Live consumer stays runtime-opt-in
via `BGP_RIS_LIVE_ENABLED` regardless.

## Migration
- File: `backend/alembic/versions/a1c7f3e9b284_bgp_hijack_monitoring.py`
- **revision = `a1c7f3e9b284`**, **down_revision = `b7e4d1a92c30`** (verified current head; no newer migration)
- Additive. Timestamps use `server_default=text("now()")`.
- Applied + downgraded cleanly against a scratch Postgres DB.

### New tables
- **`bgp_tracked_prefix`** — prefixes monitored on the public routing table. Cols:
  `id, asn_id (FK asn CASCADE), prefix (CIDR), expected_origin_asn (BigInteger),
  source (roa|announced|both|manual), enabled (bool), allowed_origins (JSONB int list),
  last_seen_origins (JSONB), last_checked_at, next_check_at, created_at, modified_at`.
  Unique `(asn_id, prefix)`; indexes on `asn_id`, `enabled`.
- **`bgp_hijack_detection`** — latch/dedup state, one row per observed hijack. Cols:
  `id, tracked_prefix_id (FK CASCADE, nullable), asn_id (FK CASCADE), tracked_prefix (CIDR),
  observed_prefix (CIDR), expected_origin_asn, observed_origin_asn, detection_kind
  (prefix_hijack|more_specific), rpki_status (invalid|unknown|valid), severity, source
  (ripestat_poll|ris_live), first_seen_at, last_seen_at, resolved_at (nullable),
  acknowledged (bool), detail (JSONB), notes, created_at, modified_at`.
  Partial index on `(asn_id, observed_prefix, observed_origin_asn, detection_kind) WHERE resolved_at IS NULL`.

### New platform_settings columns
- `bgp_monitoring_enabled` (bool, default **false** — feature ships discoverable but silent)
- `bgp_monitoring_interval_hours` (int, default 6) — per-prefix `next_check_at` cadence, clamped 1..168

### Migration lint
- Ran `python3 scripts/lint_migrations.py --baseline`; only **additions** to
  `backend/alembic/migrations_lint_baseline.txt` (no removals). Added lines cover
  my migration's downgrade drops (`a1c7f3e9b284`) plus two **sibling** migrations that
  were unbaselined on this shared branch (`a3d7f1c9e6b2` #530, `b7e4d1a92c30` #524) —
  required for `lint_migrations.py` to pass on the combined branch. Lint now `OK`.

## Alert rules
Registered in `backend/app/services/alerts.py` exactly like the existing ASN rules:
- **`bgp_prefix_hijack`** (`RULE_TYPE_BGP_PREFIX_HIJACK`) — exact-prefix hijack.
- **`bgp_more_specific_announced`** (`RULE_TYPE_BGP_MORE_SPECIFIC`) — sub-prefix hijack.
- Added to `RULE_TYPES`; matcher `_matching_bgp_hijack_subjects()` reads active
  (`resolved_at IS NULL AND acknowledged = False`) detection rows, passing per-detection
  severity (`critical` for RPKI-invalid, `warning` for unknown) as the severity override.
  Dispatch branches added in `evaluate_all` (subject_type `bgp_hijack`).
- Seed `seed_bgp_hijack_alert_rules()` inserts both **disabled by default** (rogue_dhcp/rogue_ra
  precedent for noisy external-signal rules), keyed on `rule_type`. Wired into `app/main.py` startup.
- Standard AlertEvent open/resolve + syslog/webhook/SMTP fan-out (the latch lives in the
  detection table; resolving a detection auto-resolves the mirrored AlertEvent).

## Task / consumer / env flag
- **Poll (source of truth):** `backend/app/tasks/bgp_hijack_poll.py::poll_bgp_hijacks`
  — beat entry `bgp-hijack-poll-tick` (hourly) in `celery_app.py`; also added to `include=[...]`
  and `task_routes`. Gates on `bgp_monitoring_enabled`; `autoretry_for=(ConnectionError, OSError)`
  + backoff; idempotent (per-prefix `next_check_at` pacing, upsert-latch dedup). Reconciles
  tracked prefixes from ROAs + RIPEstat announced-prefixes, evaluates each, resolves stale detections.
- **Shared core:** `backend/app/services/bgp/hijack_monitor.py` — `derive_rpki_status`
  (invalid/unknown/valid over the ROA table w/ prefix containment), `severity_for_rpki`,
  `evaluate_tracked_prefix`, `record_detection` (latch chokepoint), `resolve_stale_detections`
  (12 h delist window), `refresh_tracked_prefixes_for_asn`.
- **Optional RIS Live consumer:** `backend/app/services/bgp/ris_live.py` — standalone
  entrypoint `python -m app.services.bgp.ris_live`, gated by **`BGP_RIS_LIVE_ENABLED`** env
  (default OFF, in `app/config.py` with `bgp_ris_live_url`). Subscribes to `wss://ris-live.ripe.net/v1/ws/`
  per tracked prefix, writes into the same detection table via the same helpers. Graceful
  exit if `websockets` isn't installed (not added as a hard dep).
- **RIPEstat helpers:** added `fetch_related_prefixes` (+ `_coerce_asn`) to
  `app/services/bgp/ripestat.py`; exported via `app/services/bgp/__init__.py`. Reuses existing
  `fetch_prefix_overview` (exact origins) + `fetch_announced_prefixes` (tracked-prefix seeding).

## REST endpoints (on asns_router, `/api/v1/asns`)
All audited via `write_audit`, permission-gated (`manage_asns`), async:
- `GET  /asns/bgp/tracked-prefixes` (filter asn_id, enabled)
- `POST /asns/{asn_id}/bgp/tracked-prefixes` (add manual)
- `DELETE /asns/bgp/tracked-prefixes/{prefix_id}`
- `GET  /asns/bgp/hijacks` (filter asn_id, detection_kind, active_only, limit)
- `POST /asns/bgp/hijacks/{id}/acknowledge`
- `POST /asns/bgp/hijacks/{id}/allowlist-origin` (append observed origin to tracked prefix's allowlist + ack)
- `POST /asns/{asn_id}/refresh-bgp` (synchronous "Check BGP now")
- Settings: added `bgp_monitoring_enabled` + `bgp_monitoring_interval_hours` to
  `SettingsResponse` / `SettingsUpdate` + interval validator (1..168).

## MCP tools (`backend/app/services/ai/tools/bgp_monitor.py`, imported in tools/__init__.py)
- `find_bgp_hijacks` — read, **default_enabled=True**, module=`network.asn`
- `count_bgp_hijacks` — read, **default_enabled=True**, module=`network.asn`
- `find_tracked_prefixes` — read, **default_enabled=True**, module=`network.asn`
- `propose_allowlist_bgp_origin` — write, **default_enabled=False**, module=`network.asn`;
  backed by Operation `allowlist_bgp_origin` (preview/apply) registered in
  `operations_writes.py`, `required_permission=("write", "manage_asns")`, audited.

## Frontend
- `frontend/src/lib/api.ts` — new types (`BGPTrackedPrefix`, `BGPHijackDetection`,
  `BGPHijackKind`, `BGPHijackRpkiStatus`, `BGPRefreshResult`) + `asnsApi` methods
  (`listTrackedPrefixes`, `createTrackedPrefix`, `deleteTrackedPrefix`, `listHijacks`,
  `acknowledgeHijack`, `allowlistHijackOrigin`, `refreshBgp`).
- `frontend/src/pages/network/BgpMonitorTab.tsx` — new **BGP Monitoring** tab: detections
  table (severity + RPKI status pills + acknowledge/allowlist actions), tracked-prefixes
  table (add manual / delete), "Check BGP now" button, private-ASN notice. Uses shared
  `ConfirmModal` (no browser confirms) + `Modal`.
- `frontend/src/pages/network/AsnDetailPage.tsx` — wired the tab in (type, TABS entry, render).
- Alert rules appear in `/admin/alerts` automatically (existing admin UI).
- `npm run build` + `tsc -b` pass clean.

## Tests
- `backend/tests/test_bgp_hijack_monitor.py` (11 tests, all pass) — mocked HTTP (no live net):
  RPKI unknown/invalid/valid derivation, severity ladder, origin-AS mismatch opens detection,
  expected + allowlisted origins skipped, invalid→critical severity, more-specific detection
  (incl. own more-specific NOT firing), latch dedup (bump last_seen, no new row) + auto-resolve
  after delist window, alert evaluator opens AlertEvent w/ per-detection severity + auto-resolves
  when detection resolves, acknowledged detections never open an alert.

## Docs
- `docs/OBSERVABILITY.md` §9 — added the two rule types to the alert table and a new
  **§9.1a BGP prefix-hijack monitoring** section (tables, RPKI severity ladder, latch/auto-resolve,
  RIS Live vs periodic poll worker model + env flag, surfaces, MCP tools, feature-module decision).

## Verification done
- ruff + black clean on all changed backend files; host mypy clean on new modules.
- `app.main` imports; tools + operation + rule types confirmed registered with expected defaults.
- Migration upgrade→head + downgrade -1 clean on scratch DB; `lint_migrations.py` OK.
- Frontend `tsc -b` + `npm run build` clean.

## Unverified / notes
- Live RIPEstat / RIS Live network shapes are mocked in tests; the `related-prefixes` /
  `prefix-overview` parsers are defensive but not exercised against the real upstream.
- The RIS Live consumer requires the `websockets` package (not added to `pyproject.toml` since
  the feature never depends on it); it exits with a clear log if absent.
- `websockets` async-iteration message decode handled (`str | bytes`).
- Baseline additions include two sibling-issue migrations (see Migration lint) — expected for
  the shared `issues-523-524-527-528-530` branch; no baseline lines removed.
