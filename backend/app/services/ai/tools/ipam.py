"""Read-only IPAM tools for the Operator Copilot (issue #90 Wave 2).

Each tool wraps a small, focused query against the existing IPAM
service / model layer and returns a JSON-serializable result. The
Pydantic args models double as JSON-Schema generators for the LLM
tool-call interface.

Tools deliberately return *summaries* — operator-readable subsets of
each row's columns, not the full ORM object. Keeps token cost down
and avoids leaking secrets / large blobs.
"""

from __future__ import annotations

import ipaddress
import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asn import ASN
from app.models.auth import User
from app.models.circuit import Circuit
from app.models.dhcp import DHCPScope, DHCPStaticAssignment
from app.models.dns import DNSRecord, DNSZone
from app.models.domain import Domain
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.network import NetworkDevice
from app.models.network_service import NetworkService
from app.models.overlay import OverlayNetwork
from app.models.vrf import VRF
from app.services.ai.tools.base import register_tool
from app.services.oui import bulk_lookup_vendors, is_voip_phone_vendor, normalize_mac_key
from app.services.tags import apply_tag_filter

# ── Reference resolution ──────────────────────────────────────────────
#
# The LLM frequently passes a *name* where the schema declares a UUID
# (e.g. ``space_id="home"`` because the operator said "the home space").
# Rather than make the model take a two-step "look up the UUID, then
# query" dance for every question, the IPAM read-tools accept either
# form and resolve names case-insensitively. Returns ``None`` when the
# name doesn't match any row — the caller raises a tool-level error
# pointing the model at the right list_* tool.


async def _resolve_space_ref(db: AsyncSession, value: str) -> uuid.UUID | None:
    """Translate an ``id-or-name`` reference into an ``IPSpace.id``.

    Tries UUID parse first, then case-insensitive name match. Returns
    ``None`` if neither hits — the tool wrapper turns that into an
    operator-readable error.
    """
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        pass
    row = (
        await db.execute(select(IPSpace.id).where(func.lower(IPSpace.name) == value.lower()))
    ).scalar_one_or_none()
    return row


async def _resolve_block_ref(db: AsyncSession, value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        pass
    row = (
        await db.execute(select(IPBlock.id).where(func.lower(IPBlock.name) == value.lower()))
    ).scalar_one_or_none()
    return row


# ── list_ip_spaces ────────────────────────────────────────────────────


class ListSpacesArgs(BaseModel):
    search: str | None = Field(
        default=None,
        description="Optional case-insensitive substring match on the space name or description.",
    )
    limit: int = Field(default=50, ge=1, le=200)


@register_tool(
    name="list_ip_spaces",
    description=(
        "List IP spaces (top-level routing domains / VRFs). "
        "Returns each space's id, name, description, default flag, "
        "and tag count."
    ),
    args_model=ListSpacesArgs,
    category="ipam",
)
async def list_ip_spaces(
    db: AsyncSession, user: User, args: ListSpacesArgs
) -> list[dict[str, Any]]:
    stmt = select(IPSpace).where(IPSpace.deleted_at.is_(None))
    if args.search:
        like = f"%{args.search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(IPSpace.name).like(like),
                func.lower(IPSpace.description).like(like),
            )
        )
    stmt = stmt.order_by(IPSpace.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(s.id),
            "name": s.name,
            "description": s.description,
            "is_default": s.is_default,
            "tag_count": len(s.tags or {}),
        }
        for s in rows
    ]


# ── list_ip_blocks ────────────────────────────────────────────────────


