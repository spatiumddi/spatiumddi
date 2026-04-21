"""DHCP failover channel (Kea HA) CRUD.

A channel pairs two existing DHCPServer rows with shared HA hook
configuration — mode, heartbeat tuning, per-peer control-agent URLs,
and the auto-failover flag. Each server may belong to at most one
channel (enforced at the DB level via unique FKs).

The agent picks the channel up through the normal config bundle +
ETag long-poll flow: on create/update/delete the ETag for both peers
shifts, which drives a live HA hook install/remove on each Kea
daemon.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import or_, select

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.api.v1.dhcp._audit import write_audit
from app.core.permissions import require_resource_permission
from app.models.dhcp import DHCPFailoverChannel, DHCPServer

router = APIRouter(
    prefix="/failover-channels",
    tags=["dhcp"],
    dependencies=[Depends(require_resource_permission("dhcp_server"))],
)

VALID_MODES = {"hot-standby", "load-balancing"}


# ── Schemas ───────────────────────────────────────────────────────────────────


class ChannelCreate(BaseModel):
    name: str
    description: str = ""
    mode: str = "hot-standby"
    primary_server_id: uuid.UUID
    secondary_server_id: uuid.UUID
    primary_peer_url: str = ""
    secondary_peer_url: str = ""
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

    @model_validator(mode="after")
    def _peers_distinct(self) -> ChannelCreate:
        if self.primary_server_id == self.secondary_server_id:
            raise ValueError("primary and secondary must be different servers")
        return self


class ChannelUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    mode: str | None = None
    primary_server_id: uuid.UUID | None = None
    secondary_server_id: uuid.UUID | None = None
    primary_peer_url: str | None = None
    secondary_peer_url: str | None = None
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


class ChannelResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    mode: str
    primary_server_id: uuid.UUID
    secondary_server_id: uuid.UUID
    primary_server_name: str
    secondary_server_name: str
    primary_peer_url: str
    secondary_peer_url: str
    heartbeat_delay_ms: int
    max_response_delay_ms: int
    max_ack_delay_ms: int
    max_unacked_clients: int
    auto_failover: bool
    # Surfaced verbatim from the agent's latest ha-status-get report.
    primary_ha_state: str | None
    secondary_ha_state: str | None
    primary_ha_last_heartbeat_at: datetime | None
    secondary_ha_last_heartbeat_at: datetime | None
    created_at: datetime
    modified_at: datetime

    @classmethod
    def from_model(cls, row: DHCPFailoverChannel) -> ChannelResponse:
        p = row.primary_server
        s = row.secondary_server
        return cls(
            id=row.id,
            name=row.name,
            description=row.description,
            mode=row.mode,
            primary_server_id=row.primary_server_id,
            secondary_server_id=row.secondary_server_id,
            primary_server_name=p.name if p else "",
            secondary_server_name=s.name if s else "",
            primary_peer_url=row.primary_peer_url,
            secondary_peer_url=row.secondary_peer_url,
            heartbeat_delay_ms=row.heartbeat_delay_ms,
            max_response_delay_ms=row.max_response_delay_ms,
            max_ack_delay_ms=row.max_ack_delay_ms,
            max_unacked_clients=row.max_unacked_clients,
            auto_failover=row.auto_failover,
            primary_ha_state=p.ha_state if p else None,
            secondary_ha_state=s.ha_state if s else None,
            primary_ha_last_heartbeat_at=p.ha_last_heartbeat_at if p else None,
            secondary_ha_last_heartbeat_at=s.ha_last_heartbeat_at if s else None,
            created_at=row.created_at,
            modified_at=row.modified_at,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _assert_servers_free(
    db: DB, primary_id: uuid.UUID, secondary_id: uuid.UUID, exclude_channel: uuid.UUID | None
) -> None:
    """Reject if either server is already pinned to another channel.

    Kea's HA hook allows at most one relationship per peer, and the DB
    has unique FK constraints matching that — surfacing a friendlier
    409 here before we hit the IntegrityError path.
    """
    clause = or_(
        DHCPFailoverChannel.primary_server_id == primary_id,
        DHCPFailoverChannel.secondary_server_id == primary_id,
        DHCPFailoverChannel.primary_server_id == secondary_id,
        DHCPFailoverChannel.secondary_server_id == secondary_id,
    )
    q = select(DHCPFailoverChannel).where(clause)
    if exclude_channel is not None:
        q = q.where(DHCPFailoverChannel.id != exclude_channel)
    other = (await db.execute(q)).scalars().first()
    if other is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"One of the selected servers is already in failover channel "
                f"'{other.name}'. Remove it from that channel first."
            ),
        )


async def _load_server(db: DB, server_id: uuid.UUID) -> DHCPServer:
    row = await db.get(DHCPServer, server_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"DHCP server {server_id} not found")
    if row.driver != "kea":
        # Kea HA is a Kea hook — non-Kea drivers (Windows DHCP today)
        # have their own failover story and should not appear here.
        raise HTTPException(
            status_code=422,
            detail=(
                f"DHCP server '{row.name}' uses driver '{row.driver}'. "
                "Failover channels only support the Kea driver."
            ),
        )
    return row


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("", response_model=list[ChannelResponse])
async def list_channels(db: DB, _: CurrentUser) -> list[ChannelResponse]:
    res = await db.execute(select(DHCPFailoverChannel).order_by(DHCPFailoverChannel.name))
    return [ChannelResponse.from_model(r) for r in res.scalars().all()]


@router.post("", response_model=ChannelResponse, status_code=status.HTTP_201_CREATED)
async def create_channel(body: ChannelCreate, db: DB, user: SuperAdmin) -> ChannelResponse:
    await _load_server(db, body.primary_server_id)
    await _load_server(db, body.secondary_server_id)
    await _assert_servers_free(db, body.primary_server_id, body.secondary_server_id, None)

    dup = await db.execute(select(DHCPFailoverChannel).where(DHCPFailoverChannel.name == body.name))
    if dup.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A channel with that name exists")

    row = DHCPFailoverChannel(**body.model_dump())
    db.add(row)
    await db.flush()
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="dhcp_failover_channel",
        resource_id=str(row.id),
        resource_display=row.name,
    )
    await db.commit()
    await db.refresh(row)
    return ChannelResponse.from_model(row)


@router.get("/{channel_id}", response_model=ChannelResponse)
async def get_channel(channel_id: uuid.UUID, db: DB, _: CurrentUser) -> ChannelResponse:
    row = await db.get(DHCPFailoverChannel, channel_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return ChannelResponse.from_model(row)


@router.patch("/{channel_id}", response_model=ChannelResponse)
async def update_channel(
    channel_id: uuid.UUID, body: ChannelUpdate, db: DB, user: SuperAdmin
) -> ChannelResponse:
    row = await db.get(DHCPFailoverChannel, channel_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    changes = body.model_dump(exclude_none=True)
    new_primary = changes.get("primary_server_id", row.primary_server_id)
    new_secondary = changes.get("secondary_server_id", row.secondary_server_id)
    if new_primary == new_secondary:
        raise HTTPException(
            status_code=422, detail="primary and secondary must be different servers"
        )
    if "primary_server_id" in changes and changes["primary_server_id"] != row.primary_server_id:
        await _load_server(db, changes["primary_server_id"])
    if (
        "secondary_server_id" in changes
        and changes["secondary_server_id"] != row.secondary_server_id
    ):
        await _load_server(db, changes["secondary_server_id"])
    if "primary_server_id" in changes or "secondary_server_id" in changes:
        await _assert_servers_free(db, new_primary, new_secondary, exclude_channel=row.id)

    if "name" in changes and changes["name"] != row.name:
        dup = await db.execute(
            select(DHCPFailoverChannel).where(
                DHCPFailoverChannel.name == changes["name"],
                DHCPFailoverChannel.id != row.id,
            )
        )
        if dup.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="A channel with that name exists")

    for k, v in changes.items():
        setattr(row, k, v)
    await db.flush()
    write_audit(
        db,
        user=user,
        action="update",
        resource_type="dhcp_failover_channel",
        resource_id=str(row.id),
        resource_display=row.name,
        new_value=changes,
    )
    await db.commit()
    await db.refresh(row)
    return ChannelResponse.from_model(row)


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(channel_id: uuid.UUID, db: DB, user: SuperAdmin) -> None:
    row = await db.get(DHCPFailoverChannel, channel_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="dhcp_failover_channel",
        resource_id=str(row.id),
        resource_display=row.name,
    )
    await db.delete(row)
    await db.commit()
