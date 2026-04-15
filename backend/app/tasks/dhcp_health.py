"""DHCP server health check Celery tasks. Mirrors app.tasks.dns.check_dns_server_health."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from app.celery_app import celery_app
from app.db import AsyncSessionLocal
from app.models.dhcp import DHCPServer

# If agent hasn't heartbeat'd in this long, mark unreachable.
AGENT_STALE_AFTER = timedelta(seconds=120)

logger = structlog.get_logger(__name__)


async def _check_health(server_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        server = await db.get(DHCPServer, server_id)
        if server is None:
            return
        now = datetime.now(UTC)
        last_seen = server.agent_last_seen
        if last_seen is not None and (now - last_seen) <= AGENT_STALE_AFTER:
            server.status = "active"
        else:
            # No agent heartbeat — mark unreachable. (Driver-level health probe
            # could be added here for non-agent deployments.)
            server.status = "unreachable"
        server.last_health_check_at = now
        await db.commit()
        logger.info(
            "dhcp_health_checked",
            server_id=str(server_id),
            status=server.status,
            host=server.host,
        )


@celery_app.task(
    name="app.tasks.dhcp_health.check_dhcp_server_health",
    bind=True,
    max_retries=3,
    acks_late=True,
)
def check_dhcp_server_health(self: object, server_id: str) -> None:  # type: ignore[type-arg]
    """Idempotent single-server health check."""
    try:
        asyncio.run(_check_health(uuid.UUID(server_id)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("dhcp_health_check_error", server_id=server_id, error=str(exc))
        raise self.retry(exc=exc, countdown=30) from exc  # type: ignore[attr-defined]


async def _enqueue_all() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(DHCPServer.id))
        ids = [str(row[0]) for row in result.all()]
    for sid in ids:
        check_dhcp_server_health.delay(sid)


@celery_app.task(name="app.tasks.dhcp_health.check_all_dhcp_servers_health", bind=True)
def check_all_dhcp_servers_health(self: object) -> None:  # type: ignore[type-arg]
    """Fan-out task — enqueues one health check per registered DHCP server.

    Scheduled every 60s by Celery beat (see app.celery_app).
    """
    asyncio.run(_enqueue_all())
