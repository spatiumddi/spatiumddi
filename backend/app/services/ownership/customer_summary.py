"""Owned-resource rollup for one customer (issue #917).

"Is this customer safe to decommission?" is answered by counting what they own
across nine unrelated tables. The copilot could do it in one turn
(``get_customer_summary``); over REST it was nine list calls against nine
routers — where a ``customer_id`` filter existed at all — because
``GET /customers/{id}`` returns the row and nothing else.

Shared by both surfaces so a decommission decision made from a phone and one
made from a chat window are looking at the same numbers.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asn import ASN
from app.models.circuit import Circuit
from app.models.dns import DNSZone
from app.models.domain import Domain
from app.models.ipam import IPBlock, IPSpace, Subnet
from app.models.network_service import NetworkService
from app.models.overlay import OverlayNetwork
from app.models.ownership import Customer


async def build_customer_summary(db: AsyncSession, customer: Customer) -> dict[str, Any]:
    """The customer row plus a count of every resource type it owns.

    Soft-deleted rows are excluded where the model supports it: a
    decommission check must not be blocked by resources already in the trash,
    and must not silently ignore ones that are not.
    """

    async def _count(model: Any, *, soft_delete: bool = False) -> int:
        stmt = select(func.count()).select_from(model).where(model.customer_id == customer.id)
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
        "id": str(customer.id),
        "name": customer.name,
        "account_number": customer.account_number,
        "status": customer.status,
        "contact_email": customer.contact_email,
        "contact_phone": customer.contact_phone,
        "contact_address": customer.contact_address,
        "notes": customer.notes,
        "owned_resources": counts,
        "owned_resource_total": sum(counts.values()),
        "tags": customer.tags or {},
        "created_at": customer.created_at.isoformat() if customer.created_at else None,
    }


__all__ = ["build_customer_summary"]
