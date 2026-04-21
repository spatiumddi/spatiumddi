"""Write-through to Windows DHCP for scope / pool / static edits.

The SpatiumDDI scope / pool / static API endpoints call the helpers in
this module **after** the DB has been flushed but **before** commit,
so a WinRM failure surfaces as a 502 and rolls the transaction back —
the user sees the error instead of finding their DB and Windows have
drifted out of sync.

Under the group-centric model, scopes live on DHCPServerGroup and are
served by every member server. The helpers here find the **Windows
DHCP members** of the scope's group and push per-object changes to
each one. Kea members use the agent bundle path and are skipped here.

Why write-through per object (instead of a bundle push):

  * Windows DHCP has no "apply this whole config" entry point. Every
    change is a cmdlet against a specific scope / reservation.
  * The driver's ``apply_scope`` call resets the scope's option-values
    to exactly what our DB says. So one call covers add/update/remove
    for every option under that scope in a single round-trip.
  * Reservations and exclusions are per-object: we push one
    ``Add/Set/Remove-DhcpServerv4*`` per user action. That keeps the
    blast radius of any failure scoped to the object being edited.
"""

from __future__ import annotations

import ipaddress
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import structlog
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.drivers.dhcp import get_driver
from app.drivers.dhcp.base import RemoveReservationItem
from app.models.dhcp import DHCPPool, DHCPScope, DHCPServer, DHCPStaticAssignment
from app.models.ipam import Subnet

logger = structlog.get_logger(__name__)


class WindowsPushError(HTTPException):
    """502 — a Windows DHCP write-through failed; caller rolled back."""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=502, detail=f"Windows DHCP push failed: {detail}")


async def _windows_servers_for_group(db: AsyncSession, group_id: Any) -> list[DHCPServer]:
    """Return the Windows DHCP members of ``group_id`` (possibly empty)."""
    if group_id is None:
        return []
    res = await db.execute(
        select(DHCPServer).where(
            DHCPServer.server_group_id == group_id,
            DHCPServer.driver == "windows_dhcp",
        )
    )
    return list(res.scalars().all())


async def _scope_cidr(db: AsyncSession, scope: DHCPScope) -> ipaddress._BaseNetwork:
    """Resolve the scope's subnet to an ``ipaddress`` network."""
    subnet = await db.get(Subnet, scope.subnet_id)
    if subnet is None:
        raise WindowsPushError(f"Scope {scope.id}'s subnet is missing from IPAM")
    try:
        return ipaddress.ip_network(str(subnet.network), strict=False)
    except (ValueError, TypeError) as exc:
        raise WindowsPushError(f"Invalid subnet CIDR on scope {scope.id}: {exc}") from exc


async def _scope_range(
    db: AsyncSession, scope: DHCPScope, net: ipaddress._BaseNetwork
) -> tuple[str, str]:
    """Derive the Windows scope Start/End range."""
    pools_res = await db.execute(
        select(DHCPPool).where(
            DHCPPool.scope_id == scope.id,
            DHCPPool.pool_type == "dynamic",
        )
    )
    dynamic_pools = list(pools_res.scalars().all())
    if len(dynamic_pools) == 1:
        return str(dynamic_pools[0].start_ip), str(dynamic_pools[0].end_ip)
    if len(dynamic_pools) > 1:
        raise WindowsPushError(
            "Windows DHCP supports only one dynamic range per scope. "
            "Collapse the overlapping dynamic pools or use exclusion "
            "ranges (pool_type='excluded') to carve out sub-ranges."
        )
    hosts = list(net.hosts())
    if not hosts:
        raise WindowsPushError(f"Subnet {net} has no usable host range")
    return str(hosts[0]), str(hosts[-1])


async def push_scope_upsert(db: AsyncSession, scope: DHCPScope) -> None:
    """Push a scope create/update to every Windows DHCP member of the scope's group."""
    win_servers = await _windows_servers_for_group(db, scope.group_id)
    if not win_servers:
        return

    net = await _scope_cidr(db, scope)
    start, end = await _scope_range(db, scope, net)
    for server in win_servers:
        driver = get_driver(server.driver)
        try:
            await driver.apply_scope(  # type: ignore[attr-defined]
                server,
                scope_id=str(net.network_address),
                subnet_mask=str(net.netmask),
                start_range=start,
                end_range=end,
                name=scope.name or "",
                description=scope.description or "",
                lease_seconds=int(scope.lease_time or 86400),
                is_active=bool(scope.is_active),
                options=scope.options or {},
            )
        except Exception as exc:  # noqa: BLE001 — surface the error
            logger.warning(
                "windows_dhcp_push_scope_failed",
                scope=str(scope.id),
                server=str(server.id),
                error=str(exc),
            )
            raise WindowsPushError(str(exc)) from exc


async def push_scope_delete(db: AsyncSession, scope: DHCPScope) -> None:
    """Remove the scope from every Windows DHCP member of its group."""
    win_servers = await _windows_servers_for_group(db, scope.group_id)
    if not win_servers:
        return
    net = await _scope_cidr(db, scope)
    for server in win_servers:
        driver = get_driver(server.driver)
        try:
            await driver.remove_scope(server, str(net.network_address))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "windows_dhcp_push_scope_delete_failed",
                scope=str(scope.id),
                server=str(server.id),
                error=str(exc),
            )
            raise WindowsPushError(str(exc)) from exc


