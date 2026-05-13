"""DHCP agent endpoints: register, heartbeat, config long-poll, lease ingestion, ops ack.

Mirrors ``app.api.v1.dns.agents``. See docs/deployment/DNS_AGENT.md for the
protocol shape — DHCP reuses identical semantics.
"""

from __future__ import annotations

import asyncio
import hashlib
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
from app.api.v1.dhcp._audit import write_audit
from app.models.dhcp import (
    DHCPConfigOp,
    DHCPLease,
    DHCPServer,
    DHCPServerGroup,
)
from app.models.logs import DHCPLogEntry
from app.models.metrics import DHCPMetricSample
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
    # Phase 8f-2 — agent reports its slot state + deployment environment.
    # See DNSServer agents.py for the per-field semantics. All optional
    # so older agents keep heartbeating without a 422.
    deployment_kind: str | None = None
    installed_appliance_version: str | None = None
    current_slot: str | None = None
    durable_default: str | None = None
    is_trial_boot: bool | None = None
    last_upgrade_state: str | None = None
    last_upgrade_state_at: datetime | None = None


class AgentHeartbeatResponse(BaseModel):
    server_id: str
    status: str
    acknowledged_at: datetime
    rotated_token: str | None = None
    rotated_expires_at: datetime | None = None


class HAStatusReport(BaseModel):
    """One ``ha-status-get`` observation, relayed upstream by the agent.

    ``state`` matches the Kea state names verbatim so the UI can
    present them without translation (``waiting`` / ``syncing`` /
    ``ready`` / ``normal`` / ``communications-interrupted`` /
    ``partner-down`` / ``hot-standby`` / ``load-balancing`` /
    ``backup`` / ``passive-backup`` / ``terminated``).

    ``raw`` carries the full Kea response so future additions like
    ``unsent-update-count`` / ``in-touch`` show up in the UI without a
    schema change here.
    """

    state: str
    raw: dict[str, Any] | None = None


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


class DHCPFingerprintEntry(BaseModel):
    """One DHCP fingerprint observation pushed by the agent's scapy sniffer.

    All fields except ``mac_address`` are nullable — devices with
    minimal DHCP option chatter still produce a useful row even if
    fingerbank can't enrich them.
    """

    mac_address: str
    option_55: str | None = None
    option_60: str | None = None
    option_77: str | None = None
    client_id: str | None = None


class DHCPFingerprintBatch(BaseModel):
    fingerprints: list[DHCPFingerprintEntry]


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
            description=(
                f"auto-registered agent v{body.version}" if body.version else "auto-registered"
            ),
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
        # Phase 8f-3 — mix the fleet-upgrade intent into the ETag so a
        # Fleet view change wakes the agent's long-poll even when the
        # driver-side bundle is unchanged. Deterministic — re-reading
        # the same DB state yields the same combined ETag.
        fleet_marker = (
            f"{server.desired_appliance_version}"
            f"|{server.desired_slot_image_url}"
            f"|{int(server.reboot_requested)}"
        )
        etag = "sha256:" + hashlib.sha256(f"{bundle.etag}|{fleet_marker}".encode()).hexdigest()

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
                        {
                            "name": c.name,
                            "match_expression": c.match_expression,
                            "options": c.options,
                        }
                        for c in bundle.client_classes
                    ],
                    "mac_blocks": [
                        {
                            "mac_address": m.mac_address,
                            "reason": m.reason,
                            "description": m.description,
                        }
                        for m in bundle.mac_blocks
                    ],
                    # Kea HA hook configuration — absent when the
                    # server isn't part of a failover channel. The
                    # agent's render_kea.py keys off the presence of
                    # this ``peers`` list to decide whether to emit
                    # ``libdhcp_ha.so``.
                    "failover": (
                        {
                            "channel_id": bundle.failover.channel_id,
                            "channel_name": bundle.failover.channel_name,
                            "mode": bundle.failover.mode,
                            "this_server_name": bundle.failover.this_server_name,
                            "peers": list(bundle.failover.peers),
                            "heartbeat_delay_ms": bundle.failover.heartbeat_delay_ms,
                            "max_response_delay_ms": bundle.failover.max_response_delay_ms,
                            "max_ack_delay_ms": bundle.failover.max_ack_delay_ms,
                            "max_unacked_clients": bundle.failover.max_unacked_clients,
                        }
                        if bundle.failover is not None
                        else None
                    ),
                },
                "pending_ops": pending_ops,
                # Phase 8f-3 — fleet upgrade intent the operator set
                # from the Fleet view. Agent reads desired_*, compares
                # against its own installed version on next heartbeat /
                # bundle pickup, and writes the slot-upgrade trigger
                # if mismatched. Both values None when nothing pending.
                "fleet_upgrade": {
                    "desired_appliance_version": server.desired_appliance_version,
                    "desired_slot_image_url": server.desired_slot_image_url,
                    # Phase 8f-8 — operator-triggered reboot intent.
                    # Agent fires the reboot-pending trigger when this
                    # flips to True; heartbeat handler clears it
                    # post-reconnect.
                    "reboot_requested": server.reboot_requested,
                },
            }
        if asyncio.get_event_loop().time() >= deadline:
            return Response(status_code=304, headers={"ETag": etag})
        await asyncio.sleep(LONGPOLL_POLL_INTERVAL)


