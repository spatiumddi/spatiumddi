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
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from jose import JWTError
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DB
from app.models.audit import AuditLog
from app.models.dns import (
    DNSRecordOp,
    DNSServer,
    DNSServerGroup,
    DNSServerRuntimeState,
    DNSServerZoneState,
    DNSZone,
)
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
    # Phase 8f-2 — agent reports its slot state + deployment environment.
    # All optional so older agents that haven't been upgraded keep
    # heartbeating without a 422. Server-side fills the matching
    # ``dns_server`` columns when present.
    deployment_kind: str | None = None
    installed_appliance_version: str | None = None
    current_slot: str | None = None
    durable_default: str | None = None
    is_trial_boot: bool | None = None
    last_upgrade_state: str | None = None
    last_upgrade_state_at: datetime | None = None


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

    # ``DNS_REQUIRE_AGENT_APPROVAL`` (env / settings) gates whether
    # fingerprint changes lock the agent out pending operator approval.
    # Default: false — wiping an agent's persistent volume + redeploying
    # against the same PSK should "just work" because the agent is
    # already authenticated by the bootstrap key. Operators running
    # high-trust environments flip this to true, which engages the
    # anti-hijack behaviour: any fingerprint mismatch on re-registration
    # forces a manual approval step before the agent can pull config.
    require_approval = os.environ.get("DNS_REQUIRE_AGENT_APPROVAL", "false").lower() == "true"

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
            pending_approval=require_approval,
            is_primary=not has_primary,
            notes=f"agent v{body.version}" if body.version else "auto-registered",
        )
        pending_approval = require_approval
        db.add(server)
        await db.flush()
    else:
        # Anti-hijack: fingerprint change → force approval IFF the
        # operator has opted into the approval gate. Otherwise the
        # agent's PSK authentication is enough — a wiped agent
        # volume legitimately produces a new fingerprint and we
        # don't want to lock the operator out of their own install.
        if (
            require_approval
            and server.agent_fingerprint
            and server.agent_fingerprint != body.fingerprint
        ):
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
    request: Request,
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
    # Capture the source IP so the operator can identify which host
    # this agent is running on — the operator-set ``host`` column is
    # just a label that may not match the real machine in NAT /
    # distributed deployments.
    if request.client is not None:
        server.last_seen_ip = request.client.host

    # Phase 8f-2 — persist whatever slot state the agent reported. Only
    # overwrite when the agent actually sent a value (older agents
    # leave these as None, in which case we leave the DB columns
    # untouched rather than nulling out previously-known state).
    if body.deployment_kind is not None:
        server.deployment_kind = body.deployment_kind
    if body.installed_appliance_version is not None:
        server.installed_appliance_version = body.installed_appliance_version
    if body.current_slot is not None:
        server.current_slot = body.current_slot
    if body.durable_default is not None:
        server.durable_default = body.durable_default
    if body.is_trial_boot is not None:
        server.is_trial_boot = body.is_trial_boot
    if body.last_upgrade_state is not None:
        server.last_upgrade_state = body.last_upgrade_state
    if body.last_upgrade_state_at is not None:
        server.last_upgrade_state_at = body.last_upgrade_state_at

    # Phase 8f-7 — auto-clear the operator-intent stamp once the agent
    # confirms it landed. The Fleet view's "pending" indicator drops
    # to None as soon as installed_appliance_version matches the
    # desired one + the slot upgrade reported done. Cancellation flow
    # (operator cleared desired_ manually before the agent picked it
    # up) is handled by the Fleet endpoint's clear handler — that
    # already nulls both fields directly.
    if (
        server.desired_appliance_version is not None
        and server.installed_appliance_version
        and server.installed_appliance_version == server.desired_appliance_version
        and (server.last_upgrade_state in ("done", None))
    ):
        server.desired_appliance_version = None
        server.desired_slot_image_url = None

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


class DNSSECStateReport(BaseModel):
    """One zone's DNSSEC state, posted by the agent after signing.

    The DS rrset strings come straight from PowerDNS's
    ``GET /zones/{z}/cryptokeys`` response — operator copies them
    into the parent registrar verbatim. Empty list = unsigned.
    """

    zone_name: str
    ds_records: list[str]


class DNSSECStateBatch(BaseModel):
    zones: list[DNSSECStateReport]


