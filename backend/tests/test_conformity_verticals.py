"""Conformity checks for the AV / BACnet / OT verticals — issue #543.

Calls the seven check functions directly rather than through the policy
engine: the engine's own plumbing is covered elsewhere, and what is worth
pinning here is each check's verdict table.

The NOT_APPLICABLE cases carry the most weight. Each one encodes a
deliberate decision — absence of policy is not a violation, an
unclassified subnet is not a failure, an unknown level is not guilt — and
every one of them is the kind of thing a refactor can silently flip into
a wall of false findings that trains operators to ignore the report.

Also asserts CHECK_REGISTRY ↔ CHECK_CATALOG parity, so a check that
ships without a catalog entry (invisible in the policy picker) or a
catalog entry with no implementation (unusable once selected) fails here
instead of in the UI.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.av import AVFlowProfile, AVReservedRange
from app.models.bacnet import BACnetDevice
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.multicast import MulticastGroup
from app.models.ot import OTDevice, OTZone
from app.services.conformity.checks import (
    CHECK_CATALOG,
    CHECK_REGISTRY,
    STATUS_FAIL,
    STATUS_NOT_APPLICABLE,
    STATUS_PASS,
    STATUS_WARN,
    CheckOutcome,
)


def _now() -> datetime:
    return datetime.now(UTC)


async def _run(
    name: str, *, target: object | None, target_kind: str, db: AsyncSession
) -> CheckOutcome:
    return await CHECK_REGISTRY[name](
        db, target=target, target_kind=target_kind, args={}, now=_now()
    )


# ── Fixtures-by-hand ──────────────────────────────────────────────────


async def _make_space(db: AsyncSession) -> IPSpace:
    space = IPSpace(name=f"cv-space-{uuid.uuid4().hex[:8]}")
    db.add(space)
    await db.flush()
    return space


async def _make_subnet(db: AsyncSession, space: IPSpace, network: str = "10.0.0.0/24") -> Subnet:
    block = IPBlock(space_id=space.id, name=f"b-{uuid.uuid4().hex[:6]}", network="10.0.0.0/16")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, name="s", network=network)
    db.add(subnet)
    await db.flush()
    return subnet


async def _make_ip(db: AsyncSession, subnet: Subnet, addr: str) -> IPAddress:
    ip = IPAddress(subnet_id=subnet.id, address=addr, status="allocated")
    db.add(ip)
    await db.flush()
    return ip


async def _make_group(db: AsyncSession, space: IPSpace, address: str) -> MulticastGroup:
    group = MulticastGroup(space_id=space.id, address=address, name=f"grp-{address}")
    db.add(group)
    await db.flush()
    return group


async def _make_profile(
    db: AsyncSession,
    group: MulticastGroup,
    *,
    av_protocol: str = "dante",
    ptp_domain: int | None = None,
) -> AVFlowProfile:
    profile = AVFlowProfile(group_id=group.id, av_protocol=av_protocol, ptp_domain=ptp_domain)
    db.add(profile)
    await db.flush()
    return profile


async def _make_range(
    db: AsyncSession, space: IPSpace, cidr: str, av_protocol: str
) -> AVReservedRange:
    row = AVReservedRange(space_id=space.id, cidr=cidr, av_protocol=av_protocol)
    db.add(row)
    await db.flush()
    return row


async def _make_bacnet_device(
    db: AsyncSession,
    ip: IPAddress,
    *,
    device_instance: int,
    is_bbmd: bool = False,
    vendor_id: int | None = 8,
) -> BACnetDevice:
    device = BACnetDevice(
        ip_address_id=ip.id,
        subnet_id=ip.subnet_id,
        device_instance=device_instance,
        is_bbmd=is_bbmd,
        vendor_id=vendor_id,
    )
    db.add(device)
    await db.flush()
    return device


async def _make_ot_device(db: AsyncSession, ip: IPAddress, *, purdue_level: str | None) -> OTDevice:
    device = OTDevice(
        ip_address_id=ip.id,
        ot_protocol="profinet",
        ot_role="plc",
        purdue_level=None if purdue_level is None else Decimal(purdue_level),
    )
    db.add(device)
    await db.flush()
    return device


async def _make_ot_zone(db: AsyncSession, subnet: Subnet, purdue_level: str) -> OTZone:
    zone = OTZone(subnet_id=subnet.id, purdue_level=Decimal(purdue_level), cell_area="Line-3 Weld")
    db.add(zone)
    await db.flush()
    return zone


# ── av_flow_outside_reserved_range ────────────────────────────────────

_CHECK_AV_RANGE = "av_flow_outside_reserved_range"


async def test_av_flow_inside_declared_range_passes(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    group = await _make_group(db_session, space, "239.69.1.5")
    await _make_profile(db_session, group, av_protocol="dante")
    await _make_range(db_session, space, "239.69.0.0/16", "dante")

    outcome = await _run(
        _CHECK_AV_RANGE, target=group, target_kind="multicast_group", db=db_session
    )
    assert outcome.status == STATUS_PASS, outcome.detail
    assert outcome.diagnostic["matched_range"] == "239.69.0.0/16"


async def test_av_flow_outside_declared_range_fails(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    group = await _make_group(db_session, space, "239.100.0.1")
    await _make_profile(db_session, group, av_protocol="dante")
    await _make_range(db_session, space, "239.69.0.0/16", "dante")

    outcome = await _run(
        _CHECK_AV_RANGE, target=group, target_kind="multicast_group", db=db_session
    )
    assert outcome.status == STATUS_FAIL, outcome.detail
    assert outcome.diagnostic["declared_ranges"] == ["239.69.0.0/16"]


async def test_av_flow_not_applicable_without_a_range_for_its_protocol(
    db_session: AsyncSession,
) -> None:
    """Absence of policy is not a violation.

    The space HAS a declared range — just not for this flow's protocol —
    so this also pins the per-protocol filter: a dante flow is not
    checked against the aes67 plan.
    """
    space = await _make_space(db_session)
    group = await _make_group(db_session, space, "239.100.0.1")
    await _make_profile(db_session, group, av_protocol="dante")
    await _make_range(db_session, space, "239.0.0.0/8", "aes67")

    outcome = await _run(
        _CHECK_AV_RANGE, target=group, target_kind="multicast_group", db=db_session
    )
    assert outcome.status == STATUS_NOT_APPLICABLE, outcome.detail
    assert "dante" in outcome.detail


async def test_av_flow_range_not_applicable_without_descriptor(db_session: AsyncSession) -> None:
    """A group with no AV descriptor is a plain multicast group."""
    space = await _make_space(db_session)
    group = await _make_group(db_session, space, "239.100.0.1")
    await _make_range(db_session, space, "239.69.0.0/16", "dante")

    outcome = await _run(
        _CHECK_AV_RANGE, target=group, target_kind="multicast_group", db=db_session
    )
    assert outcome.status == STATUS_NOT_APPLICABLE, outcome.detail


# ── av_flow_no_ptp_domain ─────────────────────────────────────────────

_CHECK_AV_PTP = "av_flow_no_ptp_domain"


async def test_av_flow_with_ptp_domain_passes(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    group = await _make_group(db_session, space, "239.69.1.5")
    # Domain 0 is a real PTP domain — the check must test for None, not
    # for falsiness.
    await _make_profile(db_session, group, ptp_domain=0)

    outcome = await _run(_CHECK_AV_PTP, target=group, target_kind="multicast_group", db=db_session)
    assert outcome.status == STATUS_PASS, outcome.detail
    assert outcome.diagnostic["ptp_domain"] == 0


async def test_av_flow_without_ptp_domain_warns(db_session: AsyncSession) -> None:
    """Advisory, never a failure: undocumented state, not a broken network."""
    space = await _make_space(db_session)
    group = await _make_group(db_session, space, "239.69.1.5")
    await _make_profile(db_session, group, ptp_domain=None)

    outcome = await _run(_CHECK_AV_PTP, target=group, target_kind="multicast_group", db=db_session)
    assert outcome.status == STATUS_WARN, outcome.detail


async def test_av_flow_ptp_not_applicable_without_descriptor(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    group = await _make_group(db_session, space, "239.69.1.5")

    outcome = await _run(_CHECK_AV_PTP, target=group, target_kind="multicast_group", db=db_session)
    assert outcome.status == STATUS_NOT_APPLICABLE, outcome.detail


# ── bbmd_one_per_subnet ───────────────────────────────────────────────

_CHECK_BBMD = "bbmd_one_per_subnet"


async def test_bbmd_exactly_one_passes(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)
    await _make_bacnet_device(
        db_session, await _make_ip(db_session, subnet, "10.0.0.5"), device_instance=1, is_bbmd=True
    )
    await _make_bacnet_device(
        db_session, await _make_ip(db_session, subnet, "10.0.0.6"), device_instance=2
    )

    outcome = await _run(_CHECK_BBMD, target=subnet, target_kind="subnet", db=db_session)
    assert outcome.status == STATUS_PASS, outcome.detail
    assert outcome.diagnostic["bbmd_instances"] == [1]
    assert outcome.diagnostic["bacnet_device_count"] == 2


async def test_bbmd_zero_fails(db_session: AsyncSession) -> None:
    """Zero BBMDs leaves the subnet's devices invisible across IP routers."""
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)
    await _make_bacnet_device(
        db_session, await _make_ip(db_session, subnet, "10.0.0.5"), device_instance=1
    )
    await _make_bacnet_device(
        db_session, await _make_ip(db_session, subnet, "10.0.0.6"), device_instance=2
    )

    outcome = await _run(_CHECK_BBMD, target=subnet, target_kind="subnet", db=db_session)
    assert outcome.status == STATUS_FAIL, outcome.detail
    assert outcome.diagnostic["bbmd_count"] == 0