@router.post("/heartbeat", response_model=AgentHeartbeatResponse)
async def agent_heartbeat(
    request: Request,
    body: AgentHeartbeatRequest,
    db: DB,
    auth: tuple[DHCPServer, dict[str, Any]] = Depends(_auth_agent),
) -> AgentHeartbeatResponse:
    server, payload = auth
    now = datetime.now(UTC)
    server.agent_last_seen = now
    server.last_health_check_at = now
    server.status = "active"
    # Capture the source IP so the operator can identify which host
    # the agent is on — the operator-set ``host`` column may not
    # match the real machine in NAT / distributed deployments.
    if request.client is not None:
        server.last_seen_ip = request.client.host
    if body.agent_version:
        server.agent_version = body.agent_version

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

    # Phase 8f-7 — auto-clear operator intent once the agent confirms
    # the upgrade landed. See dns/agents.py for the full rationale.
    if (
        server.desired_appliance_version is not None
        and server.installed_appliance_version
        and server.installed_appliance_version == server.desired_appliance_version
        and (server.last_upgrade_state in ("done", None))
    ):
        server.desired_appliance_version = None
        server.desired_slot_image_url = None

    # Phase 8f-8 — clear reboot_requested once the agent reconnects
    # post-reboot. See dns/agents.py for the full rationale; ~15 s
    # safety margin so a near-instant heartbeat doesn't false-clear.
    if server.reboot_requested and server.reboot_requested_at is not None:
        elapsed = (datetime.now(UTC) - server.reboot_requested_at).total_seconds()
        if elapsed > 15:
            server.reboot_requested = False
            server.reboot_requested_at = None

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
    """Bulk lease ingestion from the agent.

    In addition to upserting the DHCPLease row, we mirror live leases into
    IPAM as ``status='dhcp'`` rows (flagged ``auto_from_lease=True``) so the
    subnet view shows actively-leased addresses alongside manual ones.

    Policy:
      - Active lease + no IPAM row → create row with status='dhcp'.
      - Active lease + existing IPAM row that's 'available' or already
        auto_from_lease → overwrite hostname/MAC and flip to 'dhcp'.
      - Active lease + existing row that's manually allocated / static_dhcp
        / reserved → leave alone (operator owns that row; lease just
        co-exists in DHCPLease).
      - Released/expired lease → if the IPAM row is auto_from_lease, remove it.
    """
    from sqlalchemy import func as sa_func

    from app.models.ipam import IPAddress, Subnet

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

        # ── IPAM mirror ────────────────────────────────────────────────────
        # Find the subnet whose CIDR contains this IP (server-side via PG).
        subnet_res = await db.execute(
            select(Subnet).where(Subnet.network.op(">>=")(sa_func.inet(ev.ip_address)))
        )
        subnet = subnet_res.scalars().first()
        if subnet is None:
            continue  # IP not in any known subnet — can't mirror

        ipam_res = await db.execute(
            select(IPAddress).where(
                IPAddress.subnet_id == subnet.id,
                IPAddress.address == ev.ip_address,
            )
        )
        ipam_row = ipam_res.scalar_one_or_none()

        is_active = ev.state == "active"
        if is_active:
            if ipam_row is None:
                ipam_row = IPAddress(
                    subnet_id=subnet.id,
                    address=ev.ip_address,
                    hostname=(ev.hostname or "")[:253],
                    mac_address=ev.mac_address,
                    status="dhcp",
                    auto_from_lease=True,
                    dhcp_lease_id=str(lease.id) if lease.id else None,
                )
                db.add(ipam_row)
            elif ipam_row.status in ("available",) or ipam_row.auto_from_lease:
                ipam_row.hostname = (ev.hostname or ipam_row.hostname or "")[:253]
                ipam_row.mac_address = ev.mac_address
                ipam_row.status = "dhcp"
                ipam_row.auto_from_lease = True
                ipam_row.dhcp_lease_id = str(lease.id) if lease.id else None
            # else: manual/static — leave it alone

            # DDNS — mirrors services/dhcp/pull_leases.py. Only fires on
            # auto-from-lease rows inside DDNS-enabled subnets; errors are
            # logged but never break the lease upsert pass (DNS will
            # reconcile on the next event or sweep).
            if ipam_row is not None and ipam_row.auto_from_lease:
                try:
                    from app.services.dns.ddns import apply_ddns_for_lease

                    await apply_ddns_for_lease(
                        db,
                        subnet=subnet,
                        ipam_row=ipam_row,
                        client_hostname=ev.hostname,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "dhcp_agent_lease_ddns_failed",
                        server=str(server.id),
                        ip=ev.ip_address,
                        error=str(exc),
                    )

            # Auto-profile (Phase 1: active layer). Subnet-level opt-in;
            # the service applies the refresh-window dedupe + per-subnet
            # concurrency cap. Like the DDNS branch above, errors are
            # logged but never break the lease pass — profiling is
            # opportunistic.
            if ipam_row is not None and ipam_row.auto_from_lease:
                try:
                    from app.services.profiling.auto_profile import (
                        maybe_enqueue_for_lease,
                    )

                    await maybe_enqueue_for_lease(db, subnet=subnet, ipam_row=ipam_row)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "dhcp_agent_lease_auto_profile_failed",
                        server=str(server.id),
                        ip=ev.ip_address,
                        error=str(exc),
                    )
        else:  # expired / released / declined
            if ipam_row is not None and ipam_row.auto_from_lease:
                # Revoke DDNS BEFORE deleting the row — revoke reads
                # dns_record_id / hostname off the row to find what to
                # delete, and we don't want those fields gone yet.
                try:
                    from app.services.dns.ddns import revoke_ddns_for_lease

                    await revoke_ddns_for_lease(db, subnet=subnet, ipam_row=ipam_row)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "dhcp_agent_lease_ddns_revoke_failed",
                        server=str(server.id),
                        ip=ev.ip_address,
                        error=str(exc),
                    )
                await db.delete(ipam_row)

    await db.commit()
    return {"upserted": upserted}


