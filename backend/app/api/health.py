"""Health probe endpoints consumed by Docker/Kubernetes."""

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, cast

import structlog
from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from starlette import status
from starlette.responses import JSONResponse

from app.db import AsyncSessionLocal

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["health"])


# ── Schema-at-head cache (issue #299 phase 1) ──────────────────────
#
# The api image bundles a fixed set of alembic revisions, so the
# "expected head" is constant for the lifetime of the process — read
# it once, cache it, never re-read. On the appliance shape the
# migrate Job lands the head into the DB AFTER the api Deployment
# starts (CNPG bootstrap takes 1-2 min on a multi-node cluster), so
# without a schema-aware readiness probe the api passes /health/ready
# the moment Postgres accepts a SELECT 1 — even though every route
# handler then 500s against missing tables. The operator-visible
# symptom is a ~1-2 min window of bare nginx 502s on every login
# attempt (issue #299). Fixing /health/ready to require the schema
# at head makes the k8s readinessProbe accurately reflect "can serve
# traffic"; the readinessProbe controlling the Service endpoint set
# is what nginx upstream resolution depends on.
#
# Single dataclass instance instead of parallel scalars so the
# linter sees one used global instead of two it doesn't recognise
# via the ``global`` declaration. Same semantics — ``head`` is set
# once on success, ``error`` is set on "no head revision" config
# bugs (also persistent), and transient exceptions are NOT cached
# (the function re-tries on the next probe).
@dataclass(slots=True)
class _SchemaHeadCache:
    head: str | None = None
    error: str | None = None


_head_cache = _SchemaHeadCache()