async def test_bbmd_two_fails(db_session: AsyncSession) -> None:
    """Two BBMDs duplicate every forwarded broadcast — the other direction
    of the same rule, and the one a "at least one" check would miss."""
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)
    await _make_bacnet_device(
        db_session, await _make_ip(db_session, subnet, "10.0.0.5"), device_instance=1, is_bbmd=True
    )
    await _make_bacnet_device(
        db_session, await _make_ip(db_session, subnet, "10.0.0.6"), device_instance=2, is_bbmd=True
    )

    outcome = await _run(_CHECK_BBMD, target=subnet, target_kind="subnet", db=db_session)
    assert outcome.status == STATUS_FAIL, outcome.detail
    assert outcome.diagnostic["bbmd_count"] == 2
    assert outcome.diagnostic["bbmd_instances"] == [1, 2]


async def test_bbmd_not_applicable_without_bacnet_devices(db_session: AsyncSession) -> None:
    """Most subnets in a mixed estate are not BACnet subnets; flagging
    them would bury the real findings."""
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)

    outcome = await _run(_CHECK_BBMD, target=subnet, target_kind="subnet", db=db_session)
    assert outcome.status == STATUS_NOT_APPLICABLE, outcome.detail


# ── bacnet_duplicate_device_instance ──────────────────────────────────

