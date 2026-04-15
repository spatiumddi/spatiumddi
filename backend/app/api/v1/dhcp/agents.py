"""DHCP agent endpoints: register, heartbeat, config long-poll, lease ingestion, ops ack.

Mirrors ``app.api.v1.dns.agents``. See docs/deployment/DNS_AGENT.md for the
protocol shape — DHCP reuses identical semantics.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from jose import JWTError
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DB
from app.api.v1.dhcp._audit import write_audit
from app.models.dhcp import (
    DHCPConfigOp,
    DHCPLease,
    DHCPServer,
    DHCPServerGroup,
)
from app.services.dhcp.agent_token import (
    hash_token,
    mint_agent_token,
    needs_rotation,
    verify_agent_token,
)
from app.services.dhcp.config_bundle import build_config_bundle

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/agents", tags=["dhcp-agents"])

LONGPOLL_TIMEOUT_SECONDS = int(os.environ.get("DHCP_AGENT_LONGPOLL_TIMEOUT", "30"))
LONGPOLL_POLL_INTERVAL = 2.0


# ── Schemas ─────────────────────────────────────────────────────────────────


class AgentRegisterRequest(BaseModel):
    hostname: str
    driver: str = "kea"
    roles: list[str] = ["standalone"]
    version: str | None = None
    group_name: str | None = None
    fingerprint: str
    agent_id: str | None = None


class AgentRegisterResponse(BaseModel):
    server_id: str
    agent_id: str
    agent_token: str
    token_expires_at: datetime
    config_etag: str | None
    pending_approval: bool


class AgentHeartbeatRequest(BaseModel):
    agent_version: str | None = None
    daemon: dict[str, Any] = {}
    config: dict[str, Any] = {}
    ops_ack: list[dict[str, Any]] = []
    failed_ops_count: int = 0


class AgentHeartbeatResponse(BaseModel):
    server_id: str
    status: str
    acknowledged_at: datetime
    rotated_token: str | None = None
    rotated_expires_at: datetime | None = None


class LeaseEvent(BaseModel):
    ip_address: str
    mac_address: str
    hostname: str | None = None
    client_id: str | None = None
    user_class: str | None = None
    state: str = "active"
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    expires_at: datetime | None = None


class LeaseEventBatch(BaseModel):
    leases: list[LeaseEvent]


# ── Auth ────────────────────────────────────────────────────────────────────


def _require_bootstrap_key(
    x_dhcp_agent_key: str | None = Header(default=None, alias="X-DHCP-Agent-Key"),
) -> str:
    expected = os.environ.get("DHCP_AGENT_KEY", "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DHCP_AGENT_KEY is not configured on the control plane",
        )
    if not x_dhcp_agent_key or not hmac.compare_digest(x_dhcp_agent_key, expected):
        raise HTTPException(status_code=401, detail="Invalid bootstrap key")
    return x_dhcp_agent_key


async def _auth_agent(
    db: DB, authorization: str | None = Header(default=None)
) -> tuple[DHCPServer, dict[str, Any]]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.split(None, 1)[1].strip()
    try:
        payload = verify_agent_token(token)
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}") from e
    server_id = payload.get("sub")
    if not server_id:
        raise HTTPException(status_code=401, detail="Token missing subject")
    server = await db.get(DHCPServer, uuid.UUID(server_id))
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")
    if server.agent_token_hash and server.agent_token_hash != hash_token(token):
        raise HTTPException(status_code=401, detail="Stale token")
    return server, payload


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.post("/register", response_model=AgentRegisterResponse)
async def agent_register(
    body: AgentRegisterRequest,
    db: DB,
    _psk: str = Depends(_require_bootstrap_key),
) -> AgentRegisterResponse:
    """Bootstrap registration — PSK → per-server JWT."""
    group: DHCPServerGroup | None = None
    if body.group_name:
        res = await db.execute(
            select(DHCPServerGroup).where(DHCPServerGroup.name == body.group_name)
        )
        group = res.scalar_one_or_none()
        if group is None:
            group = DHCPServerGroup(
                name=body.group_name, description="Auto-created by DHCP agent registration"
            )
            db.add(group)
            await db.flush()

    server: DHCPServer | None = None
    if body.agent_id:
        try:
            aid = uuid.UUID(body.agent_id)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=f"Invalid agent_id: {e}") from e
        res = await db.execute(select(DHCPServer).where(DHCPServer.agent_id == aid))
        server = res.scalar_one_or_none()
    if server is None:
        res = await db.execute(select(DHCPServer).where(DHCPServer.name == body.hostname))
        server = res.scalar_one_or_none()

    require_approval = os.environ.get("DHCP_REQUIRE_AGENT_APPROVAL", "false").lower() == "true"
    pending_approval = False
    if server is None:
        agent_id = uuid.UUID(body.agent_id) if body.agent_id else uuid.uuid4()
        server = DHCPServer(
            name=body.hostname,
            driver=body.driver,
            host=body.hostname,
            port=67,
            roles=body.roles,
            status="active",
            server_group_id=group.id if group else None,
            agent_id=agent_id,
            agent_registered=True,
            agent_approved=not require_approval,
            agent_fingerprint=body.fingerprint,
            agent_version=body.version,
            description=f"auto-registered agent v{body.version}" if body.version else "auto-registered",
        )
        pending_approval = require_approval
        db.add(server)
        await db.flush()
    else:
        if server.agent_fingerprint and server.agent_fingerprint != body.fingerprint:
            server.agent_approved = False
            pending_approval = True
            logger.warning("dhcp_agent_fingerprint_mismatch", server_id=str(server.id))
        server.agent_fingerprint = body.fingerprint
        server.driver = body.driver
        server.roles = body.roles
        server.status = "active"
        server.agent_version = body.version
        server.agent_registered = True
        if server.agent_id is None:
            server.agent_id = uuid.UUID(body.agent_id) if body.agent_id else uuid.uuid4()

    token, exp = mint_agent_token(
        server_id=str(server.id),
        agent_id=str(server.agent_id),
        fingerprint=body.fingerprint,
    )
    server.agent_token_hash = hash_token(token)
    server.agent_last_seen = datetime.now(UTC)

    write_audit(
        db,
        user=None,
        action="dhcp.agent.register",
        resource_type="dhcp_server",
        resource_id=str(server.id),
        resource_display=body.hostname,
        new_value={"driver": body.driver, "version": body.version, "roles": body.roles},
    )
    await db.commit()
    await db.refresh(server)

    logger.info(
        "dhcp_agent_registered",
        server_id=str(server.id),
        hostname=body.hostname,
        pending_approval=pending_approval,
    )
    return AgentRegisterResponse(
        server_id=str(server.id),
        agent_id=str(server.agent_id),
        agent_token=token,
        token_expires_at=exp,
        config_etag=server.config_etag,
        pending_approval=pending_approval,
    )


@router.get("/config")
async def agent_config_longpoll(
    db: DB,
    response: Response,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    auth: tuple[DHCPServer, dict[str, Any]] = Depends(_auth_agent),
) -> Any:
    """Long-poll for config changes. Returns 304 if unchanged, bundle JSON otherwise."""
    server, _payload = auth
    if not server.agent_approved:
        response.headers["X-Spatium-Pending-Approval"] = "1"
        return {"pending_approval": True, "etag": None}

    deadline = asyncio.get_event_loop().time() + LONGPOLL_TIMEOUT_SECONDS
    while True:
        bundle = await build_config_bundle(db, server)
        etag = bundle.etag

        # Pending ops fast-path
        ops_res = await db.execute(
            select(DHCPConfigOp).where(
                DHCPConfigOp.server_id == server.id, DHCPConfigOp.status == "pending"
            )
        )
        pending_ops = [
            {"op_id": str(o.id), "op_type": o.op_type, "payload": o.payload}
            for o in ops_res.scalars().all()
        ]

        if etag != if_none_match or pending_ops:
            logger.info(
                "dhcp_agent_config_200",
                server_id=str(server.id),
                etag=etag,
                if_none_match=if_none_match,
                etag_match=(etag == if_none_match),
                pending_ops=len(pending_ops),
            )
            server.config_etag = etag
            await db.commit()
            response.headers["ETag"] = etag
            return {
                "server_id": str(server.id),
                "etag": etag,
                "bundle": {
                    "server_name": bundle.server_name,
                    "driver": bundle.driver,
                    "roles": list(bundle.roles),
                    "scopes": [
                        {
                            "subnet_cidr": s.subnet_cidr,
                            "lease_time": s.lease_time,
                            "options": s.options,
                            "pools": [
                                {
                                    "start_ip": p.start_ip,
                                    "end_ip": p.end_ip,
                                    "pool_type": p.pool_type,
                                }
                                for p in s.pools
                            ],
                            "statics": [
                                {
                                    "ip_address": st.ip_address,
                                    "mac_address": st.mac_address,
                                    "hostname": st.hostname,
                                }
                                for st in s.statics
                            ],
                            "ddns_enabled": s.ddns_enabled,
                        }
                        for s in bundle.scopes
                    ],
                    "client_classes": [
                        {"name": c.name, "match_expression": c.match_expression, "options": c.options}
                        for c in bundle.client_classes
                    ],
                },
                "pending_ops": pending_ops,
            }
        if asyncio.get_event_loop().time() >= deadline:
            return Response(status_code=304, headers={"ETag": etag})
        await asyncio.sleep(LONGPOLL_POLL_INTERVAL)


@router.post("/heartbeat", response_model=AgentHeartbeatResponse)
async def agent_heartbeat(
    body: AgentHeartbeatRequest,
    db: DB,
    auth: tuple[DHCPServer, dict[str, Any]] = Depends(_auth_agent),
) -> AgentHeartbeatResponse:
    server, payload = auth
    now = datetime.now(UTC)
    server.agent_last_seen = now
    server.last_health_check_at = now
    server.status = "active"
    if body.agent_version:
        server.agent_version = body.agent_version

    for ack in body.ops_ack:
        op_id = ack.get("op_id")
        result = ack.get("result", "error")
        message = ack.get("message")
        if op_id:
            op = await db.get(DHCPConfigOp, uuid.UUID(op_id))
            if op is not None and op.server_id == server.id:
                op.status = "acked" if result == "ok" else "failed"
                op.error_msg = message
                op.acked_at = now

    rotated_token = None
    rotated_exp = None
    if needs_rotation(payload):
        rotated_token, rotated_exp = mint_agent_token(
            server_id=str(server.id),
            agent_id=str(server.agent_id),
            fingerprint=server.agent_fingerprint or "",
        )
        server.agent_token_hash = hash_token(rotated_token)

    await db.commit()
    return AgentHeartbeatResponse(
        server_id=str(server.id),
        status=server.status,
        acknowledged_at=now,
        rotated_token=rotated_token,
        rotated_expires_at=rotated_exp,
    )


@router.post("/lease-events")
async def agent_lease_events(
    body: LeaseEventBatch,
    db: DB,
    auth: tuple[DHCPServer, dict[str, Any]] = Depends(_auth_agent),
) -> dict[str, int]:
    """Bulk lease ingestion from the agent. Upsert-by-(server, ip)."""
    server, _ = auth
    now = datetime.now(UTC)
    upserted = 0
    for ev in body.leases:
        res = await db.execute(
            select(DHCPLease).where(
                DHCPLease.server_id == server.id,
                DHCPLease.ip_address == ev.ip_address,
                DHCPLease.mac_address == ev.mac_address,
            )
        )
        lease = res.scalar_one_or_none()
        if lease is None:
            lease = DHCPLease(
                server_id=server.id,
                ip_address=ev.ip_address,
                mac_address=ev.mac_address,
                hostname=ev.hostname,
                client_id=ev.client_id,
                user_class=ev.user_class,
                state=ev.state,
                starts_at=ev.starts_at,
                ends_at=ev.ends_at,
                expires_at=ev.expires_at,
                last_seen_at=now,
            )
            db.add(lease)
        else:
            lease.hostname = ev.hostname
            lease.client_id = ev.client_id
            lease.user_class = ev.user_class
            lease.state = ev.state
            lease.starts_at = ev.starts_at
            lease.ends_at = ev.ends_at
            lease.expires_at = ev.expires_at
            lease.last_seen_at = now
        upserted += 1
    await db.commit()
    return {"upserted": upserted}


@router.post("/ops/{op_id}/ack")
async def agent_ops_ack(
    op_id: uuid.UUID,
    body: dict[str, Any],
    db: DB,
    auth: tuple[DHCPServer, dict[str, Any]] = Depends(_auth_agent),
) -> dict[str, str]:
    server, _ = auth
    op = await db.get(DHCPConfigOp, op_id)
    if op is None or op.server_id != server.id:
        raise HTTPException(status_code=404, detail="Op not found")
    result = body.get("result", "error")
    op.status = "acked" if result == "ok" else "failed"
    op.error_msg = body.get("message")
    op.acked_at = datetime.now(UTC)
    await db.commit()
    return {"status": "ok"}
