"""DHCP server group CRUD.

Server groups are the primary configuration container under the group-
centric model: scopes, pools, statics, and client classes all live here,
and HA tuning (mode, heartbeat, max-response / max-ack / max-unacked,
auto-failover) lives on the group too. A group with two Kea members is
implicitly a Kea HA pair; a single-member group is standalone.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.api.v1.dhcp._audit import write_audit
from app.core.permissions import require_resource_permission
from app.models.dhcp import DHCPServer, DHCPServerGroup

router = APIRouter(
    prefix="/server-groups",
    tags=["dhcp"],
    dependencies=[Depends(require_resource_permission("dhcp_server"))],
)

VALID_MODES = {"standalone", "load-balancing", "hot-standby"}


class GroupCreate(BaseModel):
    name: str
    description: str = ""
    mode: str = "hot-standby"
    heartbeat_delay_ms: int = 10000
    max_response_delay_ms: int = 60000
    max_ack_delay_ms: int = 10000
    max_unacked_clients: int = 5
    auto_failover: bool = True

    @field_validator("mode")
    @classmethod
    def _m(cls, v: str) -> str:
        if v not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        return v


class GroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    mode: str | None = None
    heartbeat_delay_ms: int | None = None
    max_response_delay_ms: int | None = None
    max_ack_delay_ms: int | None = None
    max_unacked_clients: int | None = None
    auto_failover: bool | None = None

    @field_validator("mode")
    @classmethod
    def _m(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        return v


class ServerSummary(BaseModel):
    id: uuid.UUID
    name: str
    driver: str
    host: str
    status: str
    ha_state: str | None
    ha_peer_url: str
    agent_approved: bool


class GroupResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    mode: str
    heartbeat_delay_ms: int
    max_response_delay_ms: int
    max_ack_delay_ms: int
    max_unacked_clients: int
    auto_failover: bool
    # Computed: count of Kea servers currently in the group. ≥ 2 means
    # the group renders the libdhcp_ha.so hook on every peer.
    kea_member_count: int = 0
    # Member servers rolled up so the UI can render a group detail page
    # without a second round-trip. Empty when nothing's registered.
    servers: list[ServerSummary] = []
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


def _group_to_response(g: DHCPServerGroup) -> GroupResponse:
    kea = [s for s in (g.servers or []) if s.driver == "kea"]
    return GroupResponse(
        id=g.id,
        name=g.name,
        description=g.description,
        mode=g.mode,
        heartbeat_delay_ms=g.heartbeat_delay_ms,
        max_response_delay_ms=g.max_response_delay_ms,
        max_ack_delay_ms=g.max_ack_delay_ms,
        max_unacked_clients=g.max_unacked_clients,
        auto_failover=g.auto_failover,
        kea_member_count=len(kea),
        servers=[
            ServerSummary(
                id=s.id,
                name=s.name,
                driver=s.driver,
                host=s.host,
                status=s.status,
                ha_state=s.ha_state,
                ha_peer_url=s.ha_peer_url or "",
                agent_approved=s.agent_approved,
            )
            for s in (g.servers or [])
        ],
        created_at=g.created_at,
        modified_at=g.modified_at,
    )


@router.get("", response_model=list[GroupResponse])
async def list_groups(db: DB, _: CurrentUser) -> list[GroupResponse]:
    res = await db.execute(select(DHCPServerGroup).order_by(DHCPServerGroup.name))
    return [_group_to_response(g) for g in res.unique().scalars().all()]


@router.post("", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(body: GroupCreate, db: DB, user: SuperAdmin) -> GroupResponse:
    existing = await db.execute(select(DHCPServerGroup).where(DHCPServerGroup.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A DHCP server group with that name exists")
    g = DHCPServerGroup(**body.model_dump())
    db.add(g)
    await db.flush()
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="dhcp_server_group",
        resource_id=str(g.id),
        resource_display=g.name,
        new_value=body.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(g)
    return _group_to_response(g)


@router.get("/{group_id}", response_model=GroupResponse)
async def get_group(group_id: uuid.UUID, db: DB, _: CurrentUser) -> GroupResponse:
    g = await db.get(DHCPServerGroup, group_id)
    if g is None:
        raise HTTPException(status_code=404, detail="Server group not found")
    return _group_to_response(g)


@router.put("/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: uuid.UUID, body: GroupUpdate, db: DB, user: SuperAdmin
) -> GroupResponse:
    g = await db.get(DHCPServerGroup, group_id)
    if g is None:
        raise HTTPException(status_code=404, detail="Server group not found")
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(g, k, v)
    write_audit(
        db,
        user=user,
        action="update",
        resource_type="dhcp_server_group",
        resource_id=str(g.id),
        resource_display=g.name,
        changed_fields=list(changes.keys()),
        new_value=body.model_dump(mode="json", exclude_none=True),
    )
    await db.commit()
    await db.refresh(g)
    return _group_to_response(g)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(group_id: uuid.UUID, db: DB, user: SuperAdmin) -> None:
    g = await db.get(DHCPServerGroup, group_id)
    if g is None:
        raise HTTPException(status_code=404, detail="Server group not found")

    # ORM ``cascade="all, delete-orphan"`` on ``servers``/``scopes`` will silently
    # nuke every child row. Pre-check and return 409 so the user can't wipe a
    # populated group by mistake.
    server_count = (
        await db.execute(
            select(func.count())
            .select_from(DHCPServer)
            .where(DHCPServer.server_group_id == group_id)
        )
    ).scalar_one()
    if server_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"DHCP server group {g.name!r} still contains "
                f"{server_count} server(s). Move them to another group "
                "(or standalone) before deleting the group."
            ),
        )

    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="dhcp_server_group",
        resource_id=str(g.id),
        resource_display=g.name,
    )
    await db.delete(g)
    await db.commit()
