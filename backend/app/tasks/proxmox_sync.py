"""Periodic Proxmox VE endpoint reconcile.

Mirrors ``docker_sync`` — 30 s beat tick, per-endpoint interval
gating, platform-wide kill switch via
``PlatformSettings.integration_proxmox_enabled``. Plus a
``sync_node_now`` task for the UI's "Sync Now" button.
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
from app.models.proxmox import ProxmoxNode
from app.models.settings import PlatformSettings

logger = structlog.get_logger(__name__)

_PLATFORM_SINGLETON_ID = 1


async def _run_sweep() -> dict[str, Any]:
    from app.services.proxmox.reconcile import reconcile_node  # noqa: PLC0415

    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            ps = await db.get(PlatformSettings, _PLATFORM_SINGLETON_ID)
            if ps is None or not ps.integration_proxmox_enabled:
                return {"status": "disabled"}

            rows = (
                (await db.execute(select(ProxmoxNode).where(ProxmoxNode.enabled.is_(True))))
                .scalars()
                .all()
            )

            now = datetime.now(UTC)
            ran = 0
            skipped_interval = 0
            ok_count = 0
            err_count = 0
            errors: list[str] = []

            for node in rows:
                if node.last_synced_at is not None:
                    elapsed = now - node.last_synced_at
                    if elapsed < timedelta(seconds=node.sync_interval_seconds):
                        skipped_interval += 1
                        continue
                try:
                    summary = await reconcile_node(db, node)
                except Exception as exc:  # noqa: BLE001 — one node shouldn't poison the sweep
                    err_count += 1
                    errors.append(f"{node.name}: {exc}")
                    logger.warning(
                        "proxmox_reconcile_crash",
                        node=str(node.id),
                        error=str(exc),
                    )
                    continue
                ran += 1
                if summary.ok:
                    ok_count += 1
                else:
                    err_count += 1
                    if summary.error:
                        errors.append(f"{node.name}: {summary.error}")

            return {
                "status": "ok",
                "ran": ran,
                "skipped_interval": skipped_interval,
                "ok": ok_count,
                "errors": err_count,
                "error_messages": errors[:20],
            }
    finally:
        await engine.dispose()


async def _run_one(node_id: str) -> dict[str, Any]:
    import uuid as _uuid  # noqa: PLC0415

    from app.services.proxmox.reconcile import reconcile_node  # noqa: PLC0415

    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            node = await db.get(ProxmoxNode, _uuid.UUID(node_id))
            if node is None:
                return {"status": "not_found"}
            summary = await reconcile_node(db, node)
            return {
                "status": "ok" if summary.ok else "error",
                "error": summary.error,
                "pve_version": summary.pve_version,
                "cluster_name": summary.cluster_name,
                "node_count": summary.node_count,
                "vm_count": summary.vm_count,
                "lxc_count": summary.lxc_count,
                "blocks_created": summary.blocks_created,
                "blocks_deleted": summary.blocks_deleted,
                "subnets_created": summary.subnets_created,
                "subnets_updated": summary.subnets_updated,
                "subnets_deleted": summary.subnets_deleted,
                "subnets_matched": summary.subnets_matched,
                "addresses_created": summary.addresses_created,
                "addresses_updated": summary.addresses_updated,
                "addresses_deleted": summary.addresses_deleted,
                "skipped_no_subnet": summary.skipped_no_subnet,
            }
    finally:
        await engine.dispose()


@celery_app.task(
    name="app.tasks.proxmox_sync.sweep_proxmox_nodes",
    bind=True,
)
def sweep_proxmox_nodes(self: Any) -> dict[str, Any]:  # noqa: ARG001
    return asyncio.run(_run_sweep())


@celery_app.task(
    name="app.tasks.proxmox_sync.sync_node_now",
    bind=True,
)
def sync_node_now(self: Any, node_id: str) -> dict[str, Any]:  # noqa: ARG001
    return asyncio.run(_run_one(node_id))


__all__ = ["sweep_proxmox_nodes", "sync_node_now"]
