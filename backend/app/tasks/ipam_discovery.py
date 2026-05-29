"""IP discovery Celery tasks (issue #23).

Three tasks:

* ``run_subnet_discovery`` — single-subnet pass: sweeps the subnet
  (ping + ARP), folds live hosts into IPAM via
  ``services.ipam.discovery.reconcile_subnet``, stamps
  ``Subnet.last_discovery_at``, and writes an audit row with the
  per-bucket counters.
* ``dispatch_due_subnets`` — beat-fired every 60 s. Queues a
  ``run_subnet_discovery`` per subnet whose ``discovery_enabled`` is
  set and whose ``last_discovery_at`` is older than its per-subnet
  ``discovery_interval_minutes`` (gating in the task, not in beat, so
  interval changes in the UI take effect without restarting beat —
  same pattern as the SNMP poller and the IPAM↔DNS auto-sync).
* No janitor here — discovered rows are reclaimed by the stale-IP
  sweep (#45) / the existing trash + lease-cleanup paths.

Idempotent + safe to retry: a re-run over the same wire data only
refreshes ``last_seen_at``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.celery_app import celery_app
from app.config import settings
from app.models.audit import AuditLog
from app.models.ipam import Subnet
from app.services.ipam.discovery import reconcile_subnet, sweep_subnet

logger = structlog.get_logger(__name__)


async def _run_subnet_discovery_async(subnet_id_str: str) -> dict[str, Any]:
    engine = create_async_engine(settings.database_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            subnet = await db.get(Subnet, subnet_id_str)
            if subnet is None or subnet.deleted_at is not None:
                return {"status": "skipped", "reason": "subnet_missing"}

            sweep = await sweep_subnet(str(subnet.network))
            if sweep is None:
                # IPv6 or larger than MAX_SWEEP_HOSTS — stamp the run so
                # the dispatcher doesn't re-pick it every tick, but do no
                # writes.
                subnet.last_discovery_at = datetime.now(UTC)
                await db.commit()
                logger.info(
                    "ipam.discovery.skipped_unsweepable",
                    subnet_id=subnet_id_str,
                    network=str(subnet.network),
                )
                return {"status": "skipped", "reason": "unsweepable"}

            counts = await reconcile_subnet(db, subnet, sweep)
            subnet.last_discovery_at = datetime.now(UTC)
            db.add(
                AuditLog(
                    user_id=None,
                    user_display_name="system",
                    auth_source="system",
                    action="discover",
                    resource_type="subnet",
                    resource_id=str(subnet.id),
                    resource_display=str(subnet.network),
                    result="success",
                    new_value={
                        "alive": len(sweep.alive),
                        "method": "icmp" if sweep.icmp_used else "tcp",
                        **counts,
                    },
                )
            )
            await db.commit()
            logger.info(
                "ipam.discovery.subnet_done",
                subnet_id=subnet_id_str,
                network=str(subnet.network),
                alive=len(sweep.alive),
                **counts,
            )
            return {"status": "ok", "alive": len(sweep.alive), **counts}
    finally:
        await engine.dispose()


@celery_app.task(
    name="app.tasks.ipam_discovery.run_subnet_discovery",
    bind=True,
    autoretry_for=(ConnectionError, OSError),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def run_subnet_discovery(self: Any, subnet_id_str: str) -> dict[str, Any]:  # noqa: ARG001
    """Sweep one subnet + reconcile. Invoked on-demand and by the beat
    dispatcher."""
    return asyncio.run(_run_subnet_discovery_async(subnet_id_str))


async def _dispatch_due_async() -> int:
    engine = create_async_engine(settings.database_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    queued = 0
    try:
        async with factory() as db:
            now = datetime.now(UTC)
            rows = list(
                (
                    await db.execute(
                        select(Subnet).where(
                            Subnet.discovery_enabled.is_(True),
                            Subnet.deleted_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for s in rows:
                interval = max(5, s.discovery_interval_minutes)
                due = s.last_discovery_at is None or s.last_discovery_at <= now - timedelta(
                    minutes=interval
                )
                if not due:
                    continue
                try:
                    run_subnet_discovery.delay(str(s.id))
                    queued += 1
                except Exception as exc:  # noqa: BLE001 — broker down? give up quietly
                    logger.warning("ipam.discovery.dispatch_enqueue_failed", error=str(exc))
                    break
        return queued
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.ipam_discovery.dispatch_due_subnets", bind=True)
def dispatch_due_subnets(self: Any) -> int:  # noqa: ARG001
    """Beat-fired sweep — queues ``run_subnet_discovery`` per due subnet."""
    return asyncio.run(_dispatch_due_async())


__all__ = ["run_subnet_discovery", "dispatch_due_subnets"]
