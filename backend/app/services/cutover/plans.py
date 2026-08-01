"""Candidate discovery for a cutover plan (issue #756).

An operator building a plan should not have to *type* zone names and scope
ids that already exist on both sides of the migration. :func:`discover_candidates`
live-pulls the Windows source estate, matches each object against the
SpatiumDDI rows in the plan's target group, pre-computes the readiness
blockers, and hands back a pickable list.

Matching rules, and why:

* **Zones by name.** Windows reports ``corp.example``; SpatiumDDI stores
  ``corp.example.`` (FQDN with the trailing dot). Both sides are stripped of
  the trailing dot and lowercased before comparison. Name is the only stable
  identity across the two systems — there is no shared id.
* **Scopes by subnet CIDR.** Windows reports a ``scope_id`` (the network
  address) plus a ``subnet_cidr``; SpatiumDDI models a scope as a
  ``(group, subnet)`` pair. The subnet's CIDR is therefore the join key, run
  through :func:`ipaddress.ip_network` on both sides so ``10.0.20.0/24`` and a
  sloppier ``10.0.20.5/24`` land on the same canonical network.

The DNS and DHCP halves are independent: a plan may cover either one, and a
pull failure on one side is reported as an ``error`` string on that side while
the other still returns its candidates. Discovery is a read-only convenience —
losing half of it must not cost the operator the other half.
"""

from __future__ import annotations

import ipaddress
import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.drivers.dhcp.windows import WindowsDHCPReadOnlyDriver
from app.drivers.dns import get_driver
from app.models.cutover import CutoverItem, CutoverPlan
from app.models.dhcp import DHCPScope, DHCPServer
from app.models.dns import DNSServer, DNSZone
from app.models.ipam import Subnet
from app.services.cutover import readiness
from app.services.cutover.canonical import Blocker

logger = structlog.get_logger(__name__)

#: Windows DNS drivers we know how to pull a zone topology from. Anything else
#: on ``DNSServer.driver`` is refused up front with an operator-readable
#: message rather than an ``AttributeError`` from the driver call.
_PULLABLE_DNS_DRIVERS: frozenset[str] = frozenset({"windows_dns"})

#: Same for the DHCP side.
_PULLABLE_DHCP_DRIVERS: frozenset[str] = frozenset({"windows_dhcp"})


def _normalise_zone_name(raw: str) -> str:
    """``"Corp.Example."`` → ``"corp.example"``.

    Windows and SpatiumDDI disagree about the trailing dot and about case;
    neither disagreement is meaningful, so both are normalised away before the
    two sides are compared.
    """
    return raw.strip().rstrip(".").lower()


def _canonical_cidr(raw: str) -> str | None:
    """Canonical network string, or ``None`` when ``raw`` isn't a network."""
    try:
        return str(ipaddress.ip_network(raw, strict=False))
    except ValueError:
        # Non-CIDR junk from the wire (empty string, a hostname, a v6 literal
        # on a v4-only pull). Unmatched is the correct outcome, not a 500.
        return None


def _transient_item(
    plan: CutoverPlan,
    *,
    kind: str,
    source_ref: str,
    zone_id: uuid.UUID | None = None,
    scope_id: uuid.UUID | None = None,
) -> CutoverItem:
    """Build an unpersisted :class:`CutoverItem` for blocker evaluation.

    A candidate is by definition not yet in the plan, but the readiness
    evaluators are written against an item. The object is deliberately never
    added to the session and never attached to ``plan.items``, so nothing here
    can be flushed: it exists only to carry the four fields the evaluators
    read. Column defaults are applied at flush time, so ``stage`` / ``blockers``
    are set explicitly rather than left as ``None``.
    """
    return CutoverItem(
        plan_id=plan.id,
        kind=kind,
        source_ref=source_ref,
        zone_id=zone_id,
        scope_id=scope_id,
        stage="pending",
        blockers=[],
        notes="",
    )