_CHECK_BACNET_DUPES = "bacnet_duplicate_device_instance"


async def test_bacnet_unique_instances_pass(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)
    await _make_bacnet_device(
        db_session, await _make_ip(db_session, subnet, "10.0.0.5"), device_instance=1
    )
    await _make_bacnet_device(
        db_session, await _make_ip(db_session, subnet, "10.0.0.6"), device_instance=2
    )

    outcome = await _run(_CHECK_BACNET_DUPES, target=None, target_kind="platform", db=db_session)
    assert outcome.status == STATUS_PASS, outcome.detail


async def test_bacnet_duplicate_instances_fail(db_session: AsyncSession) -> None:
    """``uq_bacnet_device_instance`` should make this unreachable — which
    is why the duplicate has to be forced in with the constraint dropped.
    The check exists for data that arrived by a path bypassing the ORM (a
    restore from before the constraint, a bulk load with constraints off),
    which is exactly when an operator most needs to be told."""
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)
    first_ip = await _make_ip(db_session, subnet, "10.0.0.5")
    second_ip = await _make_ip(db_session, subnet, "10.0.0.6")

    await db_session.execute(
        text("ALTER TABLE bacnet_device DROP CONSTRAINT uq_bacnet_device_instance")
    )
    try:
        await _make_bacnet_device(db_session, first_ip, device_instance=77)
        await _make_bacnet_device(db_session, second_ip, device_instance=77)

        outcome = await _run(
            _CHECK_BACNET_DUPES, target=None, target_kind="platform", db=db_session
        )
        assert outcome.status == STATUS_FAIL, outcome.detail
        assert outcome.diagnostic["duplicate_instances"] == [77]
    finally:
        # Restore the schema before the next test reuses it — the session
        # fixture creates the schema once, so a dropped constraint would
        # otherwise leak for the rest of the run.
        await db_session.execute(text("DELETE FROM bacnet_device"))
        await db_session.execute(
            text(
                "ALTER TABLE bacnet_device ADD CONSTRAINT uq_bacnet_device_instance "
                "UNIQUE (device_instance)"
            )
        )
        await db_session.commit()


