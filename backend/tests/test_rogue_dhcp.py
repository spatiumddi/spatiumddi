"""Rogue DHCP server detection (#370): classification + alert matcher."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import AlertRule
from app.models.dhcp import (
    DHCPObservedResponder,
    DHCPResponderAllowlist,
    DHCPServer,
    DHCPServerGroup,
)
from app.services.alerts import (
    RULE_TYPE_ROGUE_DHCP,
    _matching_rogue_dhcp_subjects,
)
from app.services.dhcp.rogue_detection import ObservedOffer, classify_responder, record_offers


async def _group_with_server(db: AsyncSession, host: str) -> tuple[DHCPServerGroup, DHCPServer]:
    g = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    db.add(g)
    await db.flush()
    s = DHCPServer(name=f"s-{uuid.uuid4().hex[:6]}", host=host, driver="kea", server_group_id=g.id)
    db.add(s)
    await db.flush()
    return g, s


async def test_classify_known_vs_rogue(db_session: AsyncSession) -> None:
    g, _ = await _group_with_server(db_session, host="10.0.0.2")
    await db_session.commit()
    # Source matches the known member host → expected.
    assert await classify_responder(db_session, g.id, "srv-1", "10.0.0.2") == "expected"
    # Unknown source → rogue.
    assert await classify_responder(db_session, g.id, "srv-9", "10.0.0.250") == "rogue"


async def test_classify_allowlisted(db_session: AsyncSession) -> None:
    g, _ = await _group_with_server(db_session, host="10.0.0.2")
    db_session.add(DHCPResponderAllowlist(group_id=g.id, source_ip="10.0.0.99", note="edge router"))
    await db_session.commit()
    assert await classify_responder(db_session, g.id, "srv-x", "10.0.0.99") == "acknowledged"


async def test_record_offers_classifies_and_upserts(db_session: AsyncSession) -> None:
    g, server = await _group_with_server(db_session, host="10.0.0.2")
    await db_session.commit()
    counts = await record_offers(
        db_session,
        server,
        [
            ObservedOffer(server_identifier="10.0.0.2", source_ip="10.0.0.2"),  # expected
            ObservedOffer(
                server_identifier="10.0.0.250",
                source_ip="10.0.0.250",
                offered_ip="10.0.0.123",
            ),  # rogue
        ],
    )
    assert counts["expected"] == 1
    assert counts["rogue"] == 1
    # Idempotent re-report bumps last_seen, no duplicate row.
    counts2 = await record_offers(
        db_session,
        server,
        [ObservedOffer(server_identifier="10.0.0.250", source_ip="10.0.0.250")],
    )
    assert counts2["rogue"] == 1
    rows = (
        (
            await db_session.execute(
                DHCPObservedResponder.__table__.select().where(
                    DHCPObservedResponder.group_id == g.id
                )
            )
        )
        .mappings()
        .all()
    )
    assert len(rows) == 2  # one expected, one rogue — no dup


async def test_rogue_alert_matcher(db_session: AsyncSession) -> None:
    g, server = await _group_with_server(db_session, host="10.0.0.2")
    await db_session.commit()
    await record_offers(
        db_session,
        server,
        [ObservedOffer(server_identifier="10.0.0.250", source_ip="10.0.0.250")],
    )
    rule = AlertRule(
        name="rogue", rule_type=RULE_TYPE_ROGUE_DHCP, severity="warning", threshold_days=1
    )
    subjects = await _matching_rogue_dhcp_subjects(db_session, rule)
    assert len(subjects) == 1
    assert "10.0.0.250" in subjects[0][1]


async def test_rogue_alert_matcher_ignores_old_and_acknowledged(
    db_session: AsyncSession,
) -> None:
    g, server = await _group_with_server(db_session, host="10.0.0.2")
    # An acknowledged responder + a stale rogue must both be excluded.
    db_session.add(
        DHCPObservedResponder(
            group_id=g.id,
            server_identifier="ack",
            source_ip="10.0.0.99",
            classification="acknowledged",
            last_seen_at=datetime.now(UTC),
        )
    )
    db_session.add(
        DHCPObservedResponder(
            group_id=g.id,
            server_identifier="old",
            source_ip="10.0.0.200",
            classification="rogue",
            last_seen_at=datetime.now(UTC) - timedelta(days=10),
        )
    )
    await db_session.commit()
    rule = AlertRule(
        name="rogue", rule_type=RULE_TYPE_ROGUE_DHCP, severity="warning", threshold_days=1
    )
    subjects = await _matching_rogue_dhcp_subjects(db_session, rule)
    assert subjects == []