async def _safe_blockers(
    db: AsyncSession,
    *,
    plan: CutoverPlan,
    item: CutoverItem,
    zone: DNSZone | None = None,
    scope: DHCPScope | None = None,
    source_zone_meta: dict[str, Any] | None = None,
    source_scope: dict[str, Any] | None = None,
) -> list[Blocker]:
    """Evaluate one candidate's blockers, never raising.

    Discovery lists tens to hundreds of candidates in one call; a single
    evaluator failure (a half-populated zone row, a driver quirk in the pulled
    metadata) must degrade that one row rather than the whole list. The failure
    is surfaced in-band as a ``warn`` so the operator sees that the readiness
    verdict is incomplete instead of reading an empty list as "all clear".
    """
    try:
        if item.kind == "dns_zone":
            return await readiness.evaluate_dns_item(
                db, plan=plan, item=item, zone=zone, source_zone_meta=source_zone_meta
            )
        return await readiness.evaluate_dhcp_item(
            db, plan=plan, item=item, scope=scope, source_scope=source_scope
        )
    except Exception as exc:  # noqa: BLE001 — one bad candidate must not kill discovery
        logger.warning(
            "cutover.discover.blocker_eval_failed",
            plan_id=str(plan.id),
            kind=item.kind,
            source_ref=item.source_ref,
            error=str(exc),
        )
        return [
            Blocker(
                code="readiness_eval_failed",
                severity="warn",
                message=(
                    f"Could not evaluate readiness for {item.source_ref!r}: {exc}. "
                    "Add the item and re-run the parity check to get a real verdict."
                ),
            )
        ]


# ── DNS ────────────────────────────────────────────────────────────────


async def _target_zones(db: AsyncSession, group_id: uuid.UUID) -> dict[str, DNSZone]:
    """Live zones in the target group, keyed by normalised name.

    A split-horizon group holds the same name once per view. Only one of them
    can be the cutover target, so the first is kept and the readiness layer
    raises ``target_zone_view_scoped`` on it — surfacing the ambiguity to the
    operator beats silently picking a view.
    """
    rows = (await db.execute(select(DNSZone).where(DNSZone.group_id == group_id))).scalars().all()
    out: dict[str, DNSZone] = {}
    for zone in rows:
        out.setdefault(_normalise_zone_name(zone.name), zone)
    return out


async def _discover_dns(
    db: AsyncSession, *, plan: CutoverPlan, existing: set[tuple[str, str]]
) -> dict[str, Any]:
    """Pull the Windows zone topology and match it against the target group."""
    side: dict[str, Any] = {
        "available": False,
        "error": None,
        "source_server": None,
        "target_group_id": str(plan.target_dns_group_id) if plan.target_dns_group_id else None,
        "candidates": [],
    }
    if plan.source_dns_server_id is None:
        side["error"] = "This plan has no source Windows DNS server."
        return side

    server = await db.get(DNSServer, plan.source_dns_server_id)
    if server is None:
        side["error"] = "The plan's source DNS server no longer exists."
        return side

    side["available"] = True
    side["source_server"] = server.name
    if server.driver not in _PULLABLE_DNS_DRIVERS:
        side["error"] = (
            f"Zone discovery needs a Windows DNS source server; {server.name!r} uses the "
            f"{server.driver!r} driver."
        )
        return side

    try:
        zone_meta: list[dict[str, Any]] = await get_driver(server.driver).pull_zones_from_server(
            server
        )
    except Exception as exc:  # noqa: BLE001 — WinRM / auth / PowerShell errors go to the operator
        logger.warning(
            "cutover.discover.dns_pull_failed",
            plan_id=str(plan.id),
            server=server.name,
            error=str(exc),
        )
        side["error"] = f"Could not list zones on {server.name}: {exc}"
        return side

    targets = (
        await _target_zones(db, plan.target_dns_group_id)
        if plan.target_dns_group_id is not None
        else {}
    )

    candidates: list[dict[str, Any]] = []
    for meta in zone_meta:
        raw_name = str(meta.get("name") or "").strip()
        if not raw_name:
            continue
        source_ref = raw_name.rstrip(".")
        zone = targets.get(_normalise_zone_name(raw_name))
        item = _transient_item(
            plan,
            kind="dns_zone",
            source_ref=source_ref,
            zone_id=zone.id if zone is not None else None,
        )
        blockers = await _safe_blockers(db, plan=plan, item=item, zone=zone, source_zone_meta=meta)
        candidates.append(
            {
                "kind": "dns_zone",
                "source_ref": source_ref,
                "display_name": source_ref,
                "matched_id": str(zone.id) if zone is not None else None,
                "matched_display": zone.name if zone is not None else None,
                "already_in_plan": ("dns_zone", source_ref) in existing,
                "blockers": [b.as_dict() for b in blockers],
                "source": {
                    "zone_type": meta.get("zone_type"),
                    "is_ad_integrated": meta.get("is_ad_integrated"),
                    "is_reverse_lookup": meta.get("is_reverse_lookup"),
                    "dynamic_update": meta.get("dynamic_update"),
                },
            }
        )

    candidates.sort(key=lambda c: str(c["source_ref"]))
    side["candidates"] = candidates
    return side