class ListBlocksArgs(BaseModel):
    space_id: str | None = Field(
        default=None,
        description=(
            "Filter by IP space — accepts either the UUID or the "
            "space name (case-insensitive). Omit to list across all spaces."
        ),
    )
    search: str | None = Field(
        default=None,
        description="Optional substring match on the block name or network CIDR.",
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="list_ip_blocks",
    description=(
        "List IP blocks (aggregate / supernet ranges). Each block "
        "summary includes its CIDR, name, parent block, space, and "
        "computed utilization percentage."
    ),
    args_model=ListBlocksArgs,
    category="ipam",
)
async def list_ip_blocks(
    db: AsyncSession, user: User, args: ListBlocksArgs
) -> list[dict[str, Any]] | dict[str, Any]:
    stmt = select(IPBlock).where(IPBlock.deleted_at.is_(None))
    if args.space_id:
        space_uuid = await _resolve_space_ref(db, args.space_id)
        if space_uuid is None:
            return {
                "error": f"No IP space matched {args.space_id!r}.",
                "hint": "Call list_ip_spaces to see available space names + UUIDs.",
            }
        stmt = stmt.where(IPBlock.space_id == space_uuid)
    if args.search:
        like = f"%{args.search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(IPBlock.name).like(like),
                func.text(IPBlock.network).like(like),
            )
        )
    stmt = stmt.order_by(IPBlock.network.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(b.id),
            "network": str(b.network),
            "name": b.name,
            "description": b.description,
            "space_id": str(b.space_id),
            "parent_block_id": str(b.parent_block_id) if b.parent_block_id else None,
            "utilization_percent": float(b.utilization_percent or 0.0),
        }
        for b in rows
    ]


# ── list_subnets ──────────────────────────────────────────────────────


class ListSubnetsArgs(BaseModel):
    space_id: str | None = Field(
        default=None,
        description=(
            "Filter by IP space — accepts either the UUID or the "
            "space name (case-insensitive). Omit to search across all spaces."
        ),
    )
    block_id: str | None = Field(
        default=None,
        description=(
            "Filter by parent IP block — accepts either the UUID or the "
            "block name (case-insensitive)."
        ),
    )
    vlan_id: int | None = Field(default=None, description="Filter by VLAN tag (1–4094).")
    search: str | None = Field(
        default=None,
        description="Substring match on subnet name or CIDR.",
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="list_subnets",
    description=(
        "List subnets — the routable units that own IP addresses. "
        "Use this tool whenever the operator names a subnet by CIDR "
        "(e.g. '192.168.0.0/24') or by substring; pass it as the "
        "``search`` argument. The response carries ``total_ips``, "
        "``allocated_ips``, ``utilization_percent``, ``gateway``, "
        "and ``vlan_id`` for every match — one call answers most "
        "subnet questions without needing a follow-up. Also "
        "filterable by space, parent block, or VLAN tag."
    ),
    args_model=ListSubnetsArgs,
    category="ipam",
)
async def list_subnets(
    db: AsyncSession, user: User, args: ListSubnetsArgs
) -> list[dict[str, Any]] | dict[str, Any]:
    stmt = select(Subnet).where(Subnet.deleted_at.is_(None))
    if args.space_id:
        space_uuid = await _resolve_space_ref(db, args.space_id)
        if space_uuid is None:
            return {
                "error": f"No IP space matched {args.space_id!r}.",
                "hint": "Call list_ip_spaces to see available space names + UUIDs.",
            }
        stmt = stmt.where(Subnet.space_id == space_uuid)
    if args.block_id:
        block_uuid = await _resolve_block_ref(db, args.block_id)
        if block_uuid is None:
            return {
                "error": f"No IP block matched {args.block_id!r}.",
                "hint": "Call list_ip_blocks to see available block names + UUIDs.",
            }
        stmt = stmt.where(Subnet.block_id == block_uuid)
    if args.vlan_id is not None:
        stmt = stmt.where(Subnet.vlan_id == args.vlan_id)
    if args.search:
        like = f"%{args.search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Subnet.name).like(like),
                func.text(Subnet.network).like(like),
            )
        )
    stmt = stmt.order_by(Subnet.network.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(s.id),
            "network": str(s.network),
            "name": s.name,
            "description": s.description,
            "space_id": str(s.space_id),
            "block_id": str(s.block_id) if s.block_id else None,
            "vlan_id": s.vlan_id,
            "vxlan_id": s.vxlan_id,
            "gateway": str(s.gateway) if s.gateway else None,
            "utilization_percent": float(s.utilization_percent or 0.0),
            "total_ips": int(s.total_ips or 0),
            "allocated_ips": int(s.allocated_ips or 0),
            "dns_zone_id": s.dns_zone_id,
            "dhcp_server_group_id": str(s.dhcp_server_group_id) if s.dhcp_server_group_id else None,
        }
        for s in rows
    ]


