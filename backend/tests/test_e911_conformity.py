"""The three E911 conformity checks, and the civic-schema guard (#972).

A conformity check that never fires is indistinguishable from one that
passes, which is the lesson this repository has now recorded several
times — so every check here is exercised in BOTH directions: a state it
must fail, and a state it must pass. The pass case is the one that
catches a check wired to the wrong column, because a check that always
failed would be noticed in a week and one that always passes never is.

HOW TO RUN (serial, in the dev container):
    make test-one T=tests/test_e911_conformity.py
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.e911.router import CivicAddress
from app.core.crypto import encrypt_str
from app.models.e911 import (
    CIVIC_COLUMNS,
    DISPATCHABLE_DETAIL_COLUMNS,
    ERL_RULE_PRECEDENCE,
    ERL_RULE_TARGET_COLUMN,
    EmergencyResponseLocation,
    ERLBinding,
)
from app.models.ipam import IPBlock, IPSpace, Subnet
from app.models.network import NetworkDevice, NetworkInterface
from app.models.ownership import Site
from app.services.conformity.checks import (
    CHECK_CATALOG,
    CHECK_REGISTRY,
    STATUS_FAIL,
    STATUS_NOT_APPLICABLE,
    STATUS_PASS,
    STATUS_WARN,
)
from app.services.conformity.seeder import _BUILTIN_POLICIES as BUILTIN_POLICIES


def _now() -> datetime:
    return datetime.now(UTC)


async def _check(db, name, target=None, target_kind="platform", args=None):
    return await CHECK_REGISTRY[name](
        db, target=target, target_kind=target_kind, args=args or {}, now=_now()
    )


async def _voice_subnet(db: AsyncSession, *, with_site=True, role="voice") -> Subnet:
    site = Site(name=f"site-{uuid.uuid4().hex[:6]}", kind="office")
    db.add(site)
    await db.flush()
    space = IPSpace(name=f"e911c-{uuid.uuid4().hex[:8]}")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.90.0.0/16", name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network=f"10.90.{uuid.uuid4().int % 250}.0/24",
        name=f"voice-{uuid.uuid4().hex[:6]}",
        subnet_role=role,
        site_id=site.id if with_site else None,
    )
    db.add(subnet)
    await db.flush()
    subnet._site = site  # type: ignore[attr-defined]
    return subnet


async def _erl(db: AsyncSession, *, validated=None, name=None) -> EmergencyResponseLocation:
    erl = EmergencyResponseLocation(
        name=name or f"erl-{uuid.uuid4().hex[:8]}",
        country="US",
        a1="NY",
        a3="New York",
        rd="Broadway",
        hno="1234",
        flr="3",
    )
    if validated is not None:
        erl.validation_state = validated[0]
        erl.validated_at = validated[1]
        erl.validation_source = "test-provider"
    db.add(erl)
    await db.flush()
    return erl


# ══════════════════════════════════════════════════════════════════════
# e911_voice_subnet_unbound — the RAY BAUM'S gap
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_voice_subnet_with_no_erl_anywhere_fails(db_session: AsyncSession) -> None:
    subnet = await _voice_subnet(db_session)
    out = await _check(db_session, "e911_voice_subnet_unbound", subnet, "subnet")
    assert out.status == STATUS_FAIL
    assert "no ERL binding" in out.detail


@pytest.mark.asyncio
async def test_a_subnet_level_binding_passes(db_session: AsyncSession) -> None:
    subnet = await _voice_subnet(db_session)
    erl = await _erl(db_session)
    db_session.add(ERLBinding(erl_id=erl.id, rule_kind="subnet", subnet_id=subnet.id))
    await db_session.flush()
    out = await _check(db_session, "e911_voice_subnet_unbound", subnet, "subnet")
    assert out.status == STATUS_PASS
    assert out.diagnostic["rule_kind"] == "subnet"


@pytest.mark.asyncio
async def test_the_site_default_counts(db_session: AsyncSession) -> None:
    """The front door is a poor answer and it is still a dispatchable
    location. A check demanding room-level bindings would fail every site on
    day one and be turned off."""
    subnet = await _voice_subnet(db_session)
    erl = await _erl(db_session)
    db_session.add(ERLBinding(erl_id=erl.id, rule_kind="site_default", site_id=subnet.site_id))
    await db_session.flush()
    out = await _check(db_session, "e911_voice_subnet_unbound", subnet, "subnet")
    assert out.status == STATUS_PASS
    assert out.diagnostic["rule_kind"] == "site_default"


@pytest.mark.asyncio
async def test_an_inactive_erl_does_not_satisfy_the_check(
    db_session: AsyncSession,
) -> None:
    """A binding to a deactivated ERL resolves to nothing, so the subnet is
    as unbound as if the rule were absent."""
    subnet = await _voice_subnet(db_session)
    erl = await _erl(db_session)
    erl.is_active = False
    db_session.add(ERLBinding(erl_id=erl.id, rule_kind="subnet", subnet_id=subnet.id))
    await db_session.flush()
    out = await _check(db_session, "e911_voice_subnet_unbound", subnet, "subnet")
    assert out.status == STATUS_FAIL


@pytest.mark.asyncio
async def test_a_mac_pin_does_not_satisfy_the_subnet_check(
    db_session: AsyncSession,
) -> None:
    """Pinning one handset says nothing about the segment. Counting pins here
    would let a single pinned phone mark a whole floor compliant."""
    subnet = await _voice_subnet(db_session)
    erl = await _erl(db_session)
    db_session.add(ERLBinding(erl_id=erl.id, rule_kind="mac", mac_address="aa:bb:cc:dd:ee:01"))
    await db_session.flush()
    out = await _check(db_session, "e911_voice_subnet_unbound", subnet, "subnet")
    assert out.status == STATUS_FAIL


@pytest.mark.asyncio
async def test_a_non_voice_subnet_is_not_applicable(db_session: AsyncSession) -> None:
    subnet = await _voice_subnet(db_session, role="data")
    out = await _check(db_session, "e911_voice_subnet_unbound", subnet, "subnet")
    assert out.status == STATUS_NOT_APPLICABLE


# ══════════════════════════════════════════════════════════════════════
# e911_erl_validated
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_an_unbound_erl_is_not_applicable(db_session: AsyncSession) -> None:
    """An address nothing points at dispatches nobody. Failing on drafts
    would bury the ERLs actually in use."""
    await _erl(db_session)
    out = await _check(db_session, "e911_erl_validated")
    assert out.status == STATUS_NOT_APPLICABLE


@pytest.mark.asyncio
async def test_an_in_use_never_validated_erl_fails(db_session: AsyncSession) -> None:
    subnet = await _voice_subnet(db_session)
    erl = await _erl(db_session)
    db_session.add(ERLBinding(erl_id=erl.id, rule_kind="subnet", subnet_id=subnet.id))
    await db_session.flush()
    out = await _check(db_session, "e911_erl_validated")
    assert out.status == STATUS_FAIL
    assert "never been validated" in out.detail


@pytest.mark.asyncio
async def test_a_rejected_verdict_fails_harder_than_a_missing_one(
    db_session: AsyncSession,
) -> None:
    """Somebody checked and the answer was no — a stronger signal than an
    address nobody has asked about, and it must not be hidden behind the
    'never validated' message."""
    subnet = await _voice_subnet(db_session)
    rejected = await _erl(db_session, validated=("rejected", _now()), name="rejected-erl")
    never = await _erl(db_session, name="never-erl")
    db_session.add(ERLBinding(erl_id=rejected.id, rule_kind="subnet", subnet_id=subnet.id))
    db_session.add(ERLBinding(erl_id=never.id, rule_kind="site_default", site_id=subnet.site_id))
    await db_session.flush()
    out = await _check(db_session, "e911_erl_validated")
    assert out.status == STATUS_FAIL
    assert "REJECTED" in out.detail
    assert out.diagnostic["rejected"] == ["rejected-erl"]


@pytest.mark.asyncio
async def test_a_validated_erl_passes(db_session: AsyncSession) -> None:
    subnet = await _voice_subnet(db_session)
    erl = await _erl(db_session, validated=("validated", _now()))
    db_session.add(ERLBinding(erl_id=erl.id, rule_kind="subnet", subnet_id=subnet.id))
    await db_session.flush()
    out = await _check(db_session, "e911_erl_validated")
    assert out.status == STATUS_PASS


@pytest.mark.asyncio
async def test_an_old_verdict_warns_rather_than_fails(db_session: AsyncSession) -> None:
    """The address is almost certainly still right; the re-validation cadence
    is the operator's policy, not ours."""
    subnet = await _voice_subnet(db_session)
    erl = await _erl(db_session, validated=("validated", _now() - timedelta(days=400)))
    db_session.add(ERLBinding(erl_id=erl.id, rule_kind="subnet", subnet_id=subnet.id))
    await db_session.flush()
    out = await _check(db_session, "e911_erl_validated")
    assert out.status == STATUS_WARN
    assert out.diagnostic["max_age_days"] == 365


