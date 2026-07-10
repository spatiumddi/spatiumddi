"""Post-wake verify + retry tests for Scheduled Wake-on-LAN — Phase 3 (#586).

Covers the chained verify state machine (:func:`app.tasks.wol_scheduler._verify_run`)
end to end plus its two pure building blocks:

Multi-source liveness (#596) adds sections 4 + 5: the ``tcp`` / ``seen`` probes
and the fusion policy — passive sources may only confirm, never condemn; a
sighting predating the wake can never confirm it; a passive confirm never
re-stamps Seen; and ``auto`` short-circuits on the first source that answers.

* ``auto_stagger_ms`` — the stagger auto-tune bands + the operator-override
  passthrough.
* ``probe_liveness`` — ping exit-code → up/down, never-raises.
* ``_verify_run`` state machine — (a) all-up first pass finalises ``done`` with
  no re-enqueue + stamps Seen on responders; (b) a down host at ``retries=1``
  re-wakes ONLY the down host (bumping its ``wake_attempts``) and re-enqueues
  the next attempt back to ``pending``; (c) ``retries=0`` finalises ``done``
  with ``unverified_count>0`` and never re-wakes; (d) a double-fire is a no-op
  via the ``pending``-only atomic claim; (e) verify turned off mid-flight
  finalises immediately without probing.

The real UDP send (``app.services.wol.wake_from_server``) and the real ping
(``app.services.wol_scheduler.verify.run_ping``) are always patched, so no
packet or ICMP echo ever leaves the test process.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.tasks.wol_scheduler as task
from app.core.security import hash_password
from app.models.auth import User
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.wol_schedule import WolRun, WolRunTarget, WolSchedule
from app.services.wol_scheduler.verify import (
    auto_stagger_ms,
    probe_liveness,
    seen_since,
    verify_run_targets,
)

# ══════════════════════════════════════════════════════════════════════
# 1. auto_stagger_ms — bands + override passthrough (pure)
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "count,expected",
    [
        (0, 0),
        (20, 0),
        (21, 50),
        (100, 50),
        (101, 100),
        (256, 100),
        (257, 150),
        (512, 150),
    ],
)
def test_auto_stagger_bands(count: int, expected: int) -> None:
    # override == 0 → "auto": ramp large fleets by count band.
    assert auto_stagger_ms(count, 0) == expected


@pytest.mark.parametrize("count", [0, 500])
def test_auto_stagger_override_always_wins(count: int) -> None:
    # A positive operator value is returned verbatim regardless of count.
    assert auto_stagger_ms(count, 250) == 250


# ══════════════════════════════════════════════════════════════════════
# 2. probe_liveness — ping exit-code → up/down, never raises
# ══════════════════════════════════════════════════════════════════════


def _ping_result(exit_code: int, timed_out: bool = False) -> SimpleNamespace:
    return SimpleNamespace(exit_code=exit_code, timed_out=timed_out, available=True)


async def test_probe_up_on_exit_zero() -> None:
    with patch(
        "app.services.wol_scheduler.verify.run_ping",
        new=AsyncMock(return_value=_ping_result(0)),
    ):
        up, method = await probe_liveness("10.20.0.5")
    assert up is True
    assert method == "ping"


async def test_probe_down_on_nonzero_exit() -> None:
    with patch(
        "app.services.wol_scheduler.verify.run_ping",
        new=AsyncMock(return_value=_ping_result(1)),
    ):
        up, _ = await probe_liveness("10.20.0.5")
    assert up is False


async def test_probe_down_on_timeout() -> None:
    with patch(
        "app.services.wol_scheduler.verify.run_ping",
        new=AsyncMock(return_value=_ping_result(0, timed_out=True)),
    ):
        up, _ = await probe_liveness("10.20.0.5")
    assert up is False


async def test_probe_never_raises() -> None:
    # A probe exception (missing binary / crash) is a "down" verdict, never
    # an aborted pass.
    with patch(
        "app.services.wol_scheduler.verify.run_ping",
        new=AsyncMock(side_effect=RuntimeError("no ping binary")),
    ):
        up, _ = await probe_liveness("10.20.0.5")
    assert up is False


async def test_probe_no_address_is_down() -> None:
    up, _ = await probe_liveness(None)
    assert up is False


# ══════════════════════════════════════════════════════════════════════
# Fixtures / builders
# ══════════════════════════════════════════════════════════════════════


async def _owner(db: AsyncSession) -> User:
    u = User(
        username=f"admin-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@x.com",
        display_name="Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(u)
    await db.flush()
    return u


async def _subnet(db: AsyncSession) -> Subnet:
    space = IPSpace(name=f"space-{uuid.uuid4().hex[:6]}")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.20.0.0/24", name="blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id, block_id=block.id, network="10.20.0.0/24", name="net", kind="unicast"
    )
    db.add(subnet)
    await db.flush()
    return subnet


def _make_schedule(owner_id: uuid.UUID, **kw: Any) -> WolSchedule:
    base: dict[str, Any] = {
        "name": "nightly-lab",
        "enabled": True,
        "target_selector": {"mode": "address_tags", "tags": ["wake:nightly"]},
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
        "verify_method": "ping",
        "created_by_user_id": owner_id,
    }
    base.update(kw)
    return WolSchedule(**base)


async def _run_with_sent_targets(
    db: AsyncSession,
    schedule: WolSchedule,
    hosts: list[tuple[str, str, uuid.UUID | None]],
    *,
    verify_state: str = "pending",
) -> tuple[WolRun, list[WolRunTarget]]:
    """Create a committed ``wol_run`` + one SENT ``wol_run_target`` per host.

    ``hosts`` items are ``(address, mac, ip_address_id|None)``.
    """
    run = WolRun(
        schedule_id=schedule.id,
        trigger="manual",
        status="ok",
        target_count=len(hosts),
        sent_count=len(hosts),
        verify_state=verify_state,
    )
    db.add(run)
    await db.flush()
    targets: list[WolRunTarget] = []
    for address, mac, ip_id in hosts:
        t = WolRunTarget(
            run_id=run.id,
            ip_address_id=ip_id,
            address=address,
            mac=mac,
            broadcast="10.20.0.255",
            subnet_id=None,
            vantage={"kind": "server", "id": None},
            mac_source="ip",
            sent=True,
            verified=None,
            wake_attempts=1,
        )
        db.add(t)
        targets.append(t)
    await db.commit()
    return run, targets


def _ok_send() -> AsyncMock:
    return AsyncMock(return_value=SimpleNamespace(sent=True, ran_from="server"))


def _probe(up_addrs: set[str]) -> Any:
    """A patched ``probe_liveness`` — UP for addresses in ``up_addrs``, else DOWN."""

    async def fake(address: str | None, vantage: Any = None, *, method: str = "ping"):
        return (address in up_addrs), "ping"

    return fake


# ══════════════════════════════════════════════════════════════════════
# 3. _verify_run state machine
# ══════════════════════════════════════════════════════════════════════


async def test_verify_all_up_first_pass_done_and_stamps_seen(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    subnet = await _subnet(db_session)
    ip = IPAddress(subnet_id=subnet.id, address="10.20.0.11", mac_address="aa:bb:cc:dd:ee:01")
    db_session.add(ip)
    await db_session.flush()
    schedule = _make_schedule(owner.id, verify_retries=1)
    db_session.add(schedule)
    await db_session.flush()
    run, _targets = await _run_with_sent_targets(
        db_session, schedule, [("10.20.0.11", "aa:bb:cc:dd:ee:01", ip.id)]
    )

    reenqueue = MagicMock()
    with (
        patch("app.services.wol_scheduler.verify.probe_liveness", new=_probe({"10.20.0.11"})),
        patch.object(task.verify_wol_run, "apply_async", reenqueue),
    ):
        result = await task._verify_run(str(run.id), 1)

    assert result["verify_state"] == task.VERIFY_DONE
    run_id = run.id
    ip_id = ip.id
    db_session.expire_all()
    fresh = await db_session.get(WolRun, run_id)
    assert fresh is not None
    assert fresh.verify_state == task.VERIFY_DONE
    assert fresh.verified_count == 1
    assert fresh.unverified_count == 0
    # All-up ⇒ no re-wake ⇒ no re-enqueue.
    reenqueue.assert_not_called()
    # Seen stamped on the responder.
    fresh_ip = await db_session.get(IPAddress, ip_id)
    assert fresh_ip is not None
    assert fresh_ip.last_seen_method == "ping"
    assert fresh_ip.last_seen_at is not None


async def test_verify_down_host_rewakes_only_nonresponder_and_reenqueues(
    db_session: AsyncSession,
) -> None:
    owner = await _owner(db_session)
    schedule = _make_schedule(owner.id, verify_retries=1)
    db_session.add(schedule)
    await db_session.flush()
    run, _targets = await _run_with_sent_targets(
        db_session,
        schedule,
        [
            ("10.20.0.11", "aa:bb:cc:dd:ee:01", None),  # up
            ("10.20.0.12", "aa:bb:cc:dd:ee:02", None),  # down
        ],
    )

    send = _ok_send()
    reenqueue = MagicMock()
    with (
        patch("app.services.wol_scheduler.verify.probe_liveness", new=_probe({"10.20.0.11"})),
        patch("app.services.wol.wake_from_server", new=send),
        patch.object(task.verify_wol_run, "apply_async", reenqueue),
    ):
        result = await task._verify_run(str(run.id), 1)

    assert result["reenqueued"] is True
    assert result["rewoke"] == 1
    # Mutex released back to pending for the next pass.
    run_id = run.id
    db_session.expire_all()
    fresh = await db_session.get(WolRun, run_id)
    assert fresh is not None
    assert fresh.verify_state == task.VERIFY_PENDING
    # ONLY the down host was re-woken (one send call).
    assert send.await_count == 1
    # Attempt 2 re-enqueued with the wait countdown.
    reenqueue.assert_called_once()
    _args, kwargs = reenqueue.call_args
    assert kwargs["args"] == [str(run.id), 2]
    assert kwargs["countdown"] == 60
    # wake_attempts bumped on the down host only.
    rows = (
        (await db_session.execute(select(WolRunTarget).where(WolRunTarget.run_id == run_id)))
        .scalars()
        .all()
    )
    by_addr = {r.address: r for r in rows}
    assert by_addr["10.20.0.12"].wake_attempts == 2
    assert by_addr["10.20.0.12"].verified is False
    assert by_addr["10.20.0.11"].wake_attempts == 1
    assert by_addr["10.20.0.11"].verified is True


async def test_verify_zero_retries_finalises_without_rewake(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    schedule = _make_schedule(owner.id, verify_retries=0)
    db_session.add(schedule)
    await db_session.flush()
    run, _targets = await _run_with_sent_targets(
        db_session, schedule, [("10.20.0.12", "aa:bb:cc:dd:ee:02", None)]
    )

    send = _ok_send()
    reenqueue = MagicMock()
    with (
        patch("app.services.wol_scheduler.verify.probe_liveness", new=_probe(set())),
        patch("app.services.wol.wake_from_server", new=send),
        patch.object(task.verify_wol_run, "apply_async", reenqueue),
    ):
        result = await task._verify_run(str(run.id), 1)

    assert result["verify_state"] == task.VERIFY_DONE
    run_id = run.id
    db_session.expire_all()
    fresh = await db_session.get(WolRun, run_id)
    assert fresh is not None
    assert fresh.verify_state == task.VERIFY_DONE
    assert fresh.verified_count == 0
    assert fresh.unverified_count == 1
    # retries=0 ⇒ probe once, never re-wake / re-enqueue.
    send.assert_not_awaited()
    reenqueue.assert_not_called()


async def test_verify_double_fire_is_noop_via_pending_claim(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    schedule = _make_schedule(owner.id)
    db_session.add(schedule)
    await db_session.flush()
    # Run already 'verifying' (a pass is mid-flight) — a redelivery must no-op.
    run, _targets = await _run_with_sent_targets(
        db_session,
        schedule,
        [("10.20.0.11", "aa:bb:cc:dd:ee:01", None)],
        verify_state="verifying",
    )

    probe = MagicMock()
    with patch("app.services.wol_scheduler.verify.probe_liveness", new=probe):
        result = await task._verify_run(str(run.id), 1)

    assert result["skipped"] == "not_pending"
    probe.assert_not_called()
    run_id = run.id
    db_session.expire_all()
    fresh = await db_session.get(WolRun, run_id)
    assert fresh is not None
    assert fresh.verify_state == "verifying"  # untouched


async def test_verify_disabled_midflight_finalises_immediately(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    # Operator turned verify OFF after the run armed it (still pending).
    schedule = _make_schedule(owner.id, verify_enabled=False)
    db_session.add(schedule)
    await db_session.flush()
    run, _targets = await _run_with_sent_targets(
        db_session, schedule, [("10.20.0.11", "aa:bb:cc:dd:ee:01", None)]
    )

    probe = MagicMock()
    reenqueue = MagicMock()
    with (
        patch("app.services.wol_scheduler.verify.probe_liveness", new=probe),
        patch.object(task.verify_wol_run, "apply_async", reenqueue),
    ):
        result = await task._verify_run(str(run.id), 1)

    assert result["verify_state"] == task.VERIFY_DONE
    probe.assert_not_called()  # no probe when verify disabled mid-flight
    reenqueue.assert_not_called()
    run_id = run.id
    db_session.expire_all()
    fresh = await db_session.get(WolRun, run_id)
    assert fresh is not None
    assert fresh.verify_state == task.VERIFY_DONE


async def test_verify_second_pass_exhausted_finalises_unverified(db_session: AsyncSession) -> None:
    # attempt=2 with retries=1: 2 <= 1 is False ⇒ finalise (no third wave).
    owner = await _owner(db_session)
    schedule = _make_schedule(owner.id, verify_retries=1)
    db_session.add(schedule)
    await db_session.flush()
    run, targets = await _run_with_sent_targets(
        db_session, schedule, [("10.20.0.12", "aa:bb:cc:dd:ee:02", None)]
    )
    # Simulate the first re-wake already happened: it bumped the host's
    # wake_attempts AND advanced the run-level verify_attempt anchor (so the
    # attempt-guarded claim admits this attempt-2 pass — see FINDING 3).
    targets[0].wake_attempts = 2
    run.verify_attempt = 2
    await db_session.commit()

    send = _ok_send()
    reenqueue = MagicMock()
    with (
        patch("app.services.wol_scheduler.verify.probe_liveness", new=_probe(set())),
        patch("app.services.wol.wake_from_server", new=send),
        patch.object(task.verify_wol_run, "apply_async", reenqueue),
    ):
        result = await task._verify_run(str(run.id), 2)

    assert result["verify_state"] == task.VERIFY_DONE
    run_id = run.id
    db_session.expire_all()
    fresh = await db_session.get(WolRun, run_id)
    assert fresh is not None
    assert fresh.unverified_count == 1
    send.assert_not_awaited()
    reenqueue.assert_not_called()


async def test_run_wol_schedule_enqueues_verify_when_enabled(db_session: AsyncSession) -> None:
    # The dispatch runner arms verify (verify_state='pending') + enqueues the
    # attempt-1 verify task with a countdown when verify_enabled and ≥1 sent.
    owner = await _owner(db_session)
    subnet = await _subnet(db_session)
    ip = IPAddress(
        subnet_id=subnet.id,
        address="10.20.0.11",
        mac_address="aa:bb:cc:dd:ee:01",
        tags={"wake": "nightly"},
    )
    db_session.add(ip)
    await db_session.flush()
    schedule = _make_schedule(
        owner.id,
        target_selector={"mode": "address_tags", "tags": ["wake:nightly"]},
        verify_enabled=True,
        verify_wait_seconds=90,
    )
    db_session.add(schedule)
    await db_session.flush()

    reenqueue = MagicMock()
    with (
        patch("app.services.wol.wake_from_server", new=_ok_send()),
        patch.object(task.verify_wol_run, "apply_async", reenqueue),
    ):
        summary = await task.run_wol_schedule(
            db_session,
            schedule,
            trigger="manual",
            actor_id=owner.id,
            actor_display="admin",
            apply_gate=False,
            resolve_user=owner,
        )

    assert summary["sent"] == 1
    reenqueue.assert_called_once()
    _args, kwargs = reenqueue.call_args
    assert kwargs["args"] == [summary["run_id"], 1]
    assert kwargs["countdown"] == 90
    db_session.expire_all()
    fresh = await db_session.get(WolRun, uuid.UUID(summary["run_id"]))
    assert fresh is not None
    assert fresh.verify_state == task.VERIFY_PENDING


async def test_run_wol_schedule_no_verify_when_disabled(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    subnet = await _subnet(db_session)
    ip = IPAddress(
        subnet_id=subnet.id,
        address="10.20.0.11",
        mac_address="aa:bb:cc:dd:ee:01",
        tags={"wake": "nightly"},
    )
    db_session.add(ip)
    await db_session.flush()
    schedule = _make_schedule(
        owner.id,
        target_selector={"mode": "address_tags", "tags": ["wake:nightly"]},
        verify_enabled=False,
    )
    db_session.add(schedule)
    await db_session.flush()

    reenqueue = MagicMock()
    with (
        patch("app.services.wol.wake_from_server", new=_ok_send()),
        patch.object(task.verify_wol_run, "apply_async", reenqueue),
    ):
        summary = await task.run_wol_schedule(
            db_session,
            schedule,
            trigger="manual",
            actor_id=owner.id,
            actor_display="admin",
            apply_gate=False,
            resolve_user=owner,
        )

    assert summary["sent"] == 1
    reenqueue.assert_not_called()
    db_session.expire_all()
    fresh = await db_session.get(WolRun, uuid.UUID(summary["run_id"]))
    assert fresh is not None
    assert fresh.verify_state == task.VERIFY_NONE


# ══════════════════════════════════════════════════════════════════════
# 4. Crash-recovery — verify reaper reclaims wedged runs (FINDING 1)
# ══════════════════════════════════════════════════════════════════════
#
# The verify state machine has no ``status==in_progress`` row for the Phase-1
# lease reaper to catch, so ``_sweep`` folds in a verify reaper that reclaims a
# run whose ``verify_claimed_at`` lease has expired — with TWO leases (#596): a
# wedged ``verifying`` (worker SIGKILL mid-probe) on the short
# ``VERIFY_CLAIM_LEASE_SECONDS``, and the narrower ``pending`` hole
# (``apply_async`` raised at the arm / re-wake enqueue) on the LONGER
# ``VERIFY_PENDING_LEASE_SECONDS`` — because a ``pending`` run may legitimately be
# counting down up to the 3600 s max wait, and reaping it on the short lease would
# re-fire the probe early. Neither may stay wedged forever; neither may be cut
# short.


async def _seed_stale_verify(
    db: AsyncSession,
    *,
    verify_state: str,
    attempt: int = 1,
    age_seconds: int | None = None,
) -> WolRun:
    """A committed run stuck mid-verify with an EXPIRED ``verify_claimed_at``
    lease. The parent schedule has ``next_run_at=None`` so the sweep's fire loop
    never selects it — only the verify reaper touches this row."""
    owner = await _owner(db)
    schedule = _make_schedule(owner.id, verify_retries=1)
    db.add(schedule)
    await db.flush()
    run, _targets = await _run_with_sent_targets(
        db,
        schedule,
        [("10.20.0.12", "aa:bb:cc:dd:ee:02", None)],
        verify_state=verify_state,
    )
    stale = age_seconds if age_seconds is not None else task.VERIFY_CLAIM_LEASE_SECONDS + 60
    run.verify_attempt = attempt
    run.verify_claimed_at = datetime.now(UTC) - timedelta(seconds=stale)
    await db.commit()
    return run


async def test_sweep_reclaims_stale_verifying_run(db_session: AsyncSession) -> None:
    # A worker SIGKILL mid-probe leaves the run wedged at 'verifying' with an
    # expired lease. The next sweep must reset it to 'pending' + re-enqueue at
    # its attempt anchor — it must NOT stay wedged.
    run = await _seed_stale_verify(db_session, verify_state=task.VERIFY_VERIFYING, attempt=1)
    run_id = run.id
    stale_claimed_at = run.verify_claimed_at

    reenqueue = MagicMock()
    with patch.object(task.verify_wol_run, "apply_async", reenqueue):
        result = await task._sweep()

    assert result["verify_reclaimed"] == 1
    # Re-enqueued at the row's current attempt anchor with no countdown.
    reenqueue.assert_called_once()
    _args, kwargs = reenqueue.call_args
    assert kwargs["args"] == [str(run_id), 1]
    assert kwargs["countdown"] == 0

    await db_session.rollback()
    db_session.expire_all()
    fresh = await db_session.get(WolRun, run_id)
    assert fresh is not None
    # No longer wedged: back to 'pending' with a freshly-stamped lease so a
    # second tick inside the window doesn't double-fire.
    assert fresh.verify_state == task.VERIFY_PENDING
    assert fresh.verify_claimed_at is not None
    assert fresh.verify_claimed_at > stale_claimed_at


async def test_sweep_reclaims_stale_pending_orphan(db_session: AsyncSession) -> None:
    # The narrower hole: a committed 'pending' row whose enqueue raised (no task
    # scheduled). A 'pending' run is reclaimed on the LONGER lease (a legit
    # countdown may run up to the 3600 s max wait), so it must be aged past
    # VERIFY_PENDING_LEASE_SECONDS, not merely past the 'verifying' lease.
    run = await _seed_stale_verify(
        db_session,
        verify_state=task.VERIFY_PENDING,
        attempt=1,
        age_seconds=task.VERIFY_PENDING_LEASE_SECONDS + 60,
    )
    run_id = run.id
    stale_claimed_at = run.verify_claimed_at

    reenqueue = MagicMock()
    with patch.object(task.verify_wol_run, "apply_async", reenqueue):
        result = await task._sweep()

    assert result["verify_reclaimed"] == 1
    reenqueue.assert_called_once()
    _args, kwargs = reenqueue.call_args
    assert kwargs["args"] == [str(run_id), 1]

    await db_session.rollback()
    db_session.expire_all()
    fresh = await db_session.get(WolRun, run_id)
    assert fresh is not None
    assert fresh.verify_state == task.VERIFY_PENDING
    assert fresh.verify_claimed_at is not None
    assert fresh.verify_claimed_at > stale_claimed_at


async def test_sweep_does_not_reclaim_fresh_verifying_run(db_session: AsyncSession) -> None:
    # A legitimately in-flight pass (lease NOT yet expired) must NOT be reaped
    # out from under itself.
    run = await _seed_stale_verify(
        db_session, verify_state=task.VERIFY_VERIFYING, attempt=1, age_seconds=5
    )
    run_id = run.id

    reenqueue = MagicMock()
    with patch.object(task.verify_wol_run, "apply_async", reenqueue):
        result = await task._sweep()

    assert result["verify_reclaimed"] == 0
    reenqueue.assert_not_called()
    await db_session.rollback()
    db_session.expire_all()
    fresh = await db_session.get(WolRun, run_id)
    assert fresh is not None
    assert fresh.verify_state == task.VERIFY_VERIFYING  # untouched


async def test_sweep_does_not_reclaim_pending_within_countdown(
    db_session: AsyncSession,
) -> None:
    # #596 regression guard: a 'pending' run configured with a long
    # verify_wait_seconds is still counting down its enqueued task. Aged past the
    # short 'verifying' lease but within the longer 'pending' lease, it must NOT
    # be reclaimed — else the probe re-fires ~30 min early and a slow-booting host
    # reads as a false 'down' (and pages via wol_wake_failed).
    run = await _seed_stale_verify(
        db_session,
        verify_state=task.VERIFY_PENDING,
        attempt=1,
        age_seconds=task.VERIFY_CLAIM_LEASE_SECONDS + 60,  # past 'verifying', within 'pending'
    )
    run_id = run.id

    reenqueue = MagicMock()
    with patch.object(task.verify_wol_run, "apply_async", reenqueue):
        result = await task._sweep()

    assert result["verify_reclaimed"] == 0
    reenqueue.assert_not_called()
    await db_session.rollback()
    db_session.expire_all()
    fresh = await db_session.get(WolRun, run_id)
    assert fresh is not None
    assert fresh.verify_state == task.VERIFY_PENDING  # untouched, still counting down


# ══════════════════════════════════════════════════════════════════════
# 5. Try/except self-heal — a probe exception resets 'verifying' → 'pending'
# ══════════════════════════════════════════════════════════════════════


async def test_verify_probe_exception_resets_to_pending(db_session: AsyncSession) -> None:
    # A plain exception inside the post-claim body (here: the probe fan-out
    # raises) must roll the mutex back to 'pending' — NOT leave it wedged at
    # 'verifying' — so the reaper can re-enqueue the SAME attempt.
    owner = await _owner(db_session)
    schedule = _make_schedule(owner.id, verify_retries=1)
    db_session.add(schedule)
    await db_session.flush()
    run, _targets = await _run_with_sent_targets(
        db_session, schedule, [("10.20.0.12", "aa:bb:cc:dd:ee:02", None)]
    )
    run_id = run.id

    reenqueue = MagicMock()
    with (
        patch(
            "app.tasks.wol_scheduler.verify_run_targets",
            new=AsyncMock(side_effect=RuntimeError("probe pool exploded")),
        ),
        patch.object(task.verify_wol_run, "apply_async", reenqueue),
    ):
        result = await task._verify_run(str(run_id), 1)

    assert "error" in result
    # Never re-enqueues on the failure path — the reaper is the recovery.
    reenqueue.assert_not_called()
    db_session.expire_all()
    fresh = await db_session.get(WolRun, run_id)
    assert fresh is not None
    # Self-healed back to 'pending' (attempt anchor preserved), NOT wedged.
    assert fresh.verify_state == task.VERIFY_PENDING
    assert fresh.verify_attempt == 1


# ══════════════════════════════════════════════════════════════════════
# 6. Attempt-aware claim — a redelivered stale attempt N is a NO-OP after a
#    re-wake advanced the row to N+1; re-wake waves stay bounded by
#    verify_retries even under the duplicate delivery (FINDING 3)
# ══════════════════════════════════════════════════════════════════════


async def test_stale_attempt_redelivery_is_noop_and_waves_bounded(
    db_session: AsyncSession,
) -> None:
    owner = await _owner(db_session)
    # retries=1 ⇒ at most ONE re-wake wave, ever, even with a duplicate.
    schedule = _make_schedule(owner.id, verify_retries=1)
    db_session.add(schedule)
    await db_session.flush()
    run, _targets = await _run_with_sent_targets(
        db_session, schedule, [("10.20.0.12", "aa:bb:cc:dd:ee:02", None)]
    )
    run_id = run.id

    send = _ok_send()
    reenqueue = MagicMock()
    with (
        # Host stays DOWN across every pass.
        patch("app.services.wol_scheduler.verify.probe_liveness", new=_probe(set())),
        patch("app.services.wol.wake_from_server", new=send),
        patch.object(task.verify_wol_run, "apply_async", reenqueue),
    ):
        # ── Pass attempt=1: down ⇒ re-wake wave #1, reset to pending @ attempt 2.
        first = await task._verify_run(str(run_id), 1)
        assert first["reenqueued"] is True
        assert first["rewoke"] == 1
        assert send.await_count == 1  # exactly one wave so far

        db_session.expire_all()
        mid = await db_session.get(WolRun, run_id)
        assert mid is not None
        assert mid.verify_state == task.VERIFY_PENDING
        assert mid.verify_attempt == 2  # anchor advanced

        # ── Redelivered STALE attempt=1 (acks_late double-delivery arriving
        #     after the reset): the attempt-guarded claim (verify_attempt==1 but
        #     row is at 2) no-ops. No second wave, no branched attempt chain.
        dup = await task._verify_run(str(run_id), 1)
        assert dup["skipped"] == "not_pending"
        assert send.await_count == 1  # STILL one wave — the duplicate re-woke nothing

        # ── Legit attempt=2: 2 <= retries(1) is False ⇒ finalise, no re-wake.
        final = await task._verify_run(str(run_id), 2)
        assert final["verify_state"] == task.VERIFY_DONE
        assert send.await_count == 1  # bounded: total waves == verify_retries == 1

    # Exactly one re-enqueue was ever issued (the attempt-2 chain from pass 1);
    # the stale duplicate did not branch a second chain.
    reenqueue.assert_called_once()
    _args, kwargs = reenqueue.call_args
    assert kwargs["args"] == [str(run_id), 2]

    db_session.expire_all()
    fresh = await db_session.get(WolRun, run_id)
    assert fresh is not None
    assert fresh.verify_state == task.VERIFY_DONE
    assert fresh.unverified_count == 1


# ══════════════════════════════════════════════════════════════════════
# 4. Multi-source liveness — probes (issue #596)
# ══════════════════════════════════════════════════════════════════════


async def test_probe_tcp_up_on_connect() -> None:
    with patch("app.services.wol_scheduler.verify._tcp_alive", new=AsyncMock(return_value=True)):
        up, method = await probe_liveness("10.20.0.11", method="tcp")
    assert up is True
    assert method == "tcp"


async def test_probe_tcp_down_when_filtered() -> None:
    with patch("app.services.wol_scheduler.verify._tcp_alive", new=AsyncMock(return_value=False)):
        up, method = await probe_liveness("10.20.0.11", method="tcp")
    assert up is False
    assert method == "tcp"


async def test_probe_tcp_never_raises() -> None:
    """A probe blowing up is a DOWN verdict, never an aborted pass."""
    with patch(
        "app.services.wol_scheduler.verify._tcp_alive",
        new=AsyncMock(side_effect=OSError("no route")),
    ):
        up, method = await probe_liveness("10.20.0.11", method="tcp")
    assert up is False
    assert method == "tcp"


async def test_probe_passive_method_falls_back_to_ping() -> None:
    """``probe_liveness`` only runs ACTIVE probes; ``seen`` is not its job."""
    with patch(
        "app.services.wol_scheduler.verify.run_ping",
        new=AsyncMock(return_value=_ping_result(0)),
    ):
        up, method = await probe_liveness("10.20.0.11", method="seen")
    assert up is True
    assert method == "ping"


async def test_seen_since_is_wake_anchored(db_session: AsyncSession) -> None:
    """seen_since returns only IPs observed AT/AFTER the anchor — a pre-wake
    sighting (stale cache) never counts, and 'never seen' is absent."""
    subnet = await _subnet(db_session)
    wake = datetime.now(UTC)
    ip = IPAddress(
        subnet_id=subnet.id,
        address="10.20.0.11",
        mac_address="aa:bb:cc:dd:ee:01",
        last_seen_at=wake + timedelta(seconds=5),
    )
    db_session.add(ip)
    await db_session.flush()
    assert await seen_since(db_session, [ip.id], wake) == {ip.id}

    # The stale-cache false-up: a sighting from BEFORE the magic packet went out
    # says nothing about whether this wake worked.
    ip.last_seen_at = wake - timedelta(minutes=10)
    await db_session.flush()
    assert await seen_since(db_session, [ip.id], wake) == set()

    # Never observed at all.
    ip.last_seen_at = None
    await db_session.flush()
    assert await seen_since(db_session, [ip.id], wake) == set()

    # Empty input is a no-op (never a query for zero ids).
    assert await seen_since(db_session, [], wake) == set()


# ══════════════════════════════════════════════════════════════════════
# 5. Multi-source liveness — fusion in verify_run_targets (issue #596)
# ══════════════════════════════════════════════════════════════════════


def _probe_by_method(up_for: dict[str, set[str]], calls: list[tuple[str, str]]) -> Any:
    """A patched ``probe_liveness`` keyed on (method, address), recording calls.

    ``up_for`` maps an active method → the set of addresses it reports UP.
    """

    async def fake(address: str | None, vantage: Any = None, *, method: str = "ping"):
        calls.append((str(address), method))
        return (address in up_for.get(method, set())), method

    return fake


async def _seeded_target(
    db: AsyncSession,
    *,
    seen_offset: timedelta | None,
    link_ip: bool = True,
) -> tuple[WolRun, WolRunTarget, IPAddress | None]:
    """One SENT target, optionally linked to an IP seen ``seen_offset`` from the wake."""
    owner = await _owner(db)
    subnet = await _subnet(db)
    ip: IPAddress | None = None
    if link_ip:
        ip = IPAddress(subnet_id=subnet.id, address="10.20.0.11", mac_address="aa:bb:cc:dd:ee:01")
        db.add(ip)
        await db.flush()
    schedule = _make_schedule(owner.id)
    db.add(schedule)
    await db.flush()
    run, targets = await _run_with_sent_targets(
        db, schedule, [("10.20.0.11", "aa:bb:cc:dd:ee:01", ip.id if ip else None)]
    )
    await db.refresh(run)
    if ip is not None and seen_offset is not None:
        ip.last_seen_at = run.started_at + seen_offset
        await db.flush()
    return run, targets[0], ip


async def test_seen_confirms_post_wake_sighting_without_restamping(
    db_session: AsyncSession,
) -> None:
    """A passive confirm must not claim we probed the host ourselves."""
    run, target, ip = await _seeded_target(db_session, seen_offset=timedelta(seconds=5))
    assert ip is not None
    original_seen_at = ip.last_seen_at

    down = await verify_run_targets(db_session, run, 1, method="seen")

    assert down == []
    assert target.verified is True
    assert target.verify_method == "seen"
    # Seen infra untouched: the sighting belongs to whichever subsystem saw it.
    assert ip.last_seen_at == original_seen_at
    assert ip.last_seen_method is None


async def test_seen_pre_wake_sighting_is_down_not_up(db_session: AsyncSession) -> None:
    """The stale-cache false-up guard, end to end through the fusion path."""
    run, target, _ip = await _seeded_target(db_session, seen_offset=timedelta(minutes=-10))

    down = await verify_run_targets(db_session, run, 1, method="seen")

    assert down == [target]
    assert target.verified is False
    assert target.verify_method == "seen"


async def test_seen_null_ip_target_stays_unchecked(db_session: AsyncSession) -> None:
    """Abstain, don't condemn: no IPAM row means no verdict, not a down verdict."""
    run, target, _ip = await _seeded_target(db_session, seen_offset=None, link_ip=False)

    down = await verify_run_targets(db_session, run, 1, method="seen")

    assert down == []  # never a re-wake candidate
    assert target.verified is None  # honestly "not checked"
    assert target.verify_method is None


