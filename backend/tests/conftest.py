"""Pytest fixtures for integration tests.

Tests run against a real PostgreSQL instance (not mocks) to catch
ORM/query issues early. Set TEST_DATABASE_URL in the environment or
docker-compose.test.yml.

Parallel execution via ``pytest -n auto`` (pytest-xdist) is supported:
each xdist worker carves its own throwaway database off the same Postgres
instance — ``spatiumddi_test_gw0``, ``spatiumddi_test_gw1``, … — so the
session-scoped ``DROP SCHEMA`` + per-test ``TRUNCATE`` can't step on
another worker's data. The non-xdist case (``pytest`` with no ``-n``)
falls through to the unsuffixed base database name, matching pre-xdist
behaviour exactly.
"""

import os
from collections.abc import AsyncGenerator
from urllib.parse import urlsplit, urlunsplit

# IMPORTANT: the per-worker DATABASE_URL override below MUST run before any
# ``app.*`` import — ``app.config.settings`` reads ``DATABASE_URL`` from the
# environment at module-load time, and ``app.db.task_session()`` (used by
# Celery-task tests like the lease-cleanup + reservation-sweep + soft-delete
# purge sweeps) builds throwaway engines against ``settings.database_url``.
# Without this override, those tasks would query the base ``spatiumddi_test``
# database while the test fixtures wrote into ``spatiumddi_test_gw<N>``, and
# the sweeps would find an empty table and the tests would fail.


def _worker_id() -> str:
    """Return the pytest-xdist worker id, or '' when running single-process.

    xdist exposes ``PYTEST_XDIST_WORKER`` per worker process (``gw0``,
    ``gw1``, …); the controller process never sees it. Empty string means
    fall back to the base database name so plain ``pytest`` keeps working.
    """
    return os.getenv("PYTEST_XDIST_WORKER", "")


def _per_worker_url(base_url: str, worker: str) -> str:
    """Append the worker suffix to the database name segment of ``base_url``.

    ``postgresql+asyncpg://u:p@host/spatiumddi_test`` →
    ``postgresql+asyncpg://u:p@host/spatiumddi_test_gw0``.
    """
    if not worker:
        return base_url
    split = urlsplit(base_url)
    new_path = f"{split.path}_{worker}"
    return urlunsplit(split._replace(path=new_path))


def _maintenance_url(base_url: str) -> str:
    """URL for the ``postgres`` maintenance DB — used to CREATE / DROP per-worker DBs.

    ``CREATE DATABASE`` can't run inside the target database, so we
    connect to ``postgres`` (the default maintenance DB present on every
    PG cluster) to issue it.
    """
    split = urlsplit(base_url)
    # asyncpg doesn't understand the SQLAlchemy '+asyncpg' driver suffix —
    # strip it for the raw connection.
    scheme = split.scheme.replace("postgresql+asyncpg", "postgresql")
    return urlunsplit(split._replace(scheme=scheme, path="/postgres"))


def _extract_dbname(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


_BASE_TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://spatiumddi:changeme@localhost:5432/spatiumddi_test",
)
_WORKER = _worker_id()
_TEST_DATABASE_URL = _per_worker_url(_BASE_TEST_DATABASE_URL, _WORKER)

# Force the app's own ``DATABASE_URL`` to point at the per-worker test DB
# *before* the first ``app.*`` import below, so module-level engines (and
# Celery ``task_session`` engines built later) land in the right database.
# In single-process mode this is a no-op rewrite to the same URL CI already
# set; in xdist mode it swaps to the worker-suffixed name.
os.environ["DATABASE_URL"] = _TEST_DATABASE_URL

import asyncpg  # noqa: E402  — must follow the DATABASE_URL override above
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

from app.db import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.base import Base  # noqa: E402

# NullPool: open a fresh asyncpg connection on every checkout and drop it on
# release. The default QueuePool keeps connections bound to the loop they
# were opened on, which collides with pytest-asyncio's per-test loops and
# produces "another operation in progress" / "attached to a different loop"
# errors. This is the cheap, safe fix; tests are I/O-bound on Postgres
# anyway so pool reuse buys nothing here.
_test_engine = create_async_engine(_TEST_DATABASE_URL, echo=False, poolclass=NullPool)
_TestSessionLocal = async_sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)


async def _ensure_worker_database() -> None:
    """Create the per-worker test database if it doesn't already exist.

    No-op when not running under xdist (worker id empty) — the base DB
    is provisioned by CI / docker-compose for the single-process case.
    Idempotent: subsequent test runs reuse the existing per-worker DB
    and the session-scoped schema fixture wipes it clean.
    """
    if not _WORKER:
        return
    dbname = _extract_dbname(_TEST_DATABASE_URL)
    conn = await asyncpg.connect(_maintenance_url(_BASE_TEST_DATABASE_URL))
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", dbname)
        if not exists:
            # asyncpg can't parameterise identifiers; the worker id pattern
            # is r"gw\d+" so injection isn't reachable here.
            await conn.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_test_schema() -> AsyncGenerator[None, None]:
    await _ensure_worker_database()
    # Tear down any prior schema with CASCADE so circular FKs don't block the
    # drop (dns_record ↔ ip_address has a cycle that Base.metadata.drop_all
    # can't untangle). Easiest is to nuke the public schema wholesale.
    async with _test_engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)
    yield
    # Teardown: same trick in reverse. No-op for CI (ephemeral DB) but keeps
    # local runs clean.
    async with _test_engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))


@pytest_asyncio.fixture(autouse=True)
async def _isolate_db() -> AsyncGenerator[None, None]:
    """Truncate every table after each test so state doesn't leak.

    ``db_session`` alone isn't enough: HTTP tests go through FastAPI
    handlers that commit via the dependency-overridden session, so
    rolling back the fixture's session doesn't undo the inserts. A
    TRUNCATE … CASCADE on all mapped tables keeps the schema intact
    (much cheaper than drop_all + create_all) and is loop-safe because
    NullPool gives us a fresh connection.
    """
    yield
    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    if not tables:
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with _TestSessionLocal() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """HTTP test client with DB dependency overridden to the test session."""

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
