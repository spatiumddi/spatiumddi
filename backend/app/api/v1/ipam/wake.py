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
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.api.v1.dhcp._audit import write_audit
from app.core.permissions import require_permission
from app.models.appliance import Appliance
from app.services import wol
from app.services.appliance import agent_cmd
from app.services.nettools.schemas import NetToolTarget

router = APIRouter(tags=["ipam"])

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

    wire = wol.WolWireRequest(mac=mac, broadcast=broadcast, port=body.port)
    target = body.target or NetToolTarget()

    if target.kind == "server":
        try:
            await wol.send_magic_packet(wire.mac, wire.broadcast, wire.port)
        except OSError as exc:
            # No route to the broadcast (ENETUNREACH etc.) is the documented
            # single-box limitation — surface it cleanly like the appliance
            # path's 502 rather than a raw 500.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"Could not broadcast the magic packet from the server: {exc}. "
                    "Try an appliance vantage on the target's segment."
                ),
            ) from exc
        result = wol.WolResult(
            mac=wire.mac,
            broadcast=wire.broadcast,
            port=wire.port,
            sent=True,
            ran_from="server",
        )
    elif target.kind == "appliance":
        result = await _wake_via_appliance(db, current_user, wire, target)
    else:
        # dns_agent / dhcp_agent vantages are reserved in NetToolTarget but
        # not wired for WoL yet — reject clearly rather than silently no-op.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Wake-on-LAN cannot run from a {target.kind!r} vantage.",
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
        },
        result="success" if result.sent else "failure",
    )
    await db.commit()
    return result


async def _wake_via_appliance(
    db: DB,
    current_user: CurrentUser,
    wire: wol.WolWireRequest,
    target: NetToolTarget,
) -> wol.WolResult:
    if target.id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target.id is required when target.kind is 'appliance'.",
        )
    appliance = await db.get(Appliance, target.id)
    if appliance is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appliance not found.")
    ready = agent_cmd.appliance_ready(state=appliance.state, last_seen_at=appliance.last_seen_at)
    try:
        outcome = await agent_cmd.enqueue_command(
            appliance.id,
            "wol",
            wire.model_dump(mode="json"),
            ready=ready,
            # Match the sibling nettool dispatch (tools/router.py): a busy
            # supervisor mid-poll needs headroom or a healthy appliance 504s.
            timeout=30.0,
        )
    except agent_cmd.ApplianceOffline as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Appliance {appliance.hostname!r} is offline or not approved.",
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Appliance {appliance.hostname!r} did not send the packet in time.",
        ) from exc
    if outcome.error is not None or outcome.result is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Appliance {appliance.hostname!r} could not send the magic packet: "
                f"{outcome.error or 'no result returned'}"
            ),
        )
    result = wol.WolResult.model_validate(outcome.result)
    return result.model_copy(update={"ran_from": f"appliance:{appliance.hostname}"})
