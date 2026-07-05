"""BGP Looking Glass — operator CRUD + read surface (issue #566).

Mounted at ``/looking-glass`` (NOT ``/bgp`` — that's the #527 public-table
hijack monitor router, gated on the ``network.asn`` module). This router is
gated on its own ``network.looking_glass`` feature module by the top-level
``app.api.v1.router`` include (not wired here — see that file).

Endpoints:

* ``/collectors`` — list/get/rename/enable/disable/delete. Registration is
  agent-side (``POST /looking-glass/agents/register`` in ``agents.py``);
  operators never create a collector row directly. Deleting a collector
  cascades its peers (``bgp_lg_peer.collector_id`` FK ``ON DELETE CASCADE``)
  and, transitively, their routes (``bgp_lg_route.peer_id`` FK
  ``ON DELETE CASCADE``) — no manual cleanup needed here.
* ``/peers`` — full CRUD on a configured BGP session. The MD5 password is
  Fernet-encrypted on write and never returned in plaintext; responses carry
  ``md5_password_set: bool`` instead. Every mutation collects a wake on the
  owning collector's channel (post-commit flush is the caller's
  ``wake_publishing`` router dependency, wired by the integrator) so the
  collector's ConfigBundle long-poll re-fetches its peer set promptly
  instead of waiting for the 12s safety tick.
* ``/sessions`` — read-only per-peer runtime-state rollup (collector +
  peer joined), the Sessions-tab feed.
* ``/routes`` — the learned RIB, server-paginated + filterable. ``/routes``
  (list) and ``/routes/by-prefix?prefix=`` (all paths for one exact prefix,
  the CIDR passed as a query param so its slash / IPv6 colons encode cleanly)
  are distinct URL shapes so there's no route collision.

Every write handler writes an ``AuditLog`` row before ``commit()`` per
CLAUDE.md non-negotiable #4.
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DB, CurrentUser
from app.core.agent_wake import collect_wake, looking_glass_collector_channel
from app.core.crypto import encrypt_str
from app.core.permissions import require_resource_permission
from app.models.asn import ASN
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.bgp_looking_glass import BGPLGPeer, BGPLGRoute, LookingGlassCollector
from app.models.network import NetworkDevice

from .schemas import (
    CollectorRead,
    CollectorUpdate,
    PeerCreate,
    PeerRead,
    PeerUpdate,
    RouteListResponse,
    RouteRead,
    SessionRead,
)

router = APIRouter(
    tags=["looking-glass"],
    dependencies=[Depends(require_resource_permission("bgp_lg_peer"))],
)


# ── Audit helper (mirrors app.api.v1.dhcp._audit.write_audit) ──────────


def _write_audit(
    db: AsyncSession,
    *,
    user: User | None,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    resource_display: str,
    changed_fields: list[str] | None = None,
    new_value: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            user_display_name=user.display_name if user else "system",
            auth_source=getattr(user, "auth_source", "local") or "local",
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            resource_display=resource_display,
            changed_fields=changed_fields,
            new_value=new_value,
        )
    )


# ── Collectors ──────────────────────────────────────────────────────────


def _collector_to_read(c: LookingGlassCollector) -> CollectorRead:
    return CollectorRead.model_validate(c)


@router.get("/collectors", response_model=list[CollectorRead])
async def list_collectors(db: DB, _: CurrentUser) -> list[CollectorRead]:
    rows = (
        (await db.execute(select(LookingGlassCollector).order_by(LookingGlassCollector.name)))
        .scalars()
        .all()
    )
    return [_collector_to_read(c) for c in rows]


@router.get("/collectors/{collector_id}", response_model=CollectorRead)
async def get_collector(collector_id: uuid.UUID, db: DB, _: CurrentUser) -> CollectorRead:
    c = await db.get(LookingGlassCollector, collector_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Collector not found")
    return _collector_to_read(c)


@router.patch("/collectors/{collector_id}", response_model=CollectorRead)
async def update_collector(
    collector_id: uuid.UUID, body: CollectorUpdate, db: DB, user: CurrentUser
) -> CollectorRead:
    c = await db.get(LookingGlassCollector, collector_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Collector not found")

    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(c, k, v)

    _write_audit(
        db,
        user=user,
        action="update",
        resource_type="looking_glass_collector",
        resource_id=c.id,
        resource_display=c.name,
        changed_fields=list(changes.keys()),
        new_value=changes,
    )
    await db.commit()
    await db.refresh(c)
    return _collector_to_read(c)


@router.delete(
    "/collectors/{collector_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_collector(collector_id: uuid.UUID, db: DB, user: CurrentUser) -> None:
    c = await db.get(LookingGlassCollector, collector_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Collector not found")

    _write_audit(
        db,
        user=user,
        action="delete",
        resource_type="looking_glass_collector",
        resource_id=c.id,
        resource_display=c.name,
    )
    # FK CASCADE sweeps bgp_lg_peer rows (and, transitively, their
    # bgp_lg_route rows) — no manual cleanup needed.
    await db.delete(c)
    await db.commit()
    return None


# ── Peers ───────────────────────────────────────────────────────────────


def _peer_to_read(p: BGPLGPeer) -> PeerRead:
    return PeerRead(
        id=p.id,
        name=p.name,
        collector_id=p.collector_id,
        local_asn=p.local_asn,
        peer_asn=p.peer_asn,
        peer_address=p.peer_address,
        matched_asn_id=p.matched_asn_id,
        peer_router_id=p.peer_router_id,
        address_families=list(p.address_families or []),
        md5_password_set=bool(p.md5_password_encrypted),
        max_prefixes=p.max_prefixes,
        import_filter=p.import_filter or {"mode": "accept_all"},
        enabled=p.enabled,
        description=p.description,
        session_state=p.session_state,
        uptime_started_at=p.uptime_started_at,
        prefixes_received=p.prefixes_received,
        prefixes_accepted=p.prefixes_accepted,
        last_state_change=p.last_state_change,
        last_flap_at=p.last_flap_at,
        rpki_invalid_count=p.rpki_invalid_count,
        down_since=p.down_since,
        created_at=p.created_at,
        modified_at=p.modified_at,
    )


@router.get("/peers", response_model=list[PeerRead])
async def list_peers(
    db: DB, _: CurrentUser, collector_id: uuid.UUID | None = None
) -> list[PeerRead]:
    q = select(BGPLGPeer).order_by(BGPLGPeer.name)
    if collector_id is not None:
        q = q.where(BGPLGPeer.collector_id == collector_id)
    rows = (await db.execute(q)).scalars().all()
    return [_peer_to_read(p) for p in rows]


@router.post("/peers", response_model=PeerRead, status_code=status.HTTP_201_CREATED)
async def create_peer(body: PeerCreate, db: DB, user: CurrentUser) -> PeerRead:
    if (await db.get(LookingGlassCollector, body.collector_id)) is None:
        raise HTTPException(status_code=422, detail="collector_id not found")
    if body.matched_asn_id is not None and (await db.get(ASN, body.matched_asn_id)) is None:
        raise HTTPException(status_code=422, detail="matched_asn_id not found")
    if (
        body.peer_router_id is not None
        and (await db.get(NetworkDevice, body.peer_router_id)) is None
    ):
        raise HTTPException(status_code=422, detail="peer_router_id not found")

    payload = body.model_dump(exclude={"md5_password"})
    p = BGPLGPeer(**payload)
    if body.md5_password:
        p.md5_password_encrypted = encrypt_str(body.md5_password)
    db.add(p)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A peer for {body.peer_address} already exists on this collector.",
        ) from exc

    audit_payload = body.model_dump(mode="json", exclude={"md5_password"})
    audit_payload["md5_password_set"] = bool(body.md5_password)
    _write_audit(
        db,
        user=user,
        action="create",
        resource_type="bgp_lg_peer",
        resource_id=p.id,
        resource_display=p.name,
        new_value=audit_payload,
    )
    collect_wake(looking_glass_collector_channel(p.collector_id))
    await db.commit()
    await db.refresh(p)
    return _peer_to_read(p)


@router.get("/peers/{peer_id}", response_model=PeerRead)
async def get_peer(peer_id: uuid.UUID, db: DB, _: CurrentUser) -> PeerRead:
    p = await db.get(BGPLGPeer, peer_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Peer not found")
    return _peer_to_read(p)


@router.patch("/peers/{peer_id}", response_model=PeerRead)
async def update_peer(peer_id: uuid.UUID, body: PeerUpdate, db: DB, user: CurrentUser) -> PeerRead:
    p = await db.get(BGPLGPeer, peer_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Peer not found")

    old_collector_id = p.collector_id
    was_enabled = p.enabled

    raw_changes = body.model_dump(exclude_unset=True)
    md5_password = raw_changes.pop("md5_password", None)
    changes = raw_changes

    if (
        "collector_id" in changes
        and (await db.get(LookingGlassCollector, changes["collector_id"])) is None
    ):
        raise HTTPException(status_code=422, detail="collector_id not found")
    if (
        changes.get("matched_asn_id") is not None
        and (await db.get(ASN, changes["matched_asn_id"])) is None
    ):
        raise HTTPException(status_code=422, detail="matched_asn_id not found")
    if (
        changes.get("peer_router_id") is not None
        and (await db.get(NetworkDevice, changes["peer_router_id"])) is None
    ):
        raise HTTPException(status_code=422, detail="peer_router_id not found")

    for k, v in changes.items():
        setattr(p, k, v)

    # md5_password: truthy -> rotate; "" -> clear; omitted/None -> keep
    # (mirrors NetworkDeviceUpdate's secret-rotation convention).
    changed_fields = list(changes.keys())
    if md5_password:
        p.md5_password_encrypted = encrypt_str(md5_password)
        changed_fields.append("md5_password_rotated")
    elif md5_password == "":
        p.md5_password_encrypted = None
        changed_fields.append("md5_password_cleared")

    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A peer for {p.peer_address} already exists on this collector.",
        ) from exc

    # Disabling a peer removes it from the config bundle, so the collector
    # stops pushing for it and absence-reconcile never runs — its learned
    # routes would otherwise stay "active" forever. Mark them withdrawn now.
    # (Deleting a peer instead sweeps its routes via FK CASCADE.)
    if was_enabled and not p.enabled:
        await db.execute(
            update(BGPLGRoute)
            .where(BGPLGRoute.peer_id == p.id, BGPLGRoute.withdrawn_at.is_(None))
            .values(withdrawn_at=datetime.now(UTC))
        )

    audit_payload = body.model_dump(mode="json", exclude_unset=True, exclude={"md5_password"})
    _write_audit(
        db,
        user=user,
        action="update",
        resource_type="bgp_lg_peer",
        resource_id=p.id,
        resource_display=p.name,
        changed_fields=changed_fields,
        new_value=audit_payload,
    )
    # Wake the (possibly new) owning collector; if the peer moved to a
    # different collector, also wake the old one so it drops the peer.
    collect_wake(looking_glass_collector_channel(p.collector_id))
    if p.collector_id != old_collector_id:
        collect_wake(looking_glass_collector_channel(old_collector_id))
    await db.commit()
    await db.refresh(p)
    return _peer_to_read(p)


@router.delete("/peers/{peer_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_peer(peer_id: uuid.UUID, db: DB, user: CurrentUser) -> None:
    p = await db.get(BGPLGPeer, peer_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Peer not found")

    _write_audit(
        db,
        user=user,
        action="delete",
        resource_type="bgp_lg_peer",
        resource_id=p.id,
        resource_display=p.name,
    )
    collector_id = p.collector_id
    # FK CASCADE sweeps this peer's bgp_lg_route rows.
    await db.delete(p)
    collect_wake(looking_glass_collector_channel(collector_id))
    await db.commit()
    return None


# ── Sessions (read-only per-peer state rollup) ───────────────────────────


@router.get("/sessions", response_model=list[SessionRead])
async def list_sessions(
    db: DB, _: CurrentUser, collector_id: uuid.UUID | None = None
) -> list[SessionRead]:
    q = (
        select(BGPLGPeer, LookingGlassCollector)
        .join(LookingGlassCollector, LookingGlassCollector.id == BGPLGPeer.collector_id)
        .order_by(LookingGlassCollector.name, BGPLGPeer.name)
    )
    if collector_id is not None:
        q = q.where(BGPLGPeer.collector_id == collector_id)
    rows = (await db.execute(q)).all()
    return [
        SessionRead(
            peer_id=p.id,
            peer_name=p.name,
            collector_id=c.id,
            collector_name=c.name,
            collector_status=c.status,
            local_asn=p.local_asn,
            peer_asn=p.peer_asn,
            peer_address=p.peer_address,
            enabled=p.enabled,
            session_state=p.session_state,
            uptime_started_at=p.uptime_started_at,
            prefixes_received=p.prefixes_received,
            prefixes_accepted=p.prefixes_accepted,
            last_state_change=p.last_state_change,
            last_flap_at=p.last_flap_at,
            rpki_invalid_count=p.rpki_invalid_count,
            down_since=p.down_since,
        )
        for p, c in rows
    ]


# ── Routes (the learned RIB) ─────────────────────────────────────────────


def _route_to_read(r: BGPLGRoute) -> RouteRead:
    return RouteRead.model_validate(r)


def _parse_prefix(raw: str) -> str:
    try:
        return str(ipaddress.ip_network(raw, strict=False))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid prefix: {raw!r}") from exc


@router.get("/routes", response_model=RouteListResponse)
async def list_routes(
    db: DB,
    _: CurrentUser,
    prefix: str | None = Query(None, description="contains-or-within CIDR match"),
    origin_asn: int | None = None,
    community: str | None = Query(None, description="matches communities or large_communities"),
    rpki_status: str | None = None,
    peer_id: uuid.UUID | None = None,
    best_path_only: bool = False,
    withdrawn: bool = Query(False, description="include withdrawn routes (hidden by default)"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> RouteListResponse:
    """Server-paginated, filterable view over the learned RIB.

    Mirrors ``GET /ipam/addresses/search``'s ``{items,total}`` envelope —
    the RIB is "low thousands" scoped per issue #566's v1 design, so
    server-side pagination (not client windowing) is the right shape.
    """
    q = select(BGPLGRoute)

    if prefix:
        net = _parse_prefix(prefix)
        # contains-or-within: match routes that are a supernet of, equal
        # to, or a subnet of the requested prefix.
        q = q.where(or_(BGPLGRoute.prefix.op(">>=")(net), BGPLGRoute.prefix.op("<<=")(net)))
    if origin_asn is not None:
        q = q.where(BGPLGRoute.origin_asn == origin_asn)
    if community:
        q = q.where(
            or_(
                BGPLGRoute.communities.contains([community]),
                BGPLGRoute.large_communities.contains([community]),
            )
        )
    if rpki_status:
        q = q.where(BGPLGRoute.rpki_status == rpki_status)
    if peer_id is not None:
        q = q.where(BGPLGRoute.peer_id == peer_id)
    if best_path_only:
        q = q.where(BGPLGRoute.is_best.is_(True))
    if not withdrawn:
        q = q.where(BGPLGRoute.withdrawn_at.is_(None))

    total = (
        await db.execute(select(func.count()).select_from(q.order_by(None).subquery()))
    ).scalar_one()
    q = q.order_by(BGPLGRoute.prefix, BGPLGRoute.peer_id).offset(offset).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return RouteListResponse(
        items=[_route_to_read(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/routes/by-prefix", response_model=list[RouteRead])
async def get_route(
    db: DB,
    _: CurrentUser,
    prefix: str = Query(..., description="exact CIDR, e.g. 10.0.0.0/24 or 2001:db8::/32"),
    withdrawn: bool = Query(False, description="include withdrawn paths"),
) -> list[RouteRead]:
    """All paths for one exact prefix, across every peer.

    ``prefix`` is a query parameter (not a path segment) so the CIDR slash +
    IPv6 colons encode cleanly through proxies — a path-param ``{prefix:path}``
    would need raw slashes that some proxies / %2F-decoders mangle.

    Distinct from the ``prefix`` filter on ``GET /routes`` (which is a
    contains-or-within match): this is an exact-prefix lookup, feeding the
    "show ip bgp <prefix>" style detail view (and the future Query tab).
    """
    net = _parse_prefix(prefix)
    q = select(BGPLGRoute).where(BGPLGRoute.prefix == net)
    if not withdrawn:
        q = q.where(BGPLGRoute.withdrawn_at.is_(None))
    q = q.order_by(BGPLGRoute.peer_id)
    rows = (await db.execute(q)).scalars().all()
    return [_route_to_read(r) for r in rows]
