"""War-room native-surface map — the single place the poller + terminal script agree.

Defines the SpatiumDDI native observability endpoints, the canonical NDJSON field
shapes the watchdog + report read, and the response-field mappings that turn each
native payload into those canonical shapes. The poller (``poller.py``) imports the
mapper functions; the terminal fallback (``spatium-warroom.sh``) imports the path +
field-name constants via the small ``--dump-shell`` helper at the bottom so both
agree on field names without copy-paste drift.

Everything here is grounded on the live backend source (cited inline). The native
``/admin/postgres/*`` + ``/admin/redis/*`` are superadmin-gated; ``/health/platform``
is unauthenticated. The deep DB series (locks / deadlocks / per-table tuples) is NOT
exposed by any native endpoint — that is ``psql_probe.py``'s job (§5.4 observer
discipline: single-source-per-metric, and the JSON path goes through the api pool +
CNPG so it's deliberately kept off the deep-DB metrics).

Grounding:
  * /health/platform                         backend/app/api/health.py:270-470
  * /api/v1/admin/postgres/overview          backend/app/api/v1/admin/postgres.py:108-181
  * /api/v1/admin/postgres/connections       backend/app/api/v1/admin/postgres.py:300-318
  * /api/v1/admin/postgres/tables            backend/app/api/v1/admin/postgres.py:251-297
  * /api/v1/admin/redis/overview             backend/app/api/v1/admin/redis.py:109-156
  * /api/v1/admin/redis/wake-bus             backend/app/api/v1/admin/redis.py:186-202
  * /api/v1/metrics/dns/timeseries           backend/app/api/v1/metrics/router.py:92-141
  * /api/v1/metrics/dhcp/timeseries          backend/app/api/v1/metrics/router.py:144-191
  * router mount prefixes                     backend/app/api/v1/router.py:98-243 (/admin, /metrics)
  * api_v1 mounted at /api/v1                 backend/app/main.py:642
  * health router mounted at root            backend/app/main.py:638  (so /health/* is NOT under /api/v1)
  * wake-bus published_by_class / subs shape backend/app/core/agent_wake.py:333-360
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

# ── Celery queue names (Redis LLEN keys) ──────────────────────────────────────
# Tasks are routed to queues "ipam" / "dns" / "dhcp" / "default" — the literal
# queue name is the Redis list key Kombu uses (no task_default_queue override, so
# unrouted tasks land on "celery", but every scheduled/routed task in the suite's
# blast radius is on one of these four).
#   backend/app/celery_app.py:72-119 (task_routes -> {"queue": "ipam|dns|dhcp|default"})
CELERY_QUEUES = ("ipam", "dns", "dhcp", "default")

# Default env var carrying the Redis URL the poller dials for LLEN + (optionally)
# the wake bus. The contract names this explicitly — record an open_item if unset.
REDIS_URL_ENV = "SPDDI_PERF_REDIS_URL"


# ── Native endpoint paths ─────────────────────────────────────────────────────
# Relative to the api base for /api/v1 surfaces; /health/* is host-root (see below).
API_PATHS: dict[str, str] = {
    "pg_overview": "/admin/postgres/overview",
    "pg_connections": "/admin/postgres/connections",
    "pg_tables": "/admin/postgres/tables",
    "redis_overview": "/admin/redis/overview",
    "redis_wakebus": "/admin/redis/wake-bus",
    "metrics_dns": "/metrics/dns/timeseries",
    "metrics_dhcp": "/metrics/dhcp/timeseries",
}
# /health/platform is mounted at the host root (main.py:638), NOT under /api/v1.
HEALTH_PLATFORM_PATH = "/health/platform"

# The platform component names the native rollup returns, mapped to the canonical
# snake_cased keys the watchdog/report read.  health.py emits a *list* of
# {"name","status","detail"} where status ∈ {ok,warn,error}; "ok"/"warn" => up,
# "error" => down.  (health.py:294-424)
HEALTH_COMPONENT_MAP: dict[str, str] = {
    "api": "api",
    "postgres": "postgres",
    "redis": "redis",
    "celery-workers": "celery_worker",
    "celery-beat": "celery_beat",
}
HEALTH_CANONICAL_KEYS = ("api", "postgres", "redis", "celery_worker", "celery_beat")


# ── Base-URL helpers ──────────────────────────────────────────────────────────


def api_v1_base(api_base: str) -> str:
    """Normalise ``m.target.api_base`` to the ``/api/v1`` prefix.

    The manifest's ``api_base`` is e.g. ``https://10.20.0.10/api`` (per the
    contract). The native admin/metrics routers live under ``/api/v1``
    (main.py:642), so append ``/v1`` when the base ends at ``/api``, and tolerate
    a base that already carries ``/api/v1`` or a bare host.
    """
    base = api_base.rstrip("/")
    if base.endswith("/api/v1"):
        return base
    if base.endswith("/api"):
        return base + "/v1"
    return base + "/api/v1"


def host_root(api_base: str) -> str:
    """Scheme+host root of ``api_base`` (for the root-mounted /health/* routes)."""
    parts = urlsplit(api_base if "//" in api_base else "https://" + api_base)
    return urlunsplit((parts.scheme or "https", parts.netloc, "", "", ""))


# ── Canonical NDJSON shape mappers ────────────────────────────────────────────
# Each returns the EXACT dict the watchdog + report consume. An unavailable
# surface is recorded {available: false} by the caller — these mappers assume a
# successful native payload.


def map_health_platform(payload: dict[str, Any]) -> dict[str, Any]:
    """``/health/platform`` -> {"components": {api,postgres,redis,celery_worker,
    celery_beat: bool}}. bool = up (status ok|warn). (health.py:294-470)"""
    comps = {k: False for k in HEALTH_CANONICAL_KEYS}
    for c in payload.get("components", []) or []:
        name = c.get("name")
        canonical = HEALTH_COMPONENT_MAP.get(name)
        if canonical is None:
            continue
        comps[canonical] = c.get("status") in ("ok", "warn")
    out: dict[str, Any] = {"components": comps, "rollup": payload.get("status")}
    # Carry maintenance/demo hints through (cheap; the report annotates windows).
    if payload.get("maintenance_mode"):
        out["maintenance_mode"] = True
    return out


def map_pg_overview(payload: dict[str, Any]) -> dict[str, Any]:
    """``/admin/postgres/overview`` -> the canonical pg_overview shape.

    Native fields: active_connections, max_connections, cache_hit_ratio (0..1|None),
    wal_bytes (int|None on replicas), db_size_bytes, longest_transaction{age_seconds}.
    (postgres.py:73-181)
    """
    longest = payload.get("longest_transaction") or {}
    cache_hit = payload.get("cache_hit_ratio")
    return {
        "active_connections": int(payload.get("active_connections") or 0),
        "max_connections": int(payload.get("max_connections") or 0),
        "longest_txn_age_s": float(longest.get("age_seconds") or 0.0),
        "cache_hit_ratio": float(cache_hit) if cache_hit is not None else 0.0,
        "wal_bytes": int(payload.get("wal_bytes") or 0),
        "db_size_bytes": int(payload.get("db_size_bytes") or 0),
    }


def map_pg_connections(payload: dict[str, Any]) -> dict[str, Any]:
    """``/admin/postgres/connections`` -> {"by_state": {<state>: count}}.

    Native: {"rows": [{"state": str, "count": int}, ...]} where state is the raw
    pg_stat_activity state ("active"/"idle"/"idle in transaction"/...). We normalise
    "idle in transaction" -> "idle_in_transaction" so the watchdog key is stable.
    (postgres.py:300-318)
    """
    by_state: dict[str, int] = {}
    for r in payload.get("rows", []) or []:
        state = str(r.get("state", "unknown")).replace(" ", "_")
        by_state[state] = by_state.get(state, 0) + int(r.get("count") or 0)
    return {"by_state": by_state}


def map_pg_tables(payload: dict[str, Any], *, now_iso: str | None = None) -> dict[str, Any]:
    """``/admin/postgres/tables`` -> {"tables": {<table>: {dead_tup, live_tup,
    last_autovacuum_age_s, bytes}}}.

    Native row: schema_name, table_name, total_bytes, live_rows, dead_rows,
    last_autovacuum (ISO str|None). The native surface has no age field, so we
    derive ``last_autovacuum_age_s`` from (now - last_autovacuum); -1.0 when the
    table has never been autovacuumed. (postgres.py:46-57,251-297)

    NOTE (§5.4): this path goes through the api pool + CNPG. The authoritative
    per-table tuple/autovacuum series is psql_probe's pg_user_tables — this native
    rollup is corroboration only.
    """
    from datetime import datetime, timezone

    ref = datetime.now(timezone.utc)
    if now_iso:
        try:
            ref = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        except ValueError:
            # Unparseable timestamp; fall back to the wall-clock `ref` set above.
            pass
    tables: dict[str, dict[str, Any]] = {}
    for r in payload.get("rows", []) or []:
        name = r.get("table_name")
        if not name:
            continue
        last_av = r.get("last_autovacuum")
        age = -1.0
        if last_av:
            try:
                dt = datetime.fromisoformat(str(last_av).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age = max(0.0, (ref - dt).total_seconds())
            except ValueError:
                age = -1.0
        tables[name] = {
            "dead_tup": int(r.get("dead_rows") or 0),
            "live_tup": int(r.get("live_rows") or 0),
            "last_autovacuum_age_s": age,
            "bytes": int(r.get("total_bytes") or 0),
        }
    return {"tables": tables}


def map_redis_overview(payload: dict[str, Any]) -> dict[str, Any]:
    """``/admin/redis/overview`` -> {used_memory, maxmemory, evicted_keys,
    ops_per_sec, hit_ratio}.

    Native fields: used_memory_bytes, maxmemory_bytes, instantaneous_ops_per_sec,
    keyspace_hits, keyspace_misses. The native surface has no evicted_keys field
    (it's in raw INFO but not re-exposed), so we report it as -1 = "unknown via
    native; read evicted_keys from redis_exporter / the direct redis poll".
    hit_ratio derived from hits/(hits+misses). (redis.py:44-156)
    """
    hits = payload.get("keyspace_hits")
    misses = payload.get("keyspace_misses")
    hit_ratio = 0.0
    if hits is not None and misses is not None:
        total = int(hits) + int(misses)
        hit_ratio = (int(hits) / total) if total > 0 else 0.0
    return {
        "used_memory": int(payload.get("used_memory_bytes") or 0),
        "maxmemory": int(payload.get("maxmemory_bytes") or 0),
        "evicted_keys": int(payload.get("evicted_keys", -1)),  # not in native; -1=unknown
        "ops_per_sec": float(payload.get("instantaneous_ops_per_sec") or 0),
        "hit_ratio": float(hit_ratio),
    }


def map_redis_wakebus(payload: dict[str, Any]) -> dict[str, Any]:
    """``/admin/redis/wake-bus`` -> {"subscribers": int, "publishes": {<class>: int}}.

    Native: total_subscribers (int), published_by_class ({dns|dhcp|...: int}).
    (redis.py:81-202, agent_wake.py:355-360)
    """
    return {
        "subscribers": int(payload.get("total_subscribers") or 0),
        "publishes": dict(payload.get("published_by_class") or {}),
    }


def map_metrics_timeseries(payload: dict[str, Any]) -> dict[str, Any]:
    """``/metrics/{dns,dhcp}/timeseries`` -> the latest 60s bucket (the most recent
    point), plus window/bucket metadata.

    Native: {"window","bucket_seconds","points":[{t, ...counters}]}. We emit the
    last point verbatim (DNS: queries_total/noerror/nxdomain/servfail/recursion/
    rate_dropped/rate_slipped; DHCP: discover/offer/request/ack/nak/decline/release/
    inform). (metrics/router.py:49-191)
    """
    points = payload.get("points") or []
    latest = points[-1] if points else {}
    return {
        "window": payload.get("window"),
        "bucket_seconds": payload.get("bucket_seconds"),
        "n_points": len(points),
        "latest": latest,
    }


# ── Shell-export helper for spatium-warroom.sh ────────────────────────────────
# The terminal fallback sources these so its jq filters pin the real native field
# names without re-deriving them. Keep in sync with the mappers above.

SHELL_EXPORTS = {
    # health
    "HEALTH_PATH": HEALTH_PLATFORM_PATH,
    "HEALTH_COMPONENTS": " ".join(HEALTH_COMPONENT_MAP.keys()),
    # postgres overview jq paths
    "PG_OVERVIEW_PATH": API_PATHS["pg_overview"],
    "PG_FIELD_ACTIVE": ".active_connections",
    "PG_FIELD_MAX": ".max_connections",
    "PG_FIELD_CACHE": ".cache_hit_ratio",
    "PG_FIELD_WAL": ".wal_bytes",
    "PG_FIELD_DBSIZE": ".db_size_bytes",
    "PG_FIELD_LONGEST_TXN": ".longest_transaction.age_seconds",
    # postgres connections / tables
    "PG_CONNS_PATH": API_PATHS["pg_connections"],
    "PG_CONNS_ROWS": ".rows",
    "PG_TABLES_PATH": API_PATHS["pg_tables"],
    "PG_TABLES_ROWS": ".rows",
    # redis
    "REDIS_OVERVIEW_PATH": API_PATHS["redis_overview"],
    "REDIS_FIELD_USED": ".used_memory_bytes",
    "REDIS_FIELD_MAX": ".maxmemory_bytes",
    "REDIS_FIELD_OPS": ".instantaneous_ops_per_sec",
    "REDIS_FIELD_HITS": ".keyspace_hits",
    "REDIS_FIELD_MISSES": ".keyspace_misses",
    # metrics
    "METRICS_DNS_PATH": API_PATHS["metrics_dns"],
    "METRICS_DHCP_PATH": API_PATHS["metrics_dhcp"],
    # celery
    "CELERY_QUEUES": " ".join(CELERY_QUEUES),
    "REDIS_URL_ENV": REDIS_URL_ENV,
}


def _emit_shell_exports() -> None:
    """Print ``KEY=value`` lines for sourcing into spatium-warroom.sh."""
    for k, v in SHELL_EXPORTS.items():
        print(f'SPDDI_SURFACE_{k}="{v}"')


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--dump-shell":
        _emit_shell_exports()
    else:
        print("surfaces.py — war-room native-surface map. Use --dump-shell to emit "
              "KEY=value lines for spatium-warroom.sh.")