async def test_auto_short_circuits_on_first_up(db_session: AsyncSession) -> None:
    """ping down → tcp up: the chain stops at tcp and never consults seen."""
    run, target, ip = await _seeded_target(db_session, seen_offset=timedelta(minutes=-10))
    assert ip is not None
    calls: list[tuple[str, str]] = []

    with patch(
        "app.services.wol_scheduler.verify.probe_liveness",
        new=_probe_by_method({"tcp": {"10.20.0.11"}}, calls),
    ):
        down = await verify_run_targets(db_session, run, 1, method="auto")

    assert down == []
    assert target.verified is True
    assert target.verify_method == "tcp"
    assert calls == [("10.20.0.11", "ping"), ("10.20.0.11", "tcp")]
    # An ACTIVE up DOES stamp Seen, with the winning method. Seen is written via a
    # Core UPDATE (only-advance guard), so reload the row rather than reading the
    # stale identity-map instance.
    await db_session.refresh(ip)
    assert ip.last_seen_at is not None
    assert ip.last_seen_at > run.started_at
    assert ip.last_seen_method == "tcp"


async def test_auto_live_host_costs_one_ping(db_session: AsyncSession) -> None:
    run, target, _ip = await _seeded_target(db_session, seen_offset=None)
    calls: list[tuple[str, str]] = []

    with patch(
        "app.services.wol_scheduler.verify.probe_liveness",
        new=_probe_by_method({"ping": {"10.20.0.11"}}, calls),
    ):
        down = await verify_run_targets(db_session, run, 1, method="auto")

    assert down == []
    assert target.verify_method == "ping"
    assert calls == [("10.20.0.11", "ping")]  # tcp never ran


