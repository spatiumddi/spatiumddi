"""Tier 4 integration target read tools for the Operator Copilot
(issue #101).

Exposes the four read-only integration mirror surfaces — Kubernetes
clusters, Docker hosts, Proxmox endpoints, Tailscale tenants — as
list tools so operators can ask "which clusters are configured?" or
"is the Frankfurt Docker host still syncing?" without falling back
to UI navigation.

Each tool is gated by the matching feature_module
(``integrations.kubernetes`` etc.) so disabling the integration in
Settings → Features removes the tool from the AI surface in lock-step
with the sidebar entry.

Output is intentionally narrow — name / endpoint / enabled state /
last sync timestamp + error / ipam space binding. Credentials, CA
bundles, and any other secret material never enter the response
payload (the underlying models keep them in encrypted columns the
ORM doesn't surface here).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.docker import DockerHost
from app.models.kubernetes import KubernetesCluster
from app.models.proxmox import ProxmoxNode
from app.models.tailscale import TailscaleTenant
from app.services.ai.tools.base import register_tool


def _common_target_args() -> dict[str, Any]:
    """Field set every list-targets tool exposes — kept consistent so
    the LLM doesn't have to remember per-integration filter shapes."""
    return {
        "search": Field(
            default=None,
            description="Substring match on name / description / endpoint URL.",
        ),
        "enabled": Field(
            default=None,
            description="Filter by ``enabled`` flag. None = both.",
        ),
        "limit": Field(default=50, ge=1, le=500),
    }


# ── list_kubernetes_targets ───────────────────────────────────────────


class ListKubernetesTargetsArgs(BaseModel):
    search: str | None = _common_target_args()["search"]
    enabled: bool | None = _common_target_args()["enabled"]
    limit: int = _common_target_args()["limit"]


@register_tool(
    name="list_kubernetes_targets",
    module="integrations.kubernetes",
    description=(
        "List configured Kubernetes clusters that SpatiumDDI mirrors "
        "into IPAM. Each row carries id, name, description, "
        "api_server_url, enabled flag, ipam_space_id, dns_group_id, "
        "pod_cidr / service_cidr, last_synced_at, last_sync_error, "
        "cluster_version, and node_count. Use for 'which clusters are "
        "configured?', 'is cluster X syncing?', or 'when did we last "
        "pull from the prod cluster?'. Credentials are never returned."
    ),
    args_model=ListKubernetesTargetsArgs,
    category="integrations",
)
async def list_kubernetes_targets(
    db: AsyncSession, user: User, args: ListKubernetesTargetsArgs
) -> list[dict[str, Any]]:
    stmt = select(KubernetesCluster)
    if args.search:
        like = f"%{args.search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(KubernetesCluster.name).like(like),
                func.lower(KubernetesCluster.description).like(like),
                func.lower(KubernetesCluster.api_server_url).like(like),
            )
        )
    if args.enabled is not None:
        stmt = stmt.where(KubernetesCluster.enabled.is_(args.enabled))
    stmt = stmt.order_by(KubernetesCluster.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "description": r.description,
            "api_server_url": r.api_server_url,
            "enabled": r.enabled,
            "ipam_space_id": str(r.ipam_space_id),
            "dns_group_id": str(r.dns_group_id) if r.dns_group_id else None,
            "pod_cidr": r.pod_cidr,
            "service_cidr": r.service_cidr,
            "sync_interval_seconds": r.sync_interval_seconds,
            "mirror_pods": r.mirror_pods,
            "last_synced_at": r.last_synced_at.isoformat() if r.last_synced_at else None,
            "last_sync_error": r.last_sync_error,
            "cluster_version": r.cluster_version,
            "node_count": r.node_count,
        }
        for r in rows
    ]


# ── list_docker_targets ───────────────────────────────────────────────


class ListDockerTargetsArgs(BaseModel):
    search: str | None = _common_target_args()["search"]
    enabled: bool | None = _common_target_args()["enabled"]
    limit: int = _common_target_args()["limit"]


@register_tool(
    name="list_docker_targets",
    module="integrations.docker",
    description=(
        "List configured Docker hosts that SpatiumDDI mirrors into "
        "IPAM. Each row carries id, name, description, connection_type "
        "(unix / tcp), endpoint, enabled flag, ipam_space_id, "
        "dns_group_id, mirror_containers / include_default_networks / "
        "include_stopped_containers flags, sync_interval_seconds, and "
        "last_synced_at. Use for 'which Docker hosts are configured?' "
        "or 'is the docker integration syncing?'. Credentials never "
        "appear in the response."
    ),
    args_model=ListDockerTargetsArgs,
    category="integrations",
)
async def list_docker_targets(
    db: AsyncSession, user: User, args: ListDockerTargetsArgs
) -> list[dict[str, Any]]:
    stmt = select(DockerHost)
    if args.search:
        like = f"%{args.search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(DockerHost.name).like(like),
                func.lower(DockerHost.description).like(like),
                func.lower(DockerHost.endpoint).like(like),
            )
        )
    if args.enabled is not None:
        stmt = stmt.where(DockerHost.enabled.is_(args.enabled))
    stmt = stmt.order_by(DockerHost.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "description": r.description,
            "connection_type": r.connection_type,
            "endpoint": r.endpoint,
            "enabled": r.enabled,
            "ipam_space_id": str(r.ipam_space_id),
            "dns_group_id": str(r.dns_group_id) if r.dns_group_id else None,
            "mirror_containers": r.mirror_containers,
            "include_default_networks": r.include_default_networks,
            "include_stopped_containers": r.include_stopped_containers,
            "sync_interval_seconds": r.sync_interval_seconds,
            "last_synced_at": r.last_synced_at.isoformat() if r.last_synced_at else None,
        }
        for r in rows
    ]


