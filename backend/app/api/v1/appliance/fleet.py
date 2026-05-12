"""Phase 8f-5 — Fleet upgrade orchestration endpoint.

Mounted at ``/api/v1/appliance/fleet``. Endpoints:
    GET    /              — list every registered DNS + DHCP agent
                            with its slot state, deployment kind, and
                            currently-set desired version.
    POST   /{kind}/{id}/upgrade  — stamp ``desired_appliance_version``
                                    + ``desired_slot_image_url`` on
                                    the agent's server row. The agent
                                    picks it up via ConfigBundle
                                    long-poll and fires the local
                                    slot-upgrade trigger.
    POST   /{kind}/{id}/clear    — clear the desired-version stamp
                                    (operator-cancellable before the
                                    agent's next long-poll, or after
                                    a successful upgrade to leave
                                    the row in a clean state).

``kind`` is ``dns`` or ``dhcp``; the route is uniform across both
server kinds so the UI can render them in a single Fleet table.
"""

from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DB, CurrentUser
from app.core.permissions import require_permission
from app.models.audit import AuditLog
from app.models.dhcp import DHCPServer
from app.models.dns import DNSServer

logger = structlog.get_logger(__name__)

router = APIRouter()

ServerKind = Literal["dns", "dhcp"]


class FleetAgentRow(BaseModel):
    """One row in the Fleet view, common shape across DNS + DHCP."""

    kind: ServerKind
    id: str
    name: str
    host: str
    # Last-known agent state (None on rows that haven't checked in yet).
    deployment_kind: str | None
    installed_appliance_version: str | None
    current_slot: str | None
    durable_default: str | None
    is_trial_boot: bool
    last_upgrade_state: str | None
    last_upgrade_state_at: str | None
    last_seen_at: str | None
    last_seen_ip: str | None
    # Operator intent.
    desired_appliance_version: str | None
    desired_slot_image_url: str | None


class FleetResponse(BaseModel):
    agents: list[FleetAgentRow]


class UpgradeRequest(BaseModel):
    desired_appliance_version: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "CalVer release tag the agent should upgrade to (e.g. "
            "``2026.05.13-1``). Compared against the agent's installed "
            "version on the next ConfigBundle pickup; mismatch fires "
            "the slot-upgrade trigger."
        ),
    )
    desired_slot_image_url: str = Field(
        min_length=1,
        max_length=2048,
        description=(
            "URL or absolute path the agent passes to "
            "spatium-upgrade-slot apply. The Fleet UI normally fills "
            "the GitHub Release stable URL; operators on air-gapped "
            "sites can paste a self-hosted mirror or local file path."
        ),
    )


class UpgradeResponse(BaseModel):
    kind: ServerKind
    id: str
    desired_appliance_version: str
    desired_slot_image_url: str


def _serialise_dns(s: DNSServer) -> FleetAgentRow:
    return FleetAgentRow(
        kind="dns",
        id=str(s.id),
        name=s.name,
        host=s.host,
        deployment_kind=s.deployment_kind,
        installed_appliance_version=s.installed_appliance_version,
        current_slot=s.current_slot,
        durable_default=s.durable_default,
        is_trial_boot=s.is_trial_boot,
        last_upgrade_state=s.last_upgrade_state,
        last_upgrade_state_at=(
            s.last_upgrade_state_at.isoformat() if s.last_upgrade_state_at else None
        ),
        last_seen_at=s.last_seen_at.isoformat() if s.last_seen_at else None,
        last_seen_ip=s.last_seen_ip,
        desired_appliance_version=s.desired_appliance_version,
        desired_slot_image_url=s.desired_slot_image_url,
    )


def _serialise_dhcp(s: DHCPServer) -> FleetAgentRow:
    return FleetAgentRow(
        kind="dhcp",
        id=str(s.id),
        name=s.name,
        host=s.host,
        deployment_kind=s.deployment_kind,
        installed_appliance_version=s.installed_appliance_version,
        current_slot=s.current_slot,
        durable_default=s.durable_default,
        is_trial_boot=s.is_trial_boot,
        last_upgrade_state=s.last_upgrade_state,
        last_upgrade_state_at=(
            s.last_upgrade_state_at.isoformat() if s.last_upgrade_state_at else None
        ),
        last_seen_at=s.agent_last_seen.isoformat() if s.agent_last_seen else None,
        last_seen_ip=s.last_seen_ip,
        desired_appliance_version=s.desired_appliance_version,
        desired_slot_image_url=s.desired_slot_image_url,
    )