# ── DHCP ───────────────────────────────────────────────────────────────


async def _target_scopes(db: AsyncSession, group_id: uuid.UUID) -> dict[str, DHCPScope]:
    """Live scopes in the target group, keyed by canonical subnet CIDR.

    ``DHCPScope`` has no ``subnet`` relationship, so the CIDR is fetched in a
    second, column-only query rather than a join — a join would put ``Subnet``
    in the same statement as the soft-delete-filtered ``DHCPScope`` primary
    entity, and the two have independent soft-delete state.
    """
    scopes = (
        (await db.execute(select(DHCPScope).where(DHCPScope.group_id == group_id))).scalars().all()
    )
    subnet_ids = {s.subnet_id for s in scopes}
    if not subnet_ids:
        return {}
    networks: dict[uuid.UUID, str] = {
        row[0]: str(row[1])
        for row in (
            await db.execute(select(Subnet.id, Subnet.network).where(Subnet.id.in_(subnet_ids)))
        ).all()
    }

    out: dict[str, DHCPScope] = {}
    for scope in scopes:
        raw = networks.get(scope.subnet_id)
        if raw is None:
            # Subnet is soft-deleted (filtered out of the second query) — the
            # scope has no addressable CIDR to match a Windows scope against.
            continue
        cidr = _canonical_cidr(raw)
        if cidr is not None:
            out.setdefault(cidr, scope)
    return out


