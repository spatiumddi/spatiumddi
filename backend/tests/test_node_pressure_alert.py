"""The ``node_pressure`` PSI alert matcher (#983 Phase 2 item 7).

This is the alarm #980 needed and nothing could raise: the appliance dropped
relayed DHCP under CPU pressure with every dashboard green, because
utilisation cannot tell a node at 70% CPU with a run queue from one without.

Two properties matter more than the arithmetic:

  * a kubelet that reports NO PSI must never match. Below Kubernetes 1.36
    there is no reading, and firing on that would alarm every node in the
    fleet on upgrade day while saying something false — the same rule #882's
    matcher follows for an agent that has never reported.
  * ``some`` and ``full`` must not share a threshold. ``some`` at 20% is a
    busy node; ``full`` at 20% is a node that spent a fifth of five minutes
    doing no work at all. One knob cannot mean both.
"""

from __future__ import annotations

import pytest

from app.models.alerts import AlertRule
from app.services import alerts


def _rule(threshold: int | None = 50) -> AlertRule:
    return AlertRule(
        name="Node under sustained resource pressure",
        rule_type=alerts.RULE_TYPE_NODE_PRESSURE,
        severity="warning",
        enabled=True,
        threshold_percent=threshold,
    )


def _psi(some: float | None = None, full: float | None = None) -> dict | None:
    out: dict = {}
    if some is not None:
        out["some"] = {"avg10": some, "avg60": some, "avg300": some}
    if full is not None:
        out["full"] = {"avg10": full, "avg60": full, "avg300": full}
    return out or None


def _snap(**node) -> dict:
    base = {"name": "ddi1", "psi_cpu": None, "psi_memory": None, "psi_io": None}
    base.update(node)
    return {"available": True, "nodes": [base]}


@pytest.fixture
def _appliance(monkeypatch):
    """Both gates the matcher checks before doing any work."""
    from app.config import settings

    monkeypatch.setattr(settings, "appliance_mode", True, raising=False)


async def _match(monkeypatch, snap, rule=None):
    monkeypatch.setattr("app.services.appliance.cluster_health.get_cluster_health", lambda: snap)
    return await alerts._matching_node_pressure_subjects(None, rule or _rule())


# ── the null case, which is most of the fleet on day one ────────────────────


@pytest.mark.asyncio
async def test_no_psi_reported_never_matches(monkeypatch, _appliance):
    assert await _match(monkeypatch, _snap()) == []


@pytest.mark.asyncio
async def test_zero_pressure_does_not_match(monkeypatch, _appliance):
    """A 1.36 kubelet on an idle node reports 0.0 — a real reading, and not
    a reason to page."""
    snap = _snap(psi_cpu=_psi(some=0.0), psi_memory=_psi(some=0.0, full=0.0))
    assert await _match(monkeypatch, snap) == []


@pytest.mark.asyncio
async def test_unavailable_cluster_is_unknown_not_resolved(monkeypatch, _appliance):
    """NOT ``[]``. ``evaluate_all`` resolves every open event whose subject is
    absent from a pass, so returning no matches here would CLOSE the
    operator's open pressure events on a kubeapi blip and re-open them a
    minute later — a notification flap under exactly the load the rule
    reports on. Raising skips the rule for one pass instead."""
    with pytest.raises(alerts.AlertDataUnavailable):
        await _match(monkeypatch, {"available": False})


@pytest.mark.asyncio
async def test_a_health_fetch_failure_is_unknown_not_resolved(monkeypatch, _appliance):
    def _boom():
        raise RuntimeError("kubeapi unreachable")

    monkeypatch.setattr("app.services.appliance.cluster_health.get_cluster_health", _boom)
    with pytest.raises(alerts.AlertDataUnavailable):
        await alerts._matching_node_pressure_subjects(None, _rule())


@pytest.mark.asyncio
async def test_unknown_is_distinct_from_no_pressure(monkeypatch, _appliance):
    """The pair that makes the distinction real: a healthy cluster with no
    stalling returns [] (open events resolve away), an unreadable one raises
    (open events hold)."""
    assert await _match(monkeypatch, _snap(psi_cpu=_psi(some=0.0))) == []
    with pytest.raises(alerts.AlertDataUnavailable):
        await _match(monkeypatch, {"available": False})


