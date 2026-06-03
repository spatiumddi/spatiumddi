"""Operator Copilot tools for the appliance fleet (#170 Wave D2).

Four tools land here:

* ``find_pending_appliances`` — read-only list of supervisors
  sitting in pending_approval, so a superadmin can ask the Copilot
  "any pairings waiting for approval?" without clicking into the
  Fleet tab.
* ``find_appliance_fleet`` — read-only roll-up of every
  appliance row (pending + approved + rejected), with capability
  flags, role assignment, deployment kind, slot info, last-seen.
  Filterable by state / role / tag.
* ``propose_approve_appliance`` — apply-gated write proposal. The
  model proposes "approve appliance X"; the operator clicks Apply
  to actually sign the cert. Cert issuance is irreversible (the
  CA logs the serial; revoking later means a re-key), so the
  proposal contract here is load-bearing.
* ``propose_assign_role`` — apply-gated write proposal for role +
  group + tag assignment.

All four are superadmin-gated (the same gate the underlying REST
endpoints + the existing ``find_pairing_codes`` tool use). A
non-superadmin user's chat session sees a structured "ask your
platform admin" error.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import is_effective_superadmin
from app.models.appliance import (
    APPLIANCE_STATE_PENDING_APPROVAL,
    Appliance,
)
from app.models.auth import User
from app.services.ai import operations
from app.services.ai.tools.base import register_tool
from app.services.ai.tools.proposals import _persist_proposal, _proposal_result


def _superadmin_gate(user: User) -> dict[str, Any] | None:
    if not is_effective_superadmin(user):
        return {
            "error": (
                "Appliance fleet management is restricted to superadmin "
                "users. Ask your platform admin to run the query."
            )
        }
    return None


def _row_to_dict(row: Appliance) -> dict[str, Any]:
    """Compact JSON shape used by both ``find_pending_appliances``
    and ``find_appliance_fleet``. Cert bytes + pubkey blob are
    intentionally omitted — they're large and not Copilot-useful."""
    return {
        "id": str(row.id),
        "hostname": row.hostname,
        "state": row.state,
        "fingerprint_short": (
            row.public_key_fingerprint[:8] + "…" + row.public_key_fingerprint[-6:]
            if row.public_key_fingerprint
            else None
        ),
        "supervisor_version": row.supervisor_version,
        "capabilities": row.capabilities or {},
        "assigned_roles": list(row.assigned_roles or []),
        "assigned_dns_group_id": (
            str(row.assigned_dns_group_id) if row.assigned_dns_group_id else None
        ),
        "assigned_dhcp_group_id": (
            str(row.assigned_dhcp_group_id) if row.assigned_dhcp_group_id else None
        ),
        "tags": dict(row.tags or {}),
        "deployment_kind": row.deployment_kind,
        "installed_appliance_version": row.installed_appliance_version,
        "current_slot": row.current_slot,
        "durable_default": row.durable_default,
        "is_trial_boot": row.is_trial_boot,
        "last_upgrade_state": row.last_upgrade_state,
        "desired_appliance_version": row.desired_appliance_version,
        "reboot_requested": row.reboot_requested,
        "paired_at": row.paired_at.isoformat(),
        "approved_at": (row.approved_at.isoformat() if row.approved_at else None),
        "last_seen_at": (row.last_seen_at.isoformat() if row.last_seen_at else None),
        "last_seen_ip": row.last_seen_ip,
        "cert_serial": row.cert_serial,
        "cert_expires_at": (row.cert_expires_at.isoformat() if row.cert_expires_at else None),
        # #272 control-plane cluster membership (Phase 7+).
        "cluster_role": row.cluster_role,
        "desired_cluster_role": row.desired_cluster_role,
        "cluster_join_state": row.cluster_join_state,
        "node_ip": row.node_ip,
    }


# ── find_pending_appliances ────────────────────────────────────────


class FindPendingAppliancesArgs(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)