# ── get_subnet_summary ────────────────────────────────────────────────


class SubnetSummaryArgs(BaseModel):
    subnet_id: str = Field(description="Subnet UUID.")


@register_tool(
    name="get_subnet_summary",
    description=(
        "Detailed summary for one subnet: status counts (allocated, "
        "free, reserved, dhcp, etc.), gateway, VLAN, recent allocation "
        "activity. Use this when the operator asks 'how full is X?' "
        "or 'what's in X?'."
    ),
    args_model=SubnetSummaryArgs,
    category="ipam",
)
async def get_subnet_summary(
    db: AsyncSession, user: User, args: SubnetSummaryArgs
) -> dict[str, Any]:
    subnet = await db.get(Subnet, args.subnet_id)
    if subnet is None or subnet.deleted_at is not None:
        return {"error": "subnet not found", "subnet_id": args.subnet_id}
    counts_stmt = (
        select(IPAddress.status, func.count(IPAddress.id))
        .where(IPAddress.subnet_id == subnet.id)
        .group_by(IPAddress.status)
    )
    counts_rows = (await db.execute(counts_stmt)).all()
    by_status = {row[0]: int(row[1]) for row in counts_rows}
    return {
        "id": str(subnet.id),
        "network": str(subnet.network),
        "name": subnet.name,
        "description": subnet.description,
        "vlan_id": subnet.vlan_id,
        "gateway": str(subnet.gateway) if subnet.gateway else None,
        "utilization_percent": float(subnet.utilization_percent or 0.0),
        "total_ips": int(subnet.total_ips or 0),
        "allocated_ips": int(subnet.allocated_ips or 0),
        "by_status": by_status,
    }


# ── find_ip ───────────────────────────────────────────────────────────


class FindIPArgs(BaseModel):
    address: str = Field(
        description=(
            "IPv4 or IPv6 address. Supports both bare host form "
            "('10.0.0.5') and host-with-prefix form ('10.0.0.5/32')."
        )
    )


@register_tool(
    name="find_ip",
    description=(
        "Look up a single IP address by its dotted-decimal value "
        "(e.g. ``192.168.0.4``) and return its full row: hostname, "
        "FQDN, **MAC address**, status, role, owner, tags, custom "
        "fields, and which subnet it belongs to. Use this for any "
        "question of the form 'what is the X of IP Y' — host name, "
        "MAC, owner, last-seen timestamp, custom field value, etc. "
        "Do NOT use ``query_dns_records`` for IP→MAC lookups; PTR "
        "records carry hostnames, not MACs."
    ),
    args_model=FindIPArgs,
    category="ipam",
)
async def find_ip(db: AsyncSession, user: User, args: FindIPArgs) -> dict[str, Any]:
    try:
        ipaddress.ip_address(args.address.split("/", 1)[0])
    except ValueError:
        return {"error": f"invalid IP address: {args.address!r}"}
    stmt = select(IPAddress).where(
        func.host(IPAddress.address) == func.host(cast(literal(args.address), INET))
    )
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return {"matches": []}
    # OUI vendor enrichment — bulk_lookup_vendors short-circuits to {}
    # when the lookup feature is disabled, so this is a no-op cost on
    # deployments that haven't seeded the OUI table.
    vendors = await bulk_lookup_vendors(
        db, [str(ip.mac_address) if ip.mac_address else None for ip in rows]
    )
    out: list[dict[str, Any]] = []
    for ip in rows:
        sub = await db.get(Subnet, ip.subnet_id)
        mac_key = normalize_mac_key(str(ip.mac_address)) if ip.mac_address else None
        vendor = vendors.get(mac_key) if mac_key else None
        out.append(
            {
                "id": str(ip.id),
                "address": str(ip.address),
                "subnet_id": str(ip.subnet_id),
                "subnet_network": str(sub.network) if sub else None,
                "subnet_name": sub.name if sub else None,
                "status": ip.status,
                "role": ip.role,
                "hostname": ip.hostname,
                "fqdn": ip.fqdn,
                "mac_address": str(ip.mac_address) if ip.mac_address else None,
                "mac_vendor": vendor,
                "is_voip_phone": is_voip_phone_vendor(vendor),
                "description": ip.description,
                "last_seen_at": ip.last_seen_at.isoformat() if ip.last_seen_at else None,
                "last_seen_method": ip.last_seen_method,
                "tags": ip.tags or {},
            }
        )
    return {"matches": out}


