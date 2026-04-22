import json
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings


def _json_serializer(value: Any) -> str:
    """SQLAlchemy JSON/JSONB column serializer.

    Stdlib ``json.dumps`` rejects ``uuid.UUID``, ``datetime``, ``Decimal``,
    ``ipaddress.IP*Network``, and a few other perfectly-serializable-as-text
    types. We stringify any of them via ``default=str`` so audit-log writes
    and other ``JSONB`` columns that capture ``pydantic.model_dump()``
    output don't 500 when the caller forgets to coerce. Loses some type
    round-tripping on read, but the DB-side representation is JSON text
    anyway — callers that need types back go through the ORM attribute
    which re-parses strings as needed.
    """
    return json.dumps(value, default=str)


engine = create_async_engine(
    settings.database_url,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    echo=settings.debug,
    json_serializer=_json_serializer,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
