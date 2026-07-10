"""``wol_wake_failed`` alert + per-target evidence trail (issue #596 Phases 2-3).

Phase 2 keys the alert on the **schedule**, not the run: a 15-minute schedule
that fails every fire would otherwise open ~96 events a day. The matcher re-runs
the passive liveness check on every 60 s evaluator tick, so the event
auto-resolves as stragglers boot and some other subsystem stamps
``IPAddress.last_seen_at`` — no bespoke resolve path.

Covered here:

* one open event per failing **schedule**, not per run;
* recovery — a post-wake sighting on the down host resolves the event on the
  next tick (the false-down escape hatch);
* the per-schedule mute (``verify_alert_enabled=False``) never matches;
* an **ad-hoc** run (``schedule_id IS NULL``) never matches — it has no schedule
  subject, by design;
* a target with no IPAM row can't be re-checked and keeps the alert open, rather
  than silently resolving it;
* a clean run resolves; a run outside ``threshold_days`` resolves;
* Phase 3's ``verify_evidence`` trail records every source consulted, in order.

``_deliver`` is always patched, so nothing leaves the process.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.alerts as alerts_svc
from app.models.alerts import AlertEvent, AlertRule
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.wol_schedule import (
    VERIFY_STATE_DONE,
    WolRun,
    WolRunTarget,
    WolSchedule,
)
from app.services.alerts import (
    RULE_TYPE_WOL_WAKE_FAILED,
    _matching_wol_wake_failed_subjects,
    evaluate_all,
)
from app.services.wol_scheduler.verify import verify_run_targets


class _DeliverSpy:
    """Stand in for ``alerts._deliver``; nothing leaves the process."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, rule, event, targets):  # type: ignore[no-untyped-def]
        self.calls += 1
        return (False, False, False)


# ── Builders ──────────────────────────────────────────────────────────


async def _ip(db: AsyncSession, address: str = "10.40.0.5") -> IPAddress:
    space = IPSpace(name=f"space-{uuid.uuid4().hex[:6]}")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.40.0.0/24", name="blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id, block_id=block.id, network="10.40.0.0/24", name="net", kind="unicast"
    )
    db.add(subnet)
    await db.flush()
    row = IPAddress(subnet_id=subnet.id, address=address, mac_address="aa:bb:cc:dd:ee:10")
    db.add(row)
    await db.flush()
    return row


async def _schedule(db: AsyncSession, **kw) -> WolSchedule:  # type: ignore[no-untyped-def]
    base = {
        "name": f"lab-{uuid.uuid4().hex[:4]}",
        "enabled": True,
        "target_selector": {"mode": "address_tags", "tags": ["wake"]},
        "schedule_cron": "0 7 * * *",
        "timezone": "UTC",
        "vantage": {"kind": "server", "id": None},
        "repeat_count": 1,
        "repeat_interval_ms": 0,
        "stagger_ms": 0,
        "port": 9,
        "verify_enabled": True,
        "verify_wait_seconds": 60,
        "verify_retries": 1,
        "verify_method": "auto",
        "verify_alert_enabled": True,
    }
    base.update(kw)
    sched = WolSchedule(**base)
    db.add(sched)
    await db.flush()
    return sched


async def _finalised_run(
    db: AsyncSession,
    sched: WolSchedule | None,
    *,
    ip: IPAddress | None,
    verified: bool | None,
    started_days_ago: float = 0.0,
    trigger: str = "schedule",
) -> WolRun:
    """A run that already finished verifying, with one SENT target."""
    started = datetime.now(UTC) - timedelta(days=started_days_ago)
    run = WolRun(
        schedule_id=sched.id if sched else None,
        trigger=trigger,
        status="ok",
        started_at=started,
        target_count=1,
        sent_count=1,
        verify_state=VERIFY_STATE_DONE,
        verified_count=1 if verified else 0,
        unverified_count=0 if verified else 1,
    )
    db.add(run)
    await db.flush()
    db.add(
        WolRunTarget(
            run_id=run.id,
            ip_address_id=ip.id if ip else None,
            address=ip.address if ip else "10.40.0.5",
            mac="aa:bb:cc:dd:ee:10",
            broadcast="10.40.0.255",
            sent=True,
            verified=verified,
            verify_method="seen",
            wake_attempts=2,
        )
    )
    await db.commit()
    return run


