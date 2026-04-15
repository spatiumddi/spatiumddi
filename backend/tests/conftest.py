"""Pytest fixtures for integration tests.

Tests run against a real PostgreSQL instance (not mocks) to catch
ORM/query issues early. Set TEST_DATABASE_URL in the environment or
docker-compose.test.yml.
"""

import os
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_db
from app.main import app
from app.models.base import Base

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://spatiumddi:changeme@localhost:5432/spatiumddi_test",
)

# NullPool: open a fresh asyncpg connection on every checkout and drop it on
# release. The default QueuePool keeps connections bound to the loop they
# were opened on, which collides with pytest-asyncio's per-test loops and
# produces "another operation in progress" / "attached to a different loop"
# errors. This is the cheap, safe fix; tests are I/O-bound on Postgres
# anyway so pool reuse buys nothing here.
_test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
_TestSessionLocal = async_sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_test_schema() -> AsyncGenerator[None, None]:
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