def _locate_alembic_ini() -> Path | None:
    """Find ``alembic.ini`` for the running process.

    Two production deployments + the test path each put the file in
    a different place:

    * **Container image** (api / migrate Job) — baked at
      ``/app/alembic.ini`` by the Dockerfile's ``COPY``. This is
      where every appliance + helm install reads it.
    * **CI ``Backend — Tests``** — pytest runs against the source
      tree directly, working dir ``backend/``, so the file is at
      ``./alembic.ini`` relative to cwd.
    * **Dev (host venv)** — same as CI; pytest from ``backend/``.

    Search order: container path → relative to this module → cwd.
    Returns ``None`` if no candidate exists; caller surfaces a clear
    "could not read alembic head" message.
    """
    candidates = [
        Path("/app/alembic.ini"),
        # ``app/api/health.py`` → ``app/api`` → ``app`` → ``backend``,
        # where alembic.ini lives.
        Path(__file__).resolve().parent.parent.parent / "alembic.ini",
        Path.cwd() / "alembic.ini",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _expected_alembic_head() -> tuple[str | None, str | None]:
    """Read + cache the bundled alembic head.

    Returns ``(head, None)`` on success, ``(None, error_str)`` if the
    alembic.ini / scripts directory is missing or malformed.

    Cached at first call. Re-reading the script directory on every
    readiness probe call would be wasteful (and confusing during a
    multi-node rolling upgrade where the head DOES change between
    different api pods running different image tags — but each pod
    has its own image, so each pod's cache is correct for itself).
    """
    if _head_cache.head is not None or _head_cache.error is not None:
        return _head_cache.head, _head_cache.error
    ini_path = _locate_alembic_ini()
    if ini_path is None:
        # Don't cache — the absence may be a packaging bug operators
        # need to see, but tests / dev environments swap in a fresh
        # working dir between fixtures and we want the next probe to
        # re-find the file.
        msg = "alembic.ini not found (looked in /app, source tree, cwd)"
        logger.warning("readiness_schema_head_read_failed", error=msg)
        return None, msg
    try:
        # Same ScriptDirectory pattern as
        # app/services/backup/migrations.py. ScriptDirectory.
        # get_current_head() walks the versions/ tree and returns the
        # leaf revision (single-head schemas only; multi-head
        # environments aren't supported by the SpatiumDDI shape).
        from alembic.config import Config  # noqa: PLC0415
        from alembic.script import ScriptDirectory  # noqa: PLC0415

        cfg = Config(str(ini_path))
        script = ScriptDirectory.from_config(cfg)
        head = script.get_current_head()
        if head is None:
            _head_cache.error = "no head revision in script directory"
            return None, _head_cache.error
        _head_cache.head = head
        logger.info("readiness_schema_head_cached", expected_head=head, ini_path=str(ini_path))
        return head, None
    except Exception as exc:  # noqa: BLE001 — surface ANY exception
        # Don't cache transient errors — if the container's alembic
        # files genuinely missing this is a config bug operators need
        # to see; if it's a one-off blip, the next probe re-reads.
        msg = f"could not read alembic head: {exc}"
        logger.warning("readiness_schema_head_read_failed", error=str(exc))
        return None, msg


async def _check_schema_ready() -> tuple[str, str]:
    """Verify the DB schema is at the head this api image expects.

    Returns ``("ok", "<detail>")`` if the schema matches; otherwise
    ``("error", "<detail>")`` with a message the operator can act on
    ("migrate not run", "schema at X, image expects Y", etc.). The
    caller folds this into the readiness verdict alongside DB + Redis.

    Failure modes covered (each gets a distinct operator-actionable
    detail string so the cause isn't ambiguous):

    * ``alembic_version`` table doesn't exist — the migrate Job hasn't
      created the schema at all. SQLAlchemy ``ProgrammingError``
      wrapping asyncpg ``UndefinedTableError``, message contains
      ``does not exist``. Reported as ``schema not initialised: …``.
    * Other ``ProgrammingError`` shapes — permission denied,
      malformed schema, etc. Reported as
      ``schema check failed: …`` so operators don't get misled into
      thinking migrations need to run (review polish from #301).
    * ``alembic_version`` row missing — alembic stamp / upgrade was
      interrupted. Reported as ``alembic_version row missing — …``.
    * ``version_num != expected_head`` — schema is behind (migrate
      still running, or a rolling upgrade started but didn't finish).
      Reported with both revisions so operators can compare.
    * Any other exception (asyncio timeout, connection blip not
      caught upstream by the SELECT 1 check, …). Reported as
      ``schema check failed: …``.
    """
    expected, head_err = _expected_alembic_head()
    if head_err is not None:
        return "error", head_err
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("SELECT version_num FROM alembic_version"))
            row = result.fetchone()
    except ProgrammingError as exc:
        # asyncpg.exceptions.UndefinedTableError → "relation
        # 'alembic_version' does not exist". Other ProgrammingError
        # shapes (permission denied, syntax error, etc.) are real
        # config bugs the operator needs to see — don't lump them in
        # with the "schema not initialised" cold-boot case.
        logger.warning("readiness_schema_check_failed", error=str(exc), expected_head=expected)
        short = str(exc).splitlines()[0][:160]
        if "does not exist" in short:
            return "error", f"schema not initialised: {short}"
        return "error", f"schema check failed: {short}"
    except Exception as exc:  # noqa: BLE001 — surface ANY exception
        # Any other DB error — connection blip after the SELECT 1
        # succeeded, statement timeout against a wedged server, etc.
        # Don't claim "schema not initialised" — operators chasing a
        # connection issue shouldn't be sent down the migrate path.
        logger.warning("readiness_schema_check_failed", error=str(exc), expected_head=expected)
        short = str(exc).splitlines()[0][:160]
        return "error", f"schema check failed: {short}"
    if row is None:
        return "error", "alembic_version row missing — migrate not stamped"
    actual = row[0]
    if actual != expected:
        return "error", f"schema at {actual}, image expects {expected}"
    return "ok", f"schema at head {expected}"