async def push_pool_change(
    db: AsyncSession,
    pool: DHCPPool,
    *,
    action: str,
    prev_start: str | None = None,
    prev_end: str | None = None,
) -> None:
    """Push a pool create/update/delete to every Windows member of the scope's group.

    * ``dynamic`` pool → scope StartRange/EndRange; push via apply_scope.
    * ``excluded`` pool → exclusion range; push via apply_exclusion /
      remove_exclusion.
    * ``reserved`` pool → Windows has no direct equivalent. Refuse.
    """
    scope = await db.get(DHCPScope, pool.scope_id)
    if scope is None:
        return
    win_servers = await _windows_servers_for_group(db, scope.group_id)
    if not win_servers:
        return

    if pool.pool_type == "reserved":
        raise WindowsPushError(
            "Windows DHCP has no equivalent for pool_type='reserved'. "
            "Use individual reservations (static assignments) instead."
        )

    net = await _scope_cidr(db, scope)
    scope_id = str(net.network_address)

    for server in win_servers:
        driver = get_driver(server.driver)
        try:
            if pool.pool_type == "dynamic":
                # Re-apply the whole scope — the Start/End range is a scope
                # property on Windows.
                continue  # handled below by a single push_scope_upsert call
            if action in {"create", "update"}:
                if action == "update" and prev_start and prev_end:
                    if prev_start != str(pool.start_ip) or prev_end != str(pool.end_ip):
                        await driver.remove_exclusion(  # type: ignore[attr-defined]
                            server,
                            scope_id=scope_id,
                            start_ip=prev_start,
                            end_ip=prev_end,
                        )
                await driver.apply_exclusion(  # type: ignore[attr-defined]
                    server,
                    scope_id=scope_id,
                    start_ip=str(pool.start_ip),
                    end_ip=str(pool.end_ip),
                )
            elif action == "delete":
                await driver.remove_exclusion(  # type: ignore[attr-defined]
                    server,
                    scope_id=scope_id,
                    start_ip=str(pool.start_ip),
                    end_ip=str(pool.end_ip),
                )
        except WindowsPushError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "windows_dhcp_push_pool_failed",
                pool=str(pool.id),
                scope=str(scope.id),
                action=action,
                error=str(exc),
            )
            raise WindowsPushError(str(exc)) from exc

    # Dynamic pools → re-push scope (fan-out handled inside push_scope_upsert).
    if pool.pool_type == "dynamic":
        await push_scope_upsert(db, scope)


async def push_static_change(
    db: AsyncSession,
    static: DHCPStaticAssignment,
    *,
    action: str,
    prev_mac: str | None = None,
) -> None:
    """Push a static assignment change to every Windows member of the scope's group."""
    scope = await db.get(DHCPScope, static.scope_id)
    if scope is None:
        return
    win_servers = await _windows_servers_for_group(db, scope.group_id)
    if not win_servers:
        return

    net = await _scope_cidr(db, scope)
    scope_id = str(net.network_address)

    for server in win_servers:
        driver = get_driver(server.driver)
        try:
            if action in {"create", "update"}:
                if action == "update" and prev_mac and prev_mac != str(static.mac_address):
                    await driver.remove_reservation(  # type: ignore[attr-defined]
                        server, scope_id=scope_id, mac_address=prev_mac
                    )
                await driver.apply_reservation(  # type: ignore[attr-defined]
                    server,
                    scope_id=scope_id,
                    ip_address=str(static.ip_address),
                    mac_address=str(static.mac_address),
                    hostname=static.hostname or "",
                    description=static.description or "",
                )
            elif action == "delete":
                await driver.remove_reservation(  # type: ignore[attr-defined]
                    server, scope_id=scope_id, mac_address=str(static.mac_address)
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "windows_dhcp_push_static_failed",
                static=str(static.id),
                scope=str(scope.id),
                server=str(server.id),
                action=action,
                error=str(exc),
            )
            raise WindowsPushError(str(exc)) from exc


async def push_statics_bulk_delete(
    db: AsyncSession, statics: Sequence[DHCPStaticAssignment]
) -> None:
    """Batch-delete many reservations on every Windows member of each scope's group."""
    if not statics:
        return

    scope_cache: dict[Any, DHCPScope] = {}
    # Group by (server, scope) across all Windows servers in each scope's group.
    grouped: dict[tuple[Any, Any], list[DHCPStaticAssignment]] = defaultdict(list)
    server_cache: dict[Any, DHCPServer] = {}

    for st in statics:
        scope = scope_cache.get(st.scope_id)
        if scope is None:
            scope = await db.get(DHCPScope, st.scope_id)
            if scope is None:
                continue
            scope_cache[st.scope_id] = scope
        for server in await _windows_servers_for_group(db, scope.group_id):
            server_cache[server.id] = server
            grouped[(server.id, scope.id)].append(st)

    for (server_id, scope_id), rows in grouped.items():
        server = server_cache[server_id]
        scope = scope_cache[scope_id]
        net = await _scope_cidr(db, scope)
        driver = get_driver(server.driver)
        items = [
            RemoveReservationItem(
                scope_id=str(net.network_address),
                mac_address=str(st.mac_address),
            )
            for st in rows
        ]
        try:
            results = await driver.remove_reservations(server, items=items)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — whole-batch failure
            logger.warning(
                "windows_dhcp_bulk_delete_batch_failed",
                server=str(server.id),
                scope=str(scope.id),
                count=len(items),
                error=str(exc),
            )
            raise WindowsPushError(str(exc)) from exc
        errors = [r.error for r in results if not r.ok and r.error]
        if errors:
            first = errors[0]
            more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
            raise WindowsPushError(f"{first}{more}")


__all__ = [
    "WindowsPushError",
    "push_pool_change",
    "push_scope_delete",
    "push_scope_upsert",
    "push_static_change",
    "push_statics_bulk_delete",
]
