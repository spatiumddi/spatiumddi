"""Fleet-firewall apply coordination (#285 Phase 3c-2 — advisory only).

The §7 coordination wrapper. In 3c the Lease is NOT acquired by any code path
(CRUD edits a row; the heartbeat render applies on the next tick — there is no
synchronous fan-out apply yet). What ships now is:

* ``FIREWALL_LEASE_NAME`` — the reserved coordination-lease name, so the
  deferred apply/canary fan-out (the carved-out 2c follow-up) and the
  multi-node OS-upgrade orchestrator never pick colliding names.
* ``upgrade_in_flight`` — the ``/preview`` advisory check. A firewall apply
  during a multi-node OS upgrade would fight the orchestrator's per-node
  cordon/drain/reboot, so preview surfaces "an OS upgrade is in flight" rather
  than letting an operator stage an apply that the (future) fan-out would
  refuse to hold the lease for.

Real ``acquire``/``hold`` lands with the deferred apply fan-out — keeping
3c's blast radius to a read-only advisory.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

FIREWALL_LEASE_NAME = "spatium-firewall-apply-lock"


async def upgrade_in_flight(db: Any) -> bool:
    """True when a multi-node OS rolling-upgrade run is ``running`` — the
    window a firewall apply must defer to (advisory in 3c)."""
    from app.models.system_upgrade import SystemUpgradeRun

    row = (
        await db.execute(select(SystemUpgradeRun.id).where(SystemUpgradeRun.state == "running"))
    ).first()
    return row is not None
