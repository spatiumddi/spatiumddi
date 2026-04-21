"""Write-through to Windows DHCP for scope / pool / static edits.

The SpatiumDDI scope / pool / static API endpoints call the helpers in
this module **after** the DB has been flushed but **before** commit,
so a WinRM failure surfaces as a 502 and rolls the transaction back —
the user sees the error instead of finding their DB and Windows have
drifted out of sync.

Why write-through per object (instead of a bundle push):

  * Windows DHCP has no "apply this whole config" entry point. Every
    change is a cmdlet against a specific scope / reservation.
  * The driver's ``apply_scope`` call resets the scope's option-values
    to exactly what our DB says. So one call covers add/update/remove
    for every option under that scope in a single round-trip.
  * Reservations and exclusions are per-object: we push one
    ``Add/Set/Remove-DhcpServerv4*`` per user action. That keeps the
    blast radius of any failure scoped to the object being edited.

No-op for non-windows_dhcp servers — Kea uses the agent bundle path
and never hits these helpers.
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


async def _is_windows(server: DHCPServer) -> bool:
    return server.driver == "windows_dhcp"


async def _load_server(db: AsyncSession, server_id: Any) -> DHCPServer | None:
    return await db.get(DHCPServer, server_id)


async def _scope_cidr(db: AsyncSession, scope: DHCPScope) -> ipaddress._BaseNetwork:
    """Resolve the scope's subnet to an ``ipaddress`` network. Used for
    the ``ScopeId`` / ``SubnetMask`` cmdlet args.
    """
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
    """Derive the Windows scope Start/End range. If the scope has exactly
    one dynamic pool in our DB, use that; otherwise default to the full
    host range of the subnet (network + broadcast excluded).
    """
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
        # Windows DHCP scopes have exactly one StartRange/EndRange pair;
        # multiple dynamic pools have no faithful projection. Refuse
        # rather than silently picking one.
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
    """After a scope create/update in our DB, push the full scope state
    to Windows DHCP. Called from ``create_scope`` / ``update_scope``
    endpoints right before commit."""
    server = await _load_server(db, scope.server_id)
    if server is None or not await _is_windows(server):
        return

    net = await _scope_cidr(db, scope)
    start, end = await _scope_range(db, scope, net)
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
    """After an API ``delete_scope``, remove the scope from Windows."""
    server = await _load_server(db, scope.server_id)
    if server is None or not await _is_windows(server):
        return
    net = await _scope_cidr(db, scope)
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
    """After a pool create/update/delete, translate to Windows semantics.

    * ``dynamic`` pool → scope StartRange/EndRange; push via apply_scope.
    * ``excluded`` pool → exclusion range; push via apply_exclusion /
      remove_exclusion.
    * ``reserved`` pool → Windows has no direct equivalent (reservations
      are per-IP, not per-range). Refuse.

    ``prev_start`` / ``prev_end`` must be supplied on update when the
    range changed, so the old exclusion can be removed before the new
    one is added.
    """
    scope = await db.get(DHCPScope, pool.scope_id)
    if scope is None:
        return
    server = await _load_server(db, scope.server_id)
    if server is None or not await _is_windows(server):
        return

    if pool.pool_type == "reserved":
        raise WindowsPushError(
            "Windows DHCP has no equivalent for pool_type='reserved'. "
            "Use individual reservations (static assignments) instead."
        )

    net = await _scope_cidr(db, scope)
    driver = get_driver(server.driver)
    scope_id = str(net.network_address)

    try:
        if pool.pool_type == "dynamic":
            # Re-apply the whole scope — the Start/End range is a scope
            # property on Windows, so an edit to the dynamic pool
            # translates to a Set-DhcpServerv4Scope call via apply_scope.
            await push_scope_upsert(db, scope)
            return

        # excluded
        if action in {"create", "update"}:
            if action == "update" and prev_start and prev_end:
                # Range changed in place — drop the old exclusion first.
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


async def push_static_change(
    db: AsyncSession,
    static: DHCPStaticAssignment,
    *,
    action: str,
    prev_mac: str | None = None,
) -> None:
    """After a static assignment create/update/delete, push to Windows.

    ``prev_mac`` must be passed on update when the MAC changed so the
    old ClientId-keyed reservation can be removed before the new one
    is added.
    """
    scope = await db.get(DHCPScope, static.scope_id)
    if scope is None:
        return
    server = await _load_server(db, scope.server_id)
    if server is None or not await _is_windows(server):
        return

    net = await _scope_cidr(db, scope)
    driver = get_driver(server.driver)
    scope_id = str(net.network_address)

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
            action=action,
            error=str(exc),
        )
        raise WindowsPushError(str(exc)) from exc


async def push_statics_bulk_delete(
    db: AsyncSession, statics: Sequence[DHCPStaticAssignment]
) -> None:
    """Batch-delete many reservations on Windows DHCP in one round-trip per server.

    Groups ``statics`` by ``(server, scope)`` and calls the driver's
    plural ``remove_reservations`` once per group. Non-Windows servers
    fall through to the ABC's sequential default which loops over the
    singular ``remove_reservation`` — so Kea / ISC / any future driver
    handle this call identically to the loop-of-singular pattern they
    already support.

    Per-op failures on Windows surface as one ``WindowsPushError`` with
    all per-op errors concatenated — the caller rolls back the whole DB
    transaction rather than letting partial success slip through (same
    contract as ``push_static_change``).
    """
    if not statics:
        return

    # Group by (server, scope) so each driver call is against one server.
    # Keyed on IDs; collect the actual objects on the side so we can
    # resolve the CIDR once per scope.
    grouped: dict[tuple[Any, Any], list[DHCPStaticAssignment]] = defaultdict(list)
    scope_cache: dict[Any, DHCPScope] = {}
    server_cache: dict[Any, DHCPServer] = {}

    for st in statics:
        scope = scope_cache.get(st.scope_id)
        if scope is None:
            scope = await db.get(DHCPScope, st.scope_id)
            if scope is None:
                continue
            scope_cache[st.scope_id] = scope
        server = server_cache.get(scope.server_id)
        if server is None:
            server = await _load_server(db, scope.server_id)
            if server is None:
                continue
            server_cache[scope.server_id] = server
        if not await _is_windows(server):
            continue
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
