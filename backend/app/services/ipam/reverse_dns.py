"""Reverse-DNS (PTR) auto-population (issue #41).

A scheduled, platform-opt-in sweep that fills ``IPAddress.hostname`` for
operator-owned rows that have none, by issuing a PTR lookup against the
configured resolvers (or the worker's system resolvers when none are
configured).

For each resolved row:
  * ``hostname`` ← the short, leftmost label of the PTR FQDN
    (``server01`` from ``server01.corp.example.com``);
  * ``description`` ← the full PTR FQDN, **only when description is
    currently empty** so an operator's note is never clobbered. (The
    issue asks for the FQDN in ``description``; we deliberately keep the
    dedicated ``fqdn`` column out of it — that field is owned by the
    forward-DNS sync, which derives it from the assigned zone.)

Skipped:
  * rows whose hostname is authoritative from an upstream integration —
    any row carrying an integration provenance FK
    (docker / kubernetes / proxmox / tailscale / unifi) or
    ``auto_from_lease`` (a DHCP lease mirror);
  * unallocated / placeholder rows — only ``allocated`` / ``reserved`` /
    ``static_dhcp`` / ``discovered`` statuses are resolved.

The sweep only ever touches rows where ``hostname IS NULL`` so it never
overwrites an existing name; once filled, a row drops out of the next
sweep's candidate set.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ipam import IPAddress

# Statuses worth a PTR lookup — real allocations / sightings an operator
# would want named. ``dhcp`` (lease mirror) is excluded via
# ``auto_from_lease``; integration statuses are excluded via the
# provenance-FK guard in the query below.
REVERSE_DNS_CANDIDATE_STATUSES: frozenset[str] = frozenset(
    {"allocated", "reserved", "static_dhcp", "discovered"}
)

# Per-run safety caps — keep the sweep gentle on the resolver and bounded
# in wall-clock so a single beat tick can't run away.
DEFAULT_LIMIT = 256
DEFAULT_CONCURRENCY = 16
QUERY_TIMEOUT_SECONDS = 3.0


def short_label(fqdn: str) -> str:
    """Leftmost DNS label of a (possibly trailing-dot) FQDN."""
    return fqdn.rstrip(".").split(".", 1)[0]


def _build_resolver(resolvers: list[str] | None) -> Any:
    """An async dnspython resolver, optionally pinned to ``resolvers``.

    Returns None if dnspython can't be imported — defensive only; it's a
    hard dependency, but a missing import must never crash the worker.
    """
    try:
        import dns.asyncresolver  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    # configure=False skips reading /etc/resolv.conf when we're pinning an
    # explicit resolver list.
    r = dns.asyncresolver.Resolver(configure=not resolvers)
    if resolvers:
        r.nameservers = list(resolvers)
    r.lifetime = QUERY_TIMEOUT_SECONDS
    r.timeout = QUERY_TIMEOUT_SECONDS
    return r


async def resolve_ptr(ip: str, resolver: Any) -> str | None:
    """Return the PTR FQDN for ``ip`` (trailing dot stripped), or None."""
    try:
        import dns.reversename  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        rev = dns.reversename.from_address(ip)
        answer = await resolver.resolve(rev, "PTR")
    except Exception:  # noqa: BLE001 — NXDOMAIN / timeout / no-answer / bad IP
        return None
    for rdata in answer:
        name = rdata.to_text().rstrip(".")
        if name:
            return name
    return None


async def populate_reverse_dns(
    db: AsyncSession,
    *,
    resolvers: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> dict[str, int]:
    """Resolve + fill hostname/description for hostname-NULL candidate rows.

    Returns counts: ``scanned`` / ``resolved`` / ``updated`` / ``no_ptr``.
    The caller owns the transaction (commit + audit).
    """
    resolver = _build_resolver(resolvers)
    if resolver is None:
        return {"scanned": 0, "resolved": 0, "updated": 0, "no_ptr": 0}

    stmt = (
        select(IPAddress)
        .where(
            IPAddress.hostname.is_(None),
            IPAddress.auto_from_lease.is_(False),
            IPAddress.status.in_(REVERSE_DNS_CANDIDATE_STATUSES),
            IPAddress.docker_host_id.is_(None),
            IPAddress.kubernetes_cluster_id.is_(None),
            IPAddress.proxmox_node_id.is_(None),
            IPAddress.tailscale_tenant_id.is_(None),
            IPAddress.unifi_controller_id.is_(None),
        )
        .order_by(IPAddress.created_at.asc())
        .limit(max(1, min(limit, 5000)))
    )
    rows = list((await db.execute(stmt)).scalars().all())
    if not rows:
        return {"scanned": 0, "resolved": 0, "updated": 0, "no_ptr": 0}

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _lookup(ip_row: IPAddress) -> tuple[IPAddress, str | None]:
        async with sem:
            return ip_row, await resolve_ptr(str(ip_row.address), resolver)

    results = await asyncio.gather(*[_lookup(r) for r in rows])

    resolved = 0
    updated = 0
    for ip_row, raw in results:
        if not raw:
            continue
        fqdn = raw.rstrip(".")  # normalize even if the resolver kept the dot
        if not fqdn:
            continue
        resolved += 1
        # Re-check NULL in case a concurrent writer filled it mid-sweep.
        if ip_row.hostname:
            continue
        ip_row.hostname = short_label(fqdn)
        # Never clobber an operator note — only write the FQDN when blank.
        if not (ip_row.description or "").strip():
            ip_row.description = fqdn
        updated += 1

    return {
        "scanned": len(rows),
        "resolved": resolved,
        "updated": updated,
        "no_ptr": len(rows) - resolved,
    }


__all__ = [
    "REVERSE_DNS_CANDIDATE_STATUSES",
    "populate_reverse_dns",
    "resolve_ptr",
    "short_label",
]
