"""One-time firewall_extra advisory-lint sweep (#285 Phase 5)."""

from __future__ import annotations

import hashlib
import os
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appliance import APPLIANCE_STATE_APPROVED, Appliance
from app.models.audit import AuditLog
from app.tasks.firewall_lint_scan import scan_firewall_extra


async def _appliance(db: AsyncSession, firewall_extra: str | None) -> None:
    der = os.urandom(32)
    db.add(
        Appliance(
            id=uuid.uuid4(),
            hostname=f"n-{uuid.uuid4().hex[:6]}",
            public_key_der=der,
            public_key_fingerprint=hashlib.sha256(der).hexdigest(),
            state=APPLIANCE_STATE_APPROVED,
            deployment_kind="appliance",
            firewall_extra=firewall_extra,
        )
    )
    await db.flush()


async def _advisory_count(db: AsyncSession) -> int:
    return (
        await db.execute(
            select(func.count()).where(AuditLog.action == "firewall_extra_lint_advisory")
        )
    ).scalar_one()


async def test_scan_audits_findings_and_is_one_shot(db_session: AsyncSession) -> None:
    # one clean, one with a dangerous pattern (drop 22), one with a soft nit
    await _appliance(db_session, 'ip saddr { 10.0.0.0/8 } tcp dport 9090 accept comment "ok"')
    await _appliance(db_session, "ip saddr { 10.0.0.0/8 } tcp dport 22 drop")  # error finding
    await _appliance(db_session, "tcp dport 80 accept")  # warning: no saddr
    await db_session.commit()

    res = await scan_firewall_extra(db_session)
    assert res["ran"] is True
    assert res["appliances"] == 3
    assert res["with_findings"] == 2  # the clean one produces none
    assert await _advisory_count(db_session) == 2

    # second run short-circuits on the marker — no new advisory rows
    res2 = await scan_firewall_extra(db_session)
    assert res2["ran"] is False
    assert await _advisory_count(db_session) == 2


async def test_scan_with_no_extras_still_watermarks(db_session: AsyncSession) -> None:
    await _appliance(db_session, None)  # firewall_extra NULL → excluded
    await db_session.commit()
    res = await scan_firewall_extra(db_session)
    assert res["ran"] is True and res["appliances"] == 0
    # marker written → second run is a no-op
    res2 = await scan_firewall_extra(db_session)
    assert res2["ran"] is False