async def test_bacnet_duplicate_check_not_applicable_off_platform(
    db_session: AsyncSession,
) -> None:
    """Instance uniqueness is an internetwork-wide property, so the check
    only answers at platform scope."""
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)

    outcome = await _run(_CHECK_BACNET_DUPES, target=subnet, target_kind="subnet", db=db_session)
    assert outcome.status == STATUS_NOT_APPLICABLE, outcome.detail


# ── bacnet_vendor_id_unknown ──────────────────────────────────────────

_CHECK_BACNET_VENDOR = "bacnet_vendor_id_unknown"


async def test_bacnet_known_vendor_ids_pass(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)
    await _make_bacnet_device(
        db_session,
        await _make_ip(db_session, subnet, "10.0.0.5"),
        device_instance=1,
        vendor_id=8,
    )

    outcome = await _run(_CHECK_BACNET_VENDOR, target=None, target_kind="platform", db=db_session)
    assert outcome.status == STATUS_PASS, outcome.detail


async def test_bacnet_zero_or_missing_vendor_id_warns(db_session: AsyncSession) -> None:
    """Advisory: vendor id 0 is legitimately ASHRAE, but in the field it
    is almost always a misconfigured or cloned controller."""
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)
    await _make_bacnet_device(
        db_session,
        await _make_ip(db_session, subnet, "10.0.0.5"),
        device_instance=1,
        vendor_id=0,
    )
    await _make_bacnet_device(
        db_session,
        await _make_ip(db_session, subnet, "10.0.0.6"),
        device_instance=2,
        vendor_id=None,
    )

    outcome = await _run(_CHECK_BACNET_VENDOR, target=None, target_kind="platform", db=db_session)
    assert outcome.status == STATUS_WARN, outcome.detail
    assert sorted(outcome.diagnostic["device_instances"]) == [1, 2]


async def test_bacnet_vendor_check_not_applicable_off_platform(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)

    outcome = await _run(_CHECK_BACNET_VENDOR, target=subnet, target_kind="subnet", db=db_session)
    assert outcome.status == STATUS_NOT_APPLICABLE, outcome.detail


# ── ot_device_crosses_purdue_boundary ─────────────────────────────────

_CHECK_OT_BOUNDARY = "ot_device_crosses_purdue_boundary"


async def test_ot_device_matching_zone_level_passes(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)
    ip = await _make_ip(db_session, subnet, "10.0.0.5")
    await _make_ot_device(db_session, ip, purdue_level="3.5")
    await _make_ot_zone(db_session, subnet, "3.5")

    outcome = await _run(_CHECK_OT_BOUNDARY, target=ip, target_kind="ip_address", db=db_session)
    assert outcome.status == STATUS_PASS, outcome.detail
    # Compared as Decimals: the diagnostic renders whatever scale the
    # value carries ("3.5"), and the point is the level, not the spelling.
    assert Decimal(outcome.diagnostic["device_purdue_level"]) == Decimal("3.5")


async def test_ot_device_crossing_boundary_fails(db_session: AsyncSession) -> None:
    """A Level-1 controller in a Level-4 enterprise subnet is the finding
    an auditor asks for."""
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)
    ip = await _make_ip(db_session, subnet, "10.0.0.5")
    await _make_ot_device(db_session, ip, purdue_level="1")
    await _make_ot_zone(db_session, subnet, "4")

    outcome = await _run(_CHECK_OT_BOUNDARY, target=ip, target_kind="ip_address", db=db_session)
    assert outcome.status == STATUS_FAIL, outcome.detail
    assert Decimal(outcome.diagnostic["device_purdue_level"]) == Decimal("1")
    assert Decimal(outcome.diagnostic["zone_purdue_level"]) == Decimal("4")


