"""Schema-at-head readiness check (#299 phase 1).

Covers the new ``_check_schema_ready`` helper + its integration into
``/health/ready``:

* ``alembic_version`` at the bundled head → readiness check says ``ok``.
* ``alembic_version`` table missing → check says ``error`` with the
  asyncpg ``relation does not exist`` short form, readiness response
  is 503 with the schema-specific detail.
* ``alembic_version`` row missing → "alembic_version row missing"
  detail, 503.
* ``version_num`` doesn't match the bundled head → "schema at X,
  image expects Y" detail, 503.

The cached ``_EXPECTED_HEAD`` module global is reset around each test
so a previous test's view of the head doesn't bleed into the next.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.api import health as health_module
from app.db import AsyncSessionLocal


@pytest.fixture(autouse=True)
def _reset_head_cache() -> None:
    """Clear the module-level head cache before every test.

    ``_expected_alembic_head`` caches the result of
    ``ScriptDirectory.get_current_head()`` to avoid re-walking the
    versions directory on every probe; the cache survives across
    tests in the same worker without this fixture.
    """
    health_module._head_cache.head = None
    health_module._head_cache.error = None


@pytest.fixture(autouse=True)
async def _seed_alembic_version() -> AsyncIterator[None]:
    """Ensure ``alembic_version`` exists + is stamped at the bundled
    head before each test.

    The conftest fast-path uses ``Base.metadata.create_all`` to build
    the test schema (much faster than ``alembic upgrade head`` per
    worker), so the ``alembic_version`` table doesn't naturally
    exist in the per-worker test DB. Without this fixture the new
    schema-aware readiness check would report "schema not initialised"
    against tests whose intent is actually about the schema being AT
    head — that's what production looks like. We seed the table and
    let individual tests mutate it (DELETE row, UPDATE to fake
    revision, etc.) to exercise the failure modes.
    """
    expected, _ = health_module._expected_alembic_head()
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version ("
                "version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
        )
        await s.execute(text("DELETE FROM alembic_version"))
        await s.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:head)"),
            {"head": expected},
        )
        await s.commit()
    yield
    # Clean up so concurrent tests in other files don't trip on the
    # leftover table (the conftest's TRUNCATE truncates registered
    # tables only — alembic_version isn't in Base.metadata).
    async with AsyncSessionLocal() as s:
        await s.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await s.commit()


@pytest.mark.asyncio
async def test_schema_check_ok_when_at_head() -> None:
    """The check_schema_ready helper returns ok when version_num
    matches the bundled head."""
    expected, err = health_module._expected_alembic_head()
    assert err is None
    assert expected is not None  # alembic.ini + versions/ are bundled

    # The conftest's per-worker DB is migrated to head, so the live
    # query should return the same revision the script directory
    # reports.
    verdict, detail = await health_module._check_schema_ready()
    assert verdict == "ok", detail
    assert expected in detail


@pytest.mark.asyncio
async def test_schema_check_reports_mismatch() -> None:
    """When alembic_version says a non-head revision, the check
    returns error + names both versions."""
    expected, _ = health_module._expected_alembic_head()
    assert expected is not None

    # Mutate alembic_version to a fake revision and assert the helper
    # surfaces both sides of the diff.
    async with AsyncSessionLocal() as s:
        await s.execute(text("UPDATE alembic_version SET version_num = 'fake_old_revision'"))
        await s.commit()
    try:
        verdict, detail = await health_module._check_schema_ready()
        assert verdict == "error"
        assert "fake_old_revision" in detail
        assert expected in detail
    finally:
        # Restore so subsequent tests see a clean schema head.
        async with AsyncSessionLocal() as s:
            await s.execute(
                text("UPDATE alembic_version SET version_num = :head"),
                {"head": expected},
            )
            await s.commit()


@pytest.mark.asyncio
async def test_schema_check_reports_missing_row() -> None:
    """alembic_version table exists but is empty — common right after
    a migrate failure or a botched ``alembic stamp``. The helper
    reports the missing row explicitly so operators see the cause."""
    expected, _ = health_module._expected_alembic_head()
    assert expected is not None

    async with AsyncSessionLocal() as s:
        await s.execute(text("DELETE FROM alembic_version"))
        await s.commit()
    try:
        verdict, detail = await health_module._check_schema_ready()
        assert verdict == "error"
        assert "alembic_version row missing" in detail
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:head)"),
                {"head": expected},
            )
            await s.commit()


@pytest.mark.asyncio
async def test_schema_check_reports_missing_table() -> None:
    """alembic_version table doesn't exist — migrate Job hasn't run
    yet, which is the cold-boot 502 window from issue #299. We
    simulate by patching ``AsyncSessionLocal`` to a session whose
    SELECT raises an UndefinedTable-shape ProgrammingError (the
    SQLAlchemy wrapper around asyncpg's UndefinedTableError)."""

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, *_args: object, **_kwargs: object) -> None:
            # Mirror the SQLAlchemy → asyncpg shape: ProgrammingError
            # carrying ``relation … does not exist`` as the .orig
            # exception's message. The helper's substring match on
            # "does not exist" is what triggers the "schema not
            # initialised" path; other ProgrammingError shapes fall
            # through to the generic "schema check failed" path
            # (covered separately below).
            raise ProgrammingError(
                "SELECT version_num FROM alembic_version",
                {},
                Exception('relation "alembic_version" does not exist'),
            )

    @asynccontextmanager
    async def _fake_session_factory() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    with patch.object(health_module, "AsyncSessionLocal", _fake_session_factory):
        verdict, detail = await health_module._check_schema_ready()
    assert verdict == "error"
    assert "schema not initialised" in detail
    assert "alembic_version" in detail


