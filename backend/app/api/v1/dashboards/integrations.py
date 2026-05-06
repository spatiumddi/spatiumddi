"""Integrations dashboard tab summary (issue #108).

Per-integration health rollup. Returns one panel per *enabled*
integration (Kubernetes / Docker / Proxmox / Tailscale) so the
front-end doesn't have to know which feature flags are on. Each
target row contributes its ``last_synced_at`` + ``last_sync_error``
+ ``sync_interval_seconds`` so the panel can colour "stale" rows
red without per-target heuristics.

The "stale" threshold is ``2 × sync_interval_seconds`` — if a
target hasn't been seen within twice its configured cadence the
sweep loop is genuinely behind, not just mid-cycle.

Recent reconciler errors come from the audit log (every
integration writes an audit row on failed sync) filtered to the
matching ``resource_type`` set. Capped at 20 rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import desc, select

from app.api.deps import DB, CurrentUser  # noqa: F401
from app.models.audit import AuditLog
from app.models.docker import DockerHost
from app.models.kubernetes import KubernetesCluster
from app.models.proxmox import ProxmoxNode
from app.models.settings import PlatformSettings
from app.models.tailscale import TailscaleTenant

router = APIRouter()


# ── Schemas ─────────────────────────────────────────────────────────


class IntegrationTargetRow(BaseModel):
    id: str
    display: str
    sync_interval_seconds: int
    last_synced_at: datetime | None
    last_sync_error: str | None
    is_stale: bool  # last_synced_at older than 2× sync_interval, or never


class IntegrationPanel(BaseModel):
    kind: str  # "kubernetes" | "docker" | "proxmox" | "tailscale"
    label: str
    enabled: bool
    target_count: int
    healthy_count: int
    stale_count: int
    error_count: int
    targets: list[IntegrationTargetRow]


class IntegrationErrorRow(BaseModel):
    id: str
    integration: str  # resource_type from audit_log
    target_id: str
    target_display: str
    error_detail: str | None
    timestamp: datetime


class IntegrationsDashboardSummary(BaseModel):
    generated_at: datetime
    panels: list[IntegrationPanel]
    recent_errors: list[IntegrationErrorRow]


_TARGET_LIMIT = 20
_RECENT_ERROR_LIMIT = 20

# Resource_type values audit-logged by each integration's reconciler.
# Mirrors the strings emitted in ``services/{kubernetes,docker,
# proxmox,tailscale}/reconcile.py``.
_INTEGRATION_RESOURCE_TYPES = (
    "kubernetes_cluster",
    "docker_host",
    "proxmox_node",
    "tailscale_tenant",
)


def _is_stale(last: datetime | None, interval_seconds: int, now: datetime) -> bool:
    """A target is stale when it's never synced (last is None) or its
    last sync is older than 2× its configured interval. Rounded up to
    a 60 s minimum so a freshly-edited interval doesn't immediately
    flag every target red while the next tick is in flight."""
    if last is None:
        return True
    threshold = max(60, interval_seconds * 2)
    return (now - last) > timedelta(seconds=threshold)


def _build_panel(
    *,
    kind: str,
    label: str,
    enabled: bool,
    targets: list[Any],
    display_attr: str,
    now: datetime,
) -> IntegrationPanel:
    """Aggregate one integration's targets into the dashboard panel."""
    rows: list[IntegrationTargetRow] = []
    healthy = 0
    stale = 0
    errors = 0
    for t in targets:
        last = getattr(t, "last_synced_at", None)
        interval = int(getattr(t, "sync_interval_seconds", 60) or 60)
        last_err = getattr(t, "last_sync_error", None)
        is_stale = _is_stale(last, interval, now)
        if last_err:
            errors += 1
        elif is_stale:
            stale += 1
        else:
            healthy += 1
        rows.append(
            IntegrationTargetRow(
                id=str(t.id),
                display=str(getattr(t, display_attr, "") or ""),
                sync_interval_seconds=interval,
                last_synced_at=last,
                last_sync_error=last_err,
                is_stale=is_stale,
            )
        )
    return IntegrationPanel(
        kind=kind,
        label=label,
        enabled=enabled,
        target_count=len(targets),
        healthy_count=healthy,
        stale_count=stale,
        error_count=errors,
        targets=rows[:_TARGET_LIMIT],
    )


# ── Route ───────────────────────────────────────────────────────────


@router.get("/integrations/summary", response_model=IntegrationsDashboardSummary)
async def integrations_summary(
    db: DB, current_user: CurrentUser  # noqa: ARG001
) -> IntegrationsDashboardSummary:
    """Single-shot rollup for the Integrations dashboard tab."""
    now = datetime.now(UTC)
    settings = await db.get(PlatformSettings, 1)

    k8s_targets = list((await db.execute(select(KubernetesCluster))).scalars().all())
    docker_targets = list((await db.execute(select(DockerHost))).scalars().all())
    proxmox_targets = list((await db.execute(select(ProxmoxNode))).scalars().all())
    tailscale_targets = list((await db.execute(select(TailscaleTenant))).scalars().all())

    panels = [
        _build_panel(
            kind="kubernetes",
            label="Kubernetes",
            enabled=getattr(settings, "integration_kubernetes_enabled", False),
            targets=k8s_targets,
            display_attr="name",
            now=now,
        ),
        _build_panel(
            kind="docker",
            label="Docker",
            enabled=getattr(settings, "integration_docker_enabled", False),
            targets=docker_targets,
            display_attr="name",
            now=now,
        ),
        _build_panel(
            kind="proxmox",
            label="Proxmox VE",
            enabled=getattr(settings, "integration_proxmox_enabled", False),
            targets=proxmox_targets,
            display_attr="hostname",
            now=now,
        ),
        _build_panel(
            kind="tailscale",
            label="Tailscale",
            enabled=getattr(settings, "integration_tailscale_enabled", False),
            targets=tailscale_targets,
            display_attr="name",
            now=now,
        ),
    ]

    # Recent reconciler errors — one filtered audit query covers all
    # four integrations at once.
    err_rows = list(
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.result == "error")
                .where(AuditLog.resource_type.in_(_INTEGRATION_RESOURCE_TYPES))
                .order_by(desc(AuditLog.timestamp))
                .limit(_RECENT_ERROR_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    recent_errors = [
        IntegrationErrorRow(
            id=str(r.id),
            integration=r.resource_type,
            target_id=r.resource_id,
            target_display=r.resource_display or "",
            error_detail=r.error_detail,
            timestamp=r.timestamp,
        )
        for r in err_rows
    ]

    return IntegrationsDashboardSummary(
        generated_at=now,
        panels=panels,
        recent_errors=recent_errors,
    )
