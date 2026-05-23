"""Operator Copilot tools for multi-node rolling upgrades (#296 Phase A).

Phase A ships a single read-only tool — ``find_upgrade_preflight`` —
that lets the operator ask "what would block a rolling upgrade to
2026.06.01-1?" without leaving the chat drawer. The tool calls the
same preflight aggregator the REST endpoint uses, so the chat answer
matches the Fleet UI's preflight panel exactly.

Phases C/D will add ``find_upgrade_runs`` (history list) and
``propose_start_upgrade`` (apply-gated write) when those surfaces
exist. Phase A's read-only-only scope means there's nothing the model
can do here that an operator can't already do with the REST endpoint.

Superadmin-gated like every other appliance/fleet tool — operator
access only.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.services.ai.tools.base import register_tool
from app.services.upgrades import preflight


def _superadmin_gate(user: User) -> dict[str, Any] | None:
    if not user.is_superadmin:
        return {"error": ("Rolling-upgrade preflight is restricted to superadmin users.")}
    return None


class FindUpgradePreflightArgs(BaseModel):
    target_version: str = Field(
        description=(
            "CalVer tag to evaluate the upgrade path against, e.g. "
            "``2026.06.01-1``. Must be a 1-64 char string."
        ),
        min_length=1,
        max_length=64,
    )


@register_tool(
    name="find_upgrade_preflight",
    description=(
        "Run pre-flight safety checks for a multi-node rolling upgrade to "
        "the given target version (superadmin only, read-only). Returns "
        "the same structured report the Fleet UI's preflight panel shows: "
        "quorum (cluster size + Ready state), CNPG replication lag, "
        "``/var`` disk headroom, version-path validity (CalVer parse + "
        "forward-jump + 90-day-gap warning), and whether another upgrade "
        "is already in flight (Lease holder). Use to answer 'is the "
        "cluster ready for an upgrade to <tag>?', 'which check is "
        "blocking the next upgrade?', or 'what's the replication lag "
        "right now?'. Does not start an upgrade — that requires the "
        "REST start endpoint (Phase D)."
    ),
    args_model=FindUpgradePreflightArgs,
    category="admin",
    default_enabled=True,
    # The upgrade surface doesn't have a feature module — it's
    # cluster-shape infrastructure, always on for multi-node deploys.
    module=None,
)
async def find_upgrade_preflight(
    db: AsyncSession,  # noqa: ARG001 — registered tools share signature
    user: User,
    args: FindUpgradePreflightArgs,
) -> dict[str, Any]:
    if (err := _superadmin_gate(user)) is not None:
        return err
    report = await preflight.run_all(target_version=args.target_version)
    return report.to_dict()
