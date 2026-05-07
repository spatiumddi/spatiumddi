"""REST surface for BGP enrichment (issue #122).

Public, free upstream sources (RIPEstat + PeeringDB) — no API key
required, in-process cache absorbs repeated queries. Endpoints are
authenticated but not RBAC-gated; the data is public information,
the only reason to gate is to avoid abuse of our cache by
unauthenticated callers.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import CurrentUser
from app.services.bgp import (
    fetch_announced_prefixes,
    fetch_as_overview,
    fetch_asn_ixps,
    fetch_asn_network,
    fetch_prefix_overview,
    fetch_routing_history,
)

router = APIRouter()


def _validate_asn(asn: int) -> int:
    if asn < 0 or asn > 4_294_967_295:
        raise HTTPException(
            status_code=422,
            detail="asn must be a 32-bit unsigned integer (0 .. 2^32 - 1)",
        )
    return asn


def _validate_resource(resource: str) -> str:
    """Accept a v4/v6 IP or a CIDR block. Reject obvious garbage so
    we don't waste an HTTP call on a malformed input.
    """
    s = resource.strip()
    if not s:
        raise HTTPException(status_code=422, detail="resource is empty")
    try:
        if "/" in s:
            ipaddress.ip_network(s, strict=False)
        else:
            ipaddress.ip_address(s)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"resource '{s}' is not a valid IP or CIDR ({exc})",
        ) from exc
    return s


@router.get("/asn/{asn}/announced-prefixes")
async def asn_announced_prefixes(
    asn: int,
    _: CurrentUser,
) -> dict[str, Any]:
    return await fetch_announced_prefixes(_validate_asn(asn))


@router.get("/asn/{asn}/overview")
async def asn_overview(
    asn: int,
    _: CurrentUser,
) -> dict[str, Any]:
    return await fetch_as_overview(_validate_asn(asn))


@router.get("/asn/{asn}/network")
async def asn_network(
    asn: int,
    _: CurrentUser,
) -> dict[str, Any]:
    """PeeringDB network record — org metadata + peering policy."""
    return await fetch_asn_network(_validate_asn(asn))


@router.get("/asn/{asn}/ixps")
async def asn_ixps(
    asn: int,
    _: CurrentUser,
) -> dict[str, Any]:
    """IXP presence rollup from PeeringDB."""
    return await fetch_asn_ixps(_validate_asn(asn))


@router.get("/prefix/origin")
async def prefix_origin(
    _: CurrentUser,
    resource: str = Query(..., description="IPv4/v6 address or CIDR"),
) -> dict[str, Any]:
    """For a given IP or CIDR, who's announcing the enclosing
    prefix? Backed by RIPEstat ``prefix-overview``.
    """
    return await fetch_prefix_overview(_validate_resource(resource))


@router.get("/prefix/routing-history")
async def prefix_routing_history(
    _: CurrentUser,
    resource: str = Query(..., description="IPv4/v6 address or CIDR"),
) -> dict[str, Any]:
    """Timeline of origin-AS changes for the prefix. Catches
    re-homings and (in the worst case) hijack events.
    """
    return await fetch_routing_history(_validate_resource(resource))