# ── find_by_tag ───────────────────────────────────────────────────────


# Per-kind dispatch table for find_by_tag. Each entry: (model class,
# whether the model has SoftDeleteMixin so we should hide tombstoned
# rows, response-row renderer). Kept as data so adding a new tagged
# resource kind in the future is one line, not a new branch.
_TAG_KIND_TABLE: dict[str, tuple[type, bool, Any]] = {
    "ip_space": (
        IPSpace,
        True,
        lambda r: {"id": str(r.id), "name": r.name, "tags": r.tags or {}},
    ),
    "ip_block": (
        IPBlock,
        True,
        lambda r: {
            "id": str(r.id),
            "network": str(r.network),
            "name": r.name,
            "tags": r.tags or {},
        },
    ),
    "subnet": (
        Subnet,
        True,
        lambda r: {
            "id": str(r.id),
            "network": str(r.network),
            "name": r.name,
            "tags": r.tags or {},
        },
    ),
    "ip_address": (
        IPAddress,
        False,
        lambda r: {
            "id": str(r.id),
            "address": str(r.address),
            "hostname": r.hostname,
            "status": r.status,
            "tags": r.tags or {},
        },
    ),
    "asn": (
        ASN,
        False,
        lambda r: {
            "id": str(r.id),
            "number": r.number,
            "name": r.name,
            "tags": r.tags or {},
        },
    ),
    "vrf": (
        VRF,
        False,
        lambda r: {
            "id": str(r.id),
            "name": r.name,
            "route_distinguisher": r.route_distinguisher,
            "tags": r.tags or {},
        },
    ),
    "network_device": (
        NetworkDevice,
        False,
        lambda r: {
            "id": str(r.id),
            "name": r.name,
            "device_type": r.device_type,
            "tags": r.tags or {},
        },
    ),
    "domain": (
        Domain,
        False,
        lambda r: {"id": str(r.id), "name": r.name, "tags": r.tags or {}},
    ),
    "circuit": (
        Circuit,
        True,
        lambda r: {
            "id": str(r.id),
            "name": r.name,
            "ckt_id": r.ckt_id,
            "tags": r.tags or {},
        },
    ),
    "network_service": (
        NetworkService,
        True,
        lambda r: {
            "id": str(r.id),
            "name": r.name,
            "kind": r.kind,
            "tags": r.tags or {},
        },
    ),
    "overlay_network": (
        OverlayNetwork,
        True,
        lambda r: {
            "id": str(r.id),
            "name": r.name,
            "kind": r.kind,
            "tags": r.tags or {},
        },
    ),
    "dns_zone": (
        DNSZone,
        True,
        lambda r: {
            "id": str(r.id),
            "name": r.name,
            "zone_type": r.zone_type,
            "tags": r.tags or {},
        },
    ),
    "dns_record": (
        DNSRecord,
        True,
        lambda r: {
            "id": str(r.id),
            "fqdn": r.fqdn,
            "record_type": r.record_type,
            "value": r.value,
            "tags": r.tags or {},
        },
    ),
    "dhcp_scope": (
        DHCPScope,
        True,
        lambda r: {
            "id": str(r.id),
            "name": r.name,
            "tags": r.tags or {},
        },
    ),
    "dhcp_static_assignment": (
        DHCPStaticAssignment,
        False,
        lambda r: {
            "id": str(r.id),
            "ip_address": str(r.ip_address),
            "mac_address": str(r.mac_address),
            "hostname": r.hostname,
            "tags": r.tags or {},
        },
    ),
}

_TAG_KIND_DEFAULT = ["subnet", "ip_block", "ip_address", "ip_space"]