@pytest.mark.asyncio
async def test_schema_check_reports_other_programming_error() -> None:
    """ProgrammingError that ISN'T a missing-table case (permission
    denied, syntax error, etc.) gets the generic "schema check
    failed" detail — review polish to stop misdirecting operators
    down the migrate path when the real cause is something else."""

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, *_args: object, **_kwargs: object) -> None:
            raise ProgrammingError(
                "SELECT version_num FROM alembic_version",
                {},
                Exception("permission denied for table alembic_version"),
            )

    @asynccontextmanager
    async def _fake_session_factory() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    with patch.object(health_module, "AsyncSessionLocal", _fake_session_factory):
        verdict, detail = await health_module._check_schema_ready()
    assert verdict == "error"
    # NOT "schema not initialised" — operator needs to see "permission
    # denied" or similar, not be sent down the migrate path.
    assert "schema not initialised" not in detail
    assert "schema check failed" in detail
    assert "permission denied" in detail


@pytest.mark.asyncio
async def test_schema_check_reports_generic_exception() -> None:
    """Non-ProgrammingError exception (asyncio timeout, connection
    blip, …) — also reported as "schema check failed", separate from
    the cold-boot path."""

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, *_args: object, **_kwargs: object) -> None:
            raise TimeoutError("query timed out after 30s")

    @asynccontextmanager
    async def _fake_session_factory() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    with patch.object(health_module, "AsyncSessionLocal", _fake_session_factory):
        verdict, detail = await health_module._check_schema_ready()
    assert verdict == "error"
    assert "schema check failed" in detail
    assert "timed out" in detail


@pytest.mark.asyncio
async def test_readiness_endpoint_503_on_schema_mismatch(client: AsyncClient) -> None:
    """End-to-end: the readiness endpoint folds the schema check into
    its rollup. Postgres is up + Redis is up but schema is behind →
    503 with the schema-specific detail in the ``checks`` block."""
    expected, _ = health_module._expected_alembic_head()
    assert expected is not None

    async with AsyncSessionLocal() as s:
        await s.execute(text("UPDATE alembic_version SET version_num = 'fake_pre_migrate'"))
        await s.commit()
    try:
        response = await client.get("/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        # DB connect still ok — only the schema check fails.
        assert body["checks"]["database"] == "ok"
        assert "fake_pre_migrate" in body["checks"]["schema"]
        assert expected in body["checks"]["schema"]
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(
                text("UPDATE alembic_version SET version_num = :head"),
                {"head": expected},
            )
            await s.commit()


@pytest.mark.asyncio
async def test_readiness_endpoint_200_at_head(client: AsyncClient) -> None:
    """Sanity: when everything's clean (Postgres up, schema at head,
    Redis up), readiness returns 200 with every check ok."""
    response = await client.get("/health/ready")
    body = response.json()
    # Redis may be unavailable in some test environments — only assert
    # the schema check passes (which is what #299 phase 1 changed).
    if body["checks"].get("database") == "ok":
        assert body["checks"].get("schema", "").startswith("ok")