@pytest.mark.asyncio
async def test_skipped_entirely_off_appliance(monkeypatch):
    """No ServiceAccount, no Summary API — do not even make the call."""
    from app.config import settings

    monkeypatch.setattr(settings, "appliance_mode", False, raising=False)

    def _never():
        raise AssertionError("cluster health must not be fetched off-appliance")

    monkeypatch.setattr("app.services.appliance.cluster_health.get_cluster_health", _never)
    assert await alerts._matching_node_pressure_subjects(None, _rule()) == []


# ── thresholds ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cpu_some_over_threshold_warns(monkeypatch, _appliance):
    matches = await _match(monkeypatch, _snap(psi_cpu=_psi(some=61.0)))
    assert len(matches) == 1
    subject_id, display, message, severity = matches[0]
    assert (subject_id, display, severity) == ("ddi1", "ddi1", "warning")
    assert "CPU stall 61.0%" in message


@pytest.mark.asyncio
async def test_just_under_threshold_does_not_fire(monkeypatch, _appliance):
    assert await _match(monkeypatch, _snap(psi_cpu=_psi(some=49.9))) == []


@pytest.mark.asyncio
async def test_threshold_is_operator_tunable(monkeypatch, _appliance):
    snap = _snap(psi_cpu=_psi(some=25.0))
    assert await _match(monkeypatch, snap, _rule(threshold=50)) == []
    assert len(await _match(monkeypatch, snap, _rule(threshold=20))) == 1


@pytest.mark.asyncio
async def test_memory_full_is_critical_on_its_own_floor(monkeypatch, _appliance):
    """1.5% full-stall is far below the 50% ``some`` threshold and is still
    critical — every runnable task blocked is a different fact."""
    snap = _snap(psi_memory=_psi(some=2.0, full=1.5))
    matches = await _match(monkeypatch, snap)
    assert len(matches) == 1
    assert matches[0][3] == "critical"
    assert "every runnable task blocked" in matches[0][2]


@pytest.mark.asyncio
async def test_memory_full_below_its_floor_does_not_fire(monkeypatch, _appliance):
    assert await _match(monkeypatch, _snap(psi_memory=_psi(some=1.0, full=0.4))) == []


@pytest.mark.asyncio
async def test_critical_wins_over_warning_on_the_same_node(monkeypatch, _appliance):
    snap = _snap(psi_cpu=_psi(some=80.0), psi_memory=_psi(some=70.0, full=5.0))
    matches = await _match(monkeypatch, snap)
    assert len(matches) == 1
    assert matches[0][3] == "critical"
    # ...and the message still names every reason, not just the worst.
    assert "CPU stall" in matches[0][2] and "memory full-stall" in matches[0][2]


@pytest.mark.asyncio
async def test_cpu_full_is_never_evaluated(monkeypatch, _appliance):
    """The kernel reports CPU ``full`` as 0 at node level by definition, so a
    threshold on it could only ever be dead code — assert we did not add one
    that would fire on a kubelet reporting a nonzero value anyway."""
    snap = _snap(psi_cpu=_psi(some=1.0, full=99.0))
    assert await _match(monkeypatch, snap) == []


@pytest.mark.asyncio
async def test_each_node_is_its_own_subject(monkeypatch, _appliance):
    snap = {
        "available": True,
        "nodes": [
            {"name": "ddi1", "psi_cpu": _psi(some=80.0), "psi_memory": None, "psi_io": None},
            {"name": "ddi2", "psi_cpu": _psi(some=1.0), "psi_memory": None, "psi_io": None},
            {"name": "ddi3", "psi_cpu": None, "psi_memory": None, "psi_io": None},
        ],
    }
    matches = await _match(monkeypatch, snap)
    assert [m[0] for m in matches] == ["ddi1"]


# ── the helper the matcher leans on ─────────────────────────────────────────


@pytest.mark.parametrize(
    "block", [None, {}, "psi", {"some": None}, {"some": {}}, {"some": {"avg300": "x"}}]
)
def test_psi_avg300_returns_none_for_anything_unusable(block):
    assert alerts._psi_avg300(block, "some") is None


