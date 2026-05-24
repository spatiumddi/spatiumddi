"""Phase H safety-rail tests (#296).

Covers:

* ``is_upgrade_in_flight`` / ``assert_no_upgrade_in_flight`` — the
  global mutex helper: returns the right row, raises 409 with the
  right message, no-op when no in-flight run.
* ``check_cnpg_instances_ready`` — empty cluster (skip), 404 (warn),
  spec.instances=0 (ok skip), N<spec (fail), N==spec (ok).
* ``check_daemonset_pods_ready`` — all Ready (ok), one NotReady
  (fail), kubeapi unreachable (warn).
* ``verify_post_upgrade`` aggregator + ``verification_overall``
  worst-level-wins.
* ``_transition`` writes the AuditLog row that drives Phase H's
  typed events.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.upgrades import safety

# ── Mutex helper ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_upgrade_in_flight_returns_none_when_clear() -> None:
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    out = await safety.is_upgrade_in_flight(db)
    assert out is None


@pytest.mark.asyncio
async def test_is_upgrade_in_flight_returns_row_when_active() -> None:
    """Any non-terminal state surfaces. Phase D's lifecycle invariant
    keeps this to at most one row at a time."""
    fake_row = MagicMock(state="running", target_version="2026.06.01-1")
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = fake_row
    db.execute = AsyncMock(return_value=result)
    out = await safety.is_upgrade_in_flight(db)
    assert out is fake_row


@pytest.mark.asyncio
async def test_assert_no_upgrade_in_flight_passes_when_clear() -> None:
    """No row → no exception."""
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    # Should not raise.
    await safety.assert_no_upgrade_in_flight(db, operation_hint="backup creation")


@pytest.mark.asyncio
async def test_assert_no_upgrade_in_flight_raises_409_with_hint() -> None:
    """The exception message carries the operation hint + run details
    so the operator sees actionable text in the api response."""
    from fastapi import HTTPException  # noqa: PLC0415

    fake_row = MagicMock(id=uuid.uuid4(), state="running", target_version="2026.06.01-1")
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = fake_row
    db.execute = AsyncMock(return_value=result)
    with pytest.raises(HTTPException) as exc:
        await safety.assert_no_upgrade_in_flight(db, operation_hint="factory reset")
    assert exc.value.status_code == 409
    assert "factory reset" in str(exc.value.detail)
    assert "2026.06.01-1" in str(exc.value.detail)
    assert "running" in str(exc.value.detail)


# ── CNPG instance verification ───────────────────────────────────────


def test_check_cnpg_instances_empty_cluster_skips() -> None:
    """Single-node / non-CNPG deploys leave cluster_name empty; the
    check returns ok + skipped=True."""
    out = safety.check_cnpg_instances_ready("")
    assert out.level == "ok"
    assert out.detail["skipped"] is True


def test_check_cnpg_instances_404_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cluster GET returns 404 → warn (operator may have torn it
    down intentionally)."""
    monkeypatch.setattr(safety.k8s, "get_cnpg_cluster", lambda _n, namespace=None: (404, None))
    out = safety.check_cnpg_instances_ready("pg-cluster")
    assert out.level == "warn"


def test_check_cnpg_instances_fail_when_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """spec.instances=3, readyInstances=2 → fail."""
    monkeypatch.setattr(
        safety.k8s,
        "get_cnpg_cluster",
        lambda _n, namespace=None: (
            200,
            {
                "spec": {"instances": 3},
                "status": {"readyInstances": 2, "currentPrimary": "pg-1"},
            },
        ),
    )
    out = safety.check_cnpg_instances_ready("pg-cluster")
    assert out.level == "fail"
    assert "2/3" in out.message
    assert out.detail["ready_instances"] == 2


def test_check_cnpg_instances_ok_when_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """spec.instances=3, readyInstances=3 → ok."""
    monkeypatch.setattr(
        safety.k8s,
        "get_cnpg_cluster",
        lambda _n, namespace=None: (
            200,
            {
                "spec": {"instances": 3},
                "status": {"readyInstances": 3, "currentPrimary": "pg-1"},
            },
        ),
    )
    out = safety.check_cnpg_instances_ready("pg-cluster")
    assert out.level == "ok"
    assert out.detail["ready_instances"] == 3


def test_check_cnpg_instances_kubeapi_unreachable_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_args: Any, **_kw: Any) -> Any:
        raise safety.k8s.KubeapiUnavailableError("no SA mounted")

    monkeypatch.setattr(safety.k8s, "get_cnpg_cluster", _raise)
    out = safety.check_cnpg_instances_ready("pg-cluster")
    assert out.level == "warn"


# ── DaemonSet readiness verification ─────────────────────────────────


def _ds_pod(name: str, ready: bool, namespace: str = "kube-system") -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "ownerReferences": [{"kind": "DaemonSet"}],
        },
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }


def test_check_ds_pods_all_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        safety.k8s,
        "list_pods",
        lambda namespace=None: [
            _ds_pod("bind9-a", True),
            _ds_pod("kea-a", True),
        ],
    )
    out = safety.check_daemonset_pods_ready()
    assert out.level == "ok"
    assert out.detail["ds_pod_count"] == 2


def test_check_ds_pods_one_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        safety.k8s,
        "list_pods",
        lambda namespace=None: [
            _ds_pod("bind9-a", True),
            _ds_pod("kea-a", False),
        ],
    )
    out = safety.check_daemonset_pods_ready()
    assert out.level == "fail"
    assert "1 of 2" in out.message
    assert "kube-system/kea-a" in out.detail["not_ready"]