@pytest.mark.asyncio
async def test_the_max_age_is_configurable(db_session: AsyncSession) -> None:
    subnet = await _voice_subnet(db_session)
    erl = await _erl(db_session, validated=("validated", _now() - timedelta(days=40)))
    db_session.add(ERLBinding(erl_id=erl.id, rule_kind="subnet", subnet_id=subnet.id))
    await db_session.flush()
    assert (await _check(db_session, "e911_erl_validated")).status == STATUS_PASS
    tight = await _check(db_session, "e911_erl_validated", args={"max_age_days": 30})
    assert tight.status == STATUS_WARN


# ══════════════════════════════════════════════════════════════════════
# e911_port_binding_evidence_fresh
# ══════════════════════════════════════════════════════════════════════


async def _switch_with_port_binding(
    db: AsyncSession, *, last_poll_at, poll_status="ok", is_active=True
):
    space = IPSpace(name=f"e911p-{uuid.uuid4().hex[:8]}")
    db.add(space)
    await db.flush()
    device = NetworkDevice(
        name=f"sw-{uuid.uuid4().hex[:6]}",
        hostname="10.91.0.1",
        ip_address=f"10.91.{uuid.uuid4().int % 250}.1",
        snmp_version="v2c",
        ip_space_id=space.id,
        community_encrypted=encrypt_str("public"),
        poll_interval_seconds=300,
        last_poll_at=last_poll_at,
        last_poll_status=poll_status,
        is_active=is_active,
    )
    db.add(device)
    await db.flush()
    iface = NetworkInterface(device_id=device.id, if_index=12, name="Gi0/12")
    db.add(iface)
    await db.flush()
    erl = await _erl(db)
    db.add(ERLBinding(erl_id=erl.id, rule_kind="switch_port", network_interface_id=iface.id))
    await db.flush()
    return device


