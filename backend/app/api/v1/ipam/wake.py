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
from app.services import wol
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
    result = await _run_wake(db, wire, target)

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