class FindByTagArgs(BaseModel):
    key: str = Field(description="Tag key — case-sensitive.")
    value: str | None = Field(
        default=None,
        description=(
            "Optional tag value. If omitted, matches any row where "
            "the key is present regardless of value."
        ),
    )
    resource_kinds: list[str] = Field(
        default_factory=lambda: list(_TAG_KIND_DEFAULT),
        description=(
            "Which resource kinds to search. Default is the four IPAM "
            "kinds (preserves prior behaviour). Pass an extended list "
            "to also search ASNs, VRFs, network devices, domains, "
            "circuits, network services, or overlay networks. "
            "Recognised values: " + ", ".join(_TAG_KIND_TABLE) + "."
        ),
    )
    limit_per_kind: int = Field(default=25, ge=1, le=100)


@register_tool(
    name="find_by_tag",
    description=(
        "Find tagged resources across IPAM, network modeling, DNS / "
        "DHCP scopes, etc. — every resource type that carries a "
        "JSONB ``tags`` column. Filters at the database with the "
        "JSONB ``?`` / ``@>`` operators (Postgres GIN-indexed). "
        "Useful for questions like 'what's tagged owner=alice?', "
        "'which subnets have env=prod?', or (with extended "
        "resource_kinds) 'find every ASN tagged carrier=lumen'."
    ),
    args_model=FindByTagArgs,
    category="ipam",
)
async def find_by_tag(db: AsyncSession, user: User, args: FindByTagArgs) -> dict[str, Any]:
    # Collapse (key, value) → the wire-level ``key:value`` form so the
    # AI tool and the REST endpoints route through the *same* helper —
    # any future change to the operator-facing tag syntax (case
    # folding, glob support, etc.) lands in one place.
    tag_param = f"{args.key}:{args.value}" if args.value is not None else args.key

    out: dict[str, list[dict[str, Any]]] = {}
    for kind in args.resource_kinds:
        entry = _TAG_KIND_TABLE.get(kind)
        if entry is None:
            out[kind] = [
                {"error": f"unknown resource kind {kind!r}; recognised: {sorted(_TAG_KIND_TABLE)}"}
            ]
            continue
        model, has_soft_delete, render = entry
        # ``model`` carries ``type`` from the dispatch table, which mypy
        # can't tighten into a ``Select[Any]`` for a generic select
        # call — annotate explicitly.
        stmt: Any = select(model)
        if has_soft_delete:
            stmt = stmt.where(model.deleted_at.is_(None))
        stmt = apply_tag_filter(stmt, model.tags, [tag_param])
        stmt = stmt.limit(args.limit_per_kind)
        rows = (await db.execute(stmt)).scalars().all()
        out[kind] = [render(r) for r in rows]
    return {"key": args.key, "value": args.value, "results": out}


# ── count_resources ───────────────────────────────────────────────────


class CountResourcesArgs(BaseModel):
    pass


@register_tool(
    name="count_ipam_resources",
    description=(
        "Total counts of IPAM resources — spaces, blocks, subnets, IP "
        "addresses, plus a breakdown of IP addresses by status. "
        "Equivalent to the dashboard's KPI ribbon. Use this when the "
        "operator asks 'how big is my deployment?' or 'how many "
        "subnets do I have?'."
    ),
    args_model=CountResourcesArgs,
    category="ipam",
)
async def count_ipam_resources(
    db: AsyncSession, user: User, args: CountResourcesArgs
) -> dict[str, Any]:
    space_count = await db.scalar(
        select(func.count(IPSpace.id)).where(IPSpace.deleted_at.is_(None))
    )
    block_count = await db.scalar(
        select(func.count(IPBlock.id)).where(IPBlock.deleted_at.is_(None))
    )
    subnet_count = await db.scalar(select(func.count(Subnet.id)).where(Subnet.deleted_at.is_(None)))
    ip_count = await db.scalar(select(func.count(IPAddress.id)))
    by_status_rows = (
        await db.execute(
            select(IPAddress.status, func.count(IPAddress.id)).group_by(IPAddress.status)
        )
    ).all()
    return {
        "ip_spaces": int(space_count or 0),
        "ip_blocks": int(block_count or 0),
        "subnets": int(subnet_count or 0),
        "ip_addresses": int(ip_count or 0),
        "ip_addresses_by_status": {row[0]: int(row[1]) for row in by_status_rows},
    }