@pytest.mark.asyncio
async def test_no_port_bindings_is_not_applicable(db_session: AsyncSession) -> None:
    out = await _check(db_session, "e911_port_binding_evidence_fresh")
    assert out.status == STATUS_NOT_APPLICABLE


@pytest.mark.asyncio
async def test_a_freshly_polled_switch_passes(db_session: AsyncSession) -> None:
    await _switch_with_port_binding(db_session, last_poll_at=_now() - timedelta(seconds=60))
    out = await _check(db_session, "e911_port_binding_evidence_fresh")
    assert out.status == STATUS_PASS


@pytest.mark.asyncio
async def test_a_switch_that_stopped_polling_fails(db_session: AsyncSession) -> None:
    """The resolver is already degrading every lookup for these ports. This
    is what makes that visible instead of silent."""
    await _switch_with_port_binding(db_session, last_poll_at=_now() - timedelta(hours=6))
    out = await _check(db_session, "e911_port_binding_evidence_fresh")
    assert out.status == STATUS_FAIL
    assert "last polled" in out.diagnostic["devices"][0]["reason"]


@pytest.mark.asyncio
async def test_a_never_polled_switch_fails(db_session: AsyncSession) -> None:
    """Keyed on the device's poll state and not on stale evidence ROWS,
    because an unpolled switch eventually has no FDB rows at all — a check
    looking for stale rows would PASS on the worst case."""
    await _switch_with_port_binding(db_session, last_poll_at=None)
    out = await _check(db_session, "e911_port_binding_evidence_fresh")
    assert out.status == STATUS_FAIL
    assert out.diagnostic["devices"][0]["reason"] == "device has never been polled"