@router.post("/ha-status")
async def agent_ha_status(
    body: HAStatusReport,
    db: DB,
    auth: tuple[DHCPServer, dict[str, Any]] = Depends(_auth_agent),
) -> dict[str, str]:
    """Update this server's Kea HA state from the agent's periodic poll.

    Idempotent — the agent is free to call this as often as it wants
    (typical cadence is every 15-30s alongside its existing heartbeat).
    Only updates the two ``ha_*`` columns on DHCPServer; never rewrites
    config or creates audit rows. Drift-free reporting is out of band
    from the config push path.
    """
    server, _ = auth
    server.ha_state = body.state
    server.ha_last_heartbeat_at = datetime.now(UTC)
    await db.commit()
    return {"status": "ok"}


class DHCPMetricReport(BaseModel):
    """One time-bucketed sample of Kea packet counters.

    Shape mirrors ``DNSMetricReport`` — the agent emits deltas
    computed from two consecutive ``statistic-get-all`` snapshots so
    a Kea restart (counters reset to zero) only drops one bucket on
    the floor instead of creating a spurious spike when the next
    poll's counters come in lower than the previous ones.
    """

    bucket_at: datetime
    discover: int = 0
    offer: int = 0
    request: int = 0
    ack: int = 0
    nak: int = 0
    decline: int = 0
    release: int = 0
    inform: int = 0


@router.post("/metrics")
async def agent_metrics(
    body: DHCPMetricReport,
    db: DB,
    auth: tuple[DHCPServer, dict[str, Any]] = Depends(_auth_agent),
) -> dict[str, str]:
    """Ingest one sample row. Idempotent on ``(server_id, bucket_at)``."""
    server, _ = auth
    values = {
        "discover": max(0, body.discover),
        "offer": max(0, body.offer),
        "request": max(0, body.request),
        "ack": max(0, body.ack),
        "nak": max(0, body.nak),
        "decline": max(0, body.decline),
        "release": max(0, body.release),
        "inform": max(0, body.inform),
    }
    existing = await db.get(DHCPMetricSample, (server.id, body.bucket_at))
    if existing is None:
        db.add(DHCPMetricSample(server_id=server.id, bucket_at=body.bucket_at, **values))
    else:
        for k, v in values.items():
            setattr(existing, k, v)
    await db.commit()
    return {"status": "ok"}


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


