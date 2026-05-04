"""Read-only DHCP tools for the Operator Copilot (issue #90 Wave 2)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.dhcp import DHCPLease, DHCPScope, DHCPServer, DHCPServerGroup
from app.services.ai.tools.base import register_tool


class ListDHCPServersArgs(BaseModel):
    group_id: str | None = Field(default=None, description="Filter by DHCP server group UUID.")


@register_tool(
    name="list_dhcp_servers",
    description=(
        "List DHCP servers (Kea / Windows DHCP). Each summary "
        "includes name, group, server type, and HA state."
    ),
    args_model=ListDHCPServersArgs,
    category="dhcp",
)
async def list_dhcp_servers(
    db: AsyncSession, user: User, args: ListDHCPServersArgs
) -> list[dict[str, Any]]:
    stmt = select(DHCPServer)
    if args.group_id:
        stmt = stmt.where(DHCPServer.group_id == args.group_id)
    stmt = stmt.order_by(DHCPServer.name.asc())
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(s.id),
            "name": s.name,
            "group_id": str(s.group_id) if s.group_id else None,
            "server_type": s.server_type,
            "is_enabled": s.is_enabled,
            "ha_state": s.ha_state,
        }
        for s in rows
    ]


class ListDHCPScopesArgs(BaseModel):
    group_id: str | None = None
    search: str | None = Field(
        default=None,
        description="Substring match on scope name or CIDR.",
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="list_dhcp_scopes",
    description=(
        "List DHCP scopes (subnets where DHCP serves leases). Filter "
        "by server group or name / CIDR substring."
    ),
    args_model=ListDHCPScopesArgs,
    category="dhcp",
)
async def list_dhcp_scopes(
    db: AsyncSession, user: User, args: ListDHCPScopesArgs
) -> list[dict[str, Any]]:
    stmt = select(DHCPScope).where(DHCPScope.deleted_at.is_(None))
    if args.group_id:
        stmt = stmt.where(DHCPScope.group_id == args.group_id)
    if args.search:
        like = f"%{args.search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(DHCPScope.name).like(like),
                func.text(DHCPScope.subnet).like(like),
            )
        )
    stmt = stmt.order_by(DHCPScope.subnet.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(s.id),
            "group_id": str(s.group_id) if s.group_id else None,
            "subnet": str(s.subnet),
            "name": s.name,
            "address_family": s.address_family,
        }
        for s in rows
    ]


class FindDHCPLeasesArgs(BaseModel):
    server_id: str | None = None
    scope_id: str | None = None
    mac_address: str | None = Field(
        default=None,
        description="Filter by exact MAC address.",
    )
    ip_address: str | None = Field(
        default=None,
        description="Filter by exact IP address.",
    )
    hostname_search: str | None = Field(
        default=None,
        description="Substring match on the lease's reported client hostname.",
    )
    state: str | None = Field(
        default=None,
        description="Filter by lease state (active, expired, declined, released).",
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="find_dhcp_leases",
    description=(
        "Find DHCP leases. Filterable by server, scope, MAC, IP, "
        "hostname substring, or state. Returns lease metadata "
        "(IP / MAC / hostname / state / starts_at / ends_at). Use "
        "for questions like 'what's the lease for MAC X?', 'what's "
        "leased on subnet Y?', or 'find every lease for hostname Z'."
    ),
    args_model=FindDHCPLeasesArgs,
    category="dhcp",
)
async def find_dhcp_leases(
    db: AsyncSession, user: User, args: FindDHCPLeasesArgs
) -> list[dict[str, Any]]:
    stmt = select(DHCPLease)
    if args.server_id:
        stmt = stmt.where(DHCPLease.server_id == args.server_id)
    if args.scope_id:
        stmt = stmt.where(DHCPLease.scope_id == args.scope_id)
    if args.mac_address:
        stmt = stmt.where(DHCPLease.mac_address == args.mac_address)
    if args.ip_address:
        stmt = stmt.where(
            func.host(DHCPLease.ip_address) == func.host(cast(literal(args.ip_address), INET))
        )
    if args.hostname_search:
        like = f"%{args.hostname_search.lower()}%"
        stmt = stmt.where(func.lower(DHCPLease.hostname).like(like))
    if args.state:
        stmt = stmt.where(DHCPLease.state == args.state)
    stmt = stmt.order_by(DHCPLease.ends_at.desc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(le.id),
            "server_id": str(le.server_id),
            "scope_id": str(le.scope_id) if le.scope_id else None,
            "ip_address": str(le.ip_address),
            "mac_address": str(le.mac_address),
            "hostname": le.hostname,
            "state": le.state,
            "starts_at": le.starts_at.isoformat() if le.starts_at else None,
            "ends_at": le.ends_at.isoformat() if le.ends_at else None,
        }
        for le in rows
    ]


class ListServerGroupsArgs(BaseModel):
    pass


@register_tool(
    name="list_dhcp_server_groups",
    description=(
        "List DHCP server groups (logical bundles of Kea servers, "
        "with HA implicit when the group has ≥ 2 members). Each "
        "summary includes name, member count, and DDNS toggle."
    ),
    args_model=ListServerGroupsArgs,
    category="dhcp",
)
async def list_dhcp_server_groups(
    db: AsyncSession, user: User, args: ListServerGroupsArgs
) -> list[dict[str, Any]]:
    rows = (
        (await db.execute(select(DHCPServerGroup).order_by(DHCPServerGroup.name.asc())))
        .scalars()
        .all()
    )
    out: list[dict[str, Any]] = []
    for g in rows:
        member_count = await db.scalar(
            select(func.count(DHCPServer.id)).where(DHCPServer.group_id == g.id)
        )
        out.append(
            {
                "id": str(g.id),
                "name": g.name,
                "ddns_enabled": g.ddns_enabled,
                "member_count": int(member_count or 0),
            }
        )
    return out