def test_check_ds_pods_kubeapi_unreachable_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(namespace: str | None = None) -> Any:
        raise safety.k8s.KubeapiUnavailableError("no SA mounted")

    monkeypatch.setattr(safety.k8s, "list_pods", _raise)
    out = safety.check_daemonset_pods_ready()
    assert out.level == "warn"


def test_check_ds_pods_ignores_non_daemonset_pods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NotReady non-DS pod (RS-owned, etc.) shouldn't fail the DS
    readiness check — that's a different signal."""
    rs_pod_not_ready = {
        "metadata": {
            "name": "api-1",
            "namespace": "spatium",
            "ownerReferences": [{"kind": "ReplicaSet"}],
        },
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "False"}],
        },
    }
    monkeypatch.setattr(
        safety.k8s,
        "list_pods",
        lambda namespace=None: [
            _ds_pod("bind9-a", True),
            rs_pod_not_ready,
        ],
    )
    out = safety.check_daemonset_pods_ready()
    assert out.level == "ok"
    assert out.detail["ds_pod_count"] == 1


# ── Aggregator ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_post_upgrade_returns_both_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(safety.k8s, "get_cnpg_cluster", lambda _n, namespace=None: (404, None))
    monkeypatch.setattr(safety.k8s, "list_pods", lambda namespace=None: [])
    out = await safety.verify_post_upgrade(cnpg_cluster_name="pg-cluster")
    names = [c.name for c in out]
    assert "cnpg_instances" in names
    assert "daemonset_pods_ready" in names


def test_verification_overall_worst_level_wins() -> None:
    ok = safety.VerificationCheck("a", "ok", "", {})
    warn = safety.VerificationCheck("b", "warn", "", {})
    fail = safety.VerificationCheck("c", "fail", "", {})
    assert safety.verification_overall([ok, ok]) == "ok"
    assert safety.verification_overall([ok, warn]) == "warn"
    assert safety.verification_overall([warn, fail]) == "fail"
    assert safety.verification_overall([ok, warn, fail]) == "fail"
    # Empty list → ok (no failures observed).
    assert safety.verification_overall([]) == "ok"


# ── _transition writes the AuditLog row that drives typed events ─────


@pytest.mark.asyncio
async def test_transition_writes_audit_log_with_event_action() -> None:
    """Every state transition writes ``AuditLog(action='upgrade.<event>',
    resource_type='system_upgrade_run')``. The event_publisher's
    _SPECIAL_EVENT_MAP keys off those exact strings to fire the typed
    ``system.upgrade.<event>`` webhook events."""
    from app.services.upgrades import orchestrator  # noqa: PLC0415

    class _Run:
        def __init__(self) -> None:
            self.id = uuid.uuid4()
            self.state = "running"
            self.target_version = "2026.06.01-1"
            self.progress: dict[str, Any] = {"events": []}
            self.finished_at: Any = None

    run = _Run()
    db = MagicMock()
    db.add = MagicMock()

    await orchestrator._transition(
        db,  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        "halted",
        allowed_from=("running",),
        event="halted",
        actor_user_id=None,
        actor_display="admin",
        actor_source="local",
    )

    assert run.state == "halted"
    # AuditLog gets added; check its shape.
    audit_calls = [c.args[0] for c in db.add.call_args_list if hasattr(c.args[0], "action")]
    assert len(audit_calls) == 1
    al = audit_calls[0]
    assert al.action == "upgrade.halted"
    assert al.resource_type == "system_upgrade_run"
    assert al.resource_id == str(run.id)
    assert al.user_display_name == "admin"
    assert al.new_value["from_state"] == "running"
    assert al.new_value["to_state"] == "halted"


@pytest.mark.asyncio
async def test_transition_terminal_state_sets_finished_at() -> None:
    """succeeded / failed / aborted → finished_at gets stamped."""
    from app.services.upgrades import orchestrator  # noqa: PLC0415

    class _Run:
        def __init__(self) -> None:
            self.id = uuid.uuid4()
            self.state = "running"
            self.target_version = "2026.06.01-1"
            self.progress: dict[str, Any] = {"events": []}
            self.finished_at: datetime | None = None

    run = _Run()
    db = MagicMock()
    db.add = MagicMock()
    await orchestrator._transition(
        db,  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        "succeeded",
        allowed_from=("running",),
        event="succeeded",
    )
    assert run.state == "succeeded"
    assert run.finished_at is not None
    assert run.finished_at.tzinfo == UTC


# ── Event publisher mapping ──────────────────────────────────────────


def test_event_publisher_maps_upgrade_actions_cleanly() -> None:
    """The Phase H additions to _SPECIAL_EVENT_MAP collapse the
    ``upgrade.<verb>`` action + ``system_upgrade_run`` resource_type
    into clean ``system.upgrade.<verb>`` event names. Without these
    overrides the auto-derivation would produce awkward names like
    ``system_upgrade_run.upgrade.halted`` (or just None — the
    resource_type isn't in _RESOURCE_NAMESPACE)."""
    from app.services.event_publisher import _audit_to_event_type  # noqa: PLC0415

    cases = {
        "upgrade.planned": "system.upgrade.planned",
        "upgrade.started": "system.upgrade.started",
        "upgrade.succeeded": "system.upgrade.succeeded",
        "upgrade.failed": "system.upgrade.failed",
        "upgrade.halted": "system.upgrade.halted",
        "upgrade.resumed": "system.upgrade.resumed",
        "upgrade.aborted": "system.upgrade.aborted",
    }
    for action, expected in cases.items():
        assert _audit_to_event_type(action, "system_upgrade_run") == expected