def test_psi_avg300_reads_the_five_minute_window():
    """avg300, not avg10 — a burst and a condition have to be different
    numbers or 'sustained' means nothing."""
    block = {"some": {"avg10": 90.0, "avg60": 50.0, "avg300": 7.0}}
    assert alerts._psi_avg300(block, "some") == 7.0


# ── end to end: the flap this rule would otherwise cause ────────────────────
#
# The matcher raising is only half the fix. What matters is what
# ``evaluate_all`` does with it — that is the code path which resolves open
# events, and the reason a matcher returning [] on a blip would have flapped
# the alarm once a minute under load.


@pytest.mark.asyncio
async def test_evaluate_all_holds_open_events_when_data_is_unavailable(
    db_session, monkeypatch
) -> None:
    from datetime import UTC, datetime

    from app.config import settings
    from app.models.alerts import AlertEvent
    from app.models.alerts import AlertRule as RuleModel

    monkeypatch.setattr(settings, "appliance_mode", True, raising=False)
    rule = RuleModel(
        name="Node under sustained resource pressure",
        description="",
        rule_type=alerts.RULE_TYPE_NODE_PRESSURE,
        severity="warning",
        enabled=True,
        threshold_percent=50,
    )
    db_session.add(rule)
    await db_session.flush()
    event = AlertEvent(
        rule_id=rule.id,
        subject_type="node",
        subject_id="ddi1",
        subject_display="ddi1",
        severity="warning",
        message="CPU stall 80.0% of the last 5 min",
        fired_at=datetime.now(UTC),
    )
    db_session.add(event)
    await db_session.flush()

    def _boom():
        raise RuntimeError("kubeapi unreachable")

    monkeypatch.setattr("app.services.appliance.cluster_health.get_cluster_health", _boom)
    summary = await alerts.evaluate_all(db_session)

    await db_session.refresh(event)
    assert event.resolved_at is None, (
        "an unreadable cluster resolved an open pressure event — the alarm "
        "would re-open on the next good tick and re-notify, once a minute, "
        "for the duration of the incident it is reporting on"
    )
    assert summary["resolved"] == 0


@pytest.mark.asyncio
async def test_evaluate_all_still_resolves_when_the_pressure_really_clears(
    db_session, monkeypatch
) -> None:
    """The other half — the hold must not become a stuck event."""
    from datetime import UTC, datetime

    from app.config import settings
    from app.models.alerts import AlertEvent
    from app.models.alerts import AlertRule as RuleModel

    monkeypatch.setattr(settings, "appliance_mode", True, raising=False)
    rule = RuleModel(
        name="Node under sustained resource pressure",
        description="",
        rule_type=alerts.RULE_TYPE_NODE_PRESSURE,
        severity="warning",
        enabled=True,
        threshold_percent=50,
    )
    db_session.add(rule)
    await db_session.flush()
    event = AlertEvent(
        rule_id=rule.id,
        subject_type="node",
        subject_id="ddi1",
        subject_display="ddi1",
        severity="warning",
        message="CPU stall 80.0% of the last 5 min",
        fired_at=datetime.now(UTC),
    )
    db_session.add(event)
    await db_session.flush()

    monkeypatch.setattr(
        "app.services.appliance.cluster_health.get_cluster_health",
        lambda: _snap(psi_cpu=_psi(some=0.0)),
    )
    await alerts.evaluate_all(db_session)
    await db_session.refresh(event)
    assert event.resolved_at is not None


@pytest.mark.asyncio
async def test_a_zero_threshold_is_honoured_not_silently_defaulted(monkeypatch, _appliance) -> None:
    """``or 50`` would rewrite an operator's 0 to 50 — the form allows it and
    the column is numeric, so the UI would say 0 and the rule would use 50.
    Every other rule in alerts.py reads its threshold with ``is not None``;
    this one was the outlier."""
    snap = _snap(psi_cpu=_psi(some=0.0))
    assert len(await _match(monkeypatch, snap, _rule(threshold=0))) == 1
    # ...and unset still falls back to the documented default.
    assert await _match(monkeypatch, _snap(psi_cpu=_psi(some=30.0)), _rule(threshold=None)) == []
