"""Unit + sweep tests for Scheduled Wake-on-LAN — Phase 1 (issue #586).

Covers the pure service layer and the beat runner:

* ``compute_next_run`` timezone / DST correctness (a spring-forward and a
  fall-back date in a non-UTC zone) + ``is_due``.
* the built-in holiday / term gate — blackout hit → ``holiday``, outside the
  term range → ``off_term``, inside a non-blackout term → allowed.
* the target resolver — an ``address_tags`` match returns hosts *with* a MAC,
  a matched IP with no MAC is *reported as skipped* (never silently dropped),
  and the MAC fallback chain resolves via ``ip_mac_history`` and via an active
  ``DHCPLease``.
* sweep idempotency — a due schedule fires exactly once, a second immediate
  tick does NOT double-fire (``next_run_at`` advanced + the ``in_progress``
  mutex), and a gated-off scheduled occurrence still writes a ``wol_run`` with
  the ``skip_reason`` and re-stamps ``next_run_at``.

The actual UDP send is always patched (``app.services.wol.wake_from_server``)
so no magic packet ever leaves the test process.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.auth import User
from app.models.dhcp import DHCPLease, DHCPServer
from app.models.ipam import IPAddress, IPBlock, IpMacHistory, IPSpace, Subnet
from app.models.wol_schedule import WolRun, WolSchedule
from app.services.wol_scheduler import (
    SKIP_HOLIDAY,
    SKIP_OFF_TERM,
    compute_next_run,
    dispatch_wol_targets,
    evaluate_gate,
    gate_verdict,
    is_due,
    resolve_wol_targets,
)
from app.services.wol_scheduler.resolver import (
    MAC_SOURCE_HISTORY,
    MAC_SOURCE_IP,
    MAC_SOURCE_LEASE,
    MAX_WAKE_TARGETS,
    SKIP_NO_MAC,
    SKIP_OVER_CAP,
    WakeTarget,
)

_NY = "America/New_York"


# ── Fixtures / builders ───────────────────────────────────────────────


async def _superadmin(db: AsyncSession) -> User:
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


async def _subnet(db: AsyncSession, *, network: str = "10.20.0.0/24") -> Subnet:
    space = IPSpace(name=f"space-{uuid.uuid4().hex[:6]}")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network=network, name="blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id, block_id=block.id, network=network, name="net", kind="unicast"
    )
    db.add(subnet)
    await db.flush()
    return subnet


async def _addr(
    db: AsyncSession,
    subnet: Subnet,
    address: str,
    *,
    mac: str | None = None,
    tags: dict[str, str] | None = None,
    hostname: str | None = None,
) -> IPAddress:
    ip = IPAddress(
        subnet_id=subnet.id,
        address=address,
        mac_address=mac,
        tags=tags or {},
        hostname=hostname,
    )
    db.add(ip)
    await db.flush()
    return ip


def _make_schedule(**kw: Any) -> WolSchedule:
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
    }
    base.update(kw)
    return WolSchedule(**base)


def _fake_ok_send() -> AsyncMock:
    """A patched ``wake_from_server`` that never emits a packet.

    An ``AsyncMock`` (awaitable + call assertions) whose return value is the
    minimal ``WolResult`` shape the dispatch loop reads (``.sent`` /
    ``.ran_from``)."""
    return AsyncMock(return_value=SimpleNamespace(sent=True, ran_from="server"))


# ══════════════════════════════════════════════════════════════════════
# 1. compute_next_run — timezone + DST correctness
# ══════════════════════════════════════════════════════════════════════


def test_compute_next_run_is_utc_aware() -> None:
    nxt = compute_next_run("0 7 * * *", "UTC", after=datetime(2026, 6, 1, 0, 0, tzinfo=UTC))
    assert nxt.tzinfo is not None
    assert nxt == datetime(2026, 6, 1, 7, 0, tzinfo=UTC)


def test_compute_next_run_naive_after_assumed_utc() -> None:
    # A naive ``after`` must be treated as UTC, not local server time.
    nxt = compute_next_run("30 3 * * *", "UTC", after=datetime(2026, 6, 1, 0, 0))
    assert nxt == datetime(2026, 6, 1, 3, 30, tzinfo=UTC)


def test_compute_next_run_walks_local_wallclock() -> None:
    # 07:00 in a UTC-5 winter zone == 12:00 UTC, NOT 07:00 UTC.
    nxt = compute_next_run("0 7 * * *", _NY, after=datetime(2026, 1, 15, 0, 0, tzinfo=UTC))
    assert nxt.astimezone(ZoneInfo(_NY)).strftime("%H:%M") == "07:00"
    assert nxt.hour == 12  # EST offset -5


def test_compute_next_run_dst_spring_forward_holds_local_time() -> None:
    """A 07:00-local daily job keeps firing at 07:00 wall-clock across the
    US spring-forward (Sun 2026-03-08 02:00 → 03:00), so its UTC hour SHIFTS
    from 12 (EST, -5) to 11 (EDT, -4)."""
    # Fire on the Saturday before the transition (still EST).
    before = compute_next_run("0 7 * * *", _NY, after=datetime(2026, 3, 7, 0, 0, tzinfo=UTC))
    # Fire on the Sunday of the transition (now EDT).
    after = compute_next_run("0 7 * * *", _NY, after=before)

    assert before.astimezone(ZoneInfo(_NY)).strftime("%Y-%m-%d %H:%M") == "2026-03-07 07:00"
    assert after.astimezone(ZoneInfo(_NY)).strftime("%Y-%m-%d %H:%M") == "2026-03-08 07:00"
    # Same local wall-clock, DIFFERENT UTC hour — the DST-correctness proof.
    assert before.hour == 12  # EST
    assert after.hour == 11  # EDT
    assert after - before == timedelta(hours=23)  # the "lost" hour


def test_compute_next_run_dst_spring_forward_skips_nonexistent_time() -> None:
    # 02:30 doesn't exist on 2026-03-08 in America/New_York (clocks jump
    # 02:00 → 03:00); croniter rolls the fire onto the real 03:00 local.
    nxt = compute_next_run("30 2 * * *", _NY, after=datetime(2026, 3, 8, 0, 0, tzinfo=UTC))
    local = nxt.astimezone(ZoneInfo(_NY))
    assert local.date() == date(2026, 3, 8)
    assert local.strftime("%H:%M") == "03:00"


def test_compute_next_run_dst_fall_back_holds_local_time() -> None:
    """Across the US fall-back (Sun 2026-11-01 02:00 → 01:00) a 07:00-local
    job stays at 07:00 wall-clock; the UTC hour shifts 11 (EDT) → 12 (EST)."""
    before = compute_next_run("0 7 * * *", _NY, after=datetime(2026, 10, 31, 0, 0, tzinfo=UTC))
    after = compute_next_run("0 7 * * *", _NY, after=before)

    assert before.astimezone(ZoneInfo(_NY)).strftime("%Y-%m-%d %H:%M") == "2026-10-31 07:00"
    assert after.astimezone(ZoneInfo(_NY)).strftime("%Y-%m-%d %H:%M") == "2026-11-01 07:00"
    assert before.hour == 11  # EDT
    assert after.hour == 12  # EST
    assert after - before == timedelta(hours=25)  # the "gained" hour


def test_is_due_semantics() -> None:
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    assert is_due(None, now=now) is False  # manual-only never due
    assert is_due(now - timedelta(seconds=1), now=now) is True  # past → fire
    assert is_due(now, now=now) is True  # equal → fire
    assert is_due(now + timedelta(minutes=1), now=now) is False  # future → wait
    # A naive next_run_at is treated as UTC.
    assert is_due(datetime(2026, 6, 1, 11, 0), now=now) is True


# ══════════════════════════════════════════════════════════════════════
# 2. Built-in holiday / term gate
# ══════════════════════════════════════════════════════════════════════


def test_gate_blackout_date_hit_is_holiday() -> None:
    sched = _make_schedule(timezone="UTC", blackout_dates=["2026-12-25"])
    at = datetime(2026, 12, 25, 7, 0, tzinfo=UTC)
    assert gate_verdict(at, sched) == SKIP_HOLIDAY
    allowed, reason = evaluate_gate(sched, at=at)
    assert allowed is False and reason == SKIP_HOLIDAY


def test_gate_before_active_from_is_off_term() -> None:
    sched = _make_schedule(
        timezone="UTC", active_from=date(2026, 9, 1), active_until=date(2026, 12, 20)
    )
    at = datetime(2026, 8, 15, 7, 0, tzinfo=UTC)  # before term
    assert gate_verdict(at, sched) == SKIP_OFF_TERM


def test_gate_after_active_until_is_off_term() -> None:
    sched = _make_schedule(
        timezone="UTC", active_from=date(2026, 9, 1), active_until=date(2026, 12, 20)
    )
    at = datetime(2027, 1, 5, 7, 0, tzinfo=UTC)  # after term
    assert gate_verdict(at, sched) == SKIP_OFF_TERM


def test_gate_inside_term_nonblackout_is_allowed() -> None:
    sched = _make_schedule(
        timezone="UTC",
        active_from=date(2026, 9, 1),
        active_until=date(2026, 12, 20),
        blackout_dates=["2026-11-26"],  # a different day
    )
    at = datetime(2026, 10, 10, 7, 0, tzinfo=UTC)  # in term, not a blackout
    assert gate_verdict(at, sched) is None
    allowed, reason = evaluate_gate(sched, at=at)
    assert allowed is True and reason is None


def test_gate_evaluates_on_local_date_not_utc() -> None:
    # 07:00 local in a UTC-5 zone is 12:00 UTC the SAME day; a late-evening
    # local fire that crosses into the next UTC day must still gate on the
    # local calendar date. 23:30 on 2026-12-25 EST == 04:30 UTC on 2026-12-26.
    sched = _make_schedule(timezone=_NY, blackout_dates=["2026-12-25"])
    at = datetime(2026, 12, 26, 4, 30, tzinfo=UTC)  # local 2026-12-25 23:30
    assert gate_verdict(at, sched) == SKIP_HOLIDAY


# ══════════════════════════════════════════════════════════════════════
# 3. Target resolver
# ══════════════════════════════════════════════════════════════════════


async def test_resolver_address_tags_returns_hosts_with_mac(db_session: AsyncSession) -> None:
    owner = await _superadmin(db_session)
    subnet = await _subnet(db_session)
    match = await _addr(
        db_session, subnet, "10.20.0.5", mac="aa:bb:cc:dd:ee:05", tags={"wake": "nightly"}
    )
    # An untagged host must NOT be selected.
    await _addr(db_session, subnet, "10.20.0.6", mac="aa:bb:cc:dd:ee:06", tags={})

    resolved = await resolve_wol_targets(
        db_session, owner, {"mode": "address_tags", "tags": ["wake:nightly"]}
    )

    assert len(resolved.wakes) == 1
    w = resolved.wakes[0]
    assert w.ip_address_id == match.id
    assert w.mac == "aa:bb:cc:dd:ee:05"
    assert w.mac_source == MAC_SOURCE_IP
    assert w.broadcast == "10.20.0.255"
    assert resolved.skipped == []


async def test_resolver_matched_ip_without_mac_is_skipped_not_dropped(
    db_session: AsyncSession,
) -> None:
    owner = await _superadmin(db_session)
    subnet = await _subnet(db_session)
    withmac = await _addr(
        db_session, subnet, "10.20.0.5", mac="aa:bb:cc:dd:ee:05", tags={"wake": "nightly"}
    )
    nomac = await _addr(db_session, subnet, "10.20.0.9", mac=None, tags={"wake": "nightly"})

    resolved = await resolve_wol_targets(
        db_session, owner, {"mode": "address_tags", "tags": ["wake:nightly"]}
    )

    # Both matched inputs are accounted for — one wakes, one is reported.
    assert {w.ip_address_id for w in resolved.wakes} == {withmac.id}
    assert len(resolved.skipped) == 1
    skip = resolved.skipped[0]
    assert skip.ip_address_id == nomac.id
    assert skip.reason == SKIP_NO_MAC
    # Every input lands in exactly one bucket — nothing silently dropped.
    assert len(resolved.wakes) + len(resolved.skipped) == 2


async def test_resolver_mac_fallback_via_ip_mac_history(db_session: AsyncSession) -> None:
    owner = await _superadmin(db_session)
    subnet = await _subnet(db_session)
    ip = await _addr(db_session, subnet, "10.20.0.7", mac=None, tags={"wake": "nightly"})
    db_session.add(
        IpMacHistory(
            ip_address_id=ip.id,
            mac_address="aa:bb:cc:dd:ee:77",
            last_seen=datetime.now(UTC),
        )
    )
    await db_session.flush()

    resolved = await resolve_wol_targets(
        db_session, owner, {"mode": "address_tags", "tags": ["wake:nightly"]}
    )

    assert len(resolved.wakes) == 1
    w = resolved.wakes[0]
    assert w.mac == "aa:bb:cc:dd:ee:77"
    assert w.mac_source == MAC_SOURCE_HISTORY


async def test_resolver_mac_fallback_via_dhcp_lease(db_session: AsyncSession) -> None:
    owner = await _superadmin(db_session)
    subnet = await _subnet(db_session)
    ip = await _addr(db_session, subnet, "10.20.0.8", mac=None, tags={"wake": "nightly"})

    server = DHCPServer(name="kea", host="10.0.0.2")
    db_session.add(server)
    await db_session.flush()
    db_session.add(
        DHCPLease(
            server_id=server.id,
            scope_id=None,
            ip_address="10.20.0.8",
            mac_address="aa:bb:cc:dd:ee:88",
            state="active",
            last_seen_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    resolved = await resolve_wol_targets(
        db_session, owner, {"mode": "address_tags", "tags": ["wake:nightly"]}
    )

    assert len(resolved.wakes) == 1
    w = resolved.wakes[0]
    assert w.ip_address_id == ip.id
    assert w.mac == "aa:bb:cc:dd:ee:88"
    assert w.mac_source == MAC_SOURCE_LEASE


async def test_resolver_dedupes_same_mac_matched_twice(db_session: AsyncSession) -> None:
    owner = await _superadmin(db_session)
    subnet = await _subnet(db_session)
    # Two IP rows sharing one MAC on the same segment → one packet.
    await _addr(db_session, subnet, "10.20.0.5", mac="aa:bb:cc:dd:ee:05", tags={"wake": "nightly"})
    await _addr(db_session, subnet, "10.20.0.6", mac="aa:bb:cc:dd:ee:05", tags={"wake": "nightly"})

    resolved = await resolve_wol_targets(
        db_session, owner, {"mode": "address_tags", "tags": ["wake:nightly"]}
    )
    assert len(resolved.wakes) == 1


# ══════════════════════════════════════════════════════════════════════
# 4. Sweep idempotency + gated-off history
# ══════════════════════════════════════════════════════════════════════


async def _seed_due_schedule(
    db: AsyncSession, *, blackout_today: bool = False, **kw: Any
) -> WolSchedule:
    """Create an owner + a tagged wakeable host + an already-due schedule."""
    owner = await _superadmin(db)
    subnet = await _subnet(db)
    await _addr(db, subnet, "10.20.0.5", mac="aa:bb:cc:dd:ee:05", tags={"wake": "nightly"})

    now = datetime.now(UTC)
    schedule = _make_schedule(
        created_by_user_id=owner.id,
        next_run_at=now - timedelta(minutes=1),  # already due
        **kw,
    )
    if blackout_today:
        schedule.blackout_dates = [now.astimezone(UTC).date().isoformat()]
    db.add(schedule)
    await db.flush()
    return schedule


async def _run_count(db: AsyncSession, schedule_id: uuid.UUID) -> list[WolRun]:
    return list(
        (await db.execute(select(WolRun).where(WolRun.schedule_id == schedule_id))).scalars().all()
    )


async def test_sweep_fires_due_schedule_once(db_session: AsyncSession) -> None:
    import app.tasks.wol_scheduler as task

    schedule = await _seed_due_schedule(db_session)
    sid = schedule.id
    await db_session.commit()

    with patch("app.services.wol.wake_from_server", _fake_ok_send()):
        first = await task._sweep()

    assert first["fired"] == 1
    assert first["gated"] == 0

    # Fresh snapshot to read the sweep's commit — expire the identity map so
    # the session (expire_on_commit=False) doesn't hand back the stale seeded
    # instance instead of the row the sweep updated in its own session.
    await db_session.rollback()
    db_session.expire_all()
    runs = await _run_count(db_session, sid)
    assert len(runs) == 1
    assert runs[0].status == task.STATUS_OK
    assert runs[0].sent_count == 1
    assert runs[0].trigger == "schedule"

    # next_run_at advanced into the future.
    fresh = await db_session.get(WolSchedule, sid)
    assert fresh is not None
    assert fresh.next_run_at is not None
    assert fresh.next_run_at > datetime.now(UTC)


async def test_sweep_second_tick_does_not_double_fire(db_session: AsyncSession) -> None:
    import app.tasks.wol_scheduler as task

    schedule = await _seed_due_schedule(db_session)
    sid = schedule.id
    await db_session.commit()

    with patch("app.services.wol.wake_from_server", _fake_ok_send()):
        first = await task._sweep()
        # A second immediate tick — next_run_at is now in the future, so the
        # due-query returns nothing and no new run is created.
        second = await task._sweep()

    assert first["fired"] == 1
    assert second["fired"] == 0
    assert second["gated"] == 0

    await db_session.rollback()
    runs = await _run_count(db_session, sid)
    assert len(runs) == 1  # exactly one fire across two ticks — idempotent


async def test_sweep_in_progress_mutex_skips_row(db_session: AsyncSession) -> None:
    import app.tasks.wol_scheduler as task

    # A schedule already mid-flight (status in_progress with a FRESH,
    # unexpired lease) must be skipped by the sweep so a wake spanning multiple
    # ticks can't double-fire. (A NULL / stale in_progress_since is instead
    # treated as a crashed-worker orphan and reclaimed — see the stale-lease
    # test below — so the fresh lease is what proves the live-mutex skip.)
    schedule = await _seed_due_schedule(db_session)
    schedule.last_run_status = task.STATUS_IN_PROGRESS
    schedule.in_progress_since = datetime.now(UTC)
    sid = schedule.id
    await db_session.commit()

    with patch("app.services.wol.wake_from_server", _fake_ok_send()):
        result = await task._sweep()

    assert result["fired"] == 0
    assert result["skipped_in_progress"] == 1

    await db_session.rollback()
    assert await _run_count(db_session, sid) == []


async def test_sweep_gated_off_writes_skip_run_and_restamps(db_session: AsyncSession) -> None:
    import app.tasks.wol_scheduler as task

    # Blackout today → the occurrence is suppressed, but must still produce a
    # visible "skipped because holiday" run AND advance next_run_at.
    schedule = await _seed_due_schedule(db_session, blackout_today=True)
    sid = schedule.id
    await db_session.commit()

    with patch("app.services.wol.wake_from_server", _fake_ok_send()) as send:
        result = await task._sweep()

    assert result["gated"] == 1
    assert result["fired"] == 0
    send.assert_not_called()  # gated → no packet ever built

    await db_session.rollback()
    db_session.expire_all()
    runs = await _run_count(db_session, sid)
    assert len(runs) == 1
    assert runs[0].status == task.STATUS_SKIPPED
    assert runs[0].skip_reason == SKIP_HOLIDAY

    fresh = await db_session.get(WolSchedule, sid)
    assert fresh is not None
    assert fresh.last_run_status == task.STATUS_SKIPPED
    assert fresh.last_run_skip_reason == SKIP_HOLIDAY
    # Re-stamped forward even though it was gated — never a silent no-op.
    assert fresh.next_run_at is not None
    assert fresh.next_run_at > datetime.now(UTC)


async def test_sweep_disabled_module_fires_nothing(db_session: AsyncSession) -> None:
    import app.tasks.wol_scheduler as task
    from app.models.feature_module import FeatureModule
    from app.services import feature_modules

    schedule = await _seed_due_schedule(db_session)
    sid = schedule.id
    db_session.add(FeatureModule(id=task.MODULE_ID, enabled=False))
    await db_session.commit()
    feature_modules.invalidate_cache()

    try:
        with patch("app.services.wol.wake_from_server", _fake_ok_send()) as send:
            result = await task._sweep()
        assert result.get("module_disabled") == 1
        send.assert_not_called()
        await db_session.rollback()
        assert await _run_count(db_session, sid) == []
    finally:
        feature_modules.invalidate_cache()


# ══════════════════════════════════════════════════════════════════════
# 5. Dispatch containment — one bad host never aborts the batch
# ══════════════════════════════════════════════════════════════════════


def _wake(address: str, mac: str) -> WakeTarget:
    return WakeTarget(
        ip_address_id=None,
        address=address,
        mac=mac,
        subnet_id=None,
        broadcast="10.20.0.255",
        mac_source=MAC_SOURCE_IP,
    )


def _send_failing_for(bad_mac: str) -> Any:
    """A patched ``wake_from_server`` that raises a NON-``WolDispatchError`` for
    one MAC (mirroring a malformed supervisor reply tripping
    ``pydantic.ValidationError`` inside ``wake_via_appliance``) and succeeds for
    every other host."""

    async def _send(wire: Any) -> Any:
        if wire.mac == bad_mac:
            from app.services import wol

            # Missing required fields → pydantic.ValidationError (NOT a
            # WolDispatchError) — the exact class the old narrow handler let
            # escape and abort the whole run.
            wol.WolResult.model_validate({"sent": "yes"})
        return SimpleNamespace(sent=True, ran_from="server")

    return _send


async def test_dispatch_bad_host_contained_non_wol_error(db_session: AsyncSession) -> None:
    # host #2's send raises a ValidationError (non-WolDispatchError); hosts #1
    # and #3 must still dispatch and #2 is recorded failed — the module's
    # "one bad host never aborts the run" guarantee.
    bad_mac = "aa:bb:cc:dd:ee:02"
    targets = [
        _wake("10.20.0.1", "aa:bb:cc:dd:ee:01"),
        _wake("10.20.0.2", bad_mac),
        _wake("10.20.0.3", "aa:bb:cc:dd:ee:03"),
    ]

    with patch("app.services.wol.wake_from_server", new=_send_failing_for(bad_mac)):
        outcomes = await dispatch_wol_targets(
            db_session,
            targets,
            vantage={"kind": "server", "id": None},
            repeat_count=1,
            repeat_interval_ms=0,
            stagger_ms=0,
            port=9,
        )

    assert len(outcomes) == 3  # every input produced an outcome — none dropped
    by_mac = {o.target.mac: o for o in outcomes}
    assert by_mac["aa:bb:cc:dd:ee:01"].sent is True
    assert by_mac["aa:bb:cc:dd:ee:03"].sent is True
    bad = by_mac[bad_mac]
    assert bad.sent is False
    assert bad.error  # the failure is captured on the outcome, not raised


async def _seed_owner_and_tagged_hosts(
    db: AsyncSession, macs: list[str]
) -> tuple[User, Subnet, WolSchedule]:
    owner = await _superadmin(db)
    subnet = await _subnet(db)
    for i, mac in enumerate(macs):
        await _addr(db, subnet, f"10.20.0.{10 + i}", mac=mac, tags={"wake": "nightly"})
    schedule = _make_schedule(created_by_user_id=owner.id)
    db.add(schedule)
    await db.flush()
    return owner, subnet, schedule


async def test_run_wol_schedule_partial_on_one_bad_host(db_session: AsyncSession) -> None:
    import app.tasks.wol_scheduler as task

    # End-to-end containment: a 2-host run where one host's send trips a
    # ValidationError must land as PARTIAL (target_count=2, sent=1) — NOT the
    # pre-fix "failed, target_count=0, other host silently un-dispatched".
    bad_mac = "aa:bb:cc:dd:ee:06"
    owner, _subnet_row, schedule = await _seed_owner_and_tagged_hosts(
        db_session, ["aa:bb:cc:dd:ee:05", bad_mac]
    )

    with patch("app.services.wol.wake_from_server", new=_send_failing_for(bad_mac)):
        summary = await task.run_wol_schedule(
            db_session,
            schedule,
            trigger="manual",
            actor_id=owner.id,
            actor_display="admin",
            apply_gate=False,
            resolve_user=owner,
        )

    assert summary["status"] == task.STATUS_PARTIAL
    assert summary["target_count"] == 2
    assert summary["sent"] == 1
    assert summary["failed"] == 1


# ══════════════════════════════════════════════════════════════════════
# 6. Resolver — empty-tags match-nothing + hard fan-out cap
# ══════════════════════════════════════════════════════════════════════


async def test_resolver_empty_tags_tag_mode_matches_nothing(db_session: AsyncSession) -> None:
    # A stored ``{mode:'address_tags', tags:[]}`` (bypassing the schema
    # validator) must resolve to NOTHING — never every host in scope.
    owner = await _superadmin(db_session)
    subnet = await _subnet(db_session)
    await _addr(db_session, subnet, "10.20.0.5", mac="aa:bb:cc:dd:ee:05", tags={"wake": "nightly"})
    await _addr(db_session, subnet, "10.20.0.6", mac="aa:bb:cc:dd:ee:06", tags={"env": "lab"})

    resolved = await resolve_wol_targets(db_session, owner, {"mode": "address_tags", "tags": []})

    assert resolved.wakes == []
    assert resolved.skipped == []


async def test_resolver_over_cap_reports_overflow_as_skipped(db_session: AsyncSession) -> None:
    # A resolved set past the fan-out cap reports the overflow as ``over_cap``
    # skips — never silently dropped, never dispatched. Patch the cap small so
    # we don't have to seed 512 rows.
    owner = await _superadmin(db_session)
    subnet = await _subnet(db_session)
    for i in range(4):
        await _addr(
            db_session,
            subnet,
            f"10.20.0.{20 + i}",
            mac=f"aa:bb:cc:dd:ee:{20 + i:02x}",
            tags={"wake": "nightly"},
        )

    with patch("app.services.wol_scheduler.resolver.MAX_WAKE_TARGETS", 2):
        resolved = await resolve_wol_targets(
            db_session, owner, {"mode": "address_tags", "tags": ["wake:nightly"]}
        )

    assert len(resolved.wakes) == 2  # capped
    over_cap = [s for s in resolved.skipped if s.reason == SKIP_OVER_CAP]
    assert len(over_cap) == 2  # the overflow is reported, not dropped
    # Every one of the 4 matched hosts landed in exactly one bucket.
    assert len(resolved.wakes) + len(resolved.skipped) == 4
    # Sanity: the real ceiling is far above the patched test value.
    assert MAX_WAKE_TARGETS >= 512


# ══════════════════════════════════════════════════════════════════════
# 7. Gate evaluated at the fire instant, not wall-clock ``now``
# ══════════════════════════════════════════════════════════════════════


async def test_gate_evaluated_at_fire_instant_not_now(db_session: AsyncSession) -> None:
    import app.tasks.wol_scheduler as task

    # A schedule whose ``next_run_at`` lands on a blackout day must be
    # holiday-skipped even though the tick slipped to a LATER (non-blackout)
    # day. We put the candidate fire on a blackout date two days in the past;
    # real ``now`` (today) is NOT a blackout. If the runner (wrongly) gated at
    # ``now`` it would fire the two tagged hosts (status ok); gating at the true
    # ``next_run_at`` yields skip_reason=holiday.
    owner, _subnet_row, schedule = await _seed_owner_and_tagged_hosts(
        db_session, ["aa:bb:cc:dd:ee:05"]
    )
    fire_day = (datetime.now(UTC) - timedelta(days=2)).date()
    schedule.timezone = "UTC"  # local date == UTC date, no midnight ambiguity
    schedule.blackout_dates = [fire_day.isoformat()]
    schedule.next_run_at = datetime(fire_day.year, fire_day.month, fire_day.day, 12, 0, tzinfo=UTC)
    await db_session.flush()

    with patch("app.services.wol.wake_from_server", _fake_ok_send()) as send:
        summary = await task.run_wol_schedule(
            db_session,
            schedule,
            trigger="schedule",
            actor_id=None,
            actor_display=task.SYSTEM_ACTOR_DISPLAY,
            apply_gate=True,
            resolve_user=owner,
        )

    assert summary["status"] == task.STATUS_SKIPPED
    assert summary["skip_reason"] == SKIP_HOLIDAY  # gated at next_run_at, not now
    send.assert_not_called()  # gated → no packet ever built


# ══════════════════════════════════════════════════════════════════════
# 8. Atomic in_progress claim — no double-fire + stale-lease reclaim
# ══════════════════════════════════════════════════════════════════════


async def test_run_wol_schedule_rejects_fresh_in_progress_claim(db_session: AsyncSession) -> None:
    import app.tasks.wol_scheduler as task

    # A schedule another runner already holds (fresh, unexpired lease) must NOT
    # be claimable — the atomic UPDATE…RETURNING comes back empty and raises
    # ScheduleBusyError, so two back-to-back sweeps of the same due schedule
    # fire it exactly once.
    schedule = await _seed_due_schedule(db_session)
    schedule.last_run_status = task.STATUS_IN_PROGRESS
    schedule.in_progress_since = datetime.now(UTC)  # fresh lease
    sid = schedule.id
    await db_session.commit()

    with patch("app.services.wol.wake_from_server", _fake_ok_send()) as send:
        with pytest.raises(task.ScheduleBusyError):
            await task.run_wol_schedule(
                db_session,
                schedule,
                trigger="schedule",
                actor_id=None,
                actor_display=task.SYSTEM_ACTOR_DISPLAY,
                apply_gate=True,
            )

    send.assert_not_called()
    db_session.expire_all()
    assert await _run_count(db_session, sid) == []  # no fire while another holds the mutex


async def test_sweep_reclaims_stale_in_progress_lease(db_session: AsyncSession) -> None:
    import app.tasks.wol_scheduler as task

    # A crashed worker leaves the row in_progress with a past lease + an orphan
    # in_progress run. The next sweep must RECLAIM it: fail the orphan run and
    # fire a fresh one (exactly one new fire) instead of skipping forever.
    schedule = await _seed_due_schedule(db_session)
    schedule.last_run_status = task.STATUS_IN_PROGRESS
    schedule.in_progress_since = datetime.now(UTC) - timedelta(
        seconds=task.CLAIM_LEASE_SECONDS + 60
    )
    orphan = WolRun(
        schedule_id=schedule.id,
        trigger="schedule",
        started_at=schedule.in_progress_since,
        status=task.STATUS_IN_PROGRESS,
        target_count=0,
    )
    db_session.add(orphan)
    sid = schedule.id
    orphan_id = None
    await db_session.flush()
    orphan_id = orphan.id
    await db_session.commit()

    with patch("app.services.wol.wake_from_server", _fake_ok_send()):
        result = await task._sweep()

    assert result["fired"] == 1
    assert result["skipped_in_progress"] == 0

    await db_session.rollback()
    db_session.expire_all()
    runs = {r.id: r for r in await _run_count(db_session, sid)}
    assert len(runs) == 2  # the reclaimed orphan + the fresh fire
    reclaimed = runs[orphan_id]
    assert reclaimed.status == task.STATUS_FAILED
    assert reclaimed.error and "reclaimed" in reclaimed.error
    fresh_runs = [r for r in runs.values() if r.id != orphan_id]
    assert len(fresh_runs) == 1
    assert fresh_runs[0].status == task.STATUS_OK

    fresh_schedule = await db_session.get(WolSchedule, sid)
    assert fresh_schedule is not None
    assert fresh_schedule.last_run_status == task.STATUS_OK
    assert fresh_schedule.in_progress_since is None
    assert fresh_schedule.next_run_at is not None
    assert fresh_schedule.next_run_at > datetime.now(UTC)
