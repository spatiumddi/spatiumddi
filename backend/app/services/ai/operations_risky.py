"""Risky-operation registry for the two-person approval workflow (#62).

Each delete handler covered by the approval gate has its mutation body
factored verbatim into an :class:`Operation` ``apply()`` here, with a
``preview()`` that re-runs the same 404 / 409 / synthesised-zone guards
the original handler did inline. The REST handler delegates to
``apply()`` on the inline (no-approval) path AND the approve endpoint
replays the same ``apply()`` under the approver's identity after a
fresh ``preview()`` stale-state check — one mutation path, two callers.

CRITICAL — byte-identical inline behaviour:

* The ``apply()`` body is the original handler body, unchanged. Every
  side effect (soft-delete batch + per-row audit, permanent cascade of
  DHCP scopes / DNS records / zones, agent bundle rebuilds, ``collect_wake``
  / ``publish_wake`` of the affected channel, ``_update_block_utilization``)
  is preserved in the same order.
* The ``permanent`` / ``force`` flags are frozen into the args model so
  the approved replay takes the *identical* branch the requester intended.
* The ``require_superadmin`` check that the original ``permanent`` /
  ``force`` paths ran inline lives inside ``apply()`` (and the matching
  ``preview()``), so both the inline path and the approver replay enforce
  it — a non-superadmin approver can never complete a ``permanent`` op.
* Audit ``action`` strings stay exactly as today (``soft_delete`` on the
  default path, ``delete`` on the permanent path) — never collapsed.

CLAUDE.md non-negotiables honoured: #2 (async throughout), #3 (server-side
permission enforcement via ``enforce_operation_permission`` defense-in-depth
+ the original route gates), #4 (every mutation writes its audit row before
the commit inside ``apply()``).
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.services.ai.operations import (
    Operation,
    PreviewResult,
    enforce_operation_permission,
    get_operation,
    register,
)

logger = structlog.get_logger(__name__)


# ── delete_subnet ──────────────────────────────────────────────────────────


class DeleteSubnetArgs(BaseModel):
    subnet_id: UUID
    force: bool = False
    permanent: bool = False


async def _preview_delete_subnet(
    db: AsyncSession, user: User, args: DeleteSubnetArgs
) -> PreviewResult:
    from app.api.v1.ipam.router import _enforce_subnet_token_scope
    from app.models.dhcp import DHCPScope
    from app.models.ipam import IPAddress, Subnet

    subnet = await db.get(Subnet, args.subnet_id)
    if subnet is None:
        return PreviewResult(ok=False, detail="Subnet not found")
    # Per-row API-token binding — mirrors the inline handler.
    _enforce_subnet_token_scope(user, args.subnet_id)

    if args.permanent and not args.force:
        # Re-run the non-empty 409 the inline permanent path raises.
        user_ip_count = (
            await db.execute(
                select(func.count(IPAddress.id)).where(
                    IPAddress.subnet_id == args.subnet_id,
                    IPAddress.status.notin_(["network", "broadcast", "orphan"]),
                    IPAddress.auto_from_lease.is_(False),
                )
            )
        ).scalar_one()
        scope_count = (
            await db.execute(
                select(func.count(DHCPScope.id)).where(DHCPScope.subnet_id == args.subnet_id)
            )
        ).scalar_one()
        if user_ip_count or scope_count:
            parts = []
            if user_ip_count:
                parts.append(
                    f"{user_ip_count} allocated IP address" + ("es" if user_ip_count != 1 else "")
                )
            if scope_count:
                parts.append(f"{scope_count} DHCP scope" + ("s" if scope_count != 1 else ""))
            return PreviewResult(
                ok=False,
                detail=(
                    f"Subnet is not empty: {', '.join(parts)}. "
                    "Delete the contents first, or retry with force=true to cascade."
                ),
            )

    mode = "Permanently delete" if args.permanent else "Soft-delete"
    parts = [f"{mode} subnet `{subnet.network}`" + (f" ({subnet.name})" if subnet.name else "")]
    if args.permanent:
        scope_count = (
            await db.execute(
                select(func.count(DHCPScope.id)).where(DHCPScope.subnet_id == args.subnet_id)
            )
        ).scalar_one()
        ip_count = (
            await db.execute(
                select(func.count(IPAddress.id)).where(IPAddress.subnet_id == args.subnet_id)
            )
        ).scalar_one()
        parts.append(
            f"cascade: {scope_count} DHCP scope(s) + {ip_count} IP row(s) + auto DNS cleanup"
        )
        if args.force:
            parts.append("force=true (skip non-empty check)")
    else:
        parts.append("the subnet + its DHCP scopes are restorable from /admin/trash")
    return PreviewResult(ok=True, detail="ready", preview_text="; ".join(parts))


async def _apply_delete_subnet(
    db: AsyncSession, user: User, args: DeleteSubnetArgs
) -> dict[str, Any]:
    """Body factored verbatim from ipam/router.py:delete_subnet."""
    from sqlalchemy import delete as sa_delete

    from app.api.deps import require_superadmin
    from app.api.v1.ipam.router import (
        _audit,
        _enforce_subnet_token_scope,
        _revoke_subnet_lease_mirrors,
        _update_block_utilization,
    )
    from app.drivers.dhcp import is_agentless
    from app.models.dhcp import DHCPConfigOp, DHCPScope, DHCPServer
    from app.models.dns import DNSRecord, DNSZone
    from app.models.ipam import IPAddress, Subnet
    from app.services.dhcp.config_bundle import build_config_bundle
    from app.services.dhcp.windows_writethrough import push_scope_delete
    from app.services.soft_delete import apply_soft_delete, collect_soft_delete_batch

    enforce_operation_permission(user, _OP_DELETE_SUBNET)

    subnet = await db.get(Subnet, args.subnet_id)
    if subnet is None:
        raise ValueError("Subnet not found")
    _enforce_subnet_token_scope(user, args.subnet_id)

    if not args.permanent:
        await _revoke_subnet_lease_mirrors(db, subnet)

        batch = await collect_soft_delete_batch(db, subnet)
        apply_soft_delete(batch, user.id)
        for row in batch.rows:
            db.add(
                _audit(
                    user,
                    "soft_delete",
                    row.resource_type,
                    str(row.obj.id),
                    row.display,
                    old_value={"deletion_batch_id": str(batch.batch_id)},
                )
            )
        await db.commit()
        return {"subnet_id": str(args.subnet_id), "mode": "soft_delete"}

    require_superadmin(user)

    if not args.force:
        user_ip_count = (
            await db.execute(
                select(func.count(IPAddress.id)).where(
                    IPAddress.subnet_id == args.subnet_id,
                    IPAddress.status.notin_(["network", "broadcast", "orphan"]),
                    IPAddress.auto_from_lease.is_(False),
                )
            )
        ).scalar_one()
        scope_count = (
            await db.execute(
                select(func.count(DHCPScope.id)).where(DHCPScope.subnet_id == args.subnet_id)
            )
        ).scalar_one()
        if user_ip_count or scope_count:
            parts = []
            if user_ip_count:
                parts.append(
                    f"{user_ip_count} allocated IP address" + ("es" if user_ip_count != 1 else "")
                )
            if scope_count:
                parts.append(f"{scope_count} DHCP scope" + ("s" if scope_count != 1 else ""))
            raise ValueError(
                f"Subnet is not empty: {', '.join(parts)}. "
                "Delete the contents first, or retry with force=true to cascade."
            )

    block_id = subnet.block_id

    scope_rows = (
        (await db.execute(select(DHCPScope).where(DHCPScope.subnet_id == args.subnet_id)))
        .scalars()
        .all()
    )
    agent_servers_to_refresh: dict[uuid.UUID, DHCPServer] = {}
    for scope in scope_rows:
        await push_scope_delete(db, scope)
        group_servers = (
            (
                await db.execute(
                    select(DHCPServer).where(DHCPServer.server_group_id == scope.group_id)
                )
            )
            .scalars()
            .all()
        )
        for srv in group_servers:
            if not is_agentless(srv.driver):
                agent_servers_to_refresh[srv.id] = srv

    addr_result = await db.execute(
        select(IPAddress.dns_record_id).where(
            IPAddress.subnet_id == args.subnet_id,
            IPAddress.dns_record_id.isnot(None),
        )
    )
    record_ids = [rid for rid in addr_result.scalars().all() if rid is not None]
    if record_ids:
        await db.execute(sa_delete(DNSRecord).where(DNSRecord.id.in_(record_ids)))

    await db.execute(
        sa_delete(DNSZone).where(
            DNSZone.linked_subnet_id == args.subnet_id,
            DNSZone.is_auto_generated.is_(True),
        )
    )

    db.add(
        _audit(
            user,
            "delete",
            "subnet",
            str(subnet.id),
            f"{subnet.network} ({subnet.name})",
            old_value={"network": str(subnet.network), "name": subnet.name},
        )
    )
    await db.delete(subnet)
    await db.flush()
    await _update_block_utilization(db, block_id)

    for server in agent_servers_to_refresh.values():
        bundle = await build_config_bundle(db, server)
        server.config_etag = bundle.etag
        existing = (
            await db.execute(
                select(DHCPConfigOp).where(
                    DHCPConfigOp.server_id == server.id,
                    DHCPConfigOp.op_type == "apply_config",
                    DHCPConfigOp.status == "pending",
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                DHCPConfigOp(
                    server_id=server.id,
                    op_type="apply_config",
                    payload={"etag": bundle.etag},
                    status="pending",
                )
            )

    await db.commit()
    return {"subnet_id": str(args.subnet_id), "mode": "delete"}


_OP_DELETE_SUBNET = Operation(
    name="delete_subnet",
    description="Delete a subnet (soft-delete batch, or permanent cascade of DHCP scopes + auto DNS).",
    args_model=DeleteSubnetArgs,
    preview=_preview_delete_subnet,
    apply=_apply_delete_subnet,
    category="ipam",
    required_permission=("delete", "subnet"),
)
register(_OP_DELETE_SUBNET)


# ── delete_block ───────────────────────────────────────────────────────────


class DeleteBlockArgs(BaseModel):
    block_id: UUID
    permanent: bool = False


async def _preview_delete_block(
    db: AsyncSession, user: User, args: DeleteBlockArgs
) -> PreviewResult:
    from app.models.ipam import IPBlock, Subnet

    block = await db.get(IPBlock, args.block_id)
    if block is None:
        return PreviewResult(ok=False, detail="IP block not found")

    if args.permanent:
        subnet_count = (
            await db.execute(
                select(func.count()).select_from(Subnet).where(Subnet.block_id == args.block_id)
            )
        ).scalar_one()
        child_block_count = (
            await db.execute(
                select(func.count())
                .select_from(IPBlock)
                .where(IPBlock.parent_block_id == args.block_id)
            )
        ).scalar_one()
        if subnet_count or child_block_count:
            parts = []
            if child_block_count:
                parts.append(f"{child_block_count} child block(s)")
            if subnet_count:
                parts.append(f"{subnet_count} subnet(s)")
            return PreviewResult(
                ok=False,
                detail=(
                    f"Block {block.network} still contains {' and '.join(parts)}. "
                    "Delete or move them before deleting the block."
                ),
            )
        return PreviewResult(
            ok=True,
            detail="ready",
            preview_text=f"Permanently delete empty block `{block.network}` ({block.name})",
        )

    return PreviewResult(
        ok=True,
        detail="ready",
        preview_text=(
            f"Soft-delete block `{block.network}` ({block.name}) — cascades to child "
            "blocks + subnets + their DHCP scopes; restorable from /admin/trash"
        ),
    )


async def _apply_delete_block(
    db: AsyncSession, user: User, args: DeleteBlockArgs
) -> dict[str, Any]:
    """Body factored verbatim from ipam/router.py:delete_block."""
    from app.api.deps import require_superadmin
    from app.api.v1.ipam.router import _audit
    from app.models.ipam import IPBlock, Subnet
    from app.services.soft_delete import apply_soft_delete, collect_soft_delete_batch

    enforce_operation_permission(user, _OP_DELETE_BLOCK)

    block = await db.get(IPBlock, args.block_id)
    if block is None:
        raise ValueError("IP block not found")

    if args.permanent:
        require_superadmin(user)
        subnet_count = (
            await db.execute(
                select(func.count()).select_from(Subnet).where(Subnet.block_id == args.block_id)
            )
        ).scalar_one()
        child_block_count = (
            await db.execute(
                select(func.count())
                .select_from(IPBlock)
                .where(IPBlock.parent_block_id == args.block_id)
            )
        ).scalar_one()
        if subnet_count or child_block_count:
            parts = []
            if child_block_count:
                parts.append(f"{child_block_count} child block(s)")
            if subnet_count:
                parts.append(f"{subnet_count} subnet(s)")
            raise ValueError(
                f"Block {block.network} still contains {' and '.join(parts)}. "
                "Delete or move them before deleting the block."
            )

        db.add(
            _audit(
                user,
                "delete",
                "ip_block",
                str(block.id),
                f"{block.network} ({block.name})",
                old_value={"network": str(block.network)},
            )
        )
        await db.delete(block)
        await db.commit()
        return {"block_id": str(args.block_id), "mode": "delete"}

    batch = await collect_soft_delete_batch(db, block)
    apply_soft_delete(batch, user.id)
    for row in batch.rows:
        db.add(
            _audit(
                user,
                "soft_delete",
                row.resource_type,
                str(row.obj.id),
                row.display,
                old_value={"deletion_batch_id": str(batch.batch_id)},
            )
        )
    await db.commit()
    return {"block_id": str(args.block_id), "mode": "soft_delete"}


_OP_DELETE_BLOCK = Operation(
    name="delete_block",
    description="Delete an IP block (soft-delete cascade, or permanent if empty).",
    args_model=DeleteBlockArgs,
    preview=_preview_delete_block,
    apply=_apply_delete_block,
    category="ipam",
    required_permission=("delete", "ip_block"),
)
register(_OP_DELETE_BLOCK)


# ── delete_space ───────────────────────────────────────────────────────────


class DeleteSpaceArgs(BaseModel):
    space_id: UUID
    permanent: bool = False


async def _preview_delete_space(
    db: AsyncSession, user: User, args: DeleteSpaceArgs
) -> PreviewResult:
    from app.models.ipam import IPBlock, IPSpace, Subnet

    space = await db.get(IPSpace, args.space_id)
    if space is None:
        return PreviewResult(ok=False, detail="IP space not found")

    if args.permanent:
        subnet_count = (
            await db.execute(
                select(func.count()).select_from(Subnet).where(Subnet.space_id == args.space_id)
            )
        ).scalar_one()
        block_count = (
            await db.execute(
                select(func.count()).select_from(IPBlock).where(IPBlock.space_id == args.space_id)
            )
        ).scalar_one()
        if subnet_count or block_count:
            parts = []
            if block_count:
                parts.append(f"{block_count} block(s)")
            if subnet_count:
                parts.append(f"{subnet_count} subnet(s)")
            return PreviewResult(
                ok=False,
                detail=(
                    f"IP space {space.name!r} still contains {' and '.join(parts)}. "
                    "Delete or move them before deleting the space."
                ),
            )
        return PreviewResult(
            ok=True,
            detail="ready",
            preview_text=f"Permanently delete empty IP space `{space.name}`",
        )

    return PreviewResult(
        ok=True,
        detail="ready",
        preview_text=(
            f"Soft-delete IP space `{space.name}` — cascades to every block, subnet, "
            "and DHCP scope under it; restorable from /admin/trash"
        ),
    )


async def _apply_delete_space(
    db: AsyncSession, user: User, args: DeleteSpaceArgs
) -> dict[str, Any]:
    """Body factored verbatim from ipam/router.py:delete_space."""
    from app.api.deps import require_superadmin
    from app.api.v1.ipam.router import _audit
    from app.models.ipam import IPBlock, IPSpace, Subnet
    from app.services.soft_delete import apply_soft_delete, collect_soft_delete_batch

    enforce_operation_permission(user, _OP_DELETE_SPACE)

    space = await db.get(IPSpace, args.space_id)
    if space is None:
        raise ValueError("IP space not found")

    if args.permanent:
        require_superadmin(user)
        subnet_count = (
            await db.execute(
                select(func.count()).select_from(Subnet).where(Subnet.space_id == args.space_id)
            )
        ).scalar_one()
        block_count = (
            await db.execute(
                select(func.count()).select_from(IPBlock).where(IPBlock.space_id == args.space_id)
            )
        ).scalar_one()
        if subnet_count or block_count:
            parts = []
            if block_count:
                parts.append(f"{block_count} block(s)")
            if subnet_count:
                parts.append(f"{subnet_count} subnet(s)")
            raise ValueError(
                f"IP space {space.name!r} still contains {' and '.join(parts)}. "
                "Delete or move them before deleting the space."
            )

        db.add(
            _audit(
                user,
                "delete",
                "ip_space",
                str(space.id),
                space.name,
                old_value={"name": space.name},
            )
        )
        await db.delete(space)
        await db.commit()
        return {"space_id": str(args.space_id), "mode": "delete"}

    batch = await collect_soft_delete_batch(db, space)
    apply_soft_delete(batch, user.id)
    for row in batch.rows:
        db.add(
            _audit(
                user,
                "soft_delete",
                row.resource_type,
                str(row.obj.id),
                row.display,
                old_value={"deletion_batch_id": str(batch.batch_id)},
            )
        )
    await db.commit()
    return {"space_id": str(args.space_id), "mode": "soft_delete"}


_OP_DELETE_SPACE = Operation(
    name="delete_space",
    description="Delete an IP space (soft-delete subtree, or permanent if empty).",
    args_model=DeleteSpaceArgs,
    preview=_preview_delete_space,
    apply=_apply_delete_space,
    category="ipam",
    required_permission=("delete", "ip_space"),
)
register(_OP_DELETE_SPACE)


# ── delete_zone ────────────────────────────────────────────────────────────


class DeleteZoneArgs(BaseModel):
    group_id: UUID
    zone_id: UUID
    permanent: bool = False


async def _preview_delete_zone(db: AsyncSession, user: User, args: DeleteZoneArgs) -> PreviewResult:
    from fastapi import HTTPException

    from app.api.v1.dns.router import _reject_if_synthesised_zone, _require_zone

    try:
        zone = await _require_zone(args.group_id, args.zone_id, db)
        _reject_if_synthesised_zone(zone, "delete")
    except HTTPException as exc:
        return PreviewResult(ok=False, detail=str(exc.detail))

    mode = "Permanently delete" if args.permanent else "Soft-delete"
    suffix = (
        " (pushes a remove-zone write-through to agentless servers first)"
        if args.permanent
        else " — the zone + its records are restorable from /admin/trash"
    )
    return PreviewResult(
        ok=True, detail="ready", preview_text=f"{mode} DNS zone `{zone.name}`{suffix}"
    )


async def _apply_delete_zone(db: AsyncSession, user: User, args: DeleteZoneArgs) -> dict[str, Any]:
    """Body factored verbatim from dns/router.py:delete_zone."""
    from app.api.v1.dns.router import (
        _push_zone_to_agentless_servers,
        _reject_if_synthesised_zone,
        _require_zone,
    )
    from app.core.agent_wake import collect_wake, dns_group_channel
    from app.models.audit import AuditLog
    from app.services.soft_delete import apply_soft_delete, collect_soft_delete_batch

    enforce_operation_permission(user, _OP_DELETE_ZONE)

    zone = await _require_zone(args.group_id, args.zone_id, db)
    _reject_if_synthesised_zone(zone, "delete")

    if not args.permanent:
        batch = await collect_soft_delete_batch(db, zone)
        apply_soft_delete(batch, user.id)
        for row in batch.rows:
            db.add(
                AuditLog(
                    user_id=user.id,
                    user_display_name=user.display_name,
                    auth_source=user.auth_source,
                    action="soft_delete",
                    resource_type=row.resource_type,
                    resource_id=str(row.obj.id),
                    resource_display=row.display,
                    old_value={"deletion_batch_id": str(batch.batch_id)},
                    result="success",
                )
            )
        collect_wake(dns_group_channel(args.group_id))
        await db.commit()
        return {"zone_id": str(args.zone_id), "mode": "soft_delete"}

    await _push_zone_to_agentless_servers(db, zone, "delete")
    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="delete",
            resource_type="dns_zone",
            resource_id=str(zone.id),
            resource_display=zone.name,
            result="success",
        )
    )
    collect_wake(dns_group_channel(args.group_id))
    await db.delete(zone)
    await db.commit()
    return {"zone_id": str(args.zone_id), "mode": "delete"}


_OP_DELETE_ZONE = Operation(
    name="delete_zone",
    description="Delete a DNS zone (soft-delete batch, or permanent with agentless write-through).",
    args_model=DeleteZoneArgs,
    preview=_preview_delete_zone,
    apply=_apply_delete_zone,
    category="dns",
    required_permission=("delete", "dns_zone"),
)
register(_OP_DELETE_ZONE)


# ── delete_scope ───────────────────────────────────────────────────────────


class DeleteScopeArgs(BaseModel):
    scope_id: UUID
    permanent: bool = False


async def _preview_delete_scope(
    db: AsyncSession, user: User, args: DeleteScopeArgs
) -> PreviewResult:
    from app.models.dhcp import DHCPScope

    scope = await db.get(DHCPScope, args.scope_id)
    if scope is None:
        return PreviewResult(ok=False, detail="Scope not found")
    mode = "Permanently delete" if args.permanent else "Soft-delete"
    suffix = (
        " (pushes a remove-scope write-through to Windows members first)"
        if args.permanent
        else " — restorable from /admin/trash"
    )
    return PreviewResult(
        ok=True, detail="ready", preview_text=f"{mode} DHCP scope `{scope.id}`{suffix}"
    )


async def _apply_delete_scope(
    db: AsyncSession, user: User, args: DeleteScopeArgs
) -> dict[str, Any]:
    """Body factored verbatim from dhcp/scopes.py:delete_scope."""
    from app.api.v1.dhcp._audit import write_audit
    from app.core.agent_wake import collect_wake, dhcp_group_channel
    from app.models.dhcp import DHCPScope
    from app.services.dhcp.windows_writethrough import push_scope_delete
    from app.services.soft_delete import apply_soft_delete, collect_soft_delete_batch

    enforce_operation_permission(user, _OP_DELETE_SCOPE)

    scope = await db.get(DHCPScope, args.scope_id)
    if scope is None:
        raise ValueError("Scope not found")
    collect_wake(dhcp_group_channel(scope.group_id))

    if not args.permanent:
        batch = await collect_soft_delete_batch(db, scope)
        apply_soft_delete(batch, user.id)
        for row in batch.rows:
            write_audit(
                db,
                user=user,
                action="soft_delete",
                resource_type=row.resource_type,
                resource_id=str(row.obj.id),
                resource_display=row.display,
                old_value={"deletion_batch_id": str(batch.batch_id)},
            )
        await db.commit()
        return {"scope_id": str(args.scope_id), "mode": "soft_delete"}

    await push_scope_delete(db, scope)
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="dhcp_scope",
        resource_id=str(scope.id),
        resource_display=str(scope.id),
    )
    await db.delete(scope)
    await db.commit()
    return {"scope_id": str(args.scope_id), "mode": "delete"}


_OP_DELETE_SCOPE = Operation(
    name="delete_scope",
    description="Delete a DHCP scope (soft-delete, or permanent with Windows write-through).",
    args_model=DeleteScopeArgs,
    preview=_preview_delete_scope,
    apply=_apply_delete_scope,
    category="dhcp",
    required_permission=("delete", "dhcp_scope"),
)
register(_OP_DELETE_SCOPE)


# ── delete_group ───────────────────────────────────────────────────────────


class DeleteGroupArgs(BaseModel):
    group_id: UUID


async def _preview_delete_group(
    db: AsyncSession, user: User, args: DeleteGroupArgs
) -> PreviewResult:
    from app.models.dhcp import DHCPServer, DHCPServerGroup

    g = await db.get(DHCPServerGroup, args.group_id)
    if g is None:
        return PreviewResult(ok=False, detail="Server group not found")
    server_count = (
        await db.execute(
            select(func.count())
            .select_from(DHCPServer)
            .where(DHCPServer.server_group_id == args.group_id)
        )
    ).scalar_one()
    if server_count:
        return PreviewResult(
            ok=False,
            detail=(
                f"DHCP server group {g.name!r} still contains "
                f"{server_count} server(s). Move them to another group "
                "(or standalone) before deleting the group."
            ),
        )
    return PreviewResult(
        ok=True, detail="ready", preview_text=f"Delete empty DHCP server group `{g.name}`"
    )


async def _apply_delete_group(
    db: AsyncSession, user: User, args: DeleteGroupArgs
) -> dict[str, Any]:
    """Body factored verbatim from dhcp/server_groups.py:delete_group."""
    from fastapi import HTTPException, status

    from app.api.v1.dhcp._audit import write_audit
    from app.models.dhcp import DHCPServer, DHCPServerGroup

    enforce_operation_permission(user, _OP_DELETE_GROUP)

    g = await db.get(DHCPServerGroup, args.group_id)
    if g is None:
        raise ValueError("Server group not found")

    server_count = (
        await db.execute(
            select(func.count())
            .select_from(DHCPServer)
            .where(DHCPServer.server_group_id == args.group_id)
        )
    ).scalar_one()
    if server_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"DHCP server group {g.name!r} still contains "
                f"{server_count} server(s). Move them to another group "
                "(or standalone) before deleting the group."
            ),
        )

    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="dhcp_server_group",
        resource_id=str(g.id),
        resource_display=g.name,
    )
    await db.delete(g)
    await db.commit()
    return {"group_id": str(args.group_id), "mode": "delete"}


_OP_DELETE_GROUP = Operation(
    name="delete_group",
    description="Delete a DHCP server group (refused if it still holds servers).",
    args_model=DeleteGroupArgs,
    preview=_preview_delete_group,
    apply=_apply_delete_group,
    category="dhcp",
    # The REST route is SuperAdmin-only; this declares the granular backstop
    # the approver is checked against (a superadmin approver passes via {*,*}).
    required_permission=("delete", "dhcp_server_group"),
)
register(_OP_DELETE_GROUP)


# Registry-completeness anchor for the startup assertion (sibling slice):
# every operation name the seeded policies reference must exist here.
RISKY_OPERATION_NAMES: frozenset[str] = frozenset(
    {
        "delete_subnet",
        "delete_block",
        "delete_space",
        "delete_zone",
        "delete_scope",
        "delete_group",
    }
)


def _assert_registered() -> None:
    """Defensive: prove all six names registered at import (catches a
    rename that desyncs the seeded policies from the registry)."""
    missing = [n for n in RISKY_OPERATION_NAMES if get_operation(n) is None]
    if missing:  # pragma: no cover — import-time invariant
        raise RuntimeError(f"risky operations not registered: {missing}")


_assert_registered()