@register_tool(
    name="find_pending_appliances",
    description=(
        "List Application appliances sitting in pending_approval state "
        "(superadmin only). Each row carries the supervisor's hostname, "
        "fingerprint, advertised capabilities (can_run_dns_bind9 / "
        "can_run_dhcp / has_baked_images / cpu_count / memory_mb), and "
        "the paired-from IP + timestamp. Use to answer 'any pairings "
        "waiting for approval?', 'has dns-east-2 paired yet?', or "
        "'how many appliances are stuck in pending?'. Returns the most "
        "recent ``limit`` pending rows."
    ),
    args_model=FindPendingAppliancesArgs,
    category="admin",
    default_enabled=True,
    module="appliance.fleet",
)
async def find_pending_appliances(
    db: AsyncSession, user: User, args: FindPendingAppliancesArgs
) -> dict[str, Any]:
    if (err := _superadmin_gate(user)) is not None:
        return err
    stmt = (
        select(Appliance)
        .where(Appliance.state == APPLIANCE_STATE_PENDING_APPROVAL)
        .order_by(Appliance.paired_at.desc())
        .limit(args.limit)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return {
        "appliances": [_row_to_dict(r) for r in rows],
        "count": len(rows),
    }


# ── find_appliance_fleet ───────────────────────────────────────────


class FindApplianceFleetArgs(BaseModel):
    state: Literal["pending_approval", "approved", "rejected"] | None = Field(
        default=None,
        description="Filter by appliance state. Omit for all states.",
    )
    role: Literal["dns-bind9", "dns-powerdns", "dhcp", "observer", "custom"] | None = Field(
        default=None,
        description=(
            "Filter by an assigned role. Returns rows whose "
            "``assigned_roles`` includes this value."
        ),
    )
    tag_key: str | None = Field(
        default=None,
        description=(
            "Filter by a tag key. Pair with ``tag_value`` to require "
            "an exact match; supply only ``tag_key`` to require "
            "presence of the key with any value."
        ),
    )
    tag_value: str | None = Field(
        default=None,
        description="Required value for ``tag_key`` (exact match).",
    )
    limit: int = Field(default=50, ge=1, le=200)


@register_tool(
    name="find_appliance_fleet",
    description=(
        "Roll up the SpatiumDDI appliance fleet (superadmin only). "
        "Returns every appliance row — pending + approved + rejected — "
        "with the supervisor's capabilities, assigned roles, group "
        "FKs, deployment kind, installed appliance version, slot info, "
        "and last-seen timestamps. Filterable by state, by an assigned "
        "role, or by a tag key/value. Use to answer questions like "
        "'which boxes can run DHCP?', 'which appliances tagged "
        "site=prod-east are running BIND9?', or 'what version is the "
        "fleet on?'. The result is read-only — write actions go "
        "through ``propose_approve_appliance`` / ``propose_assign_role``."
    ),
    args_model=FindApplianceFleetArgs,
    category="admin",
    default_enabled=True,
    module="appliance.fleet",
)
async def find_appliance_fleet(
    db: AsyncSession, user: User, args: FindApplianceFleetArgs
) -> dict[str, Any]:
    if (err := _superadmin_gate(user)) is not None:
        return err

    stmt = select(Appliance).order_by(Appliance.paired_at.desc())
    if args.state is not None:
        stmt = stmt.where(Appliance.state == args.state)
    rows = list((await db.execute(stmt)).scalars().all())

    # Role + tag filters are JSONB — easier to filter in Python than
    # to fold them into the SQL with @> operators (the role filter
    # would need a JSONB containment expression that's awkward to
    # express in SQLAlchemy core).
    if args.role is not None:
        rows = [r for r in rows if args.role in (r.assigned_roles or [])]
    if args.tag_key is not None:
        if args.tag_value is not None:
            rows = [r for r in rows if (r.tags or {}).get(args.tag_key) == args.tag_value]
        else:
            rows = [r for r in rows if args.tag_key in (r.tags or {})]

    rows = rows[: args.limit]
    return {
        "appliances": [_row_to_dict(r) for r in rows],
        "count": len(rows),
    }


# ── find_control_plane_vip ─────────────────────────────────────────


class FindControlPlaneVipArgs(BaseModel):
    pass


@register_tool(
    name="find_control_plane_vip",
    description=(
        "Read the cluster-wide MetalLB control-plane VIP config "
        "(superadmin only, #272 Phase 7c). Returns whether MetalLB is "
        "enabled, the L2 address pool, and the floating VIP that fronts "
        "the Web UI across control-plane nodes. Read-only — changing the "
        "VIP is a high-blast-radius operation (it can sever Web UI / "
        "agent reachability) and is done in Fleet → Control plane, not "
        "via the Copilot."
    ),
    args_model=FindControlPlaneVipArgs,
    category="admin",
    default_enabled=True,
    module="appliance.fleet",
)
async def find_control_plane_vip(
    db: AsyncSession, user: User, args: FindControlPlaneVipArgs
) -> dict[str, Any]:
    if (err := _superadmin_gate(user)) is not None:
        return err
    from app.models.settings import PlatformSettings

    row = (
        await db.execute(select(PlatformSettings).where(PlatformSettings.id == 1))
    ).scalar_one_or_none()
    if row is None:
        return {"enabled": False, "pool_addresses": [], "control_plane_vip": ""}
    return {
        "enabled": bool(row.metallb_enabled),
        "pool_addresses": list(row.metallb_pool_addresses or []),
        "control_plane_vip": row.control_plane_vip or "",
    }


# ── find_k8s_pods ──────────────────────────────────────────────────


class FindK8sPodsArgs(BaseModel):
    namespace: str | None = Field(
        default="spatium",
        description=(
            "Namespace to list pods in (default 'spatium', where the "
            "control-plane workloads live). Pass an empty string for the "
            "api pod's own namespace; there is no all-namespaces mode."
        ),
    )
    only_unhealthy: bool = Field(
        default=False,
        description="Return only pods that aren't Running + fully Ready.",
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="find_k8s_pods",
    description=(
        "List Kubernetes pods on the appliance control plane (superadmin "
        "only, #272). Returns per-pod namespace / name / phase / node / "
        "ready / restarts — the same view as `kubectl get pods`. Use to "
        "answer 'is anything crash-looping?' or 'which node is the "
        "primary postgres on?'. Appliance control plane only; read-only."
    ),
    args_model=FindK8sPodsArgs,
    category="admin",
    default_enabled=True,
    module="appliance.cluster",
)
async def find_k8s_pods(db: AsyncSession, user: User, args: FindK8sPodsArgs) -> dict[str, Any]:
    if (err := _superadmin_gate(user)) is not None:
        return err
    import asyncio  # noqa: PLC0415

    from app.config import settings  # noqa: PLC0415
    from app.services.appliance import k8s  # noqa: PLC0415

    if not settings.appliance_mode:
        return {
            "error": (
                "Kubernetes introspection is only available on the "
                "SpatiumDDI OS appliance control plane."
            )
        }
    try:
        items = await asyncio.to_thread(k8s.list_pods, args.namespace or None)
    except k8s.KubeapiUnavailableError as exc:
        return {"error": f"kubeapi unreachable: {exc}"}

    pods: list[dict[str, Any]] = []
    for it in items:
        meta = it.get("metadata") or {}
        spec = it.get("spec") or {}
        st = it.get("status") or {}
        cs = st.get("containerStatuses") or []
        ready_n = sum(1 for c in cs if c.get("ready"))
        restarts = sum(int(c.get("restartCount") or 0) for c in cs)
        all_ready = bool(cs) and ready_n == len(cs)
        phase = st.get("phase")
        if args.only_unhealthy and phase in ("Running", "Succeeded") and all_ready:
            continue
        pods.append(
            {
                "namespace": meta.get("namespace"),
                "name": meta.get("name"),
                "phase": phase,
                "node": spec.get("nodeName"),
                "ready": f"{ready_n}/{len(cs)}" if cs else "0/0",
                "restarts": restarts,
            }
        )
    pods = pods[: args.limit]
    return {"pods": pods, "count": len(pods)}


# ── find_cluster_health ────────────────────────────────────────────


class FindClusterHealthArgs(BaseModel):
    pass


@register_tool(
    name="find_cluster_health",
    description=(
        "Roll up the appliance control-plane cluster health (superadmin "
        "only, #272). Returns the settled control-plane member count, an "
        "etcd-quorum assessment (odd count + all members reporting "
        "ready), and a per-node summary (hostname / node IP / cluster "
        "role / join state / last-seen). Use to answer 'is the control "
        "plane healthy?' or 'did node X finish joining?'. Read-only."
    ),
    args_model=FindClusterHealthArgs,
    category="admin",
    default_enabled=True,
    module="appliance.cluster",
)
async def find_cluster_health(
    db: AsyncSession, user: User, args: FindClusterHealthArgs
) -> dict[str, Any]:
    if (err := _superadmin_gate(user)) is not None:
        return err
    from app.models.appliance import (  # noqa: PLC0415
        APPLIANCE_STATE_APPROVED,
        CLUSTER_ROLE_MEMBER,
        CLUSTER_ROLE_PRIMARY,
    )

    rows = list(
        (
            await db.execute(
                select(Appliance).where(
                    Appliance.state == APPLIANCE_STATE_APPROVED,
                    Appliance.cluster_role.in_((CLUSTER_ROLE_PRIMARY, CLUSTER_ROLE_MEMBER)),
                )
            )
        )
        .scalars()
        .all()
    )
    nodes = [
        {
            "hostname": r.hostname,
            "node_ip": r.node_ip,
            "cluster_role": r.cluster_role,
            "cluster_join_state": r.cluster_join_state,
            "last_seen_at": (r.last_seen_at.isoformat() if r.last_seen_at else None),
        }
        for r in sorted(rows, key=lambda r: (r.cluster_role != CLUSTER_ROLE_PRIMARY, r.hostname))
    ]
    member_count = len(rows)
    all_ready = all(r.cluster_join_state in (None, "ready") for r in rows)
    has_primary = any(r.cluster_role == CLUSTER_ROLE_PRIMARY for r in rows)
    odd = member_count % 2 == 1
    quorum_ok = member_count >= 1 and odd and all_ready and has_primary
    return {
        "member_count": member_count,
        "etcd_quorum_ok": quorum_ok,
        "quorum_notes": {
            "odd_member_count": odd,
            "all_members_ready": all_ready,
            "has_primary_seed": has_primary,
            "tolerates_node_loss": max(0, (member_count - 1) // 2),
        },
        "nodes": nodes,
    }


# ── propose_approve_appliance ──────────────────────────────────────


class ProposeApproveApplianceArgs(BaseModel):
    appliance_id: str = Field(
        description=(
            "UUID of the pending appliance row to approve. Use "
            "``find_pending_appliances`` first to discover it."
        )
    )


@register_tool(
    name="propose_approve_appliance",
    description=(
        "Propose approving a pending Application appliance "
        "(superadmin only). Approval is irreversible cryptographically "
        "— the control plane's internal CA signs an X.509 cert against "
        "the supervisor's submitted Ed25519 pubkey + records the "
        "serial in the audit log. The model proposes the approval "
        "and the operator must click Apply for the cert to issue. "
        "Use this when the operator confirms a pending pairing is "
        "expected; otherwise prefer ``find_pending_appliances`` to "
        "let the operator inspect the row in the Fleet tab and "
        "approve from there."
    ),
    args_model=ProposeApproveApplianceArgs,
    category="admin",
    default_enabled=True,
    module="appliance.fleet",
)
async def propose_approve_appliance(
    db: AsyncSession, user: User, args: ProposeApproveApplianceArgs
) -> dict[str, Any]:
    if (err := _superadmin_gate(user)) is not None:
        return err
    op = operations.get_operation("approve_appliance")
    if op is None:
        return {"error": "Operation 'approve_appliance' is not registered"}
    preview = await op.preview(db, user, args)
    if not preview.ok:
        return {
            "kind": "proposal_rejected",
            "operation": "approve_appliance",
            "detail": preview.detail,
        }
    proposal = await _persist_proposal(
        db,
        user=user,
        operation="approve_appliance",
        args=args.model_dump(),
        preview_text=preview.preview_text,
    )
    return _proposal_result(proposal, preview_text=preview.preview_text)


# ── propose_assign_role ───────────────────────────────────────────


class ProposeAssignRoleArgs(BaseModel):
    appliance_id: str = Field(description="UUID of the approved appliance row.")
    roles: list[str] = Field(
        description=(
            "Subset of dns-bind9 / dns-powerdns / dhcp / observer / "
            "custom. dns-bind9 + dns-powerdns are mutually exclusive. "
            "Empty list = idle (no service containers will run)."
        )
    )
    dns_group_id: str | None = Field(
        default=None,
        description=(
            "Optional DNSServerGroup UUID. Required if roles include "
            "a DNS role and there's no existing assignment."
        ),
    )
    dhcp_group_id: str | None = Field(
        default=None,
        description=(
            "Optional DHCPServerGroup UUID. Required if roles include "
            "dhcp and there's no existing assignment."
        ),
    )


@register_tool(
    name="propose_assign_role",
    description=(
        "Propose role + group assignment for an approved appliance "
        "(superadmin only). The model proposes the assignment + the "
        "operator clicks Apply for the supervisor to actually start / "
        "stop service containers on the next heartbeat. Server-side "
        "validation rejects roles the supervisor doesn't advertise "
        "capability for, and rejects the dns-bind9 + dns-powerdns "
        "combo (one engine per appliance). For tags or firewall "
        "edits, point the operator at the Fleet tab drilldown."
    ),
    args_model=ProposeAssignRoleArgs,
    category="admin",
    default_enabled=True,
    module="appliance.fleet",
)
async def propose_assign_role(
    db: AsyncSession, user: User, args: ProposeAssignRoleArgs
) -> dict[str, Any]:
    if (err := _superadmin_gate(user)) is not None:
        return err
    op = operations.get_operation("assign_appliance_role")
    if op is None:
        return {"error": "Operation 'assign_appliance_role' is not registered"}
    preview = await op.preview(db, user, args)
    if not preview.ok:
        return {
            "kind": "proposal_rejected",
            "operation": "assign_appliance_role",
            "detail": preview.detail,
        }
    proposal = await _persist_proposal(
        db,
        user=user,
        operation="assign_appliance_role",
        args=args.model_dump(),
        preview_text=preview.preview_text,
    )
    return _proposal_result(proposal, preview_text=preview.preview_text)


__all__ = [
    "find_pending_appliances",
    "find_appliance_fleet",
    "propose_approve_appliance",
    "propose_assign_role",
]


# Tell mypy + ruff these uuid imports are intentional — we accept
# string-form UUIDs from the LLM but resolve them server-side via
# the operations' apply functions.
_ = uuid
