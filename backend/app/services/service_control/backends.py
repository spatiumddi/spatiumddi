"""Backend detection + the unified service inventory (issue #890).

``capability()`` is the load-bearing entry point: it answers *what can
this deployment actually do* before anything is attempted, so the UI
renders the buttons that exist rather than learning from a 503 that the
one it drew does not. Detection order is kubernetes → compose → none,
because an appliance has both a ServiceAccount and (historically) a
docker socket, and the pods are the real workloads there.

Every action resolves its target against a fresh ``list_services()``.
That makes the live inventory the allowlist — there is no separate list
to keep in sync, and a caller-supplied id that is not currently ours is
a 404 rather than a string handed to a daemon.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio
import structlog

from app.config import settings
from app.services.service_control import compose as compose_backend
from app.services.service_control import kube as kube_backend

logger = structlog.get_logger(__name__)

Backend = Literal["kubernetes", "compose", "none"]
Action = Literal["start", "stop", "restart"]

ACTIONS: tuple[str, ...] = ("start", "stop", "restart")

# Kubernetes offers restart only — see ``kube.py`` for why start/stop are
# deliberately absent rather than merely unimplemented.
_KUBE_ACTIONS: tuple[str, ...] = ("restart",)
_COMPOSE_ACTIONS: tuple[str, ...] = ("start", "stop", "restart")


class ServiceControlError(RuntimeError):
    """A lifecycle action could not be performed."""


@dataclass(frozen=True)
class ServiceSummary:
    """One controllable unit, in whichever vocabulary its backend uses.

    ``id`` is stable across polls and is what an action names: the
    compose *service* name (``api``, ``worker``) or ``Kind/name`` for a
    workload. Not a container id or pod name — those churn on every
    restart, which is precisely what the operator just asked for.
    """

    id: str
    name: str
    kind: str
    state: str
    image: str = ""
    detail: str = ""
    actions: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ServiceControlCapability:
    backend: Backend
    #: ``k3s-appliance`` / ``kubernetes`` / ``compose`` / ``none`` — the
    #: backend plus enough context for the UI to name it honestly.
    flavor: str
    enabled: bool
    supported_actions: tuple[str, ...]
    #: Populated whenever ``enabled`` is false, or the backend is
    #: reachable but degraded. Always operator-actionable prose.
    reason: str | None = None


def _control_gate_open() -> bool:
    """Is lifecycle control allowed on this deployment?

    Off by default. The appliance is the exception: it already ships
    ``POST /appliance/containers/{name}/{action}`` with the same blast
    radius, so requiring a new opt-in there would remove a control that
    exists rather than adding one that does not.
    """
    return bool(settings.service_control_enabled or settings.appliance_mode)


async def capability() -> ServiceControlCapability:
    """Which lifecycle backend is live, and may it be used."""
    gate = _control_gate_open()

    if kube_backend.available():
        flavor = "k3s-appliance" if settings.appliance_mode else "kubernetes"
        reason = None
        if not gate:
            reason = (
                "Service control is disabled. Set SERVICE_CONTROL_ENABLED=true on "
                "the api (Helm: api.serviceControl.enabled) and grant the rollout "
                "RBAC with api.serviceControlRBAC.enabled."
            )
        return ServiceControlCapability(
            backend="kubernetes",
            flavor=flavor,
            enabled=gate,
            supported_actions=_KUBE_ACTIONS,
            reason=reason,
        )

    if compose_backend.socket_available():
        identity = await compose_backend.own_identity()
        if identity is None:
            return ServiceControlCapability(
                backend="none",
                flavor="compose",
                enabled=False,
                supported_actions=(),
                reason=(
                    "The Docker socket is mounted but the api container's own "
                    "compose project could not be read, so there is no safe scope "
                    "to act within. Service control stays off."
                ),
            )
        reason = None
        if not gate:
            reason = (
                "Service control is disabled. Set SERVICE_CONTROL_ENABLED=true on "
                "the api container — a restart-capable docker.sock is equivalent to "
                "root on the host, so it is opt-in."
            )
        return ServiceControlCapability(
            backend="compose",
            flavor="compose",
            enabled=gate,
            supported_actions=_COMPOSE_ACTIONS,
            reason=reason,
        )

    return ServiceControlCapability(
        backend="none",
        flavor="none",
        enabled=False,
        supported_actions=(),
        reason=(
            "No lifecycle backend is reachable: neither a Kubernetes ServiceAccount "
            f"nor {compose_backend.DOCKER_SOCKET} is mounted into the api container."
        ),
    )


async def list_services(cap: ServiceControlCapability | None = None) -> list[ServiceSummary]:
    """The controllable inventory for the live backend.

    Returns an empty list — not an error — when no backend is available,
    so a caller can render the capability's ``reason`` without a special
    case. A backend that IS available but fails to answer raises, because
    an empty inventory would read as "nothing to restart".
    """
    cap = cap or await capability()
    if cap.backend == "kubernetes":
        try:
            rows = await anyio.to_thread.run_sync(kube_backend.list_workloads)
        except kube_backend.KubeControlError as exc:
            raise ServiceControlError(str(exc)) from exc
        return [
            ServiceSummary(
                id=f"{r['kind']}/{r['name']}",
                name=r["name"],
                kind=r["kind"],
                state=r["state"],
                image=r["image"],
                detail=f"{r['ready']}/{r['desired']} ready",
                actions=_KUBE_ACTIONS if cap.enabled else (),
                extra={
                    "component": r["component"],
                    "desired": r["desired"],
                    "ready": r["ready"],
                    "last_restarted_at": r["last_restarted_at"],
                },
            )
            for r in rows
        ]

    if cap.backend == "compose":
        identity = await compose_backend.own_identity()
        if identity is None:
            raise ServiceControlError("compose project for the api container is unknown")
        try:
            rows = [
                compose_backend.summarise(r)
                for r in await compose_backend.list_project_containers(identity.project)
            ]
        except compose_backend.ComposeUnavailableError as exc:
            raise ServiceControlError(str(exc)) from exc
        out: list[ServiceSummary] = []
        for r in rows:
            # A container with no compose-service label is a one-off
            # (``compose run``) and has no stable id to act on.
            if not r["service"]:
                continue
            out.append(
                ServiceSummary(
                    id=r["service"],
                    name=r["container_name"] or r["service"],
                    kind="container",
                    state=r["state"],
                    image=r["image"],
                    detail=r["status"],
                    actions=_COMPOSE_ACTIONS if cap.enabled else (),
                    extra={
                        "container_id": r["container_id"],
                        "project": identity.project,
                        # Stamped per row so ``_self_service_id`` compares
                        # against the daemon's own answer rather than
                        # ``$HOSTNAME``, which an operator can override.
                        "self_container_id": identity.container_id,
                    },
                )
            )
        out.sort(key=lambda s: s.id)
        return out

    return []


def _self_service_id(services: list[ServiceSummary]) -> str | None:
    """Which inventory row is the api process serving this request?

    Only meaningful on compose, where stopping or restarting ourselves
    kills the connection mid-response. On Kubernetes a rollout keeps the
    current pod serving until the replacement is ready, so there is
    nothing to defer.

    Matches on the container id the *daemon* reported for us, falling
    back to ``$HOSTNAME`` only when that is missing: compose lets a
    service set ``hostname:``, and there the two are not the same value.
    Getting this wrong is not a security problem but it is a visible one
    — the operator gets a connection reset instead of a 202.
    """
    host = os.environ.get("HOSTNAME", "").strip()
    for svc in services:
        own = str(svc.extra.get("self_container_id") or "")
        cid = str(svc.extra.get("container_id") or "")
        if own and cid and cid == own:
            return svc.id
        if host and cid and cid.startswith(host[:12]):
            return svc.id
    return None


@dataclass(frozen=True)
class ActionPlan:
    """A resolved, allowlisted action — everything needed to perform it."""

    service: ServiceSummary
    action: str
    #: True when the target is the api container answering this request.
    #: The router returns 202 and performs the action *after* the
    #: response, because on compose the daemon stops us immediately and
    #: the operator would otherwise see a connection reset.
    is_self: bool


async def plan_action(service_id: str, action: str) -> ActionPlan:
    """Resolve ``service_id`` against the live inventory.

    Raises ``LookupError`` when the id is not currently one of ours and
    ``ServiceControlError`` when the backend or action is unavailable —
    the router maps those to 404 and 409/503 respectively.
    """
    cap = await capability()
    if not cap.enabled:
        raise ServiceControlError(cap.reason or "Service control is not available.")
    if action not in cap.supported_actions:
        raise ServiceControlError(
            f"{cap.backend} supports {list(cap.supported_actions)}, not {action!r}"
        )
    services = await list_services(cap)
    match = next((s for s in services if s.id == service_id), None)
    if match is None:
        raise LookupError(service_id)
    return ActionPlan(service=match, action=action, is_self=_self_service_id(services) == match.id)


async def apply_action(plan: ActionPlan) -> None:
    """Perform a previously-resolved action. Raises ``ServiceControlError``."""
    if plan.service.kind == "container":
        container_id = str(plan.service.extra.get("container_id") or "")
        if not container_id:
            raise ServiceControlError(f"no container id for {plan.service.id!r}")
        try:
            await compose_backend.act(container_id, plan.action)
        except compose_backend.ComposeUnavailableError as exc:
            raise ServiceControlError(str(exc)) from exc
        return

    try:
        await anyio.to_thread.run_sync(
            kube_backend.rollout_restart, plan.service.kind, plan.service.name
        )
    except kube_backend.KubeControlError as exc:
        raise ServiceControlError(str(exc)) from exc


async def apply_action_detached(plan: ActionPlan) -> None:
    """Run ``apply_action`` after the response has been sent.

    Used for a self-targeted compose action: the daemon kills this
    process the moment it accepts the request, so signalling inline would
    abort the response and the operator would see a connection reset
    instead of the 202 that says it worked.
    """
    try:
        await apply_action(plan)
    except ServiceControlError as exc:
        # Nothing left to return it to — the response is already sent —
        # so the log is the only record, and the audit row written before
        # the response deliberately says "accepted", not "succeeded".
        logger.warning(
            "service_control_detached_action_failed",
            service=plan.service.id,
            action=plan.action,
            error=str(exc),
        )


__all__ = [
    "ACTIONS",
    "Action",
    "ActionPlan",
    "Backend",
    "ServiceControlCapability",
    "ServiceControlError",
    "ServiceSummary",
    "apply_action",
    "apply_action_detached",
    "capability",
    "list_services",
    "plan_action",
]
