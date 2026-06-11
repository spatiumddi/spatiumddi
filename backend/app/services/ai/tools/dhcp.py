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
    DHCPPhoneProfile,
    DHCPPhoneProfileScope,
    DHCPPool,
    DHCPPXEProfile,
    DHCPScope,
    DHCPServer,
    DHCPServerGroup,
    DHCPStaticAssignment,
)
from app.models.dhcp_fingerprint import DHCPFingerprint
from app.services.ai.tools.base import register_tool
from app.services.oui import bulk_lookup_vendors, is_voip_phone_vendor, normalize_mac_key


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
            "v6_address_mode": getattr(s, "v6_address_mode", "stateful"),
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
    device_class: str | None = Field(
        default=None,
        description=(
            "Filter by fingerbank device class (passive DHCP fingerprinting), "
            "e.g. 'Phone, Tablet or Wearable', 'Operating System', 'Hardware "
            "Manufacturer'. Only leases whose MAC has a matching fingerprint "
            "are returned."
        ),
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="find_dhcp_leases",
    description=(
        "Find DHCP leases. Filterable by server, scope, MAC, IP, "
        "hostname substring, state, or fingerbank device class. Returns "
        "lease metadata (IP / MAC / mac_vendor / device_class / "
        "device_name / hostname / state / starts_at / ends_at). Use "
        "for questions like 'what's the lease for MAC X?', 'what's "
        "leased on subnet Y?', or 'show me the phones on this network'."
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
    if args.device_class:
        stmt = stmt.join(
            DHCPFingerprint, DHCPFingerprint.mac_address == DHCPLease.mac_address
        ).where(DHCPFingerprint.fingerbank_device_class == args.device_class)
    stmt = stmt.order_by(DHCPLease.ends_at.desc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    vendors = await bulk_lookup_vendors(db, [str(le.mac_address) for le in rows])
    # Batch-fetch fingerprints for the result MACs (one query) so each lease
    # can report its fingerbank device class/name without a per-row lookup.
    macs = [str(le.mac_address) for le in rows if le.mac_address]
    fps: dict[str, DHCPFingerprint] = {}
    if macs:
        fp_rows = (
            await db.execute(select(DHCPFingerprint).where(DHCPFingerprint.mac_address.in_(macs)))
        ).scalars()
        for fp in fp_rows:
            fps[normalize_mac_key(str(fp.mac_address))] = fp
    out: list[dict[str, Any]] = []
    for le in rows:
        mac_key = normalize_mac_key(str(le.mac_address))
        vendor = vendors.get(mac_key) if mac_key else None
        fp = fps.get(mac_key) if mac_key else None
        out.append(
            {
                "id": str(le.id),
                "server_id": str(le.server_id),
                "scope_id": str(le.scope_id) if le.scope_id else None,
                "ip_address": str(le.ip_address),
                "mac_address": str(le.mac_address),
                "mac_vendor": vendor,
                "is_voip_phone": is_voip_phone_vendor(vendor),
                "device_class": fp.fingerbank_device_class if fp else None,
                "device_name": fp.fingerbank_device_name if fp else None,
                "device_manufacturer": fp.fingerbank_manufacturer if fp else None,
                "fingerbank_score": fp.fingerbank_score if fp else None,
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
        "summary includes name, member count, DDNS toggle, and "
        "dhcp_socket_mode ('direct' = raw sockets that hear broadcast "
        "DISCOVERs from on-LAN clients; 'relay' = udp, relay-only)."
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
                # #365 — "direct" (raw sockets, hears broadcast DISCOVERs) or
                # "relay" (udp sockets, relay-only). Helps the copilot answer
                # "why isn't this DHCP server replying to direct clients?".
                "dhcp_socket_mode": g.dhcp_socket_mode,
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
        description="Filter by pool_type: dynamic / excluded / reserved / pd (v6 prefix delegation).",
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
            # DHCPv6 prefix delegation (issue #368) — populated only on
            # pool_type == "pd" pools.
            "pd_prefix": getattr(p, "pd_prefix", None),
            "delegated_length": getattr(p, "delegated_length", None),
            "excluded_prefix": getattr(p, "excluded_prefix", None),
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
            "duid": getattr(s, "duid", None),
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


# ── list_phone_profiles ──────────────────────────────────────────────


class ListPhoneProfilesArgs(BaseModel):
    group_id: str | None = Field(default=None, description="Filter by DHCP server group UUID.")
    enabled: bool | None = Field(default=None, description="Filter by ``enabled`` flag.")
    vendor: str | None = Field(default=None, description="Filter by curated vendor label.")
    search: str | None = Field(default=None, description="Substring match on profile name.")
    limit: int = Field(default=50, ge=1, le=500)


@register_tool(
    name="list_phone_profiles",
    description=(
        "List VoIP phone provisioning profiles — group-scoped, attached "
        "to scopes via the dhcp_phone_profile_scope join. Each row "
        "carries id, group_id, name, vendor (curated label like "
        "'Polycom' / 'Yealink' / 'Cisco SPA' or null for custom), "
        "vendor_class_match (option-60 substring fence), enabled "
        "flag, the option set delivered (DHCP option codes + values), "
        "and the count of attached scopes. Use for 'is the Polycom "
        "profile attached anywhere?' or 'which voice VLANs have phone "
        "profiles?'."
    ),
    args_model=ListPhoneProfilesArgs,
    category="dhcp",
)
async def list_phone_profiles(
    db: AsyncSession, user: User, args: ListPhoneProfilesArgs
) -> list[dict[str, Any]]:
    stmt = select(DHCPPhoneProfile)
    if args.group_id:
        stmt = stmt.where(DHCPPhoneProfile.group_id == args.group_id)
    if args.enabled is not None:
        stmt = stmt.where(DHCPPhoneProfile.enabled.is_(args.enabled))
    if args.vendor:
        stmt = stmt.where(func.lower(DHCPPhoneProfile.vendor) == args.vendor.lower())
    if args.search:
        stmt = stmt.where(func.lower(DHCPPhoneProfile.name).like(f"%{args.search.lower()}%"))
    stmt = stmt.order_by(DHCPPhoneProfile.name.asc()).limit(args.limit)
    rows = list((await db.execute(stmt)).scalars().all())

    if not rows:
        return []

    # Roll up scope-attachment counts in one query rather than per-row.
    counts_stmt = (
        select(
            DHCPPhoneProfileScope.profile_id,
            func.count(DHCPPhoneProfileScope.scope_id),
        )
        .where(DHCPPhoneProfileScope.profile_id.in_([p.id for p in rows]))
        .group_by(DHCPPhoneProfileScope.profile_id)
    )
    counts: dict[Any, int] = {}
    for pid, n in (await db.execute(counts_stmt)).all():
        counts[pid] = int(n)

    return [
        {
            "id": str(p.id),
            "group_id": str(p.group_id),
            "name": p.name,
            "description": p.description,
            "vendor": p.vendor,
            "vendor_class_match": p.vendor_class_match,
            "enabled": p.enabled,
            "option_count": len(p.option_set or []),
            "options": [
                {
                    "code": o.get("code"),
                    "name": o.get("name"),
                    "value": o.get("value"),
                }
                for o in (p.option_set or [])
            ],
            "scope_count": counts.get(p.id, 0),
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


class FindDHCPPoolOccupancyArgs(BaseModel):
    group_id: str | None = Field(default=None, description="Filter to one DHCP server group UUID.")
    scope_id: str | None = Field(default=None, description="Filter to one DHCP scope UUID.")
    min_percent: float = Field(
        default=0.0,
        description="Only return pools at or above this live occupancy percent (0-100).",
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="find_dhcp_pool_occupancy",
    description=(
        "Live occupancy of dynamic DHCP pools — assigned vs total addresses, "
        "free count, and occupancy percent, computed from active leases inside "
        "each pool range (works for Kea and Windows DHCP). Sorted most-full "
        "first. Use to answer 'which pools are near capacity / exhausted?'."
    ),
    args_model=FindDHCPPoolOccupancyArgs,
    category="dhcp",
)
async def find_dhcp_pool_occupancy(
    db: AsyncSession, user: User, args: FindDHCPPoolOccupancyArgs
) -> list[dict[str, Any]]:
    from app.services.dhcp.pool_occupancy import compute_pool_occupancy_batch

    stmt = select(DHCPPool, DHCPScope).join(DHCPScope, DHCPScope.id == DHCPPool.scope_id)
    stmt = stmt.where(DHCPPool.pool_type == "dynamic")
    if args.scope_id:
        stmt = stmt.where(DHCPPool.scope_id == args.scope_id)
    if args.group_id:
        stmt = stmt.where(DHCPScope.group_id == args.group_id)
    # ``.unique()`` is required because the ORM entities carry eager-loaded
    # collection relationships (DHCPScope.pools etc.).
    rows = (await db.execute(stmt)).unique().all()

    # One batched lease query for all pools rather than one per pool (N+1).
    occ_by_pool = await compute_pool_occupancy_batch(db, [pool for pool, _ in rows])

    out: list[dict[str, Any]] = []
    for pool, scope in rows:
        occ = occ_by_pool[pool.id]
        if occ.percent < args.min_percent:
            continue
        out.append(
            {
                "pool_id": str(pool.id),
                "pool_name": pool.name or None,
                "scope_id": str(pool.scope_id),
                "scope_name": scope.name or None,
                "group_id": str(scope.group_id),
                "start_ip": str(pool.start_ip),
                "end_ip": str(pool.end_ip),
                "assigned": occ.assigned,
                "total": occ.total,
                "free": occ.free,
                "occupancy_percent": round(occ.percent, 1),
            }
        )
    out.sort(key=lambda r: r["occupancy_percent"], reverse=True)
    return out[: args.limit]


# ── find_dhcp_responders (issue #370) ─────────────────────────────────


class FindDHCPRespondersArgs(BaseModel):
    group_id: str | None = Field(
        default=None, description="Filter to one DHCP server group by UUID."
    )
    classification: str | None = Field(
        default=None,
        description="Filter by classification: expected / acknowledged / rogue.",
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="find_dhcp_responders",
    description=(
        "List DHCP servers the active rogue-detection probe has observed "
        "answering on managed segments (issue #370). Each row carries the "
        "source IP / MAC, server-identifier, offered IP, classification "
        "(expected = a known group member, acknowledged = operator-allowlisted, "
        "rogue = unknown responder), and last-seen time. Filter "
        "classification='rogue' to answer 'is there a rogue DHCP server on my "
        "network?'. Read-only; only has data on segments running the probe."
    ),
    args_model=FindDHCPRespondersArgs,
    category="dhcp",
)
async def find_dhcp_responders(
    db: AsyncSession, user: User, args: FindDHCPRespondersArgs
) -> list[dict[str, Any]]:
    from app.models.dhcp import DHCPObservedResponder  # noqa: PLC0415

    stmt = select(DHCPObservedResponder)
    if args.group_id:
        stmt = stmt.where(DHCPObservedResponder.group_id == args.group_id)
    if args.classification:
        stmt = stmt.where(DHCPObservedResponder.classification == args.classification.lower())
    stmt = stmt.order_by(DHCPObservedResponder.last_seen_at.desc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "group_id": str(r.group_id),
            "server_identifier": r.server_identifier,
            "source_ip": str(r.source_ip),
            "source_mac": str(r.source_mac) if r.source_mac else None,
            "giaddr": str(r.giaddr) if r.giaddr else None,
            "offered_ip": str(r.offered_ip) if r.offered_ip else None,
            "classification": r.classification,
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        }
        for r in rows
    ]
