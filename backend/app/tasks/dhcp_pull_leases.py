"""Periodic "DHCP lease pull" task.

Fired every 60 seconds by Celery Beat. Gates on
``PlatformSettings.dhcp_pull_leases_enabled`` + its interval, so the
beat schedule stays static while the UI can change cadence live.

Each run iterates every ``DHCPServer`` whose driver is registered as
agentless (today: ``windows_dhcp``) and calls
``pull_leases_from_server`` to:

  1. Poll the server for active leases (driver-specific — WinRM +
     ``Get-DhcpServerv4Lease`` for windows_dhcp).
  2. Upsert ``DHCPLease`` rows keyed by ``(server_id, ip_address)``.
  3. Mirror each lease into IPAM (``IPAddress`` with ``status="dhcp"``
     and ``auto_from_lease=True``) when the lease IP falls within a
     known subnet.

Never deletes. Expired leases are cleaned up by the existing
``dhcp_lease_cleanup`` sweep (state=active + expires_at past grace →
expired + auto_from_lease IPAM row removed). That keeps the two-way
contract with the control plane:

  * lease appears on wire  → here mirrors it into DB + IPAM
  * lease drops off wire   → stays "active" until its TTL passes, then
                              the sweep handles cleanup uniformly

Idempotent: re-running is a no-op whenever DB and wire already agree.
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
from app.drivers.dhcp import is_agentless
from app.models.audit import AuditLog
from app.models.dhcp import DHCPServer
from app.models.settings import PlatformSettings

logger = structlog.get_logger(__name__)

_SINGLETON_ID = 1


async def _run_pull() -> dict[str, Any]:
    # Deferred import — keeps celery-worker startup light and avoids pulling
    # in the API router graph just to register the task.
    from app.services.dhcp.pull_leases import (  # noqa: PLC0415
        pull_leases_from_server,
    )

    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as db:
            ps = await db.get(PlatformSettings, _SINGLETON_ID)
            if ps is None or not ps.dhcp_pull_leases_enabled:
                return {"status": "disabled"}

            now = datetime.now(UTC)
            interval = timedelta(minutes=max(1, ps.dhcp_pull_leases_interval_minutes))
            if ps.dhcp_pull_leases_last_run_at is not None:
                elapsed = now - ps.dhcp_pull_leases_last_run_at
                if elapsed < interval:
                    return {
                        "status": "skipped",
                        "reason": "interval_not_elapsed",
                        "wait_seconds": int((interval - elapsed).total_seconds()),
                    }

            servers = list((await db.execute(select(DHCPServer))).scalars().all())

            servers_scanned = 0
            total_server_leases = 0
            total_imported = 0
            total_refreshed = 0
            total_removed = 0
            total_ipam_created = 0
            total_ipam_refreshed = 0
            total_ipam_revoked = 0
            total_out_of_scope = 0
            total_scopes_imported = 0
            total_scopes_refreshed = 0
            total_scopes_skipped = 0
            total_pools_synced = 0
            total_statics_synced = 0
            errors: list[str] = []

            for server in servers:
                if not is_agentless(server.driver):
                    continue

                servers_scanned += 1
                try:
                    result = await pull_leases_from_server(db, server, apply=True)
                except Exception as exc:  # noqa: BLE001 — don't let one server poison the run
                    errors.append(f"{server.name}: {exc}")
                    logger.warning(
                        "dhcp_pull_leases_server_failed",
                        server=str(server.id),
                        driver=server.driver,
                        error=str(exc),
                    )
                    continue

                total_server_leases += result.server_leases
                total_imported += result.imported
                total_refreshed += result.refreshed
                total_removed += result.removed
                total_ipam_created += result.ipam_created
                total_ipam_refreshed += result.ipam_refreshed
                total_ipam_revoked += result.ipam_revoked
                total_out_of_scope += result.out_of_scope
                total_scopes_imported += result.scopes_imported
                total_scopes_refreshed += result.scopes_refreshed
                total_scopes_skipped += result.scopes_skipped_no_subnet
                total_pools_synced += result.pools_synced
                total_statics_synced += result.statics_synced
                errors.extend(f"{server.name}: {e}" for e in result.errors)

            ps.dhcp_pull_leases_last_run_at = now

            if (
                total_imported
                or total_refreshed
                or total_removed
                or total_ipam_created
                or total_ipam_revoked
                or errors
            ):
                db.add(
                    AuditLog(
                        user_display_name="<system>",
                        auth_source="system",
                        action="dhcp-lease-pull",
                        resource_type="platform",
                        resource_id=str(_SINGLETON_ID),
                        resource_display="auto-pull",
                        result="error" if errors else "success",
                        new_value={
                            "servers_scanned": servers_scanned,
                            "server_leases": total_server_leases,
                            "imported": total_imported,
                            "refreshed": total_refreshed,
                            "removed": total_removed,
                            "ipam_created": total_ipam_created,
                            "ipam_refreshed": total_ipam_refreshed,
                            "ipam_revoked": total_ipam_revoked,
                            "out_of_scope": total_out_of_scope,
                            "scopes_imported": total_scopes_imported,
                            "scopes_refreshed": total_scopes_refreshed,
                            "scopes_skipped_no_subnet": total_scopes_skipped,
                            "pools_synced": total_pools_synced,
                            "statics_synced": total_statics_synced,
                            "errors": errors[:20],
                        },
                    )
                )
            await db.commit()

            logger.info(
                "dhcp_pull_leases_completed",
                servers_scanned=servers_scanned,
                server_leases=total_server_leases,
                imported=total_imported,
                refreshed=total_refreshed,
                removed=total_removed,
                ipam_created=total_ipam_created,
                ipam_refreshed=total_ipam_refreshed,
                ipam_revoked=total_ipam_revoked,
                out_of_scope=total_out_of_scope,
                scopes_imported=total_scopes_imported,
                scopes_refreshed=total_scopes_refreshed,
                scopes_skipped_no_subnet=total_scopes_skipped,
                pools_synced=total_pools_synced,
                statics_synced=total_statics_synced,
                error_count=len(errors),
            )
            return {
                "status": "ran",
                "servers_scanned": servers_scanned,
                "server_leases": total_server_leases,
                "imported": total_imported,
                "refreshed": total_refreshed,
                "removed": total_removed,
                "ipam_created": total_ipam_created,
                "ipam_refreshed": total_ipam_refreshed,
                "ipam_revoked": total_ipam_revoked,
                "out_of_scope": total_out_of_scope,
                "scopes_imported": total_scopes_imported,
                "scopes_refreshed": total_scopes_refreshed,
                "scopes_skipped_no_subnet": total_scopes_skipped,
                "pools_synced": total_pools_synced,
                "statics_synced": total_statics_synced,
                "errors": len(errors),
            }
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.dhcp_pull_leases.auto_pull_dhcp_leases", bind=True)
def auto_pull_dhcp_leases(self: object) -> dict[str, Any]:  # type: ignore[type-arg]
    """Celery beat entrypoint — fires every 60s; the task itself checks the
    platform-settings gate and the per-run interval."""
    try:
        return asyncio.run(_run_pull())
    except Exception as exc:  # noqa: BLE001
        logger.exception("dhcp_pull_leases_failed", error=str(exc))
        raise
