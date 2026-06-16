"""HTTP-01 challenge token store (issue #438 Phase 4).

The CA fetches ``http://<domain>/.well-known/acme-challenge/<token>`` and
expects the key-authorization back verbatim. This module is the storage
side: the orchestrator :func:`publish`es the token→key-authorization
mapping before telling the CA to validate, the unauthenticated well-known
endpoint :func:`lookup`s it, and :func:`cleanup` removes it after.

Cluster-global (DB-backed) on purpose: behind the MetalLB VIP the CA may
hit any frontend replica, which proxies to any api pod — per-pod memory
wouldn't survive that fan-out.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.acme_client import ACMEHTTPChallenge

logger = structlog.get_logger(__name__)


async def publish(
    db: AsyncSession, order_id: uuid.UUID, token: str, key_authorization: str
) -> None:
    """Upsert the token → key-authorization mapping for an order.

    Idempotent on ``token`` (a re-run re-publishes the same value), so a
    retry doesn't collide on the unique token index.
    """
    stmt = (
        pg_insert(ACMEHTTPChallenge)
        .values(token=token, key_authorization=key_authorization, order_id=order_id)
        .on_conflict_do_update(
            index_elements=["token"],
            set_={"key_authorization": key_authorization, "order_id": order_id},
        )
    )
    await db.execute(stmt)


async def lookup(db: AsyncSession, token: str) -> str | None:
    """Return the key-authorization for ``token`` (the well-known endpoint)."""
    return (
        await db.execute(
            select(ACMEHTTPChallenge.key_authorization).where(ACMEHTTPChallenge.token == token)
        )
    ).scalar_one_or_none()


async def cleanup(db: AsyncSession, order_id: uuid.UUID) -> int:
    """Delete every http-01 challenge row for an order. Returns the count."""
    result = await db.execute(
        delete(ACMEHTTPChallenge).where(ACMEHTTPChallenge.order_id == order_id)
    )
    return result.rowcount or 0


__all__ = ["cleanup", "lookup", "publish"]