async def test_ot_boundary_not_applicable_when_device_level_unset(
    db_session: AsyncSession,
) -> None:
    """An unrecorded level is missing documentation, not a segmentation
    violation — treating unknown as guilty would make the check unusable
    during rollout."""
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)
    ip = await _make_ip(db_session, subnet, "10.0.0.5")
    await _make_ot_device(db_session, ip, purdue_level=None)
    await _make_ot_zone(db_session, subnet, "4")

    outcome = await _run(_CHECK_OT_BOUNDARY, target=ip, target_kind="ip_address", db=db_session)
    assert outcome.status == STATUS_NOT_APPLICABLE, outcome.detail


async def test_ot_boundary_not_applicable_when_zone_missing(db_session: AsyncSession) -> None:
    """The other half of "either level unset": the device is classified
    but its subnet has never been zoned."""
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)
    ip = await _make_ip(db_session, subnet, "10.0.0.5")
    await _make_ot_device(db_session, ip, purdue_level="1")

    outcome = await _run(_CHECK_OT_BOUNDARY, target=ip, target_kind="ip_address", db=db_session)
    assert outcome.status == STATUS_NOT_APPLICABLE, outcome.detail


async def test_ot_boundary_not_applicable_without_descriptor(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)
    ip = await _make_ip(db_session, subnet, "10.0.0.5")
    await _make_ot_zone(db_session, subnet, "4")

    outcome = await _run(_CHECK_OT_BOUNDARY, target=ip, target_kind="ip_address", db=db_session)
    assert outcome.status == STATUS_NOT_APPLICABLE, outcome.detail


# ── ot_zone_missing_purdue_level ──────────────────────────────────────

_CHECK_OT_ZONE = "ot_zone_missing_purdue_level"


async def test_ot_zoned_subnet_passes(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)
    ip = await _make_ip(db_session, subnet, "10.0.0.5")
    await _make_ot_device(db_session, ip, purdue_level="2")
    await _make_ot_zone(db_session, subnet, "2")

    outcome = await _run(_CHECK_OT_ZONE, target=subnet, target_kind="subnet", db=db_session)
    assert outcome.status == STATUS_PASS, outcome.detail
    assert outcome.diagnostic["ot_device_count"] == 1


async def test_ot_unzoned_subnet_with_devices_warns(db_session: AsyncSession) -> None:
    """This is the check that tells an operator why their segmentation
    report is empty."""
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)
    ip = await _make_ip(db_session, subnet, "10.0.0.5")
    await _make_ot_device(db_session, ip, purdue_level="2")

    outcome = await _run(_CHECK_OT_ZONE, target=subnet, target_kind="subnet", db=db_session)
    assert outcome.status == STATUS_WARN, outcome.detail
    assert outcome.diagnostic["ot_device_count"] == 1


async def test_ot_zone_check_not_applicable_without_devices(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space)
    await _make_ip(db_session, subnet, "10.0.0.5")

    outcome = await _run(_CHECK_OT_ZONE, target=subnet, target_kind="subnet", db=db_session)
    assert outcome.status == STATUS_NOT_APPLICABLE, outcome.detail


# ── Registry ↔ catalog parity ─────────────────────────────────────────


async def test_registry_and_catalog_are_in_sync() -> None:
    """A check with no catalog entry never appears in the policy picker;
    a catalog entry with no implementation blows up when selected.
    Neither is visible without this assertion."""
    catalog_names = [entry["name"] for entry in CHECK_CATALOG]
    assert len(catalog_names) == len(set(catalog_names)), "duplicate CHECK_CATALOG entry"
    assert set(CHECK_REGISTRY) == set(catalog_names)


async def test_every_vertical_check_is_registered() -> None:
    """The seven checks #543 adds, named explicitly so a rename has to be
    a deliberate act rather than a silently-empty test file."""
    for name in (
        _CHECK_AV_RANGE,
        _CHECK_AV_PTP,
        _CHECK_BBMD,
        _CHECK_BACNET_DUPES,
        _CHECK_BACNET_VENDOR,
        _CHECK_OT_BOUNDARY,
        _CHECK_OT_ZONE,
    ):
        assert name in CHECK_REGISTRY