@router.get("/health/live", status_code=status.HTTP_200_OK)
async def liveness() -> dict:
    """Liveness probe — returns 200 if the process is running."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    """
    Readiness probe — checks DB connectivity, DB SCHEMA-at-head, and
    Redis connectivity. Returns 200 if ready, 503 with failed-check
    details if not.

    Schema-at-head is the key check for the cold-boot + post-restore
    + mid-rolling-upgrade windows where Postgres is up + accepting
    connections but the migrate Job hasn't landed the bundled
    alembic revisions yet. Without this check the api passes the
    readiness probe the moment a ``SELECT 1`` succeeds, gets added to
    the Service endpoint set, and serves 500s on every actual route
    handler until migrations finish — which the operator sees as
    bare nginx 502s through the frontend proxy (issue #299).
    """
    checks: dict[str, str] = {}
    healthy = True

    # Database connectivity check. Same SELECT 1 as before — separate
    # from the schema check so an operator looking at a 503 response
    # can tell "Postgres is down" apart from "Postgres is up but the
    # schema is behind."
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        logger.warning("readiness_check_failed", check="database", error=str(exc))
        checks["database"] = f"error: {exc}"
        healthy = False

    # Schema-at-head check. Only run when the DB connect succeeded —
    # otherwise the schema check would surface a confusing
    # "could not connect" error on top of the database error above.
    if checks["database"] == "ok":
        verdict, detail = await _check_schema_ready()
        checks["schema"] = "ok" if verdict == "ok" else f"error: {detail}"
        if verdict != "ok":
            healthy = False

    # Redis check
    try:
        from app.config import settings
        from app.core.redis_client import make_async_redis

        r = make_async_redis(settings.redis_url, socket_connect_timeout=2)
        await cast(Awaitable[bool], r.ping())
        await r.aclose()
        checks["redis"] = "ok"
    except Exception as exc:
        logger.warning("readiness_check_failed", check="redis", error=str(exc))
        checks["redis"] = f"error: {exc}"
        healthy = False

    body = {"status": "ok" if healthy else "degraded", "checks": checks}
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=code, content=body)


@router.get("/health/startup")
async def startup() -> JSONResponse:
    """Startup probe (Kubernetes slow-start containers) — same logic as readiness."""
    return await readiness()


@router.get("/health/platform")
async def platform_health() -> JSONResponse:
    """Dashboard-oriented rollup of every control-plane component.

    Distinct from ``/health/ready`` in that this one is dashboard-
    facing rather than an orchestrator probe: it enumerates the
    individual pieces (db, redis, celery workers, celery beat) instead
    of returning a single binary verdict, so the UI can show a
    per-component status dot and explain what's wrong. Individual
    failures never make the endpoint itself fail — the caller always
    gets a 200 with the rollup. The top-level ``status`` folds the
    components into a single ``ok`` / ``degraded`` verdict for headline
    display.

    SECURITY (#400 / M5): this endpoint is mounted UNAUTHENTICATED
    (the AppLayout polls it before login for the demo-mode /
    maintenance banner), so component ``detail`` fields must never echo
    raw backend exception strings — those can leak DSNs, internal
    hostnames, driver versions, and stack-frame paths to an anonymous
    caller. On error we log the full ``str(exc)`` server-side (where
    operators can see it) and return only a fixed, generic detail
    string. The ``ok`` / ``error`` / ``warn`` status semantics are
    unchanged, so the per-component dots still render correctly.
    """
    components: list[dict[str, Any]] = [{"name": "api", "status": "ok", "detail": "responding"}]

    # Database
    t0 = monotonic()
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        components.append(
            {
                "name": "postgres",
                "status": "ok",
                "detail": f"SELECT 1 in {(monotonic() - t0) * 1000:.0f} ms",
            }
        )
    except Exception as exc:
        # SECURITY (#400 / M5): never echo str(exc) to this unauthenticated
        # endpoint — it can leak the Postgres DSN / host. Log it server-side.
        logger.warning("platform_health_check_failed", component="postgres", error=str(exc))
        components.append({"name": "postgres", "status": "error", "detail": "postgres error"})

    # Redis
    t0 = monotonic()
    try:
        from app.config import settings
        from app.core.redis_client import make_async_redis

        r = make_async_redis(settings.redis_url, socket_connect_timeout=2)
        await cast(Awaitable[bool], r.ping())
        await r.aclose()
        components.append(
            {
                "name": "redis",
                "status": "ok",
                "detail": f"ping in {(monotonic() - t0) * 1000:.0f} ms",
            }
        )
    except Exception as exc:
        # SECURITY (#400 / M5): generic detail only — str(exc) would leak the
        # Redis URL / host to an unauthenticated caller.
        logger.warning("platform_health_check_failed", component="redis", error=str(exc))
        components.append({"name": "redis", "status": "error", "detail": "redis error"})

    # Celery workers — `inspect().ping()` is a sync, broker-backed RPC
    # that can hang, so run it in a threadpool with a short overall
    # timeout. Returns a mapping like ``{"celery@worker-1": {"ok": "pong"}}``
    # or ``None`` when no worker responds in time.
    def _inspect_ping() -> dict[str, Any] | None:
        from app.celery_app import celery_app  # noqa: PLC0415

        return celery_app.control.inspect(timeout=2).ping()

    try:
        ping = await asyncio.wait_for(asyncio.to_thread(_inspect_ping), timeout=3)
    except TimeoutError:
        ping = None
        workers_detail = "inspect timed out"
    except Exception as exc:  # noqa: BLE001
        # SECURITY (#400 / M5): generic detail only — str(exc) here can carry
        # the broker URL / host. Log the real error server-side.
        logger.warning("platform_health_check_failed", component="celery-workers", error=str(exc))
        ping = None
        workers_detail = "inspect error"
    else:
        workers_detail = None

    if ping:
        workers = sorted(ping.keys())
        components.append(
            {
                "name": "celery-workers",
                "status": "ok",
                "detail": f"{len(workers)} alive",
                "workers": workers,
            }
        )
    else:
        components.append(
            {
                "name": "celery-workers",
                "status": "error",
                "detail": workers_detail or "no workers responding",
                "workers": [],
            }
        )

    # Celery beat — written by ``app.tasks.heartbeat.beat_tick`` every 30 s
    # to ``spatium:beat:heartbeat`` with a 5-minute TTL. Missing key →
    # beat is stopped or has been stopped for >5 min. Present but older
    # than 90 s → degraded (two beat intervals missed).
    try:
        from app.config import settings
        from app.core.redis_client import make_async_redis
        from app.tasks.heartbeat import BEAT_HEARTBEAT_KEY  # noqa: PLC0415

        # Sentinel-aware (HA Redis uses ``sentinel://``, which raw
        # ``aioredis.from_url`` rejects with "must specify one of redis:// …"
        # — the same helper the redis + workers checks above use).
        r = make_async_redis(settings.redis_url, socket_connect_timeout=2)
        raw = await r.get(BEAT_HEARTBEAT_KEY)
        await r.aclose()
        if raw is None:
            components.append(
                {
                    "name": "celery-beat",
                    "status": "error",
                    "detail": "no heartbeat — beat is stopped",
                }
            )
        else:
            ts_str = raw.decode() if isinstance(raw, bytes) else raw
            ts = datetime.fromisoformat(ts_str)
            age_s = (datetime.now(UTC) - ts).total_seconds()
            if age_s > 90:
                status_str = "warn"
                detail = f"last tick {age_s:.0f}s ago (stalled)"
            else:
                status_str = "ok"
                detail = f"last tick {age_s:.0f}s ago"
            components.append(
                {
                    "name": "celery-beat",
                    "status": status_str,
                    "detail": detail,
                    "last_tick": ts.isoformat(),
                }
            )
    except Exception as exc:
        # SECURITY (#400 / M5): generic detail only — str(exc) would leak the
        # Redis URL / internal state to an unauthenticated caller.
        logger.warning("platform_health_check_failed", component="celery-beat", error=str(exc))
        components.append({"name": "celery-beat", "status": "error", "detail": "celery-beat error"})

    rollup = "ok"
    for c in components:
        if c["status"] == "error":
            rollup = "degraded"
            break
        if c["status"] == "warn" and rollup == "ok":
            rollup = "degraded"

    # Surface demo-mode to the frontend so AppLayout can render a
    # persistent banner. Cheap to bundle here — every authenticated
    # page already polls /health/platform for the status dots, so we
    # avoid a separate round-trip on every page load.
    from app.config import settings as _settings

    # Maintenance mode (issue #57) — same rationale: bundle the read-only
    # banner state into the existing poll instead of a separate request.
    # Failure to read it never fails the endpoint (it's a UI hint, not a
    # gate — the middleware is the real enforcement point).
    maintenance_enabled = False
    maintenance_message = ""
    maintenance_started_at: str | None = None
    try:
        from app.core import maintenance_mode as _maintenance_mode

        async with AsyncSessionLocal() as session:
            (
                maintenance_enabled,
                maintenance_message,
                _started,
            ) = await _maintenance_mode.get_maintenance_state(session)
        maintenance_started_at = _started.isoformat() if _started else None
    except Exception:  # noqa: BLE001 — UI hint only
        pass

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": rollup,
            "components": components,
            "demo_mode": bool(_settings.demo_mode),
            "maintenance_mode": maintenance_enabled,
            "maintenance_message": maintenance_message,
            "maintenance_started_at": maintenance_started_at,
        },
    )
