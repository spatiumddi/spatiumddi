"""Service lifecycle control across deployment shapes (issue #890).

Three properties carry the security of this feature, and each fails
*quietly* rather than loudly if it regresses:

* **The inventory is the allowlist.** An action resolves its target
  against a fresh listing. A regression that passed ``service_id``
  straight to the daemon would work perfectly for every legitimate
  call and only show up when someone sent a name that wasn't ours.
* **Compose scope is our own project.** Widening to a name prefix, or
  falling back to "all containers" when self-identification fails,
  would let the api act on a co-tenant container on the same host.
* **The kubernetes backend offers restart only.** ``stop`` there would
  mean scaling to zero with nothing durable remembering the replica
  count.

Plus the capability contract itself: an available-but-unqueryable
backend must report an error, never an empty inventory — "nothing to
restart" and "I was not allowed to look" are opposite answers.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.audit import AuditLog
from app.models.auth import User
from app.services import service_control
from app.services.service_control import backends, kube
from app.services.service_control.backends import (
    ServiceControlCapability,
    ServiceControlError,
    ServiceSummary,
    plan_action,
)
from app.services.service_control.compose import SelfIdentity

_IDENTITY = SelfIdentity(project="proj", container_id="cccccccccccc")


# ── capability detection ───────────────────────────────────────────


def _no_backends():
    return (
        patch.object(kube, "available", return_value=False),
        patch.object(backends.compose_backend, "socket_available", return_value=False),
    )


@pytest.mark.asyncio
async def test_no_backend_names_the_missing_mount() -> None:
    kube_p, sock_p = _no_backends()
    with kube_p, sock_p:
        cap = await backends.capability()
    assert cap.backend == "none"
    assert cap.enabled is False
    assert cap.supported_actions == ()
    # The reason has to be actionable — this is the string the UI shows
    # instead of a button.
    assert "docker.sock" in (cap.reason or "")


@pytest.mark.asyncio
async def test_kubernetes_wins_over_compose_when_both_are_present() -> None:
    """An appliance has both. The pods are the real workloads there."""
    with (
        patch.object(kube, "available", return_value=True),
        patch.object(backends.compose_backend, "socket_available", return_value=True),
        patch.object(backends.settings, "appliance_mode", True),
    ):
        cap = await backends.capability()
    assert cap.backend == "kubernetes"
    assert cap.flavor == "k3s-appliance"
    # Appliance mode implies the gate — the same control already ships
    # at POST /appliance/containers/{name}/{action}.
    assert cap.enabled is True


@pytest.mark.asyncio
async def test_kubernetes_offers_restart_only() -> None:
    with (
        patch.object(kube, "available", return_value=True),
        patch.object(backends.settings, "appliance_mode", False),
        patch.object(backends.settings, "service_control_enabled", True),
    ):
        cap = await backends.capability()
    assert cap.backend == "kubernetes"
    assert cap.supported_actions == ("restart",)
    assert "stop" not in cap.supported_actions


@pytest.mark.asyncio
async def test_gate_off_reports_the_exact_toggle_to_flip() -> None:
    with (
        patch.object(kube, "available", return_value=True),
        patch.object(backends.settings, "appliance_mode", False),
        patch.object(backends.settings, "service_control_enabled", False),
    ):
        cap = await backends.capability()
    assert cap.enabled is False
    assert "SERVICE_CONTROL_ENABLED" in (cap.reason or "")


@pytest.mark.asyncio
async def test_compose_fails_closed_when_it_cannot_identify_itself() -> None:
    """No self-identification means no safe scope — not a wider one.

    Falling back to a name prefix would let a co-tenant container named
    ``spatiumddi-anything`` be restarted by this endpoint.
    """
    with (
        patch.object(kube, "available", return_value=False),
        patch.object(backends.compose_backend, "socket_available", return_value=True),
        patch.object(backends.compose_backend, "own_identity", AsyncMock(return_value=None)),
        patch.object(backends.settings, "service_control_enabled", True),
    ):
        cap = await backends.capability()
    assert cap.backend == "none"
    assert cap.enabled is False
    assert "compose project" in (cap.reason or "")


@pytest.mark.asyncio
async def test_compose_backend_offers_all_three_actions() -> None:
    with (
        patch.object(kube, "available", return_value=False),
        patch.object(backends.compose_backend, "socket_available", return_value=True),
        patch.object(
            backends.compose_backend,
            "own_identity",
            AsyncMock(return_value=_IDENTITY),
        ),
        patch.object(backends.settings, "service_control_enabled", True),
    ):
        cap = await backends.capability()
    assert cap.backend == "compose"
    assert set(cap.supported_actions) == {"start", "stop", "restart"}


# ── inventory ──────────────────────────────────────────────────────


def _compose_row(service: str, name: str, state: str = "running") -> dict[str, Any]:
    return {
        "Id": "c" * 64,
        "Names": [f"/{name}"],
        "Image": "img:tag",
        "State": state,
        "Status": "Up 2 minutes",
        "Labels": {
            "com.docker.compose.project": "proj",
            "com.docker.compose.service": service,
        },
    }


@pytest.mark.asyncio
async def test_compose_inventory_skips_one_off_containers() -> None:
    """``compose run`` containers have no service label and no stable id."""
    rows = [_compose_row("api", "proj-api-1"), {**_compose_row("", "proj-run-abc")}]
    rows[1]["Labels"].pop("com.docker.compose.service")
    with (
        patch.object(
            backends.compose_backend,
            "own_identity",
            AsyncMock(return_value=_IDENTITY),
        ),
        patch.object(
            backends.compose_backend,
            "list_project_containers",
            AsyncMock(return_value=rows),
        ),
    ):
        cap = ServiceControlCapability(
            backend="compose",
            flavor="compose",
            enabled=True,
            supported_actions=("start", "stop", "restart"),
        )
        services = await backends.list_services(cap)
    assert [s.id for s in services] == ["api"]


@pytest.mark.asyncio
async def test_disabled_gate_lists_but_offers_no_actions() -> None:
    """Read-only is a real state — the operator still gets to see what runs."""
    with (
        patch.object(
            backends.compose_backend,
            "own_identity",
            AsyncMock(return_value=_IDENTITY),
        ),
        patch.object(
            backends.compose_backend,
            "list_project_containers",
            AsyncMock(return_value=[_compose_row("api", "proj-api-1")]),
        ),
    ):
        cap = ServiceControlCapability(
            backend="compose",
            flavor="compose",
            enabled=False,
            supported_actions=("start", "stop", "restart"),
            reason="off",
        )
        services = await backends.list_services(cap)
    assert services[0].actions == ()


@pytest.mark.asyncio
async def test_unqueryable_backend_raises_rather_than_reporting_empty() -> None:
    """An empty inventory reads as "nothing to restart" — the opposite answer."""
    with (
        patch.object(kube, "available", return_value=True),
        patch.object(
            kube,
            "list_workloads",
            side_effect=kube.KubeControlError("kubeapi forbade listing deployments"),
        ),
    ):
        cap = ServiceControlCapability(
            backend="kubernetes",
            flavor="kubernetes",
            enabled=True,
            supported_actions=("restart",),
        )
        with pytest.raises(ServiceControlError, match="forbade"):
            await backends.list_services(cap)


# ── the inventory is the allowlist ─────────────────────────────────


_ENABLED_COMPOSE = ServiceControlCapability(
    backend="compose",
    flavor="compose",
    enabled=True,
    supported_actions=("start", "stop", "restart"),
)


@pytest.mark.asyncio
async def test_unknown_service_id_never_reaches_the_daemon() -> None:
    listed = [
        ServiceSummary(
            id="api",
            name="proj-api-1",
            kind="container",
            state="running",
            actions=("restart",),
            extra={"container_id": "abc123"},
        )
    ]
    with (
        patch.object(backends, "capability", AsyncMock(return_value=_ENABLED_COMPOSE)),
        patch.object(backends, "list_services", AsyncMock(return_value=listed)),
    ):
        with pytest.raises(LookupError):
            await plan_action("../../etc/passwd", "restart")
        with pytest.raises(LookupError):
            await plan_action("some-other-stacks-container", "restart")


@pytest.mark.asyncio
async def test_action_unsupported_by_the_backend_is_refused() -> None:
    kube_cap = ServiceControlCapability(
        backend="kubernetes",
        flavor="kubernetes",
        enabled=True,
        supported_actions=("restart",),
    )
    with patch.object(backends, "capability", AsyncMock(return_value=kube_cap)):
        with pytest.raises(ServiceControlError, match="not 'stop'"):
            await plan_action("Deployment/api", "stop")


@pytest.mark.asyncio
async def test_action_refused_while_the_gate_is_closed() -> None:
    closed = ServiceControlCapability(
        backend="compose",
        flavor="compose",
        enabled=False,
        supported_actions=("start", "stop", "restart"),
        reason="Service control is disabled.",
    )
    with patch.object(backends, "capability", AsyncMock(return_value=closed)):
        with pytest.raises(ServiceControlError, match="disabled"):
            await plan_action("api", "restart")


# ── kubernetes label scoping ───────────────────────────────────────


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ({"app.kubernetes.io/name": "spatiumddi"}, True),
        ({"app.kubernetes.io/part-of": "spatiumddi"}, True),
        ({"app.kubernetes.io/name": "someone-elses-app"}, False),
        ({}, False),
    ],
)
def test_only_spatiumddi_labelled_workloads_are_ours(
    labels: dict[str, str], expected: bool
) -> None:
    assert kube.is_spatium_workload({"metadata": {"labels": labels}}) is expected


def test_daemonset_readiness_reads_its_own_status_fields() -> None:
    """A DaemonSet has no ``spec.replicas`` — reading it would report 0/0
    ready on every node-local DNS / DHCP workload on the appliance."""
    summary = kube.summarise_workload(
        "DaemonSet",
        {
            "metadata": {"name": "dns-bind9", "labels": {}},
            "spec": {"template": {"spec": {"containers": [{"image": "bind9:1"}]}}},
            "status": {"desiredNumberScheduled": 3, "numberReady": 3},
        },
    )
    assert summary["desired"] == 3
    assert summary["ready"] == 3
    assert summary["state"] == "running"


def test_scaled_to_zero_is_stopped_not_degraded() -> None:
    summary = kube.summarise_workload(
        "Deployment",
        {
            "metadata": {"name": "worker", "labels": {}},
            "spec": {"replicas": 0, "template": {"spec": {"containers": []}}},
            "status": {},
        },
    )
    assert summary["state"] == "stopped"


def test_unsupported_kind_is_refused_before_any_kubeapi_call() -> None:
    with patch.object(kube, "namespace", return_value="spatium"):
        with pytest.raises(kube.KubeControlError, match="unsupported workload kind"):
            kube.rollout_restart("Secret", "spatium-appliance-tls")


# ── REST surface ───────────────────────────────────────────────────


async def _superadmin(db: AsyncSession) -> str:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


@pytest.mark.asyncio
async def test_list_reports_capability_even_with_no_backend(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = {"Authorization": f"Bearer {await _superadmin(db_session)}"}
    kube_p, sock_p = _no_backends()
    with kube_p, sock_p:
        r = await client.get("/api/v1/system/services", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["capability"]["backend"] == "none"
    assert body["services"] == []
    # The capability, not a 503 — the UI has to tell "cannot" from "down".
    assert body["capability"]["reason"]


@pytest.mark.asyncio
async def test_non_superadmin_is_forbidden(client: AsyncClient, db_session: AsyncSession) -> None:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Plain",
        hashed_password=hash_password("x"),
        is_superadmin=False,
    )
    db_session.add(user)
    await db_session.flush()
    h = {"Authorization": f"Bearer {create_access_token(str(user.id))}"}
    assert (await client.get("/api/v1/system/services", headers=h)).status_code == 403
    assert (await client.post("/api/v1/system/services/api/restart", headers=h)).status_code == 403


@pytest.mark.asyncio
async def test_unknown_service_is_404_and_writes_no_audit_row(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = {"Authorization": f"Bearer {await _superadmin(db_session)}"}
    with (
        patch.object(backends, "capability", AsyncMock(return_value=_ENABLED_COMPOSE)),
        patch.object(backends, "list_services", AsyncMock(return_value=[])),
    ):
        r = await client.post("/api/v1/system/services/nope/restart", headers=h)
    assert r.status_code == 404, r.text
    rows = (
        (await db_session.execute(select(AuditLog).where(AuditLog.resource_type == "service")))
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_bad_action_is_400(client: AsyncClient, db_session: AsyncSession) -> None:
    h = {"Authorization": f"Bearer {await _superadmin(db_session)}"}
    r = await client.post("/api/v1/system/services/api/nuke", headers=h)
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_action_audits_before_signalling_and_records_accepted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``accepted``, not ``success``.

    On compose a self-targeted restart kills this process the moment the
    daemon takes the request, so nothing survives to observe the outcome
    — and the row has to be durable before the signal either way.
    """
    h = {"Authorization": f"Bearer {await _superadmin(db_session)}"}
    listed = [
        ServiceSummary(
            id="worker",
            name="proj-worker-1",
            kind="container",
            state="running",
            actions=("restart",),
            extra={"container_id": "abc123"},
        )
    ]
    applied: list[tuple[str, str]] = []

    async def _fake_apply(plan: backends.ActionPlan) -> None:
        applied.append((plan.service.id, plan.action))

    with (
        patch.object(backends, "capability", AsyncMock(return_value=_ENABLED_COMPOSE)),
        patch.object(backends, "list_services", AsyncMock(return_value=listed)),
        patch("app.api.v1.system.services.apply_action", _fake_apply),
    ):
        r = await client.post("/api/v1/system/services/worker/restart", headers=h)

    assert r.status_code == 202, r.text
    assert r.json()["status"] == "accepted"
    assert r.json()["self_targeted"] is False
    assert applied == [("worker", "restart")]

    row = (
        (await db_session.execute(select(AuditLog).where(AuditLog.resource_type == "service")))
        .scalars()
        .one()
    )
    assert row.action == "service_restart"
    assert row.resource_id == "worker"
    assert row.result == "accepted"
    assert row.new_value == {
        "kind": "container",
        "action": "restart",
        "self_targeted": False,
    }


