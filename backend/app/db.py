import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, with_loader_criteria

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


# ── Per-task DB session for Celery ────────────────────────────────────────
#
# Celery's ``asyncio.run(...)`` pattern spins up a fresh event loop per
# task invocation. The shared ``engine`` / ``AsyncSessionLocal`` above
# bind their asyncpg connections to whichever loop first checked them
# out — re-using them from a later task surfaces as
# ``RuntimeError: Future attached to a different loop``.
#
# Tasks should call this helper instead of importing ``AsyncSessionLocal``
# directly. It builds a throwaway engine + session factory scoped to the
# current call so the connection lifecycle matches the loop lifecycle.
# Cost is one extra TCP handshake per task — acceptable for our task
# cadence (seconds, not milliseconds).


@asynccontextmanager
async def task_session() -> AsyncGenerator[AsyncSession, None]:
    """Per-Celery-task DB session — fresh engine, fresh loop binding."""

    task_engine = create_async_engine(
        settings.database_url,
        future=True,
        json_serializer=_json_serializer,
    )
    factory = async_sessionmaker(task_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await task_engine.dispose()


# ── Global soft-delete filter ──────────────────────────────────────────────
#
# A ``do_orm_execute`` event listener injects ``Model.deleted_at IS NULL``
# into every SELECT touching one of the in-scope models. Callers that
# legitimately need to see soft-deleted rows (Trash list, Restore endpoint,
# the nightly purge sweep, audit / diagnostic tooling) opt in with
# ``execution_options(include_deleted=True)``.
#
# Implementation note: ``with_loader_criteria(include_aliases=True)`` makes
# the criterion apply through every JOIN / relationship load too, so we
# don't have to thread the option through ``selectinload`` paths or
# relationship loads. The hook is mounted on the AsyncSession's underlying
# sync_session_class because that's where SQLAlchemy fires ORM events from
# under the AsyncSession wrapper.


def _soft_delete_models() -> tuple[type, ...]:
    """Resolve in-scope models lazily so circular imports don't bite.

    Importing the model modules at module-load time would create a cycle
    (``app.db`` ↔ ``app.models.*`` ↔ ``app.models.audit_forward`` …).
    The hook fires per-execute so the cost of one-time import-and-cache
    is trivial.
    """

    from app.models.dhcp import DHCPScope
    from app.models.dns import DNSRecord, DNSZone
    from app.models.ipam import IPBlock, IPSpace, Subnet

    return (IPSpace, IPBlock, Subnet, DNSZone, DNSRecord, DHCPScope)


_CACHED_SOFT_DELETE_MODELS: tuple[type, ...] | None = None


def _get_soft_delete_models() -> tuple[type, ...]:
    global _CACHED_SOFT_DELETE_MODELS
    if _CACHED_SOFT_DELETE_MODELS is None:
        _CACHED_SOFT_DELETE_MODELS = _soft_delete_models()
    return _CACHED_SOFT_DELETE_MODELS


def _statement_references(statement: Any, model: type) -> bool:
    """Cheap check — is ``model`` mentioned in the FROM-clause graph?

    Walks the statement's ``column_descriptions`` (top-level entities)
    plus any explicit FROMs reachable via ``get_final_froms``. Avoids the
    cost of injecting loader criteria for models that aren't in the
    query at all (e.g. an audit-log SELECT shouldn't carry six dangling
    loader options for IPSpace / IPBlock / Subnet / DNSZone / DNSRecord
    / DHCPScope just because they exist).
    """

    try:
        for desc in getattr(statement, "column_descriptions", None) or []:
            if desc.get("entity") is model:
                return True
        froms = statement.get_final_froms() if hasattr(statement, "get_final_froms") else []
        for fr in froms or []:
            mapper = getattr(fr, "_annotations", {}).get("parententity")
            if mapper is not None and getattr(mapper, "class_", None) is model:
                return True
            if getattr(fr, "name", None) == getattr(model, "__tablename__", None):
                return True
    except Exception:  # pragma: no cover — defensive, never block a query
        return True
    return False


@event.listens_for(Session, "do_orm_execute")
def _filter_soft_deleted(execute_state: Any) -> None:
    """Inject ``deleted_at IS NULL`` into every SELECT against in-scope models.

    Skips:
      * non-SELECT statements (UPDATE / DELETE / INSERT have their own
        WHERE clause already)
      * statements that opt out via ``include_deleted=True``
      * models that aren't referenced in the statement at all

    Implementation note: ``propagate_to_loaders=False`` keeps the criterion
    off relationship loads. Without it, a SELECT against ``DHCPScope`` (which
    eager-joins ``pools`` / ``statics``) would require ``.unique()`` on
    every result — a sprawling regression. We accept that relationship
    loads can surface soft-deleted descendants; the cascade soft-delete
    pattern compensates by stamping parents + children atomically, so a
    "live" parent can't point at a soft-deleted child via the relationship
    in normal flow.
    """

    if not execute_state.is_select:
        return
    if execute_state.execution_options.get("include_deleted", False):
        return

    statement = execute_state.statement
    for model in _get_soft_delete_models():
        if not _statement_references(statement, model):
            continue
        statement = statement.options(
            with_loader_criteria(
                model,
                lambda cls: cls.deleted_at.is_(None),
                include_aliases=True,
                propagate_to_loaders=False,
            )
        )
    execute_state.statement = statement
