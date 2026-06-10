"""``decom_expiring`` alert rule (#46).

Verifies the matcher surfaces subnets whose planned ``decom_date`` falls
inside the threshold window, excludes far-future / NULL / soft-deleted
rows, escalates severity as the date nears, and surfaces a past-due
message at critical severity.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import AlertRule
from app.models.ipam import IPBlock, IPSpace, Subnet
from app.services.alerts import _matching_decom_expiring_subjects


async def _make_subnet(
    db: AsyncSession,
    *,
    network: str,
    decom_days: int | None,
    soft_deleted: bool = False,
) -> Subnet:
    """Create a subnet whose ``decom_date`` is ``decom_days`` from today
    (None = no scheduled decom). ``decom_days`` may be negative (past-due).
    """
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.0.0.0/8", name="b")
    db.add(block)
    await db.flush()
    decom_date = None
    if decom_days is not None:
        decom_date = (datetime.now(UTC) + timedelta(days=decom_days)).date()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network=network,
        name=f"sn-{uuid.uuid4().hex[:6]}",
        total_ips=254,
        decom_date=decom_date,
    )
    if soft_deleted:
        subnet.deleted_at = datetime.now(UTC)
    db.add(subnet)
    await db.flush()
    return subnet


async def test_decom_expiring_matches_and_excludes(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    rule = AlertRule(
        name="Decom expiry",
        rule_type="decom_expiring",
        severity="info",
        threshold_days=30,
        enabled=True,
    )
    db_session.add(rule)

    near = await _make_subnet(db_session, network="10.0.1.0/24", decom_days=10)  # inside
    far = await _make_subnet(db_session, network="10.0.2.0/24", decom_days=365)  # must NOT match
    none = await _make_subnet(db_session, network="10.0.3.0/24", decom_days=None)  # NULL
    deleted = await _make_subnet(
        db_session, network="10.0.4.0/24", decom_days=5, soft_deleted=True
    )  # must NOT match
    await db_session.commit()

    subjects = await _matching_decom_expiring_subjects(db_session, rule, now)
    ids = {sid for sid, _disp, _msg, _sev in subjects}

    assert str(near.id) in ids
    assert str(far.id) not in ids
    assert str(none.id) not in ids
    assert str(deleted.id) not in ids


async def test_decom_expiring_severity_escalates(db_session: AsyncSession) -> None:
    """Base severity is a floor; the actual decom proximity escalates it.

    threshold/4 (30/4 = 7.5) → warning, threshold/12 (30/12 = 2.5) → critical.
    """
    now = datetime.now(UTC)
    rule = AlertRule(
        name="Decom expiry",
        rule_type="decom_expiring",
        severity="info",
        threshold_days=30,
        enabled=True,
    )
    db_session.add(rule)

    warn = await _make_subnet(db_session, network="10.1.1.0/24", decom_days=6)  # ≤ 7.5 → warning
    crit = await _make_subnet(db_session, network="10.1.2.0/24", decom_days=2)  # ≤ 2.5 → critical
    await db_session.commit()

    subjects = await _matching_decom_expiring_subjects(db_session, rule, now)
    sev_by_id = {sid: sev for sid, _disp, _msg, sev in subjects}

    assert sev_by_id[str(warn.id)] == "warning"
    assert sev_by_id[str(crit.id)] == "critical"


async def test_decom_expiring_past_due_is_critical(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    rule = AlertRule(
        name="Decom expiry",
        rule_type="decom_expiring",
        severity="info",
        threshold_days=30,
        enabled=True,
    )
    db_session.add(rule)

    overdue = await _make_subnet(db_session, network="10.2.1.0/24", decom_days=-3)  # past-due
    await db_session.commit()

    subjects = await _matching_decom_expiring_subjects(db_session, rule, now)
    by_id = {sid: (msg, sev) for sid, _disp, msg, sev in subjects}

    assert str(overdue.id) in by_id
    msg, sev = by_id[str(overdue.id)]
    assert sev == "critical"
    assert "overdue" in msg.lower()
