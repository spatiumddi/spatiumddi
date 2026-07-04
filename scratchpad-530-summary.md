# Issue #530 — GeoDNS / topology-aware steering for GSLB pools

Adds client-location/subnet-aware steering to DNS pools (GSLB-lite). A pool
member can now carry a **serving scope** (client CIDRs and/or a Site); a scoped
member is served only to clients whose resolver source IP matches, while a
member with no scope stays a **default** target served to everyone. Rendered on
BIND9 via synthesized `view { match-clients … }` blocks. Health-check gating is
preserved and composed with geo scoping.

## Migration
- New file: `backend/alembic/versions/a3d7f1c9e6b2_dns_pool_member_geo_steering.py`
- **revision** = `a3d7f1c9e6b2`
- **down_revision** = `c9d4a1e8b672` (confirmed current head — nothing else chains off it)
- Adds `dns_pool_member.serving_cidrs` (JSONB, NOT NULL, `server_default '[]'::jsonb`)
  + `dns_pool_member.site_id` (UUID, nullable) + index `ix_dns_pool_member_site`
  + FK `fk_dns_pool_member_site` → `site(id)` `ON DELETE SET NULL`. Additive only.
- Downgrade drops exactly the columns/index/FK it created; the 3 destructive-op
  findings were added to `backend/alembic/migrations_lint_baseline.txt` via
  `python3 scripts/lint_migrations.py --baseline` (only my 3 entries added; linter
  now passes).

## Model columns added (`backend/app/models/dns.py`, `DNSPoolMember`)
- `serving_cidrs: Mapped[list]` — JSONB list of client CIDR strings, default `[]`.
- `site_id: Mapped[uuid.UUID | None]` — FK to `site`, `ondelete="SET NULL"`, indexed.
- Empty CIDRs + null site ⇒ default target (historical behaviour).

## Driver / render approach
- **No BIND9 driver change needed** — the driver template already renders views
  with `match-clients` ACLs + per-`view_name` zones. Geo logic lives entirely in
  **bundle assembly**.
- New shared helper `backend/app/services/dns/pool_geo.py`:
  - `build_geo_steering(db, group_id)` — resolves each member's scope
    (`serving_cidrs` ∪ Site's subnet CIDRs via `subnet.site_id`), canonicalises +
    dedupes CIDRs, groups members by distinct scope into deterministic geo views
    (`spatium-geo-1…N`), returns `GeoSteering(views, member_view)`.
  - `build_view_descriptors(ordered_views, geo)` — unified ordered descriptor list:
    operator split-horizon views (their order) → geo views → catch-all
    `spatium-geo-default` (`match-clients { any; }`, last so BIND first-match-wins
    picks a specific geo view, else the default set).
  - `records_for_view(rec_rows, view_desc, geo)` — per-view record filter composing
    operator `view_id` scoping (#24) with geo: a geo member's record only in its geo
    view; an operator-scoped record only in its operator view; everything else
    (`view_id IS NULL`, not geo-scoped) shared into every view. So a geo view serves
    `{geo members} ∪ {default members}` and the catch-all serves only default members.
  - No `DNSView`/`DNSAcl` rows persisted — pure render-time concern.
- Wired into **both** bundle builders:
  - `backend/app/services/dns/agent_config.py` (LIVE path agents consume) — geo
    forces views mode on; zone loop + `views_block` rebuilt off the unified
    descriptors; records fold into the structural etag under views mode so geo
    changes re-render view-correctly.
  - `backend/app/services/dns/config_bundle.py` (dataclass path → `BIND9Driver.
    render_server_config`) — mirrored so the named.conf render (and the driver test)
    emit geo views.

## Schema (Pydantic) — `backend/app/api/v1/dns/pool_router.py`
- `PoolMemberWrite`: `serving_cidrs: list[str] = []` + `site_id: uuid.UUID | None`,
  with a CIDR-normalising validator (`_normalise_cidrs`, canonical form, 422 on bad).
- `PoolMemberResponse`: surfaces `serving_cidrs` + `site_id`.
- `PoolMemberUpdate`: `serving_cidrs: list[str] | None` + `site_id: uuid.UUID | None`,
  applied via `model_fields_set` so an explicit `serving_cidrs=[]` / `site_id=null`
  **clears** the scope (kept out of the `exclude_none` generic loop). Scope changes
  don't touch DNSRecord rows (only their per-view placement), so no `apply_pool_state`
  reconcile is needed — the agent's next long-poll re-renders. `create_pool`,
  `add_member` validate `site_id` exists (`_assert_site_exists` → 404).

## Frontend
- `frontend/src/lib/api.ts` — `DNSPoolMember` + `DNSPoolMemberWrite` gain
  `serving_cidrs` / `site_id`; `updatePoolMember` data type widened to accept them.
- `frontend/src/pages/dns/PoolsView.tsx` — member editor now has per-member
  "Serving scope — client CIDRs" text box + "Serving scope — Site" `<select>` (loaded
  via `sitesApi.list`), wired through create/update/add with change detection; a `geo`
  chip renders on scoped member rows; an amber helper note explains resolver-source-IP
  + TTL semantics.

## MCP
- Existing pool MCP tool: `list_dns_pools` (read-only). **Extended** its per-member
  output with `serving_cidrs` + `site_id`, and enriched the tool description ("which
  datacenter does the EU client get for www?"). No new `propose_*` tool added (per the
  task — surfacing scope on the existing read tool satisfies non-negotiable #13).

## Docs
- `docs/features/DNS.md` — new **§17 GSLB pools + geo/topology-aware steering**:
  serving-scope model, synthesized-geo-view rendering, resolver-source-IP v1 semantics,
  the ECS stretch note, and the TTL-race caveat.
- `docs/drivers/DNS_DRIVERS.md` — BIND9 Named.conf section notes geo views render like
  operator split-horizon views (synthesis in `pool_geo`), catch-all default view, ECS note.

## Tests
- `backend/tests/test_dns_pool_geo_steering.py` (new): `build_geo_steering` CIDR-scope
  grouping + member→view mapping; Site-scope subnet resolution; inactive-without-scopes;
  live agent bundle geo view/ACL + per-view record scoping (geo view = geo+default,
  catch-all = default only); health-gating composes (unhealthy geo member not served);
  BIND9 driver named.conf renders the geo `view` + `match-clients` ACL + catch-all, with
  per-view zone-file record scoping. Mirrors `test_dns_bulk_create_and_recursion.py` +
  `test_dns_driver.py` style.

## Linters
- `ruff check` + `black --check` pass on all changed Python (incl. test + migration).
- Migration lint baseline updated. Frontend `tsc -b` passes clean.

## Unverified / notes
- **Tests were NOT executed** — this environment has no backend venv/Postgres
  (`asyncpg` missing, conftest needs per-worker DBs). Logic reviewed statically against
  the existing split-horizon paths; assertions modelled on real bundle/driver shapes.
- Geo + **operator split-horizon views in the same group** is an advanced combo: geo
  members render only into geo views (not operator views), and a client matching an
  operator view first won't get geo steering. Documented as intended-for-groups-without-
  operator-views. Geo + **catalog zones** is unsupported (same as views + catalog today).
- A pure serving-scope edit fires no `enqueue_record_op` (records unchanged), so it
  isn't wake-published; it converges on the ~12 s `WAKE_TICK_SECONDS` safety tick.
- Site scope resolves from `subnet.site_id` (live subnets; soft-deleted excluded by the
  default ORM filter). IPBlock-level `site_id` is not folded in (subnets are the
  client-facing routable nets); can be added later if needed.
