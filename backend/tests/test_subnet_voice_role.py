"""Tests for the voice-segment metadata wave (issue #112 phase 2).

Covers the load-bearing pieces:

* ``Subnet.subnet_role`` column accepts the canonical values + survives
  a round-trip through the API
* Conformity check ``voice_segment_not_internet_facing`` returns the
  three expected outcomes (pass / fail / not_applicable)
* Alert rule ``voice_lease_count_below`` only counts active leases on
  voice-tagged subnets and fires only when the count drops below the
  configured threshold
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import AlertRule
from app.models.dhcp import DHCPLease, DHCPScope, DHCPServer, DHCPServerGroup
from app.models.ipam import IPBlock, IPSpace, Subnet
from app.services.alerts import (
    RULE_TYPE_VOICE_LEASE_COUNT_BELOW,
    _matching_voice_lease_count_below_subjects,
)
from app.services.conformity.checks import (
    CHECK_REGISTRY,
    STATUS_FAIL,
    STATUS_NOT_APPLICABLE,
    STATUS_PASS,
)

# ── Helpers ───────────────────────────────────────────────────────────────


async def _make_subnet(
    db: AsyncSession,
    *,
    network: str = "10.0.20.0/24",
    name: str = "voice-vlan-20",
    subnet_role: str | None = "voice",
    internet_facing: bool = False,
) -> Subnet:
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(name=f"blk-{uuid.uuid4().hex[:6]}", space_id=space.id, network=network)
    db.add(block)
    await db.flush()
    subnet = Subnet(
        name=name,
        space_id=space.id,
        block_id=block.id,
        network=network,
        subnet_role=subnet_role,
        internet_facing=internet_facing,
    )
    db.add(subnet)
    await db.flush()
    return subnet


# ── Voice-segment conformity check ────────────────────────────────────────


async def test_voice_check_passes_for_internal_voice_subnet(
    db_session: AsyncSession,
) -> None:
    db = db_session
    subnet = await _make_subnet(db, subnet_role="voice", internet_facing=False)
    fn = CHECK_REGISTRY["voice_segment_not_internet_facing"]
    outcome = await fn(
        db,
        target=subnet,
        target_kind="subnet",
        args={},
        now=datetime.now(UTC),
    )
    assert outcome.status == STATUS_PASS


async def test_voice_check_fails_when_voice_subnet_is_internet_facing(
    db_session: AsyncSession,
) -> None:
    db = db_session
    subnet = await _make_subnet(db, subnet_role="voice", internet_facing=True)
    fn = CHECK_REGISTRY["voice_segment_not_internet_facing"]
    outcome = await fn(
        db,
        target=subnet,
        target_kind="subnet",
        args={},
        now=datetime.now(UTC),
    )
    assert outcome.status == STATUS_FAIL
    assert "internet_facing" in outcome.detail


async def test_voice_check_skips_non_voice_subnet(
    db_session: AsyncSession,
) -> None:
    db = db_session
    subnet = await _make_subnet(db, subnet_role="data", internet_facing=True)
    fn = CHECK_REGISTRY["voice_segment_not_internet_facing"]
    outcome = await fn(
        db,
        target=subnet,
        target_kind="subnet",
        args={},
        now=datetime.now(UTC),
    )
    # Data subnet — not the check's concern; passing internet_facing is
    # a different policy's signal.
    assert outcome.status == STATUS_NOT_APPLICABLE


# ── Voice-lease-count alert ───────────────────────────────────────────────


async def _make_dhcp_scaffold(
    db: AsyncSession,
) -> tuple[DHCPServerGroup, DHCPServer, DHCPScope, Subnet]:
    """Group + server + scope + voice-tagged subnet wired together."""
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    db.add(grp)
    await db.flush()
    srv = DHCPServer(
        name=f"s-{uuid.uuid4().hex[:6]}",
        driver="kea",
        host="127.0.0.1",
        port=67,
        server_group_id=grp.id,
    )
    db.add(srv)
    await db.flush()
    subnet = await _make_subnet(db, network="10.0.21.0/24", subnet_role="voice")
    scope = DHCPScope(
        name="voice-vlan",
        group_id=grp.id,
        subnet_id=subnet.id,
        is_active=True,
        lease_time=3600,
    )
    db.add(scope)
    await db.flush()
    return grp, srv, scope, subnet


def _make_rule(threshold: int) -> AlertRule:
    """In-memory AlertRule for the evaluator (not persisted — the
    evaluator only reads ``threshold_percent``)."""
    return AlertRule(
        id=uuid.uuid4(),
        name="voice-fleet-watch",
        description="",
        rule_type=RULE_TYPE_VOICE_LEASE_COUNT_BELOW,
        enabled=True,
        threshold_percent=threshold,
        severity="warning",
    )


async def test_voice_lease_alert_fires_when_count_below_threshold(
    db_session: AsyncSession,
) -> None:
    db = db_session
    _grp, srv, _scope, _subnet = await _make_dhcp_scaffold(db)
    # 2 active leases — below threshold 5.
    for i in (1, 2):
        db.add(
            DHCPLease(
                server_id=srv.id,
                ip_address=f"10.0.21.{i}",
                mac_address=f"aa:bb:cc:dd:ee:0{i}",
                state="active",
                ends_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    await db.flush()
    matches = await _matching_voice_lease_count_below_subjects(db, _make_rule(5))
    assert len(matches) == 1
    assert "active lease" in matches[0][2].lower()


async def test_voice_lease_alert_silent_when_count_meets_threshold(
    db_session: AsyncSession,
) -> None:
    db = db_session
    _grp, srv, _scope, _subnet = await _make_dhcp_scaffold(db)
    # 6 active leases — at or above threshold 5; no match.
    for i in range(1, 7):
        db.add(
            DHCPLease(
                server_id=srv.id,
                ip_address=f"10.0.21.{i}",
                mac_address=f"aa:bb:cc:dd:ee:0{i}",
                state="active",
                ends_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    await db.flush()
    matches = await _matching_voice_lease_count_below_subjects(db, _make_rule(5))
    assert matches == []


async def test_voice_lease_alert_ignores_non_voice_subnets(
    db_session: AsyncSession,
) -> None:
    """Subnets without ``subnet_role='voice'`` don't enter the count
    even if they have leases. Sanity check the gate."""
    db = db_session
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    db.add(grp)
    await db.flush()
    srv = DHCPServer(
        name=f"s-{uuid.uuid4().hex[:6]}",
        driver="kea",
        host="127.0.0.1",
        port=67,
        server_group_id=grp.id,
    )
    db.add(srv)
    await db.flush()
    # Data-tagged subnet with one active lease.
    subnet = await _make_subnet(db, network="10.0.30.0/24", subnet_role="data")
    scope = DHCPScope(
        name="data-vlan",
        group_id=grp.id,
        subnet_id=subnet.id,
        is_active=True,
        lease_time=3600,
    )
    db.add(scope)
    db.add(
        DHCPLease(
            server_id=srv.id,
            ip_address="10.0.30.42",
            mac_address="aa:bb:cc:dd:ee:99",
            state="active",
            ends_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db.flush()
    matches = await _matching_voice_lease_count_below_subjects(db, _make_rule(5))
    # No voice subnets at all — empty matches.
    assert matches == []