@router.post("/dnssec-state")
async def agent_dnssec_state(
    body: DNSSECStateBatch,
    db: DB,
    auth: tuple[DNSServer, dict[str, Any]] = Depends(_auth_agent),
) -> dict[str, int]:
    """Agents POST DS-record state per zone after a signing change
    (issue #127, Phase 3c.fe).

    Updates ``DNSZone.dnssec_ds_records`` + ``dnssec_synced_at`` so
    the operator-facing zone-edit page can render the DS rrset
    without round-tripping the agent on every page load.

    Empty ``ds_records`` = the zone was just unsigned; we clear the
    cache so the UI doesn't display stale records the parent zone
    no longer trusts.

    Unknown zone names are silently skipped (deleted between sign
    and report) — same fail-soft semantic as ``/zone-state``.
    """
    _, _ = auth
    now = datetime.now(UTC)
    updated = 0

    if not body.zones:
        return {"updated": 0}

    # DNSZone.name is stored *with* the trailing dot in the DB
    # (per ZoneCreate.ensure_trailing_dot validator). The agent
    # ships fully-qualified names that already carry the dot; we
    # build both forms in the IN-clause so the lookup matches
    # regardless of whether the agent normalised before sending,
    # then key the dict by the canonical (trailing-dot) form.
    name_set: set[str] = set()
    for e in body.zones:
        n = e.zone_name
        name_set.add(n)
        name_set.add(n.rstrip("."))
        if not n.endswith("."):
            name_set.add(n + ".")
    res = await db.execute(select(DNSZone).where(DNSZone.name.in_(name_set)))
    zones_by_name: dict[str, DNSZone] = {z.name.rstrip("."): z for z in res.scalars().all()}

    for entry in body.zones:
        zone = zones_by_name.get(entry.zone_name.rstrip("."))
        if zone is None:
            continue
        zone.dnssec_ds_records = entry.ds_records or None
        zone.dnssec_synced_at = now
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
    """Ingest a batch of query-log lines from the agent.

    The ingest is driver-aware: BIND9 lines go through
    ``bind9_parser`` (RFC 5424 + BIND's ``query: ...`` body), and
    PowerDNS lines go through ``pdns_parser`` (``Remote ip:port
    wants 'qname|qtype'`` shape). Both parsers normalise into the
    shared :class:`ParsedQueryLine` dataclass so this endpoint
    stays driver-agnostic past the dispatch.

    Capped at 1000 lines per request to keep individual transactions
    bounded. Anything beyond is dropped with a count returned so the
    agent can log + alert.

    Lines that the parser couldn't pull a ``qname`` out of are
    silently dropped — pdns mixes startup banners + status messages
    into the same stderr stream the agent captures, so non-query
    lines are expected and stored as noise without filling the DB.
    """
    from app.services.logs import bind9_parser, pdns_parser  # noqa: PLC0415

    server, _ = auth
    if server.driver == "powerdns":
        parse_fn = pdns_parser.parse_query_line
    else:
        # BIND9 (and any future driver until it ships its own parser)
        # stays on the BIND parser. Mismatched-driver lines just won't
        # parse and end up as noise rows the operator can ignore.
        parse_fn = bind9_parser.parse_query_line

    capped = body.lines[:1000]
    dropped = max(0, len(body.lines) - len(capped))
    now = datetime.now(UTC)
    inserted = 0
    for raw in capped:
        parsed = parse_fn(raw, fallback_ts=now)
        if parsed is None or parsed.qname is None:
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


# ── Admin runtime-state push (rendered config + rndc status) ─────────


class RenderedConfigFile(BaseModel):
    """One materialised file from the agent's rendered config tree."""

    path: str  # relative path inside the rendered/ dir, e.g. "named.conf" or "zones/example.com.db"
    content: str


class RenderedConfigReport(BaseModel):
    """Snapshot the agent ships after a successful structural apply.

    The agent walks ``state_dir/rendered/`` and ships every file it
    finds. Total payload is bounded by the size of the operator's
    config — typically <100 KB even for groups with hundreds of zones.
    """

    files: list[RenderedConfigFile]


# Hard cap on what the control plane will accept. Defends against an
# agent shipping an unbounded zone tree without the operator noticing.
_RENDERED_FILES_MAX = 5_000
_RENDERED_FILE_SIZE_MAX = 256 * 1024  # 256 KB per file
_RENDERED_TOTAL_SIZE_MAX = 8 * 1024 * 1024  # 8 MB total


@router.post("/admin/rendered-config")
async def agent_rendered_config(
    body: RenderedConfigReport,
    db: DB,
    auth: tuple[DNSServer, dict[str, Any]] = Depends(_auth_agent),
) -> dict[str, Any]:
    """Ingest the agent's most-recent rendered config snapshot.

    Idempotent: writes (or upserts) the single ``DNSServerRuntimeState``
    row keyed on server_id. The previous snapshot is replaced wholesale
    — there is no history kept beyond "current".
    """
    server, _ = auth
    files = body.files[:_RENDERED_FILES_MAX]
    total = 0
    sanitised: list[dict[str, str]] = []
    for f in files:
        if len(f.content) > _RENDERED_FILE_SIZE_MAX:
            # Truncate rather than reject — operator wants to *see*
            # something even if a single file blew the cap.
            content = f.content[:_RENDERED_FILE_SIZE_MAX] + "\n... [truncated by control plane]\n"
        else:
            content = f.content
        total += len(content)
        if total > _RENDERED_TOTAL_SIZE_MAX:
            break
        sanitised.append({"path": f.path, "content": content})

    now = datetime.now(UTC)
    state = await db.get(DNSServerRuntimeState, server.id)
    if state is None:
        state = DNSServerRuntimeState(
            server_id=server.id,
            rendered_files=sanitised,
            rendered_at=now,
        )
        db.add(state)
    else:
        state.rendered_files = sanitised
        state.rendered_at = now
    await db.commit()
    return {"status": "ok", "files": len(sanitised)}


class RndcStatusReport(BaseModel):
    text: str


@router.post("/admin/rndc-status")
async def agent_rndc_status(
    body: RndcStatusReport,
    db: DB,
    auth: tuple[DNSServer, dict[str, Any]] = Depends(_auth_agent),
) -> dict[str, str]:
    """Ingest the agent's most-recent ``rndc status`` output.

    The agent shells out to ``rndc status`` once a minute. We keep the
    raw text plus a timestamp; the UI shows it on the Overview tab so
    operators can confirm ``named`` is up + which zones are loaded
    without SSHing into the host.
    """
    server, _ = auth
    text = body.text[:_RENDERED_FILE_SIZE_MAX]  # rndc status is normally a few KB
    now = datetime.now(UTC)
    state = await db.get(DNSServerRuntimeState, server.id)
    if state is None:
        state = DNSServerRuntimeState(
            server_id=server.id,
            rndc_status_text=text,
            rndc_observed_at=now,
        )
        db.add(state)
    else:
        state.rndc_status_text = text
        state.rndc_observed_at = now
    await db.commit()
    return {"status": "ok"}
