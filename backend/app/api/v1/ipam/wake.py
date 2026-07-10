"""Wake-on-LAN action for an IP address (issue #533).

A single ``POST /ipam/addresses/{id}/wake`` that sends a magic packet to
the IP's MAC. The broadcast target is derived from the IP's subnet, so the
UI just needs the address id. Two vantages, mirroring the network-tools
pattern:

* ``server`` (default) — the api container broadcasts directly. Only wakes
  hosts on a segment the control plane can reach (the common single-box
  case).
* ``appliance`` — dispatch to a Fleet appliance whose NIC sits on the
  target's segment, so the packet originates in the right broadcast
  domain. Reuses the generic nettool command channel (``agent_cmd``).

Gated by ``use_network_tools`` (WoL is a network tool) and audited against
the IP row, like the other active tools.

**Post-wake verify (issue #596 Phase 1b).** Optionally, the wake can arm the
same liveness-verify + bounded re-wake chain that scheduled wakes use. Because
that machinery is keyed on a ``WolRun``, an opted-in ad-hoc wake mints an
ephemeral run (``schedule_id = NULL``, ``trigger = "adhoc"``) with exactly one
target and carries its verify config in ``WolRun.verify_params`` — there is no
parent schedule row to read it from. The run shows up in the Wake Schedules
History tab like any other. A failing ad-hoc wake does **not** raise an alert:
alerting is keyed on a schedule subject, and an ad-hoc run has none.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.api.v1.dhcp._audit import write_audit
from app.core.permissions import require_permission
from app.models.ipam import IPAddress
from app.models.wol_schedule import (
    STATUS_OK,
    VERIFY_STATE_PENDING,
    WolRun,
    WolRunTarget,
)
from app.services import wol
from app.services.feature_modules import is_module_enabled
from app.services.nettools.schemas import NetToolTarget

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["ipam"])

# The verify chain lives behind the Wake Schedules feature module — its History
# surface, its beat sweep (which reaps a crash-wedged verify) and its REST reads
# are all module-gated. Arming a verify while the module is off would strand the
# run: invisible in the UI and never reclaimed. Refuse instead of stranding it.
_WAKE_SCHEDULER_MODULE = "tools.wake_scheduler"

# WoL is a network tool — reuse the existing tools permission (already seeded
# into the Network Editor built-in role). ``read`` matches the rest of the
# network-tools surface (ping / nmap / dig all gate on read:use_network_tools),
# so an operator granted read to unlock the tools page can also wake a host.
PERMISSION = "use_network_tools"
_RequirePerm = Depends(require_permission("read", PERMISSION))


class WakeRequest(BaseModel):
    port: int = Field(default=9, ge=1, le=65535)
    # Optional run-from vantage. None / kind="server" ⇒ the api container
    # sends; kind="appliance" + id ⇒ dispatch to that appliance's segment.
    target: NetToolTarget | None = None

    # ── Post-wake verify (#596 Phase 1b) — off unless asked for ──────────
    verify: bool = False
    # Grace for the host to POST + bring its NIC up before the first probe,
    # and the gap between re-wake passes. Bounds mirror the schedule schema.
    verify_wait_seconds: int = Field(default=60, ge=5, le=3600)
    # Re-wake passes after the first probe. Defaults to 1 (probe, re-wake once,
    # probe again) — the operator's "if it didn't come up, try again".
    verify_retries: int = Field(default=1, ge=0, le=10)
    verify_method: Literal["ping", "tcp", "seen", "auto"] = "auto"


@router.post(
    "/addresses/{address_id}/wake",
    response_model=wol.WolResult,
    dependencies=[_RequirePerm],
)
async def wake_address(
    address_id: uuid.UUID,
    body: WakeRequest,
    db: DB,
    current_user: CurrentUser,
) -> wol.WolResult:
    # Shared resolver — same IP → (mac, broadcast) derivation the AI operation
    # uses, so the logic + messages don't drift.
    try:
        ip, mac, broadcast = await wol.resolve_wake_params(db, address_id)
    except wol.WolTargetError as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND if exc.not_found else status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=str(exc),
        ) from exc

    if body.verify and not await is_module_enabled(db, _WAKE_SCHEDULER_MODULE):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Post-wake verify requires the Wake Schedules feature module "
                f"({_WAKE_SCHEDULER_MODULE}), which is disabled."
            ),
        )

    wire = wol.WolWireRequest(mac=mac, broadcast=broadcast, port=body.port)
    target = body.target or NetToolTarget()
    result = await _run_wake(db, wire, target)

    # Only arm verify for a packet that actually went out — a failed send has
    # nothing to verify, and minting a run for it would show up in History as a
    # phantom wake.
    run_id = (
        await _arm_adhoc_verify(db, body, ip, wire, target, current_user.id)
        if body.verify and result.sent
        else None
    )

    write_audit(
        db,
        user=current_user,
        action="wake_on_lan",
        resource_type="ip_address",
        resource_id=str(ip.id),
        resource_display=str(ip.address),
        new_value={
            "mac": wire.mac,
            "broadcast": wire.broadcast,
            "port": wire.port,
            "ran_from": result.ran_from,
            # NULL unless the operator opted into post-wake verify.
            "verify_run_id": str(run_id) if run_id else None,
            "verify_method": body.verify_method if run_id else None,
        },
        result="success" if result.sent else "failure",
    )
    # The run + its target must be durable before the verify task can claim
    # them; the task's atomic ``pending`` claim runs in a separate session.
    await db.commit()

    if run_id is not None:
        _enqueue_adhoc_verify(run_id, body.verify_wait_seconds)
    return result


async def _arm_adhoc_verify(
    db: DB,
    body: WakeRequest,
    ip: IPAddress,
    wire: wol.WolWireRequest,
    target: NetToolTarget,
    actor_id: uuid.UUID,
) -> uuid.UUID:
    """Mint the ephemeral ``WolRun`` + single ``WolRunTarget`` for an ad-hoc wake.

    Does not commit — the caller owns the transaction (the audit row lands in the
    same one). The run is left in ``verify_state='pending'`` at ``verify_attempt
    = 1`` with the lease stamped, exactly as :func:`run_wol_schedule` arms a
    scheduled run, so if the enqueue below never reaches the broker the sweep's
    verify reaper reclaims it rather than leaving it wedged.
    """
    now = datetime.now(UTC)
    vantage = wol.vantage_to_jsonb(target)
    run = WolRun(
        schedule_id=None,
        trigger="adhoc",
        status=STATUS_OK,
        started_at=now,
        finished_at=now,
        target_count=1,
        sent_count=1,
        verify_state=VERIFY_STATE_PENDING,
        verify_attempt=1,
        verify_claimed_at=now,
        # The sole source of verify + re-wake config for a schedule-less run.
        # Keys are the WolSchedule attribute names verbatim, so _verify_run reads
        # either source with the same key and a snapshot can't silently drift from
        # the column it mirrors (see _cfg in tasks/wol_scheduler.py).
        verify_params={
            "verify_method": body.verify_method,
            "verify_wait_seconds": body.verify_wait_seconds,
            "verify_retries": body.verify_retries,
            "vantage": vantage,
            "port": wire.port,
            # One packet, no burst: an ad-hoc wake is a deliberate single action.
            "repeat_count": 1,
            "repeat_interval_ms": 0,
            "stagger_ms": 0,
        },
        triggered_by_user_id=actor_id,
    )
    db.add(run)
    await db.flush()
    db.add(
        WolRunTarget(
            run_id=run.id,
            ip_address_id=ip.id,
            address=str(ip.address),
            mac=wire.mac,
            subnet_id=ip.subnet_id,
            broadcast=wire.broadcast,
            vantage=vantage,
            mac_source="ip",
            sent=True,
            verified=None,
            wake_attempts=1,
        )
    )
    await db.flush()
    return run.id


def _enqueue_adhoc_verify(run_id: uuid.UUID, wait_seconds: int) -> None:
    """Best-effort push of the first verify pass to the broker.

    A broker hiccup must never fail a wake that already went out on the wire. The
    run is committed at ``pending``, so the sweep's verify reaper picks it up once
    the lease expires — the enqueue is an optimisation, never the only path.
    """
    # Lazy import — keep the task module (celery bootstrap) off the router's
    # import-time graph, matching the wake-scheduler router + pcap.
    from app.tasks.wol_scheduler import verify_wol_run  # noqa: PLC0415

    try:
        verify_wol_run.apply_async(args=[str(run_id), 1], countdown=max(0, wait_seconds))
    except Exception as exc:  # noqa: BLE001 — verify is best-effort.
        logger.warning("wol_adhoc_verify_enqueue_failed", run_id=str(run_id), error=str(exc))


async def _run_wake(db: DB, wire: wol.WolWireRequest, target: NetToolTarget) -> wol.WolResult:
    """Send via the requested vantage, translating WolDispatchError → HTTP.
    The heavy lifting (wake_from_server / wake_via_appliance) lives in the wol
    service and is shared with POST /tools/wol; this is just the HTTP mapping."""
    try:
        if target.kind == "server":
            return await wol.wake_from_server(wire)
        if target.kind == "appliance":
            if target.id is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="target.id is required when target.kind is 'appliance'.",
                )
            return await wol.wake_via_appliance(db, target.id, wire)
    except wol.WolDispatchError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    # dns_agent / dhcp_agent vantages are reserved in NetToolTarget but not
    # wired for WoL yet — reject clearly rather than silently no-op.
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Wake-on-LAN cannot run from a {target.kind!r} vantage.",
    )