@router.get(
    "",
    response_model=FleetResponse,
    dependencies=[Depends(require_permission("read", "appliance"))],
    summary="List every registered DNS + DHCP agent with current + desired state",
)
async def list_fleet(db: DB) -> FleetResponse:
    """Snapshot every registered agent for the Fleet view.

    Includes rows that haven't reported slot state yet (older agents,
    docker / k8s deploys) — the UI shows them with NULL slot info and
    branches the affordances on ``deployment_kind``.
    """
    dns_rows = (await db.execute(select(DNSServer))).scalars().all()
    dhcp_rows = (await db.execute(select(DHCPServer))).scalars().all()
    agents = [_serialise_dns(s) for s in dns_rows] + [_serialise_dhcp(s) for s in dhcp_rows]
    # Stable sort: kind, then host name (operator-friendly).
    agents.sort(key=lambda a: (a.kind, a.name.lower()))
    return FleetResponse(agents=agents)


async def _load_server(db: DB, kind: ServerKind, server_id: str) -> DNSServer | DHCPServer:
    """Resolve a kind+id into the matching SQLAlchemy row."""
    import uuid as _uuid

    try:
        uid = _uuid.UUID(server_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "server_id must be a UUID") from exc
    server: DNSServer | DHCPServer | None
    if kind == "dns":
        server = await db.get(DNSServer, uid)
    else:
        server = await db.get(DHCPServer, uid)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{kind} server not found")
    return server


@router.post(
    "/{kind}/{server_id}/upgrade",
    response_model=UpgradeResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Stamp desired_appliance_version + desired_slot_image_url on an agent row",
)
async def schedule_upgrade(
    kind: ServerKind,
    server_id: str,
    body: UpgradeRequest,
    db: DB,
    user: CurrentUser,
) -> UpgradeResponse:
    server = await _load_server(db, kind, server_id)
    if server.deployment_kind not in ("appliance", None):
        # Docker / k8s rows can't accept a slot upgrade — the Fleet UI
        # shows operator copy-paste commands instead. 422 here so the
        # API surface clearly distinguishes "wrong server kind" from
        # the per-row "no Upgrade button" affordance the UI renders.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"agent deployment_kind={server.deployment_kind!r} doesn't accept slot upgrades; "
            "upgrade docker / k8s deployments by rolling the container image instead",
        )

    server.desired_appliance_version = body.desired_appliance_version
    server.desired_slot_image_url = body.desired_slot_image_url

    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="fleet_schedule_upgrade",
            resource_type=f"{kind}_server",
            resource_id=server_id,
            resource_display=server.name,
            new_value={
                "desired_appliance_version": body.desired_appliance_version,
                "desired_slot_image_url": body.desired_slot_image_url,
            },
            result="success",
        )
    )
    await db.commit()
    logger.info(
        "appliance_fleet_upgrade_scheduled",
        kind=kind,
        server_id=server_id,
        desired_version=body.desired_appliance_version,
        user=user.username,
    )
    return UpgradeResponse(
        kind=kind,
        id=server_id,
        desired_appliance_version=body.desired_appliance_version,
        desired_slot_image_url=body.desired_slot_image_url,
    )


@router.post(
    "/{kind}/{server_id}/clear",
    response_model=FleetAgentRow,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Clear the desired-version stamp on an agent row",
)
async def clear_upgrade(
    kind: ServerKind,
    server_id: str,
    db: DB,
    user: CurrentUser,
) -> FleetAgentRow:
    """Clear ``desired_*`` fields. Cancellation path before the agent's
    next long-poll picks up the intent; also useful as a clean-up
    after a successful upgrade so the row's "pending" indicator
    clears in the Fleet view."""
    server = await _load_server(db, kind, server_id)
    prior = {
        "desired_appliance_version": server.desired_appliance_version,
        "desired_slot_image_url": server.desired_slot_image_url,
    }
    server.desired_appliance_version = None
    server.desired_slot_image_url = None

    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="fleet_clear_upgrade",
            resource_type=f"{kind}_server",
            resource_id=server_id,
            resource_display=server.name,
            old_value=prior,
            result="success",
        )
    )
    await db.commit()
    logger.info(
        "appliance_fleet_upgrade_cleared",
        kind=kind,
        server_id=server_id,
        user=user.username,
    )
    if isinstance(server, DNSServer):
        return _serialise_dns(server)
    return _serialise_dhcp(server)