# ── list_proxmox_targets ──────────────────────────────────────────────


class ListProxmoxTargetsArgs(BaseModel):
    search: str | None = _common_target_args()["search"]
    enabled: bool | None = _common_target_args()["enabled"]
    limit: int = _common_target_args()["limit"]


@register_tool(
    name="list_proxmox_targets",
    module="integrations.proxmox",
    description=(
        "List configured Proxmox VE endpoints that SpatiumDDI mirrors "
        "into IPAM. Each row carries id, name, description, endpoint, "
        "enabled flag, ipam_space_id, sync_interval_seconds, and "
        "last_synced_at. Use for 'which PVE endpoints are configured?' "
        "or 'is Proxmox syncing?'. API tokens never appear."
    ),
    args_model=ListProxmoxTargetsArgs,
    category="integrations",
)
async def list_proxmox_targets(
    db: AsyncSession, user: User, args: ListProxmoxTargetsArgs
) -> list[dict[str, Any]]:
    stmt = select(ProxmoxNode)
    if args.search:
        like = f"%{args.search.lower()}%"
        cols = [
            func.lower(ProxmoxNode.name).like(like),
            func.lower(ProxmoxNode.description).like(like),
        ]
        # ProxmoxNode may or may not have ``endpoint`` / ``api_url``.
        # Probe for both common attribute names so the tool stays robust
        # if the column gets renamed; the search just narrows further
        # when present.
        for col_name in ("endpoint", "api_url"):
            col = getattr(ProxmoxNode, col_name, None)
            if col is not None:
                cols.append(func.lower(col).like(like))
        stmt = stmt.where(or_(*cols))
    if args.enabled is not None:
        stmt = stmt.where(ProxmoxNode.enabled.is_(args.enabled))
    stmt = stmt.order_by(ProxmoxNode.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        last_synced = getattr(r, "last_synced_at", None)
        row: dict[str, Any] = {
            "id": str(r.id),
            "name": r.name,
            "description": getattr(r, "description", "") or "",
            "enabled": r.enabled,
            "ipam_space_id": (str(r.ipam_space_id) if getattr(r, "ipam_space_id", None) else None),
            "sync_interval_seconds": getattr(r, "sync_interval_seconds", None),
            "last_synced_at": last_synced.isoformat() if last_synced else None,
        }
        for opt in ("endpoint", "api_url"):
            val = getattr(r, opt, None)
            if val is not None:
                row[opt] = val
        out.append(row)
    return out


# ── list_tailscale_targets ────────────────────────────────────────────


class ListTailscaleTargetsArgs(BaseModel):
    search: str | None = _common_target_args()["search"]
    enabled: bool | None = _common_target_args()["enabled"]
    limit: int = _common_target_args()["limit"]


@register_tool(
    name="list_tailscale_targets",
    module="integrations.tailscale",
    description=(
        "List configured Tailscale tenants (tailnets) that SpatiumDDI "
        "mirrors into IPAM. Each row carries id, name, description, "
        "enabled flag, ipam_space_id, sync_interval_seconds, and "
        "last_synced_at. Use for 'which tailnets are connected?' or "
        "'is Tailscale syncing?'. PAT tokens never appear."
    ),
    args_model=ListTailscaleTargetsArgs,
    category="integrations",
)
async def list_tailscale_targets(
    db: AsyncSession, user: User, args: ListTailscaleTargetsArgs
) -> list[dict[str, Any]]:
    stmt = select(TailscaleTenant)
    if args.search:
        like = f"%{args.search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(TailscaleTenant.name).like(like),
                func.lower(TailscaleTenant.description).like(like),
            )
        )
    if args.enabled is not None:
        stmt = stmt.where(TailscaleTenant.enabled.is_(args.enabled))
    stmt = stmt.order_by(TailscaleTenant.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        last_synced = getattr(r, "last_synced_at", None)
        out.append(
            {
                "id": str(r.id),
                "name": r.name,
                "description": getattr(r, "description", "") or "",
                "enabled": r.enabled,
                "ipam_space_id": (
                    str(r.ipam_space_id) if getattr(r, "ipam_space_id", None) else None
                ),
                "sync_interval_seconds": getattr(r, "sync_interval_seconds", None),
                "last_synced_at": last_synced.isoformat() if last_synced else None,
            }
        )
    return out
