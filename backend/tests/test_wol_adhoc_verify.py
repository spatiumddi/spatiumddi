"""Ad-hoc single-host wake → post-wake verify (issue #596 Phase 1b).

``POST /ipam/addresses/{id}/wake`` used to send a magic packet and forget. It can
now opt into the same verify + bounded re-wake chain scheduled wakes use, by
minting an ephemeral ``WolRun`` (``schedule_id IS NULL``, ``trigger='adhoc'``)
carrying its config in ``WolRun.verify_params`` — there is no parent schedule row
for ``_verify_run`` to read.

Covered here:

* opt-out (the default) still mints **no** run — today's behaviour, byte for byte;
* opt-in mints exactly one run + one target, stamps ``verify_params``, and
  enqueues the first pass with the operator's ``verify_wait_seconds`` countdown;
* a **failed send** mints nothing (no phantom wake in History);
* verify while the ``tools.wake_scheduler`` module is off is refused (422), not
  stranded as an invisible, never-reaped run;
* ``_verify_run`` on an ad-hoc run reads its ``verify_params`` — the regression
  this phase exists to prevent, where a schedule-less run silently fell back to
  hardcoded defaults and ignored the operator's chosen method.

The real UDP send is always patched; no packet leaves the test process.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.tasks.wol_scheduler as task
from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.feature_module import FeatureModule
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.wol_schedule import WolRun, WolRunTarget
from app.services import feature_modules, wol

_MODULE = "tools.wake_scheduler"


# ── Builders ──────────────────────────────────────────────────────────


async def _superadmin(db: AsyncSession) -> tuple[User, str]:
    u = User(
        username=f"admin-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@x.com",
        display_name="Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(u)
    await db.flush()
    return u, create_access_token(str(u.id))


async def _wakeable_ip(db: AsyncSession) -> IPAddress:
    """An IP with a MAC + a subnet, so ``resolve_wake_params`` succeeds."""
    space = IPSpace(name=f"space-{uuid.uuid4().hex[:6]}")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.30.0.0/24", name="blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id, block_id=block.id, network="10.30.0.0/24", name="net", kind="unicast"
    )
    db.add(subnet)
    await db.flush()
    ip = IPAddress(subnet_id=subnet.id, address="10.30.0.9", mac_address="aa:bb:cc:00:11:22")
    db.add(ip)
    await db.flush()
    await db.commit()
    return ip


def _result(sent: bool) -> wol.WolResult:
    return wol.WolResult(
        mac="aa:bb:cc:00:11:22",
        broadcast="10.30.0.255",
        port=9,
        sent=sent,
        ran_from="server",
    )


def _sent() -> AsyncMock:
    return AsyncMock(return_value=_result(True))


def _not_sent() -> AsyncMock:
    return AsyncMock(return_value=_result(False))


async def _runs(db: AsyncSession) -> list[WolRun]:
    """Every WolRun in the session. No ``expire_all()`` — it would expire the
    caller's ``ip`` / ``run`` handles too, and a later *sync* attribute read on an
    expired instance raises MissingGreenlet under asyncio."""
    return list((await db.execute(select(WolRun))).scalars().all())


def _url(ip: IPAddress) -> str:
    return f"/api/v1/ipam/addresses/{ip.id}/wake"


# ══════════════════════════════════════════════════════════════════════
# Opt-out — today's behaviour is preserved exactly
# ══════════════════════════════════════════════════════════════════════


async def test_wake_without_verify_mints_no_run(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _user, token = await _superadmin(db_session)
    ip = await _wakeable_ip(db_session)

    with patch("app.services.wol.wake_from_server", new=_sent()):
        resp = await client.post(_url(ip), json={}, headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.json()["sent"] is True
    assert await _runs(db_session) == []


# ══════════════════════════════════════════════════════════════════════
# Opt-in — the ephemeral run
# ══════════════════════════════════════════════════════════════════════


async def test_wake_with_verify_mints_adhoc_run_and_enqueues(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _user, token = await _superadmin(db_session)
    ip = await _wakeable_ip(db_session)
    ip_id, url = ip.id, _url(ip)
    enqueue = MagicMock()

    with (
        patch("app.services.wol.wake_from_server", new=_sent()),
        patch.object(task.verify_wol_run, "apply_async", enqueue),
    ):
        resp = await client.post(
            url,
            json={
                "verify": True,
                "verify_wait_seconds": 120,
                "verify_retries": 2,
                "verify_method": "seen",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200

    runs = await _runs(db_session)
    assert len(runs) == 1
    run = runs[0]
    assert run.schedule_id is None
    assert run.trigger == "adhoc"
    assert run.verify_state == task.VERIFY_PENDING
    assert run.verify_attempt == 1
    assert run.verify_claimed_at is not None  # lease stamped → reaper can reclaim
    assert run.triggered_by_user_id is not None  # who woke it survives in History
    # The config the operator asked for, snapshotted onto the run.
    # Keys mirror the WolSchedule column names verbatim (so _verify_run reads
    # either source with the same key — no snapshot drift).
    assert run.verify_params["verify_method"] == "seen"
    assert run.verify_params["verify_wait_seconds"] == 120
    assert run.verify_params["verify_retries"] == 2

    targets = list(
        (await db_session.execute(select(WolRunTarget).where(WolRunTarget.run_id == run.id)))
        .scalars()
        .all()
    )
    assert len(targets) == 1
    assert targets[0].sent is True
    assert targets[0].ip_address_id == ip_id
    assert targets[0].address == "10.30.0.9"
    assert targets[0].verified is None  # not probed yet

    # First pass enqueued at the operator's wait, anchored at attempt 1.
    enqueue.assert_called_once()
    _args, kwargs = enqueue.call_args
    assert kwargs["args"] == [str(run.id), 1]
    assert kwargs["countdown"] == 120


async def test_failed_send_mints_no_run(client: AsyncClient, db_session: AsyncSession) -> None:
    """A packet that never went out has nothing to verify."""
    _user, token = await _superadmin(db_session)
    ip = await _wakeable_ip(db_session)
    enqueue = MagicMock()

    with (
        patch("app.services.wol.wake_from_server", new=_not_sent()),
        patch.object(task.verify_wol_run, "apply_async", enqueue),
    ):
        resp = await client.post(
            _url(ip), json={"verify": True}, headers={"Authorization": f"Bearer {token}"}
        )

    assert resp.status_code == 200
    assert resp.json()["sent"] is False
    assert await _runs(db_session) == []
    enqueue.assert_not_called()


async def test_verify_refused_when_module_disabled(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Don't strand a run the History tab can't show and the sweep won't reap."""
    _user, token = await _superadmin(db_session)
    ip = await _wakeable_ip(db_session)
    url = _url(ip)
    db_session.add(FeatureModule(id=_MODULE, enabled=False))
    await db_session.commit()
    feature_modules.invalidate_cache()

    try:
        with patch("app.services.wol.wake_from_server", new=_sent()) as send:
            resp = await client.post(
                url, json={"verify": True}, headers={"Authorization": f"Bearer {token}"}
            )
        assert resp.status_code == 422
        assert _MODULE in resp.json()["detail"]
        send.assert_not_awaited()  # refused before the packet goes out
        assert await _runs(db_session) == []
    finally:
        feature_modules.invalidate_cache()

    # ...and an un-opted-in wake still works with the module off.
    with patch("app.services.wol.wake_from_server", new=_sent()):
        resp = await client.post(url, json={}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    feature_modules.invalidate_cache()


# ══════════════════════════════════════════════════════════════════════
# _verify_run honours verify_params on a schedule-less run
# ══════════════════════════════════════════════════════════════════════


async def test_verify_run_reads_verify_params_when_no_schedule(
    db_session: AsyncSession,
) -> None:
    """The Phase-1b regression guard.

    Before ``verify_params`` existed, a ``schedule_id IS NULL`` run fell through
    to hardcoded defaults: method ``ping``, retries 1. Here the run asks for
    ``seen`` with ``retries=0``; the pass must probe passively (never calling the
    active probe) and finalise without a re-wake.
    """
    ip = await _wakeable_ip(db_session)
    run = WolRun(
        schedule_id=None,
        trigger="adhoc",
        status="ok",
        target_count=1,
        sent_count=1,
        verify_state=task.VERIFY_PENDING,
        verify_attempt=1,
        verify_params={"verify_method": "seen", "verify_wait_seconds": 30, "verify_retries": 0},
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        WolRunTarget(
            run_id=run.id,
            ip_address_id=ip.id,
            address="10.30.0.9",
            mac="aa:bb:cc:00:11:22",
            broadcast="10.30.0.255",
            sent=True,
            verified=None,
            wake_attempts=1,
        )
    )
    await db_session.commit()
    await db_session.refresh(run)

    # A post-wake sighting recorded by some other subsystem (SNMP / DHCP / nmap).
    ip.last_seen_at = run.started_at
    await db_session.commit()

    active = AsyncMock()
    send = AsyncMock(return_value=_result(True))
    with (
        patch("app.services.wol_scheduler.verify.probe_liveness", new=active),
        patch("app.services.wol.wake_from_server", new=send),
    ):
        result = await task._verify_run(str(run.id), 1)

    assert result["verify_state"] == task.VERIFY_DONE
    assert result["verified"] == 1
    assert result["unverified"] == 0
    active.assert_not_awaited()  # method='seen' ⇒ no active probe ran
    send.assert_not_awaited()  # retries=0 and it verified anyway ⇒ no re-wake

    run_id = run.id
    db_session.expire_all()
    target = (
        await db_session.execute(select(WolRunTarget).where(WolRunTarget.run_id == run_id))
    ).scalar_one()
    assert target.verified is True
    assert target.verify_method == "seen"
