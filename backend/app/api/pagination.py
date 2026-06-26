"""Shared pagination envelope for list endpoints (#455).

A generic ``{items, total, page, page_size}`` response so list endpoints that
can grow unbounded (DNS zone records, DHCP leases, …) paginate server-side
instead of returning the whole table in one query + payload. New list
endpoints should adopt ``Page[T]`` for a consistent shape — the same shape the
nmap / network-device list endpoints already use ad-hoc.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

# Default page size for paginated list endpoints. 100 keeps a 20k-record zone
# (the #455 trigger) to a 200-row poll instead of pulling the whole table.
DEFAULT_PAGE_SIZE = 100
# Upper bound a client may request. Bounded so "fetch all" can't sneak back in
# through an enormous page_size — bulk export has its own dedicated endpoint.
MAX_PAGE_SIZE = 1000


class Page[T](BaseModel):
    """A single page of ``items`` plus the counters the UI needs to render
    page controls (``total`` across all pages, current ``page``, ``page_size``).
    """

    items: list[T]
    total: int
    page: int
    page_size: int


async def paginate(
    db: AsyncSession, base: Select[Any], *, page: int, page_size: int
) -> tuple[list[Any], int]:
    """Return ``(page_rows, total)`` for ``base``, a ``select`` of ORM entities.

    ``base`` should already carry its ``WHERE`` + ``ORDER BY``. ``total`` is
    counted with the ordering stripped (it's irrelevant to a count and lets
    Postgres skip the sort); the returned slice keeps the order and applies
    ``LIMIT``/``OFFSET``. Callers map the scalar rows to their response model.
    """
    total = (
        await db.execute(select(func.count()).select_from(base.order_by(None).subquery()))
    ).scalar_one()
    rows = (await db.execute(base.limit(page_size).offset((page - 1) * page_size))).scalars().all()
    return list(rows), total