@pytest.mark.asyncio
async def test_a_failing_poll_status_fails(db_session: AsyncSession) -> None:
    """``"error"`` was the status this test originally used and no poller
    ever writes it — the real vocabulary is pending | success | partial |
    failed | timeout. Asserting on a value that cannot occur is a test that
    proves nothing, which is how the `partial` misclassification survived."""
    await _switch_with_port_binding(
        db_session, last_poll_at=_now() - timedelta(seconds=60), poll_status="failed"
    )
    out = await _check(db_session, "e911_port_binding_evidence_fresh")
    assert out.status == STATUS_FAIL
    assert "failed" in out.diagnostic["devices"][0]["reason"]


@pytest.mark.asyncio
async def test_a_deactivated_switch_fails(db_session: AsyncSession) -> None:
    await _switch_with_port_binding(
        db_session, last_poll_at=_now() - timedelta(seconds=60), is_active=False
    )
    out = await _check(db_session, "e911_port_binding_evidence_fresh")
    assert out.status == STATUS_FAIL


# ══════════════════════════════════════════════════════════════════════
# Single-source-of-truth guards
# ══════════════════════════════════════════════════════════════════════


def test_e911_schema_covers_every_civic_element() -> None:
    """The API schema and the column set must not drift.

    ``CivicAddress`` declares its 31 fields explicitly for readability, so
    this is the guard that keeps ``CIVIC_ELEMENTS`` the single source of
    truth — promised in that class's own docstring. Adding a column without
    the field would publish an address the API cannot set; adding the field
    without the column would 500 on write.
    """
    assert set(CivicAddress.model_fields) == set(CIVIC_COLUMNS)
    model_cols = {c.name for c in EmergencyResponseLocation.__table__.columns}
    assert set(CIVIC_COLUMNS) <= model_cols


def test_every_dispatchable_detail_column_exists() -> None:
    model_cols = {c.name for c in EmergencyResponseLocation.__table__.columns}
    assert set(DISPATCHABLE_DETAIL_COLUMNS) <= model_cols
    # Must be a strict subset of the civic elements: these are the RAY
    # BAUM'S "room number, floor number, or similar", not a separate idea.
    assert set(DISPATCHABLE_DETAIL_COLUMNS) < set(CIVIC_COLUMNS)


def test_every_rule_kind_has_a_target_column_and_a_precedence_slot() -> None:
    """Three constants describe the binding rules and all three must agree,
    or a rule becomes unreachable: the resolver walks ERL_RULE_PRECEDENCE,
    the API validator reads ERL_RULE_TARGET_COLUMN, and the DB CHECK
    enumerates the kinds."""
    assert set(ERL_RULE_PRECEDENCE) == set(ERL_RULE_TARGET_COLUMN)
    binding_cols = {c.name for c in ERLBinding.__table__.columns}
    assert set(ERL_RULE_TARGET_COLUMN.values()) <= binding_cols
    check = next(
        c for c in ERLBinding.__table__.constraints if c.name == "ck_erl_binding_rule_kind"
    )
    for kind in ERL_RULE_PRECEDENCE:
        assert f"'{kind}'" in str(check.sqltext), kind


