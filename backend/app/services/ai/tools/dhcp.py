"""Read-only DHCP tools for the Operator Copilot (issue #90 Wave 2)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.dhcp import (
    DHCPClientClass,
    DHCPLease,
    DHCPMACBlock,
    DHCPOptionTemplate,
    DHCPPool,
    DHCPPXEProfile,
    DHCPScope,
    DHCPServer,
    DHCPServerGroup,
    DHCPStaticAssignment,
)
from app.services.ai.tools.base import register_tool
from app.services.oui import bulk_lookup_vendors, normalize_mac_key


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
    vendors = await bulk_lookup_vendors(db, [str(le.mac_address) for le in rows])
    out: list[dict[str, Any]] = []
    for le in rows:
        mac_key = normalize_mac_key(str(le.mac_address))
        out.append(
            {
                "id": str(le.id),
                "server_id": str(le.server_id),
                "scope_id": str(le.scope_id) if le.scope_id else None,
                "ip_address": str(le.ip_address),
                "mac_address": str(le.mac_address),
                "mac_vendor": vendors.get(mac_key) if mac_key else None,
                "hostname": le.hostname,
                "state": le.state,
                "starts_at": le.starts_at.isoformat() if le.starts_at else None,
                "ends_at": le.ends_at.isoformat() if le.ends_at else None,
            }
        )
    return out


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


# ── Tier 3 DHCP sub-resource depth (issue #101) ───────────────────────


# ── list_dhcp_pools ───────────────────────────────────────────────────


class ListDHCPPoolsArgs(BaseModel):
    scope_id: str | None = Field(default=None, description="Filter to one DHCP scope by UUID.")
    pool_type: str | None = Field(
        default=None,
        description="Filter by pool_type: dynamic / excluded / reserved.",
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="list_dhcp_pools",
    description=(
        "List DHCP pools — IP ranges within a scope, classified as "
        "dynamic (lease pool), excluded (skip during allocation), or "
        "reserved (operator-managed). Each row carries id, scope_id, "
        "name, start_ip + end_ip, pool_type, optional class_restriction, "
        "lease_time_override, and any options_override. Use for "
        "'what's the dynamic range in the corp scope?', 'show "
        "excluded ranges', or 'is the IoT pool restricted to a "
        "client class?'."
    ),
    args_model=ListDHCPPoolsArgs,
    category="dhcp",
)
async def list_dhcp_pools(
    db: AsyncSession, user: User, args: ListDHCPPoolsArgs
) -> list[dict[str, Any]]:
    stmt = select(DHCPPool)
    if args.scope_id:
        stmt = stmt.where(DHCPPool.scope_id == args.scope_id)
    if args.pool_type:
        stmt = stmt.where(DHCPPool.pool_type == args.pool_type.lower())
    stmt = stmt.order_by(DHCPPool.scope_id, DHCPPool.start_ip).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(p.id),
            "scope_id": str(p.scope_id),
            "name": p.name,
            "start_ip": str(p.start_ip),
            "end_ip": str(p.end_ip),
            "pool_type": p.pool_type,
            "class_restriction": p.class_restriction,
            "lease_time_override": p.lease_time_override,
            "options_override": p.options_override,
        }
        for p in rows
    ]


# ── list_dhcp_statics ─────────────────────────────────────────────────


class ListDHCPStaticsArgs(BaseModel):
    scope_id: str | None = Field(default=None, description="Filter to one DHCP scope by UUID.")
    mac_address: str | None = Field(default=None, description="Exact MAC address match.")
    ip_address: str | None = Field(default=None, description="Exact IP address match.")
    hostname_contains: str | None = Field(
        default=None, description="Substring match on the hostname."
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="list_dhcp_statics",
    description=(
        "List DHCP static reservations (MAC → IP). Filterable by "
        "scope, MAC, IP, or hostname substring. Each row carries id, "
        "scope_id, ip_address, mac_address, client_id, hostname, "
        "description, options_override, and the linked IPAM "
        "ip_address_id when bound. Use for 'show statics for the "
        "voip scope', 'is 11:22:33:44:55:66 reserved?', or 'find "
        "every static for hostname matching printer*'."
    ),
    args_model=ListDHCPStaticsArgs,
    category="dhcp",
)
async def list_dhcp_statics(
    db: AsyncSession, user: User, args: ListDHCPStaticsArgs
) -> list[dict[str, Any]]:
    stmt = select(DHCPStaticAssignment)
    if args.scope_id:
        stmt = stmt.where(DHCPStaticAssignment.scope_id == args.scope_id)
    if args.mac_address:
        stmt = stmt.where(DHCPStaticAssignment.mac_address == args.mac_address.lower())
    if args.ip_address:
        stmt = stmt.where(DHCPStaticAssignment.ip_address == args.ip_address)
    if args.hostname_contains:
        stmt = stmt.where(
            func.lower(DHCPStaticAssignment.hostname).like(f"%{args.hostname_contains.lower()}%")
        )
    stmt = stmt.order_by(DHCPStaticAssignment.ip_address.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(s.id),
            "scope_id": str(s.scope_id),
            "ip_address": str(s.ip_address),
            "mac_address": str(s.mac_address),
            "client_id": s.client_id,
            "hostname": s.hostname,
            "description": s.description,
            "options_override": s.options_override,
            "ip_address_id": str(s.ip_address_id) if s.ip_address_id else None,
        }
        for s in rows
    ]


# ── list_dhcp_client_classes ──────────────────────────────────────────


class ListDHCPClientClassesArgs(BaseModel):
    group_id: str | None = Field(default=None, description="Filter by DHCP server group UUID.")
    search: str | None = Field(default=None, description="Substring match on class name.")
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="list_dhcp_client_classes",
    description=(
        "List DHCP client classes — group-scoped expressions used "
        "for conditional option delivery. Each row carries id, "
        "group_id, name, match_expression (the Kea expression), "
        "description, and the option overrides JSON. Use for 'what "
        "client classes are defined for corp?' or 'show me the "
        "match expression for the IoT class'."
    ),
    args_model=ListDHCPClientClassesArgs,
    category="dhcp",
)
async def list_dhcp_client_classes(
    db: AsyncSession, user: User, args: ListDHCPClientClassesArgs
) -> list[dict[str, Any]]:
    stmt = select(DHCPClientClass)
    if args.group_id:
        stmt = stmt.where(DHCPClientClass.group_id == args.group_id)
    if args.search:
        stmt = stmt.where(func.lower(DHCPClientClass.name).like(f"%{args.search.lower()}%"))
    stmt = stmt.order_by(DHCPClientClass.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(c.id),
            "group_id": str(c.group_id),
            "name": c.name,
            "match_expression": c.match_expression,
            "description": c.description,
            "options": c.options,
        }
        for c in rows
    ]


# ── list_dhcp_option_templates ────────────────────────────────────────


class ListDHCPOptionTemplatesArgs(BaseModel):
    group_id: str | None = Field(default=None, description="Filter by DHCP server group UUID.")
    address_family: str | None = Field(
        default=None,
        description="Filter by address family: ``ipv4`` or ``ipv6``.",
    )
    search: str | None = Field(default=None, description="Substring match on template name.")
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="list_dhcp_option_templates",
    description=(
        "List DHCP option templates — reusable named bundles of "
        "option-code → value pairs scoped per server group. Apply "
        "stamps the bundle into a scope's options dict at apply "
        "time (no runtime re-bind). Each row carries id, group_id, "
        "name, address_family (ipv4 / ipv6), description, and the "
        "options JSON. Use for 'what option templates exist?' or "
        "'show me the options in the corp template'."
    ),
    args_model=ListDHCPOptionTemplatesArgs,
    category="dhcp",
)
async def list_dhcp_option_templates(
    db: AsyncSession, user: User, args: ListDHCPOptionTemplatesArgs
) -> list[dict[str, Any]]:
    stmt = select(DHCPOptionTemplate)
    if args.group_id:
        stmt = stmt.where(DHCPOptionTemplate.group_id == args.group_id)
    if args.address_family:
        stmt = stmt.where(DHCPOptionTemplate.address_family == args.address_family.lower())
    if args.search:
        stmt = stmt.where(func.lower(DHCPOptionTemplate.name).like(f"%{args.search.lower()}%"))
    stmt = stmt.order_by(DHCPOptionTemplate.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(t.id),
            "group_id": str(t.group_id),
            "name": t.name,
            "address_family": t.address_family,
            "description": t.description,
            "options": t.options,
        }
        for t in rows
    ]


# ── list_pxe_profiles ─────────────────────────────────────────────────


class ListPXEProfilesArgs(BaseModel):
    group_id: str | None = Field(default=None, description="Filter by DHCP server group UUID.")
    enabled: bool | None = Field(default=None, description="Filter by ``enabled`` flag.")
    search: str | None = Field(default=None, description="Substring match on profile name.")
    limit: int = Field(default=50, ge=1, le=500)


@register_tool(
    name="list_pxe_profiles",
    description=(
        "List PXE / iPXE provisioning profiles — group-scoped, "
        "operator-pickable per scope via DHCPScope.pxe_profile_id. "
        "Each row carries id, group_id, name, description, "
        "next_server (TFTP/HTTP boot server IP), enabled flag, and "
        "the per-arch matches (vendor_class + arch_code → boot "
        "file). Use for 'what PXE profiles are configured?' or 'is "
        "the lab profile enabled?'."
    ),
    args_model=ListPXEProfilesArgs,
    category="dhcp",
)
async def list_pxe_profiles(
    db: AsyncSession, user: User, args: ListPXEProfilesArgs
) -> list[dict[str, Any]]:
    stmt = select(DHCPPXEProfile)
    if args.group_id:
        stmt = stmt.where(DHCPPXEProfile.group_id == args.group_id)
    if args.enabled is not None:
        stmt = stmt.where(DHCPPXEProfile.enabled.is_(args.enabled))
    if args.search:
        stmt = stmt.where(func.lower(DHCPPXEProfile.name).like(f"%{args.search.lower()}%"))
    stmt = stmt.order_by(DHCPPXEProfile.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(p.id),
            "group_id": str(p.group_id),
            "name": p.name,
            "description": p.description,
            "next_server": p.next_server,
            "enabled": p.enabled,
            "match_count": len(p.matches or []),
            "matches": [
                {
                    "vendor_class": getattr(m, "vendor_class", None),
                    "arch_code": getattr(m, "arch_code", None),
                    "boot_file": getattr(m, "boot_file", None),
                    "priority": getattr(m, "priority", None),
                }
                for m in (p.matches or [])
            ],
        }
        for p in rows
    ]


# ── list_dhcp_mac_blocks ──────────────────────────────────────────────


class ListDHCPMACBlocksArgs(BaseModel):
    group_id: str | None = Field(default=None, description="Filter by DHCP server group UUID.")
    mac_address: str | None = Field(default=None, description="Exact MAC match.")
    enabled: bool | None = Field(default=None, description="Filter by ``enabled`` flag.")
    reason: str | None = Field(
        default=None,
        description="Filter by reason: rogue / lost_stolen / quarantine / policy / other.",
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="list_dhcp_mac_blocks",
    description=(
        "List blocked MAC addresses — group-global, applies to "
        "every scope in the group. Each row carries id, group_id, "
        "mac_address, reason, description, enabled flag, and "
        "expires_at (when timed). Use for 'is the rogue MAC "
        "blocked?', 'list lost/stolen entries', or 'when does the "
        "quarantine expire?'."
    ),
    args_model=ListDHCPMACBlocksArgs,
    category="dhcp",
)
async def list_dhcp_mac_blocks(
    db: AsyncSession, user: User, args: ListDHCPMACBlocksArgs
) -> list[dict[str, Any]]:
    stmt = select(DHCPMACBlock)
    if args.group_id:
        stmt = stmt.where(DHCPMACBlock.group_id == args.group_id)
    if args.mac_address:
        stmt = stmt.where(DHCPMACBlock.mac_address == args.mac_address.lower())
    if args.enabled is not None:
        stmt = stmt.where(DHCPMACBlock.enabled.is_(args.enabled))
    if args.reason:
        stmt = stmt.where(DHCPMACBlock.reason == args.reason.lower())
    stmt = stmt.order_by(DHCPMACBlock.mac_address.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(b.id),
            "group_id": str(b.group_id),
            "mac_address": str(b.mac_address),
            "reason": b.reason,
            "description": b.description,
            "enabled": b.enabled,
            "expires_at": b.expires_at.isoformat() if b.expires_at else None,
        }
        for b in rows
    ]