async def test_auto_falls_through_to_seen_when_active_probes_fail(
    db_session: AsyncSession,
) -> None:
    """The ICMP+TCP-silent-but-alive host, rescued by a post-wake sighting."""
    run, target, ip = await _seeded_target(db_session, seen_offset=timedelta(seconds=30))
    assert ip is not None
    original_seen_at = ip.last_seen_at
    calls: list[tuple[str, str]] = []

    with patch(
        "app.services.wol_scheduler.verify.probe_liveness",
        new=_probe_by_method({}, calls),  # nothing answers actively
    ):
        down = await verify_run_targets(db_session, run, 1, method="auto")

    assert down == []
    assert target.verified is True
    assert target.verify_method == "seen"
    assert calls == [("10.20.0.11", "ping"), ("10.20.0.11", "tcp")]
    assert ip.last_seen_at == original_seen_at  # passive never re-stamps


async def test_auto_all_sources_down_is_a_rewake_candidate(db_session: AsyncSession) -> None:
    run, target, _ip = await _seeded_target(db_session, seen_offset=timedelta(minutes=-10))
    calls: list[tuple[str, str]] = []

    with patch(
        "app.services.wol_scheduler.verify.probe_liveness",
        new=_probe_by_method({}, calls),
    ):
        down = await verify_run_targets(db_session, run, 1, method="auto")

    assert down == [target]
    assert target.verified is False
    assert target.verify_method == "seen"  # the last source consulted


async def test_richer_method_can_only_shrink_the_down_set(db_session: AsyncSession) -> None:
    """A passive confirm removes a re-wake that ping-only would have fired."""
    run, target, _ip = await _seeded_target(db_session, seen_offset=timedelta(seconds=30))
    calls: list[tuple[str, str]] = []

    with patch(
        "app.services.wol_scheduler.verify.probe_liveness",
        new=_probe_by_method({}, calls),
    ):
        ping_down = await verify_run_targets(db_session, run, 1, method="ping")
    assert ping_down == [target]  # ping-only: re-wake it

    # Re-probe the same row (verified is False → still a candidate) under auto.
    with patch(
        "app.services.wol_scheduler.verify.probe_liveness",
        new=_probe_by_method({}, calls),
    ):
        auto_down = await verify_run_targets(db_session, run, 1, method="auto")
    assert auto_down == []  # seen rescues it — no needless re-wake
    assert target.verified is True
