"""Multi-node rolling upgrades — read-only surface (#296 Phase A).

Mounted at ``/api/v1/upgrades``. Phase A ships only:

    GET   /preflight?target=<calver-tag>   — run safety checks; no
                                              writes; no lease acquired.
    GET   /lease                           — current mutex state for
                                              surfacing "an upgrade is
                                              in flight" on the Fleet UI.

Phases C/D will add ``POST /start``, ``POST /halt``, ``GET /runs``,
etc. as the orchestrator lands; we deliberately keep the Phase A
surface read-only so operators can poke at preflight without any
write-permission risk.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.permissions import require_permission
from app.services.upgrades import mutex, preflight

logger = structlog.get_logger(__name__)

router = APIRouter()


class PreflightCheckOut(BaseModel):
    name: str
    level: str = Field(description="ok | warn | fail")
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class PreflightReportOut(BaseModel):
    target_version: str
    current_version: str
    overall: str = Field(description="worst level across results")
    can_start: bool = Field(description="overall != 'fail'")
    results: list[PreflightCheckOut]


class LeaseStateOut(BaseModel):
    held: bool
    holder: str | None
    renew_time: str | None
    transitions: int
    expired: bool


@router.get(
    "/preflight",
    response_model=PreflightReportOut,
    dependencies=[Depends(require_permission("read", "appliance"))],
    summary="Run pre-flight safety checks for a rolling upgrade to <target>",
)
async def get_preflight(target: str) -> PreflightReportOut:
    """Run every Phase A safety check and return the aggregate.

    Read-only — does not acquire the upgrade lease, does not write a
    ``system_upgrade_run`` row. The operator can call this freely;
    Phases C/D's ``POST /start`` re-runs the same checks under a held
    lease before any cluster mutation.
    """
    if not target or len(target) > 64:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "target must be a 1-64 char CalVer tag (e.g. 2026.06.01-1)",
        )
    report = await preflight.run_all(target_version=target)
    return PreflightReportOut(**report.to_dict())


@router.get(
    "/lease",
    response_model=LeaseStateOut,
    dependencies=[Depends(require_permission("read", "appliance"))],
    summary="Current state of the cluster-wide upgrade mutex",
)
async def get_lease_state() -> LeaseStateOut:
    """Surface the upgrade Lease without trying to claim it.

    UI polls this on the Fleet → Upgrades tab to render a banner
    ("upgrade in flight, holder=api-2") and disable the Start button.
    """
    state = mutex.get_state()
    return LeaseStateOut(
        held=state.held,
        holder=state.holder,
        renew_time=state.renew_time,
        transitions=state.transitions,
        expired=state.expired,
    )