def test_the_three_checks_are_catalogued_and_seeded() -> None:
    """A check with no catalogue entry is invisible in the policy picker; a
    policy with no seeded row never runs on an existing install."""
    names = {
        "e911_voice_subnet_unbound",
        "e911_erl_validated",
        "e911_port_binding_evidence_fresh",
    }
    assert names <= set(CHECK_REGISTRY)
    assert names <= {e["name"] for e in CHECK_CATALOG}
    assert names <= {p["check_kind"] for p in BUILTIN_POLICIES}
    seeded = [p for p in BUILTIN_POLICIES if p["check_kind"] in names]
    # The regulatory citation is the point of these three: they are the
    # first policies in the tree that are not `framework: custom`.
    for policy in seeded:
        assert policy["framework"] == "RAY BAUM'S Act"
        assert policy["reference"] == "47 CFR 9.16(b)"


# ══════════════════════════════════════════════════════════════════════
# Regressions from /code-review
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_switch_collecting_neither_fdb_nor_lldp_fails(
    db_session: AsyncSession,
) -> None:
    """The hole the first version left open, and precisely the silent
    degradation the check claims to catch: a switch polled perfectly on
    schedule but collecting NEITHER the forwarding table nor LLDP can never
    produce port evidence, so every lookup for its ports degrades forever
    while the device reports a healthy poll."""
    device = await _switch_with_port_binding(
        db_session, last_poll_at=_now() - timedelta(seconds=30)
    )
    device.poll_fdb = False
    device.poll_lldp = False
    await db_session.flush()
    out = await _check(db_session, "e911_port_binding_evidence_fresh")
    assert out.status == STATUS_FAIL
    assert "no port evidence can exist" in out.diagnostic["devices"][0]["reason"]


@pytest.mark.asyncio
async def test_either_fdb_or_lldp_alone_is_enough(db_session: AsyncSession) -> None:
    """Control: the resolver needs one of the two, not both. Demanding both
    would fail every LLDP-only or FDB-only switch for no reason."""
    device = await _switch_with_port_binding(
        db_session, last_poll_at=_now() - timedelta(seconds=30)
    )
    device.poll_fdb = False
    device.poll_lldp = True
    await db_session.flush()
    assert (await _check(db_session, "e911_port_binding_evidence_fresh")).status == STATUS_PASS


@pytest.mark.asyncio
async def test_a_partial_poll_is_not_reported_as_unpolled(
    db_session: AsyncSession,
) -> None:
    """The real vocabulary is pending | success | partial | failed | timeout.
    The first version tested ``not in ("ok", "success")`` — "ok" is not a
    value any poller writes — so a ``partial`` poll, where perhaps only the
    unrelated IGMP leg failed, was reported as not polled at all."""
    await _switch_with_port_binding(
        db_session, last_poll_at=_now() - timedelta(seconds=30), poll_status="partial"
    )
    assert (await _check(db_session, "e911_port_binding_evidence_fresh")).status == STATUS_PASS


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_status", ["failed", "timeout"])
async def test_a_genuinely_failed_poll_still_fails(
    db_session: AsyncSession, bad_status: str
) -> None:
    await _switch_with_port_binding(
        db_session, last_poll_at=_now() - timedelta(seconds=30), poll_status=bad_status
    )
    out = await _check(db_session, "e911_port_binding_evidence_fresh")
    assert out.status == STATUS_FAIL
    assert bad_status in out.diagnostic["devices"][0]["reason"]


def test_network_editor_can_manage_locations() -> None:
    """Every sibling vertical registry (bacnet_device / dicom_ae / ot_device)
    is granted to the builtin Network Editor. Leaving e911_location out made
    writes undocumented superadmin-only, with the UI quietly hiding its own
    buttons for the role that exists to do this work."""
    from app.main import _BUILTIN_ROLES

    _description, perms = _BUILTIN_ROLES["Network Editor"]
    resources = {p["resource_type"] for p in perms}
    assert "e911_location" in resources
    # Anchored against a sibling so a restructure of the role breaks this
    # test rather than silently dropping the grant.
    assert "dicom_ae" in resources