async def _discover_dhcp(
    db: AsyncSession, *, plan: CutoverPlan, existing: set[tuple[str, str]]
) -> dict[str, Any]:
    """Pull the Windows scope topology and match it against the target group."""
    side: dict[str, Any] = {
        "available": False,
        "error": None,
        "source_server": None,
        "target_group_id": str(plan.target_dhcp_group_id) if plan.target_dhcp_group_id else None,
        "candidates": [],
    }
    if plan.source_dhcp_server_id is None:
        side["error"] = "This plan has no source Windows DHCP server."
        return side

    server = await db.get(DHCPServer, plan.source_dhcp_server_id)
    if server is None:
        side["error"] = "The plan's source DHCP server no longer exists."
        return side

    side["available"] = True
    side["source_server"] = server.name
    if server.driver not in _PULLABLE_DHCP_DRIVERS:
        side["error"] = (
            f"Scope discovery needs a Windows DHCP source server; {server.name!r} uses the "
            f"{server.driver!r} driver."
        )
        return side

    try:
        raw_scopes: list[dict[str, Any]] = await WindowsDHCPReadOnlyDriver().get_scopes(server)
    except Exception as exc:  # noqa: BLE001 — WinRM / auth / PowerShell errors go to the operator
        logger.warning(
            "cutover.discover.dhcp_pull_failed",
            plan_id=str(plan.id),
            server=server.name,
            error=str(exc),
        )
        side["error"] = f"Could not list scopes on {server.name}: {exc}"
        return side

    targets = (
        await _target_scopes(db, plan.target_dhcp_group_id)
        if plan.target_dhcp_group_id is not None
        else {}
    )

    candidates: list[dict[str, Any]] = []
    for raw in raw_scopes:
        source_ref = str(raw.get("scope_id") or "").strip()
        cidr = _canonical_cidr(str(raw.get("subnet_cidr") or ""))
        if not source_ref:
            # ``scope_id`` is the item's identity on the Windows side; without
            # it there is nothing stable to add to the plan. Fall back to the
            # network address when the CIDR came through.
            if cidr is None:
                continue
            source_ref = cidr.split("/")[0]
        scope = targets.get(cidr) if cidr is not None else None
        item = _transient_item(
            plan,
            kind="dhcp_scope",
            source_ref=source_ref,
            scope_id=scope.id if scope is not None else None,
        )
        blockers = await _safe_blockers(db, plan=plan, item=item, scope=scope, source_scope=raw)
        name = str(raw.get("name") or "")
        candidates.append(
            {
                "kind": "dhcp_scope",
                "source_ref": source_ref,
                "display_name": f"{cidr or source_ref}" + (f" — {name}" if name else ""),
                "matched_id": str(scope.id) if scope is not None else None,
                "matched_display": cidr if scope is not None else None,
                "already_in_plan": ("dhcp_scope", source_ref) in existing,
                "blockers": [b.as_dict() for b in blockers],
                "source": {
                    "subnet_cidr": cidr,
                    "name": name,
                    "is_active": raw.get("is_active"),
                    "lease_time": raw.get("lease_time"),
                    "pool_count": len(raw.get("pools") or []),
                    "reservation_count": len(raw.get("statics") or []),
                },
            }
        )

    candidates.sort(key=lambda c: str(c["source_ref"]))
    side["candidates"] = candidates
    return side


# ── public entry point ─────────────────────────────────────────────────


async def discover_candidates(db: AsyncSession, *, plan: CutoverPlan) -> dict[str, Any]:
    """Live-pull the Windows source estate and return addable plan candidates.

    Returns ``{"dns": {…}, "dhcp": {…}}``. Each side carries:

    ``available``
        The plan names a source server of the right kind and it still exists.
        ``False`` means this half of the plan simply isn't configured — a
        DNS-only plan reports ``available=False`` on the DHCP side, which is
        not an error state.
    ``error``
        Why the side produced no candidates, in operator-readable prose.
        Populated for a missing source server, a non-Windows driver, or a
        failed pull. Never raised — the sides are independent, and a WinRM
        timeout on DHCP must not cost the operator their zone list.
    ``source_server`` / ``target_group_id``
        What was pulled from, and what it was matched against.
    ``candidates``
        One entry per object on the Windows side, sorted by ``source_ref``,
        each with its matched SpatiumDDI id (or ``None``), whether it is
        already an item on this plan, and its pre-computed blockers.

    Read-only: nothing here writes to the database.
    """
    existing_rows = (
        await db.execute(
            select(CutoverItem.kind, CutoverItem.source_ref).where(CutoverItem.plan_id == plan.id)
        )
    ).all()
    existing: set[tuple[str, str]] = {(str(row[0]), str(row[1])) for row in existing_rows}

    # Sequential, not gathered: both halves talk WinRM to the same estate over
    # the same operator credentials, and a Windows box handles two concurrent
    # PowerShell remoting sessions worse than it handles two sequential ones.
    return {
        "dns": await _discover_dns(db, plan=plan, existing=existing),
        "dhcp": await _discover_dhcp(db, plan=plan, existing=existing),
    }