# ── Activity log ingestion ───────────────────────────────────────────


class DHCPLogBatch(BaseModel):
    """Batch of raw ``kea-dhcp4`` log lines pushed by the agent.

    Same shape as the DNS query log batch. The agent tails Kea's
    file output (we configure a file ``output_options`` in the
    rendered ``kea-dhcp4.conf`` so the lines are tail-able), batches
    them, and POSTs every few seconds.
    """

    lines: list[str]


@router.post("/log-entries")
async def agent_log_entries(
    body: DHCPLogBatch,
    db: DB,
    auth: tuple[DHCPServer, dict[str, Any]] = Depends(_auth_agent),
) -> dict[str, Any]:
    """Ingest a batch of Kea log lines from the agent.

    Capped at 1000 lines per request. The parser tolerates lines it
    can't fully match — they still get inserted with the raw text
    preserved so the UI shows everything Kea emitted.
    """
    from app.services.logs.kea_parser import parse_kea_line  # noqa: PLC0415

    server, _ = auth
    capped = body.lines[:1000]
    dropped = max(0, len(body.lines) - len(capped))
    now = datetime.now(UTC)
    inserted = 0
    for raw in capped:
        parsed = parse_kea_line(raw, fallback_ts=now)
        if parsed is None:
            continue
        db.add(
            DHCPLogEntry(
                server_id=server.id,
                ts=parsed.ts,
                severity=parsed.severity,
                code=parsed.code,
                mac_address=parsed.mac_address,
                ip_address=parsed.ip_address,
                transaction_id=parsed.transaction_id,
                raw=parsed.raw,
            )
        )
        inserted += 1
    await db.commit()
    return {"status": "ok", "inserted": inserted, "dropped": dropped}


# ── DHCP fingerprint ingestion (Phase 2 device profiling) ─────────────


@router.post("/dhcp-fingerprints")
async def agent_dhcp_fingerprints(
    body: DHCPFingerprintBatch,
    db: DB,
    auth: tuple[DHCPServer, dict[str, Any]] = Depends(_auth_agent),
) -> dict[str, int]:
    """Bulk fingerprint upsert from the agent's scapy sniffer.

    Capped at 500 entries per request so a misbehaving agent can't
    OOM us. For each fingerprint we either create a new
    ``dhcp_fingerprint`` row or refresh ``last_seen_at`` on the
    existing one. Fresh / signature-changed rows enqueue a Celery
    task that does the slow part (fingerbank lookup +
    ``IPAddress.device_*`` stamping) so the agent's POST returns
    fast.

    No audit row written — fingerprint observations are too
    high-volume to land in the audit log; the agent generates one
    per DISCOVER/REQUEST per device. Operator-triggered actions
    against this surface DO write audit (see the IPAM router's
    fingerprint endpoints).
    """
    from app.services.profiling.passive import upsert_fingerprint

    server, _ = auth
    capped = body.fingerprints[:500]
    dropped = max(0, len(body.fingerprints) - len(capped))
    upserted = 0
    enqueue_macs: list[str] = []
    for fp in capped:
        try:
            _, signature_changed = await upsert_fingerprint(
                db,
                mac_address=fp.mac_address,
                option_55=fp.option_55,
                option_60=fp.option_60,
                option_77=fp.option_77,
                client_id=fp.client_id,
            )
        except Exception as exc:  # noqa: BLE001
            # One bad row shouldn't kill the batch — log + skip.
            logger.warning(
                "dhcp_fingerprint_upsert_failed",
                server=str(server.id),
                mac=fp.mac_address,
                error=str(exc),
            )
            continue
        upserted += 1
        if signature_changed:
            enqueue_macs.append(fp.mac_address)

    await db.commit()

    # Dispatch Celery tasks for fresh / changed fingerprints. Lazy
    # import to avoid pulling Celery into the request import graph
    # for every other endpoint in this module.
    if enqueue_macs:
        try:
            from app.tasks.dhcp_fingerprint import lookup_fingerprint_task

            for mac in enqueue_macs:
                lookup_fingerprint_task.delay(mac)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "dhcp_fingerprint_dispatch_failed",
                server=str(server.id),
                count=len(enqueue_macs),
                error=str(exc),
            )

    return {
        "upserted": upserted,
        "dropped": dropped,
        "enqueued": len(enqueue_macs),
    }