@pytest.mark.asyncio
async def test_service_control_module_exports_stay_stable() -> None:
    """The package facade is what the router, the MCP tool and the
    operation all import — a rename here breaks three callers at once."""
    for name in ("capability", "list_services", "apply_action", "ACTIONS"):
        assert hasattr(service_control, name)


@pytest.mark.asyncio
async def test_self_targeted_action_is_deferred_past_the_response(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The api restarting itself must return 202 first.

    On compose the daemon stops this process the moment it accepts the
    request, so signalling inline aborts the response and the operator
    sees a connection reset rather than the confirmation that it worked.
    """
    h = {"Authorization": f"Bearer {await _superadmin(db_session)}"}
    listed = [
        ServiceSummary(
            id="api",
            name="proj-api-1",
            kind="container",
            state="running",
            actions=("restart",),
            extra={
                "container_id": _IDENTITY.container_id,
                "self_container_id": _IDENTITY.container_id,
            },
        )
    ]
    inline: list[str] = []
    deferred: list[str] = []

    async def _inline(plan: backends.ActionPlan) -> None:
        inline.append(plan.service.id)

    async def _detached(plan: backends.ActionPlan) -> None:
        deferred.append(plan.service.id)

    with (
        patch.object(backends, "capability", AsyncMock(return_value=_ENABLED_COMPOSE)),
        patch.object(backends, "list_services", AsyncMock(return_value=listed)),
        patch("app.api.v1.system.services.apply_action", _inline),
        patch("app.api.v1.system.services.apply_action_detached", _detached),
    ):
        r = await client.post("/api/v1/system/services/api/restart", headers=h)

    assert r.status_code == 202, r.text
    assert r.json()["self_targeted"] is True
    assert inline == []
    assert deferred == ["api"]


def test_self_detection_prefers_the_daemons_container_id(monkeypatch) -> None:
    """``$HOSTNAME`` is not the container id when a service sets
    ``hostname:``, so matching on it alone misses ourselves."""
    monkeypatch.setenv("HOSTNAME", "a-friendly-name")
    services = [
        ServiceSummary(
            id="api",
            name="proj-api-1",
            kind="container",
            state="running",
            extra={
                "container_id": _IDENTITY.container_id,
                "self_container_id": _IDENTITY.container_id,
            },
        ),
        ServiceSummary(
            id="worker",
            name="proj-worker-1",
            kind="container",
            state="running",
            extra={
                "container_id": "dddddddddddd",
                "self_container_id": _IDENTITY.container_id,
            },
        ),
    ]
    assert backends._self_service_id(services) == "api"