async def _rule(db: AsyncSession, *, enabled: bool = True) -> AlertRule:
    rule = AlertRule(
        name="Wake-on-LAN verify failed",
        rule_type=RULE_TYPE_WOL_WAKE_FAILED,
        severity="warning",
        threshold_days=1,
        enabled=enabled,
    )
    db.add(rule)
    await db.commit()
    return rule


async def _open_events(db: AsyncSession, rule: AlertRule) -> list[AlertEvent]:
    """Open events for ``rule``. No ``expire_all()`` — it would expire the
    caller's ``ip`` / ``run`` handles, and a later *sync* attribute access on an
    expired instance raises MissingGreenlet under asyncio. The filter is applied
    in SQL, so a stale identity map can't leak a resolved event through."""
    return list(
        (
            await db.execute(
                select(AlertEvent).where(
                    AlertEvent.rule_id == rule.id,
                    AlertEvent.resolved_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


# ══════════════════════════════════════════════════════════════════════
# Matcher
# ══════════════════════════════════════════════════════════════════════


async def test_failing_run_matches_its_schedule(db_session: AsyncSession) -> None:
    ip = await _ip(db_session)
    sched = await _schedule(db_session)
    await _finalised_run(db_session, sched, ip=ip, verified=False)
    rule = await _rule(db_session)

    matches = await _matching_wol_wake_failed_subjects(db_session, rule)

    assert len(matches) == 1
    subject_id, display, message = matches[0]
    assert subject_id == str(sched.id)  # subject is the SCHEDULE, not the run
    assert display == sched.name
    assert "did not come up" in message
    assert "10.40.0.5" in message


async def test_post_wake_sighting_resolves_the_match(db_session: AsyncSession) -> None:
    """The false-down escape hatch: the straggler booted twenty minutes late."""
    ip = await _ip(db_session)
    sched = await _schedule(db_session)
    run = await _finalised_run(db_session, sched, ip=ip, verified=False)
    rule = await _rule(db_session)
    assert len(await _matching_wol_wake_failed_subjects(db_session, rule)) == 1

    # Some other subsystem (SNMP ARP, a DHCP lease, an nmap sweep) sees it.
    ip.last_seen_at = run.started_at + timedelta(minutes=20)
    await db_session.commit()

    assert await _matching_wol_wake_failed_subjects(db_session, rule) == []


async def test_pre_wake_sighting_does_not_resolve(db_session: AsyncSession) -> None:
    """A sighting older than the wake proves nothing — the alert must stand."""
    ip = await _ip(db_session)
    sched = await _schedule(db_session)
    run = await _finalised_run(db_session, sched, ip=ip, verified=False)
    rule = await _rule(db_session)

    ip.last_seen_at = run.started_at - timedelta(hours=3)
    await db_session.commit()

    assert len(await _matching_wol_wake_failed_subjects(db_session, rule)) == 1


async def test_muted_schedule_never_matches(db_session: AsyncSession) -> None:
    ip = await _ip(db_session)
    sched = await _schedule(db_session, verify_alert_enabled=False)
    await _finalised_run(db_session, sched, ip=ip, verified=False)
    rule = await _rule(db_session)

    assert await _matching_wol_wake_failed_subjects(db_session, rule) == []


async def test_adhoc_run_never_matches(db_session: AsyncSession) -> None:
    """An ad-hoc run has no schedule subject, so it cannot open an event."""
    ip = await _ip(db_session)
    await _finalised_run(db_session, None, ip=ip, verified=False, trigger="adhoc")
    rule = await _rule(db_session)

    assert await _matching_wol_wake_failed_subjects(db_session, rule) == []


async def test_clean_run_does_not_match(db_session: AsyncSession) -> None:
    ip = await _ip(db_session)
    sched = await _schedule(db_session)
    await _finalised_run(db_session, sched, ip=ip, verified=True)
    rule = await _rule(db_session)

    assert await _matching_wol_wake_failed_subjects(db_session, rule) == []


async def test_run_outside_window_does_not_match(db_session: AsyncSession) -> None:
    """A lab PC off over the weekend must not pin an alert Friday → Monday."""
    ip = await _ip(db_session)
    sched = await _schedule(db_session)
    await _finalised_run(db_session, sched, ip=ip, verified=False, started_days_ago=5)
    rule = await _rule(db_session)

    assert await _matching_wol_wake_failed_subjects(db_session, rule) == []


async def test_null_ip_target_keeps_the_alert_open(db_session: AsyncSession) -> None:
    """No IPAM row means the passive re-check can't clear it — stay noisy."""
    sched = await _schedule(db_session)
    await _finalised_run(db_session, sched, ip=None, verified=False)
    rule = await _rule(db_session)

    assert len(await _matching_wol_wake_failed_subjects(db_session, rule)) == 1


async def test_disabled_verify_schedule_never_matches(db_session: AsyncSession) -> None:
    ip = await _ip(db_session)
    sched = await _schedule(db_session, verify_enabled=False)
    await _finalised_run(db_session, sched, ip=ip, verified=False)
    rule = await _rule(db_session)

    assert await _matching_wol_wake_failed_subjects(db_session, rule) == []


# ══════════════════════════════════════════════════════════════════════
# End to end through evaluate_all — open, dedupe, auto-resolve
# ══════════════════════════════════════════════════════════════════════


async def test_one_event_per_schedule_and_auto_resolve(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two failing runs of one schedule hold ONE open event; a sighting closes it."""
    spy = _DeliverSpy()
    monkeypatch.setattr(alerts_svc, "_deliver", spy)

    ip = await _ip(db_session)
    sched = await _schedule(db_session)
    await _finalised_run(db_session, sched, ip=ip, verified=False, started_days_ago=0.5)
    run2 = await _finalised_run(db_session, sched, ip=ip, verified=False)
    rule = await _rule(db_session)
    # Pin values now; evaluate_all commits and the handles must stay readable.
    sched_id, sighting = str(sched.id), run2.started_at + timedelta(minutes=15)

    await evaluate_all(db_session)
    events = await _open_events(db_session, rule)
    assert len(events) == 1  # per schedule, not per run
    assert events[0].subject_type == "wol_schedule"
    assert events[0].subject_id == sched_id
    assert spy.calls == 1  # delivered once through the evaluator path

    # A second tick with the same failure neither opens nor re-delivers.
    await evaluate_all(db_session)
    assert len(await _open_events(db_session, rule)) == 1
    assert spy.calls == 1

    # The straggler shows up on the network → next tick auto-resolves.
    ip.last_seen_at = sighting
    await db_session.commit()
    await evaluate_all(db_session)
    assert await _open_events(db_session, rule) == []


async def test_disabled_rule_opens_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule seeds OFF; until an operator enables it, nothing fires."""
    monkeypatch.setattr(alerts_svc, "_deliver", _DeliverSpy())
    ip = await _ip(db_session)
    sched = await _schedule(db_session)
    await _finalised_run(db_session, sched, ip=ip, verified=False)
    rule = await _rule(db_session, enabled=False)

    await evaluate_all(db_session)
    assert await _open_events(db_session, rule) == []


# ══════════════════════════════════════════════════════════════════════
# Phase 3 — the evidence trail
# ══════════════════════════════════════════════════════════════════════


async def test_evidence_trail_records_every_source_consulted(
    db_session: AsyncSession,
) -> None:
    """A down host under ``auto`` records ping, then tcp, then seen — in order."""
    ip = await _ip(db_session)
    sched = await _schedule(db_session)
    run = await _finalised_run(db_session, sched, ip=ip, verified=None)
    await db_session.refresh(run)

    async def _all_down(address, vantage=None, *, method="ping"):  # type: ignore[no-untyped-def]
        return False, method

    with patch("app.services.wol_scheduler.verify.probe_liveness", new=_all_down):
        down = await verify_run_targets(db_session, run, 1, method="auto")

    assert len(down) == 1
    trail = down[0].verify_evidence
    assert trail is not None
    assert [e["source"] for e in trail] == ["ping", "tcp", "seen"]
    assert all(e["up"] is False for e in trail)
    assert trail[2]["detail"] == "no sighting since the wake"
    assert all("observed_at" in e for e in trail)


async def test_evidence_trail_short_circuits_with_the_winning_source_last(
    db_session: AsyncSession,
) -> None:
    """ping down → tcp up: the trail stops at tcp; seen was never consulted."""
    ip = await _ip(db_session)
    sched = await _schedule(db_session)
    run = await _finalised_run(db_session, sched, ip=ip, verified=None)
    await db_session.refresh(run)

    async def _tcp_only(address, vantage=None, *, method="ping"):  # type: ignore[no-untyped-def]
        return method == "tcp", method

    with patch("app.services.wol_scheduler.verify.probe_liveness", new=_tcp_only):
        down = await verify_run_targets(db_session, run, 1, method="auto")

    assert down == []
    target = (
        await db_session.execute(select(WolRunTarget).where(WolRunTarget.run_id == run.id))
    ).scalar_one()
    trail = target.verify_evidence
    assert [e["source"] for e in trail] == ["ping", "tcp"]
    assert trail[0]["up"] is False
    assert trail[1]["up"] is True
    assert target.verify_method == "tcp"


async def test_seen_confirmation_records_the_sighting_time(
    db_session: AsyncSession,
) -> None:
    ip = await _ip(db_session)
    sched = await _schedule(db_session)
    run = await _finalised_run(db_session, sched, ip=ip, verified=None)
    await db_session.refresh(run)
    sighting = run.started_at + timedelta(seconds=42)
    ip.last_seen_at = sighting
    await db_session.commit()

    with patch(
        "app.services.wol_scheduler.verify.probe_liveness",
        new=AsyncMock(return_value=(False, "ping")),
    ):
        down = await verify_run_targets(db_session, run, 1, method="seen")

    assert down == []
    target = (
        await db_session.execute(select(WolRunTarget).where(WolRunTarget.run_id == run.id))
    ).scalar_one()
    trail = target.verify_evidence
    assert [e["source"] for e in trail] == ["seen"]
    assert trail[0]["up"] is True
    assert sighting.isoformat() in trail[0]["detail"]


# ══════════════════════════════════════════════════════════════════════
# Phase 3 — Operator Copilot reads
# ══════════════════════════════════════════════════════════════════════


async def test_mcp_find_and_count_wake_failures(db_session: AsyncSession) -> None:
    """Both tools see scheduled AND ad-hoc failures; clean runs don't inflate them."""
    from app.services.ai.tools.wol_scheduler import (
        CountWolWakeFailuresArgs,
        FindWolWakeFailuresArgs,
        count_wol_wake_failures,
        find_wol_wake_failures,
    )

    ip = await _ip(db_session)
    sched = await _schedule(db_session)
    await _finalised_run(db_session, sched, ip=ip, verified=False)
    await _finalised_run(db_session, sched, ip=ip, verified=True)
    await _finalised_run(db_session, None, ip=ip, verified=False, trigger="adhoc")

    rows = await find_wol_wake_failures(db_session, None, FindWolWakeFailuresArgs(days=7))
    assert len(rows) == 2  # the clean run is excluded
    triggers = {r["trigger"] for r in rows}
    assert triggers == {"schedule", "adhoc"}
    scheduled = next(r for r in rows if r["trigger"] == "schedule")
    assert scheduled["schedule_name"] == sched.name
    assert scheduled["unverified_count"] == 1
    adhoc = next(r for r in rows if r["trigger"] == "adhoc")
    assert adhoc["schedule_id"] is None
    assert adhoc["schedule_name"] is None

    counts = await count_wol_wake_failures(db_session, None, CountWolWakeFailuresArgs(days=7))
    assert counts == {
        "runs_with_failures": 2,
        "hosts_unverified": 2,
        "runs_verified_clean": 1,
    }


async def test_mcp_find_wake_failures_filters_by_schedule(db_session: AsyncSession) -> None:
    from app.services.ai.tools.wol_scheduler import (
        FindWolWakeFailuresArgs,
        find_wol_wake_failures,
    )

    ip = await _ip(db_session)
    sched = await _schedule(db_session)
    await _finalised_run(db_session, sched, ip=ip, verified=False)
    await _finalised_run(db_session, None, ip=ip, verified=False, trigger="adhoc")

    rows = await find_wol_wake_failures(
        db_session, None, FindWolWakeFailuresArgs(days=7, schedule_id=str(sched.id))
    )
    assert len(rows) == 1
    assert rows[0]["trigger"] == "schedule"

    # A malformed UUID is an empty result, never a 500.
    assert (
        await find_wol_wake_failures(
            db_session, None, FindWolWakeFailuresArgs(days=7, schedule_id="not-a-uuid")
        )
        == []
    )
