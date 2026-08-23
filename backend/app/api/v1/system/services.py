"""Capability-aware service lifecycle control (issue #890).

* ``GET  /system/services``               — capability + inventory
* ``POST /system/services/{id}/{action}`` — start / stop / restart

The capability comes back on every list, so the UI never has to infer
"unsupported" from a 503 — which is what it had to do before, and why
the same 503 meant both "this deployment can't do that" and "the daemon
is down".

Superadmin on both routes, demo-mode-blocked on the action, and every
action writes an audit row **before** the response. That ordering is not
cosmetic: on docker-compose a self-targeted restart kills this process
the moment the daemon accepts it, so the row has to be durable first,
and it records ``accepted`` rather than ``success`` because nothing here
survives to observe the outcome.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel

from app.api.deps import DB, SuperAdmin
from app.core.demo_mode import forbid_in_demo_mode
from app.models.audit import AuditLog
from app.services import service_control
from app.services.service_control.backends import (
    apply_action,
    apply_action_detached,
    plan_action,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


class ServiceCapabilityResponse(BaseModel):
    backend: str
    flavor: str
    enabled: bool
    supported_actions: list[str]
    reason: str | None


class ServiceRow(BaseModel):
    id: str
    name: str
    kind: str
    state: str
    image: str
    detail: str
    actions: list[str]
    component: str | None = None
    desired: int | None = None
    ready: int | None = None
    last_restarted_at: str | None = None


class ServiceListResponse(BaseModel):
    capability: ServiceCapabilityResponse
    services: list[ServiceRow]
    #: Set when the backend is live but could not be queried. The
    #: capability still describes what this deployment *can* do, so the
    #: UI distinguishes "no permission to list" from "nothing to list".
    error: str | None = None


class ServiceActionResponse(BaseModel):
    id: str
    action: str
    status: str
    #: True when the target is the api process answering this request —
    #: the action runs after the response and the UI should expect the
    #: session to blink rather than treat a dropped poll as a failure.
    self_targeted: bool


def _row(svc: service_control.ServiceSummary) -> ServiceRow:
    return ServiceRow(
        id=svc.id,
        name=svc.name,
        kind=svc.kind,
        state=svc.state,
        image=svc.image,
        detail=svc.detail,
        actions=list(svc.actions),
        component=svc.extra.get("component"),
        desired=svc.extra.get("desired"),
        ready=svc.extra.get("ready"),
        last_restarted_at=svc.extra.get("last_restarted_at"),
    )


@router.get("", response_model=ServiceListResponse)
async def list_services(_: SuperAdmin) -> ServiceListResponse:
    """Which lifecycle backend is live, and what it can act on."""
    cap = await service_control.capability()
    cap_out = ServiceCapabilityResponse(
        backend=cap.backend,
        flavor=cap.flavor,
        enabled=cap.enabled,
        supported_actions=list(cap.supported_actions),
        reason=cap.reason,
    )
    try:
        rows = await service_control.list_services(cap)
    except service_control.ServiceControlError as exc:
        return ServiceListResponse(capability=cap_out, services=[], error=str(exc))
    return ServiceListResponse(capability=cap_out, services=[_row(r) for r in rows])


@router.post(
    "/{service_id}/{action}",
    response_model=ServiceActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def act_on_service(
    service_id: str,
    action: str,
    user: SuperAdmin,
    db: DB,
    background: BackgroundTasks,
) -> ServiceActionResponse:
    """Start / stop / restart one service. 202 — the outcome is async.

    ``service_id`` is resolved against the live inventory rather than
    passed through, so the set of things this endpoint can touch is
    exactly the set it just listed.
    """
    forbid_in_demo_mode("Service lifecycle control is disabled")
    if action not in service_control.ACTIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"action must be one of {list(service_control.ACTIONS)}",
        )
    try:
        plan = await plan_action(service_id, action)
    except LookupError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No controllable service {service_id!r} in this deployment.",
        ) from exc
    except service_control.ServiceControlError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action=f"service_{plan.action}",
            resource_type="service",
            resource_id=plan.service.id,
            resource_display=plan.service.name,
            # ``accepted``, not ``success``: the action is signalled after
            # this row is committed and, when self-targeted, after this
            # process has stopped existing.
            result="accepted",
            new_value={
                "kind": plan.service.kind,
                "action": plan.action,
                "self_targeted": plan.is_self,
            },
        )
    )
    await db.commit()

    if plan.is_self:
        # Defer past the response — see ``apply_action_detached``.
        background.add_task(apply_action_detached, plan)
    else:
        try:
            await apply_action(plan)
        except service_control.ServiceControlError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    logger.info(
        "service_control_action",
        service=plan.service.id,
        action=plan.action,
        self_targeted=plan.is_self,
        user=user.username,
    )
    return ServiceActionResponse(
        id=plan.service.id,
        action=plan.action,
        status="accepted",
        self_targeted=plan.is_self,
    )
