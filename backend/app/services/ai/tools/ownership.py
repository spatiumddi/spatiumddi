"""Tier 2 ownership read tools for the Operator Copilot (issue #101).

Surfaces the logical-ownership entities — Customer / Site / Provider —
to the LLM so operators can ask "which subnets does customer X own?",
"what's at the NYC site?", and "which circuits does Cogent supply us?"
without falling through to the catch-all "I can't answer that".

Soft-delete aware: ``Customer`` is soft-deletable so list / summary
filter on ``deleted_at IS NULL``. Site + Provider are hard-deleted so
no soft-delete gate.

The ``get_customer_summary`` tool aggregates counts of every owned
resource type via per-table COUNT queries — small N (operators have
~tens of customers, each owning ~tens of rows), so 8 quick COUNTs
beat one mega-JOIN for clarity.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asn import ASN
from app.models.auth import User
from app.models.circuit import Circuit
from app.models.dns import DNSZone
from app.models.domain import Domain
from app.models.ipam import IPBlock, IPSpace, Subnet
from app.models.network_service import NetworkService
from app.models.overlay import OverlayNetwork
from app.models.ownership import (
    CUSTOMER_STATUSES,
    PROVIDER_KINDS,
    SITE_KINDS,
    Customer,
    Provider,
    Site,
)
from app.services.ai.tools.base import register_tool


def _try_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


async def _resolve_customer(db: AsyncSession, ref: str) -> Customer | None:
    """Look up by UUID first, else case-insensitive name match. Returns
    ``None`` if neither path finds an active customer."""
    u = _try_uuid(ref)
    if u is not None:
        row = await db.get(Customer, u)
        if row is not None and row.deleted_at is None:
            return row
        return None
    stmt = (
        select(Customer)
        .where(Customer.deleted_at.is_(None))
        .where(func.lower(Customer.name) == ref.lower())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


# ── list_customers ────────────────────────────────────────────────────


class ListCustomersArgs(BaseModel):
    search: str | None = Field(
        default=None,
        description="Substring match on customer name, account number, or contact email.",
    )
    status: Literal["active", "inactive", "decommissioning"] | None = Field(
        default=None, description="Filter by lifecycle status."
    )
    limit: int = Field(default=50, ge=1, le=500)


@register_tool(
    name="list_customers",
    module="network.customer",
    description=(
        "List customers (logical owners of network resources). Use for "
        "questions about who owns what, account numbers, contact info, "
        "or status. Each row carries id, name, account_number, status, "
        "contact_email/phone, and tag count. For a deep roll-up of one "
        "customer's owned subnets / circuits / services / zones, call "
        "``get_customer_summary``."
    ),
    args_model=ListCustomersArgs,
    category="ownership",
)
async def list_customers(
    db: AsyncSession, user: User, args: ListCustomersArgs
) -> list[dict[str, Any]]:
    stmt = select(Customer).where(Customer.deleted_at.is_(None))
    if args.search:
        like = f"%{args.search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Customer.name).like(like),
                func.lower(Customer.account_number).like(like),
                func.lower(Customer.contact_email).like(like),
            )
        )
    if args.status:
        if args.status not in CUSTOMER_STATUSES:
            return [{"error": f"unknown status {args.status!r}"}]
        stmt = stmt.where(Customer.status == args.status)
    stmt = stmt.order_by(Customer.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "account_number": r.account_number,
            "status": r.status,
            "contact_email": r.contact_email,
            "contact_phone": r.contact_phone,
            "tag_count": len(r.tags or {}),
        }
        for r in rows
    ]


# ── get_customer_summary ──────────────────────────────────────────────


class GetCustomerSummaryArgs(BaseModel):
    customer_ref: str = Field(
        description=(
            "Customer UUID or exact (case-insensitive) name. Use "
            "``list_customers`` first if you only have a substring."
        )
    )


@register_tool(
    name="get_customer_summary",
    module="network.customer",
    description=(
        "Full detail of one customer plus a count of every owned "
        "resource type — IP spaces / blocks / subnets, circuits, "
        "services, ASNs, DNS zones, domains, overlay networks. Use "
        "to answer 'how much does customer X own' or 'is this "
        "customer safe to decommission'. ``customer_ref`` accepts a "
        "UUID or the exact customer name."
    ),
    args_model=GetCustomerSummaryArgs,
    category="ownership",
)
async def get_customer_summary(
    db: AsyncSession, user: User, args: GetCustomerSummaryArgs
) -> dict[str, Any]:
    cust = await _resolve_customer(db, args.customer_ref)
    if cust is None:
        return {"error": f"No active customer matched {args.customer_ref!r}."}

    async def _count(model: Any, *, soft_delete: bool = False) -> int:
        stmt = select(func.count()).select_from(model).where(model.customer_id == cust.id)
        if soft_delete:
            stmt = stmt.where(model.deleted_at.is_(None))
        return int((await db.execute(stmt)).scalar_one())

    counts = {
        "ip_spaces": await _count(IPSpace, soft_delete=True),
        "ip_blocks": await _count(IPBlock, soft_delete=True),
        "subnets": await _count(Subnet, soft_delete=True),
        "circuits": await _count(Circuit, soft_delete=True),
        "services": await _count(NetworkService, soft_delete=True),
        "asns": await _count(ASN),
        "dns_zones": await _count(DNSZone),
        "domains": await _count(Domain),
        "overlays": await _count(OverlayNetwork, soft_delete=True),
    }
    return {
        "id": str(cust.id),
        "name": cust.name,
        "account_number": cust.account_number,
        "status": cust.status,
        "contact_email": cust.contact_email,
        "contact_phone": cust.contact_phone,
        "contact_address": cust.contact_address,
        "notes": cust.notes,
        "owned_resources": counts,
        "owned_resource_total": sum(counts.values()),
        "tags": cust.tags or {},
        "created_at": cust.created_at.isoformat() if cust.created_at else None,
    }


# ── list_sites ────────────────────────────────────────────────────────


class ListSitesArgs(BaseModel):
    search: str | None = Field(
        default=None,
        description="Substring match on site name or code.",
    )
    kind: str | None = Field(
        default=None,
        description=(
            "Filter by site kind: datacenter / branch / pop / colo / "
            "cloud_region / customer_premise."
        ),
    )
    region: str | None = Field(
        default=None,
        description="Filter by free-form geo region (e.g. 'us-east-1', 'EMEA').",
    )
    limit: int = Field(default=50, ge=1, le=500)


@register_tool(
    name="list_sites",
    module="network.site",
    description=(
        "List sites (physical / logical locations). Use for "
        "'what's at NYC?', 'list all branch sites', or to enumerate "
        "the parent → child site hierarchy. Each row carries id, "
        "name, code, kind, region, parent_site_id (when nested), and "
        "tag count."
    ),
    args_model=ListSitesArgs,
    category="ownership",
)
async def list_sites(db: AsyncSession, user: User, args: ListSitesArgs) -> list[dict[str, Any]]:
    stmt = select(Site)
    if args.search:
        like = f"%{args.search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Site.name).like(like),
                func.lower(Site.code).like(like),
            )
        )
    if args.kind:
        if args.kind not in SITE_KINDS:
            return [{"error": f"unknown site kind {args.kind!r}"}]
        stmt = stmt.where(Site.kind == args.kind)
    if args.region:
        stmt = stmt.where(func.lower(Site.region) == args.region.lower())
    stmt = stmt.order_by(Site.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "code": r.code,
            "kind": r.kind,
            "region": r.region,
            "parent_site_id": str(r.parent_site_id) if r.parent_site_id else None,
            "tag_count": len(r.tags or {}),
        }
        for r in rows
    ]


# ── list_providers ────────────────────────────────────────────────────


class ListProvidersArgs(BaseModel):
    search: str | None = Field(
        default=None,
        description="Substring match on provider name, account number, or contact email.",
    )
    kind: str | None = Field(
        default=None,
        description=(
            "Filter by kind: transit / peering / carrier / cloud / " "registrar / sdwan_vendor."
        ),
    )
    limit: int = Field(default=50, ge=1, le=500)


@register_tool(
    name="list_providers",
    module="network.provider",
    description=(
        "List external providers (transit ISPs, peering partners, "
        "cloud, carriers, domain registrars). Use for 'who supplies "
        "circuit X', 'list our transit providers', or 'find the "
        "registrar for example.com'. Each row carries id, name, "
        "kind, account_number, contact info, and the optional default "
        "ASN linkage."
    ),
    args_model=ListProvidersArgs,
    category="ownership",
)
async def list_providers(
    db: AsyncSession, user: User, args: ListProvidersArgs
) -> list[dict[str, Any]]:
    stmt = select(Provider)
    if args.search:
        like = f"%{args.search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Provider.name).like(like),
                func.lower(Provider.account_number).like(like),
                func.lower(Provider.contact_email).like(like),
            )
        )
    if args.kind:
        if args.kind not in PROVIDER_KINDS:
            return [{"error": f"unknown provider kind {args.kind!r}"}]
        stmt = stmt.where(Provider.kind == args.kind)
    stmt = stmt.order_by(Provider.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "kind": r.kind,
            "account_number": r.account_number,
            "contact_email": r.contact_email,
            "contact_phone": r.contact_phone,
            "default_asn_id": str(r.default_asn_id) if r.default_asn_id else None,
            "tag_count": len(r.tags or {}),
        }
        for r in rows
    ]
