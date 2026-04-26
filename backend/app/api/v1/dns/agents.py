"""DNS agent endpoints: register, heartbeat, config long-poll, record-ops, ops/ack.

See docs/deployment/DNS_AGENT.md §§2-5 for the full protocol.
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
from app.models.audit import AuditLog
from app.models.dns import DNSRecordOp, DNSServer, DNSServerGroup, DNSServerZoneState, DNSZone
from app.models.logs import DNSQueryLogEntry
from app.models.metrics import DNSMetricSample
from app.services.dns.agent_config import build_config_bundle
from app.services.dns.agent_token import (
    hash_token,
    mint_agent_token,
    needs_rotation,
    verify_agent_token,
)
from app.services.dns.record_ops import ack_op

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/agents", tags=["dns-agents"])

LONGPOLL_TIMEOUT_SECONDS = int(os.environ.get("DNS_AGENT_LONGPOLL_TIMEOUT", "30"))
LONGPOLL_POLL_INTERVAL = 2.0


# ── Schemas ────────────────────────────────────────────────────────────────────


class AgentRegisterRequestV2(BaseModel):
    hostname: str
    driver: str = "bind9"
    roles: list[str] = ["authoritative"]
    version: str | None = None
    group_name: str | None = None
    fingerprint: str
    agent_id: str | None = None  # persisted UUID from previous runs


class AgentRegisterResponseV2(BaseModel):
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
    disk_free_bytes: int | None = None
    zone_serials: dict[str, int] = {}


class AgentHeartbeatResponseV2(BaseModel):
    server_id: str
    status: str
    acknowledged_at: datetime
    rotated_token: str | None = None
    rotated_expires_at: datetime | None = None


# ── Auth dependencies ──────────────────────────────────────────────────────────


def _require_bootstrap_key(
    x_dns_agent_key: str | None = Header(default=None, alias="X-DNS-Agent-Key")
) -> str:
    expected = os.environ.get("DNS_AGENT_KEY", "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DNS_AGENT_KEY is not configured on the control plane",
        )
    if not x_dns_agent_key or not hmac.compare_digest(x_dns_agent_key, expected):
        raise HTTPException(status_code=401, detail="Invalid bootstrap key")
    return x_dns_agent_key


async def _auth_agent(
    db: DB,
    authorization: str | None = Header(default=None),
) -> tuple[DNSServer, dict[str, Any]]:
    """Verify the Bearer agent_token, return (server, jwt_payload)."""
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
    server = await db.get(DNSServer, uuid.UUID(server_id))
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")
    # Defence-in-depth: verify hash match if we have one stored
    if server.agent_jwt_hash and server.agent_jwt_hash != hash_token(token):
        # Token rotated out — reject stale one
        raise HTTPException(status_code=401, detail="Stale token")
    return server, payload


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.post("/register", response_model=AgentRegisterResponseV2)
async def agent_register(
    body: AgentRegisterRequestV2,
    db: DB,
    _psk: str = Depends(_require_bootstrap_key),
) -> AgentRegisterResponseV2:
    """Bootstrap registration: PSK-authenticated; returns a per-server JWT."""
    # Resolve or create group
    if body.group_name:
        res = await db.execute(select(DNSServerGroup).where(DNSServerGroup.name == body.group_name))
        group = res.scalar_one_or_none()
        if group is None:
            group = DNSServerGroup(
                name=body.group_name, description="Auto-created by agent registration"
            )
            db.add(group)
            await db.flush()
    else:
        res = await db.execute(select(DNSServerGroup).order_by(DNSServerGroup.created_at).limit(1))
        group = res.scalar_one_or_none()
        if group is None:
            group = DNSServerGroup(name="default", description="Auto-created by agent registration")
            db.add(group)
            await db.flush()

    # Find by agent_id first (stable across restarts) then by hostname
    server: DNSServer | None = None
    if body.agent_id:
        try:
            aid = uuid.UUID(body.agent_id)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=f"Invalid agent_id: {e}") from e
        res = await db.execute(select(DNSServer).where(DNSServer.agent_id == aid))
        server = res.scalar_one_or_none()

    if server is None:
        res = await db.execute(
            select(DNSServer).where(DNSServer.group_id == group.id, DNSServer.name == body.hostname)
        )
        server = res.scalar_one_or_none()

    # Auto-generate group TSIG key on first registration if not set.
    # Used by the agent's RFC 2136 dynamic update path over loopback.
    if not group.tsig_key_secret:
        import base64
        import secrets

        group.tsig_key_name = f"spatium-{group.name}".replace(" ", "-").lower()
        group.tsig_key_secret = base64.b64encode(secrets.token_bytes(32)).decode()
        group.tsig_key_algorithm = "hmac-sha256"

    # First server in the group is auto-elected primary so DDNS ops have
    # somewhere to land. Operator can flip later via API.
    primary_res = await db.execute(
        select(DNSServer)
        .where(DNSServer.group_id == group.id, DNSServer.is_primary.is_(True))
        .limit(1)
    )
    has_primary = primary_res.scalar_one_or_none() is not None

    pending_approval = False
    if server is None:
        agent_id = uuid.UUID(body.agent_id) if body.agent_id else uuid.uuid4()
        server = DNSServer(
            group_id=group.id,
            name=body.hostname,
            driver=body.driver,
            host=body.hostname,
            port=53,
            roles=body.roles,
            status="active",
            agent_id=agent_id,
            agent_fingerprint=body.fingerprint,
            pending_approval=False,
            is_primary=not has_primary,
            notes=f"agent v{body.version}" if body.version else "auto-registered",
        )
        db.add(server)
        await db.flush()
    else:
        # Anti-hijack: fingerprint change → force approval
        if server.agent_fingerprint and server.agent_fingerprint != body.fingerprint:
            server.pending_approval = True
            pending_approval = True
            logger.warning("dns_agent_fingerprint_mismatch", server_id=str(server.id))
        server.agent_fingerprint = body.fingerprint
        server.driver = body.driver
        server.roles = body.roles
        server.status = "active"
        if server.agent_id is None:
            server.agent_id = uuid.UUID(body.agent_id) if body.agent_id else uuid.uuid4()

    # Mint token
    token, exp = mint_agent_token(
        server_id=str(server.id),
        agent_id=str(server.agent_id),
        fingerprint=body.fingerprint,
    )
    server.agent_jwt_hash = hash_token(token)
    server.last_seen_at = datetime.now(UTC)

    db.add(
        AuditLog(
            user_display_name="system:dns-agent",
            auth_source="system",
            action="dns.agent.register",
            resource_type="dns_server",
            resource_id=str(server.id),
            resource_display=body.hostname,
            new_value={"driver": body.driver, "version": body.version, "roles": body.roles},
            result="success",
        )
    )
    await db.commit()
    await db.refresh(server)

    logger.info(
        "dns_agent_registered",
        server_id=str(server.id),
        hostname=body.hostname,
        driver=body.driver,
        pending_approval=pending_approval,
    )

    return AgentRegisterResponseV2(
        server_id=str(server.id),
        agent_id=str(server.agent_id),
        agent_token=token,
        token_expires_at=exp,
        config_etag=server.last_config_etag,
        pending_approval=pending_approval,
    )


@router.get("/config")
async def agent_config_longpoll(
    db: DB,
    response: Response,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    auth: tuple[DNSServer, dict[str, Any]] = Depends(_auth_agent),
) -> Any:
    """Long-poll for config changes.

    Returns 304 if the server's current bundle matches If-None-Match.
    Otherwise holds the connection up to LONGPOLL_TIMEOUT_SECONDS waiting for
    any change, then returns the current bundle with a new ETag.
    """
    server, _payload = auth
    if server.pending_approval:
        response.headers["X-Spatium-Pending-Approval"] = "1"
        return {"pending_approval": True, "etag": None}

    deadline = asyncio.get_event_loop().time() + LONGPOLL_TIMEOUT_SECONDS
    while True:
        bundle = await build_config_bundle(db, server)
        etag = bundle["etag"]
        # Early return if there are pending ops (fast-path per §3)
        has_pending_ops = bool(bundle.get("pending_record_ops"))
        if etag != if_none_match or has_pending_ops:
            server.last_config_etag = etag
            await db.commit()
            response.headers["ETag"] = etag
            return bundle
        if asyncio.get_event_loop().time() >= deadline:
            response.status_code = 304
            response.headers["ETag"] = etag
            return Response(status_code=304, headers={"ETag": etag})
        await asyncio.sleep(LONGPOLL_POLL_INTERVAL)


@router.post("/heartbeat", response_model=AgentHeartbeatResponseV2)
async def agent_heartbeat(
    body: AgentHeartbeatRequest,
    db: DB,
    auth: tuple[DNSServer, dict[str, Any]] = Depends(_auth_agent),
) -> AgentHeartbeatResponseV2:
    """Heartbeat: updates last_seen_at, processes op ACKs, rotates token if near expiry."""
    server, payload = auth
    now = datetime.now(UTC)
    server.last_seen_at = now
    server.last_health_check_at = now
    server.status = "active"

    # Process op ACKs
    for ack in body.ops_ack:
        op_id = ack.get("op_id")
        result = ack.get("result", "error")
        message = ack.get("message")
        if op_id:
            await ack_op(db, op_id, result, message)

    rotated_token = None
    rotated_exp = None
    if needs_rotation(payload):
        rotated_token, rotated_exp = mint_agent_token(
            server_id=str(server.id),
            agent_id=str(server.agent_id),
            fingerprint=server.agent_fingerprint or "",
        )
        server.agent_jwt_hash = hash_token(rotated_token)

    await db.commit()
    return AgentHeartbeatResponseV2(
        server_id=str(server.id),
        status=server.status,
        acknowledged_at=now,
        rotated_token=rotated_token,
        rotated_expires_at=rotated_exp,
    )


@router.get("/record-ops")
async def agent_record_ops(
    db: DB,
    auth: tuple[DNSServer, dict[str, Any]] = Depends(_auth_agent),
) -> dict[str, Any]:
    """Return the queue of pending record ops targeting this server.

    Agents typically pick ops up from the long-poll bundle, but this endpoint
    lets an agent drain ops out-of-band (e.g. after a restart).
    """
    server, _ = auth
    res = await db.execute(
        select(DNSRecordOp)
        .where(DNSRecordOp.server_id == server.id, DNSRecordOp.state == "pending")
        .order_by(DNSRecordOp.created_at)
    )
    ops = res.scalars().all()
    return {
        "server_id": str(server.id),
        "ops": [
            {
                "op_id": str(o.id),
                "zone_name": o.zone_name,
                "op": o.op,
                "record": o.record,
                "target_serial": o.target_serial,
            }
            for o in ops
        ],
    }


@router.post("/ops/{op_id}/ack")
async def agent_ops_ack(
    op_id: uuid.UUID,
    body: dict[str, Any],
    db: DB,
    auth: tuple[DNSServer, dict[str, Any]] = Depends(_auth_agent),
) -> dict[str, str]:
    """Out-of-band op acknowledgment (also piggybacked on heartbeat)."""
    server, _ = auth
    op = await db.get(DNSRecordOp, op_id)
    if op is None or op.server_id != server.id:
        raise HTTPException(status_code=404, detail="Op not found")
    await ack_op(db, str(op_id), body.get("result", "error"), body.get("message"))
    await db.commit()
    return {"status": "ok"}


class ZoneStateEntry(BaseModel):
    zone_name: str
    serial: int


class ZoneStateReport(BaseModel):
    zones: list[ZoneStateEntry]


@router.post("/zone-state")
async def agent_zone_state(
    body: ZoneStateReport,
    db: DB,
    auth: tuple[DNSServer, dict[str, Any]] = Depends(_auth_agent),
) -> dict[str, int]:
    """Agents POST the serial they just rendered, per zone.

    Called after a successful ``apply_config`` pass — the serial
    reported here is the "ground truth" of what this particular
    server is serving, as distinct from ``DNSZone.last_serial`` which is
    the latest value the control plane *pushed*. Used for per-server
    drift detection on the zone detail page + (future) a
    ``zone_serial_drift`` alert-rule type.

    Upsert by ``(server_id, zone_id)`` — no history, one row per
    pair. Unknown zone names are silently skipped (zone deleted from
    control plane but agent still serves it; the next config bundle
    will drop it).
    """
    server, _ = auth
    now = datetime.now(UTC)
    updated = 0

    # Index known zones by name for one DB round-trip on the lookup.
    names = [e.zone_name.rstrip(".") for e in body.zones]
    if not names:
        return {"updated": 0}
    res = await db.execute(select(DNSZone).where(DNSZone.name.in_(names)))
    zones_by_name: dict[str, DNSZone] = {}
    for z in res.scalars().all():
        zones_by_name[z.name.rstrip(".")] = z

    for entry in body.zones:
        key = entry.zone_name.rstrip(".")
        zone = zones_by_name.get(key)
        if zone is None:
            continue

        # Upsert: look up existing row, update or insert.
        existing_res = await db.execute(
            select(DNSServerZoneState).where(
                DNSServerZoneState.server_id == server.id,
                DNSServerZoneState.zone_id == zone.id,
            )
        )
        row = existing_res.scalar_one_or_none()
        if row is None:
            row = DNSServerZoneState(
                server_id=server.id,
                zone_id=zone.id,
                current_serial=entry.serial,
                reported_at=now,
            )
            db.add(row)
        else:
            row.current_serial = entry.serial
            row.reported_at = now
        updated += 1

    await db.commit()
    return {"updated": updated}


class DNSMetricReport(BaseModel):
    """One time-bucketed sample of BIND9 query counters.

    Agents report *deltas* (the difference between two consecutive
    polls of the statistics-channels endpoint), already bucketed to
    whatever cadence the agent runs at (default 60 s). That keeps
    counter resets on daemon restart from back-propagating into the
    stored time series — the agent absorbs them.
    """

    bucket_at: datetime
    queries_total: int = 0
    noerror: int = 0
    nxdomain: int = 0
    servfail: int = 0
    recursion: int = 0


@router.post("/metrics")
async def agent_metrics(
    body: DNSMetricReport,
    db: DB,
    auth: tuple[DNSServer, dict[str, Any]] = Depends(_auth_agent),
) -> dict[str, str]:
    """Ingest one sample row from the agent's MetricsPoller thread.

    Idempotent on ``(server_id, bucket_at)`` — if the agent retries a
    POST after a transient failure it overwrites the prior row
    rather than duplicating. Counters that arrive negative (e.g. a
    buggy agent) are clamped to zero so the dashboard can't render
    impossible dips.
    """
    server, _ = auth
    values = {
        "queries_total": max(0, body.queries_total),
        "noerror": max(0, body.noerror),
        "nxdomain": max(0, body.nxdomain),
        "servfail": max(0, body.servfail),
        "recursion": max(0, body.recursion),
    }
    existing = await db.get(DNSMetricSample, (server.id, body.bucket_at))
    if existing is None:
        db.add(DNSMetricSample(server_id=server.id, bucket_at=body.bucket_at, **values))
    else:
        for k, v in values.items():
            setattr(existing, k, v)
    await db.commit()
    return {"status": "ok"}


# ── Query log ingestion ──────────────────────────────────────────────


class QueryLogBatch(BaseModel):
    """Batch of raw BIND9 query log lines pushed by the agent.

    The agent tails the configured query log file (default
    ``/var/log/named/queries.log``), collects up to ~200 lines or 5 s
    worth of activity, and POSTs them here. The control plane parses
    each line into structured fields and inserts. Idempotency is not
    enforced — duplicates are rare (they'd require the agent to
    retry a partially-applied batch) and harmless (rows have a
    monotonic ``id`` PK; nothing depends on uniqueness).
    """

    lines: list[str]


@router.post("/query-log-entries")
async def agent_query_log_entries(
    body: QueryLogBatch,
    db: DB,
    auth: tuple[DNSServer, dict[str, Any]] = Depends(_auth_agent),
) -> dict[str, Any]:
    """Ingest a batch of BIND9 query log lines from the agent.

    Capped at 1000 lines per request to keep individual transactions
    bounded. Anything beyond is dropped with a count returned so the
    agent can log + alert.
    """
    from app.services.logs.bind9_parser import parse_query_line  # noqa: PLC0415

    server, _ = auth
    capped = body.lines[:1000]
    dropped = max(0, len(body.lines) - len(capped))
    now = datetime.now(UTC)
    inserted = 0
    for raw in capped:
        parsed = parse_query_line(raw, fallback_ts=now)
        if parsed is None:
            continue
        db.add(
            DNSQueryLogEntry(
                server_id=server.id,
                ts=parsed.ts,
                client_ip=parsed.client_ip,
                client_port=parsed.client_port,
                qname=parsed.qname,
                qclass=parsed.qclass,
                qtype=parsed.qtype,
                flags=parsed.flags,
                view=parsed.view,
                raw=parsed.raw,
            )
        )
        inserted += 1
    await db.commit()
    return {"status": "ok", "inserted": inserted, "dropped": dropped}
