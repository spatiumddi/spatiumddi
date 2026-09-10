"""The E911 resolver: identity → dispatchable location (#972 Phase 1).

These are the tests that matter most in the feature, because the failure
mode of getting the resolver wrong is an ambulance sent to the wrong
floor. They are written against a real database rather than mocks: the
freshness rule, the precedence walk and the LLDP-disagreement signal are
all expressed as SQL, and a mocked session would prove only that the
Python around the queries runs.

Every case asserts on ``rule_matched`` / ``confidence`` /
``degraded_reason`` as well as on the ERL, because "the right address for
the wrong reason" is how this feature regresses without anybody noticing:
a resolver that returned the site default for everything would satisfy a
test that only checked the address of a device whose binding happened to
be the site default.

HOW TO RUN (serial, in the dev container — never ``make test`` on this box):
    make test-one T=tests/test_e911_resolver.py
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_str
from app.models.dhcp import DHCPLease, DHCPServer
from app.models.e911 import EmergencyResponseLocation, ERLBinding
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.network import (
    NetworkDevice,
    NetworkFdbEntry,
    NetworkInterface,
    NetworkNeighbour,
)
from app.models.ownership import Site
from app.services.e911.resolver import resolve_location

PHONE_MAC = "aa:bb:cc:11:22:33"
OTHER_MAC = "aa:bb:cc:99:88:77"
PHONE_IP = "10.20.3.44"

#: The device polls every 300 s, so the freshness window is 600 s.
POLL_INTERVAL = 300
WINDOW = POLL_INTERVAL * 2


class Fixture:
    """The estate every test works against, with knobs for each scenario."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
        self.erls: dict[str, EmergencyResponseLocation] = {}


async def _estate(db: AsyncSession) -> Fixture:
    """A site, a voice subnet, a switch, a port, and three ERLs.

    Three ERLs at three granularities is the whole point: a resolver that
    degrades has to land on a *different* one, and with a single ERL every
    assertion would pass regardless of which rule fired.
    """
    f = Fixture()

    site = Site(name="HQ", kind="office")
    db.add(site)
    await db.flush()
    f.site = site

    space = IPSpace(name=f"e911-{uuid.uuid4().hex[:8]}")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.20.0.0/16", name="hq")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network="10.20.3.0/24",
        name="voice-fl3",
        subnet_role="voice",
        site_id=site.id,
    )
    db.add(subnet)
    await db.flush()
    f.subnet = subnet

    ip = IPAddress(subnet_id=subnet.id, address=PHONE_IP, status="allocated")
    db.add(ip)
    await db.flush()
    f.ip = ip

    device = NetworkDevice(
        name="sw-fl3",
        hostname="10.20.0.1",
        ip_address="10.20.0.1",
        snmp_version="v2c",
        ip_space_id=space.id,
        community_encrypted=encrypt_str("public"),
        poll_interval_seconds=POLL_INTERVAL,
        site_id=site.id,
    )
    db.add(device)
    await db.flush()
    f.device = device

    iface = NetworkInterface(
        device_id=device.id, if_index=12, name="Gi3/0/12", alias="Bldg-A-Fl3-Rm312"
    )
    db.add(iface)
    await db.flush()
    f.iface = iface

    for key, name, detail in (
        ("room", "Bldg A — Floor 3 — Room 312", {"room": "312", "flr": "3"}),
        ("floor", "Bldg A — Floor 3", {"flr": "3"}),
        ("site", "Bldg A — front door", {}),
        ("pinned", "Bldg A — Floor 7 — Room 701", {"room": "701", "flr": "7"}),
    ):
        erl = EmergencyResponseLocation(
            name=name,
            site_id=site.id,
            country="US",
            a1="NY",
            a3="New York",
            rd="Broadway",
            hno="1234",
            bld="A",
            **detail,
        )
        db.add(erl)
        await db.flush()
        f.erls[key] = erl

    await db.flush()
    return f


async def _bind(db: AsyncSession, erl: EmergencyResponseLocation, kind: str, **target):
    b = ERLBinding(erl_id=erl.id, rule_kind=kind, **target)
    db.add(b)
    await db.flush()
    return b


async def _fdb(db: AsyncSession, f: Fixture, *, mac: str, age_seconds: int):
    e = NetworkFdbEntry(
        device_id=f.device.id,
        interface_id=f.iface.id,
        mac_address=mac,
        vlan_id=120,
        fdb_type="learned",
        first_seen=f.now - timedelta(seconds=age_seconds + 60),
        last_seen=f.now - timedelta(seconds=age_seconds),
    )
    db.add(e)
    await db.flush()
    return e


async def _lldp(
    db: AsyncSession, f: Fixture, *, chassis_id: str, age_seconds: int, port_id="Gi3/0/12"
):
    n = NetworkNeighbour(
        device_id=f.device.id,
        interface_id=f.iface.id,
        local_port_num=12,
        remote_chassis_id_subtype=4,
        remote_chassis_id=chassis_id,
        remote_port_id_subtype=5,
        remote_port_id=port_id,
        first_seen=f.now - timedelta(seconds=age_seconds + 60),
        last_seen=f.now - timedelta(seconds=age_seconds),
    )
    db.add(n)
    await db.flush()
    return n


async def _lease(db: AsyncSession, f: Fixture, *, ip=PHONE_IP, mac=PHONE_MAC):
    server = DHCPServer(
        name=f"kea-{uuid.uuid4().hex[:6]}",
        driver="kea",
        host="10.20.0.9",
    )
    db.add(server)
    await db.flush()
    lease = DHCPLease(
        server_id=server.id,
        ip_address=ip,
        mac_address=mac,
        state="active",
        last_seen_at=f.now - timedelta(seconds=30),
    )
    db.add(lease)
    await db.flush()
    return lease


# ══════════════════════════════════════════════════════════════════════
# The freshness rule — a stale precise answer is worse than a fresh
# coarse one
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_fresh_switch_port_gives_the_room(db_session: AsyncSession) -> None:
    f = await _estate(db_session)
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=60)
    await _bind(db_session, f.erls["room"], "switch_port", network_interface_id=f.iface.id)
    await _bind(db_session, f.erls["floor"], "subnet", subnet_id=f.subnet.id)

    r = await resolve_location(db_session, mac=PHONE_MAC, now=f.now)
    assert r.rule_matched == "switch_port"
    assert r.erl is not None and r.erl.room == "312"
    assert r.confidence == "observed"
    assert r.degraded_reason is None
    assert r.evidence_age_seconds == 60


@pytest.mark.asyncio
async def test_a_stale_switch_port_is_refused_and_degrades_to_the_subnet(
    db_session: AsyncSession,
) -> None:
    """THE test. The phone was re-patched on another floor; our FDB copy
    still says port 12. Returning Room 312 here sends the ambulance to the
    wrong floor, so the room-level answer is refused."""
    f = await _estate(db_session)
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=WINDOW + 1)
    await _bind(db_session, f.erls["room"], "switch_port", network_interface_id=f.iface.id)
    await _bind(db_session, f.erls["floor"], "subnet", subnet_id=f.subnet.id)

    r = await resolve_location(db_session, ip=PHONE_IP, mac=PHONE_MAC, now=f.now)
    assert r.rule_matched == "subnet"
    assert r.erl is not None and r.erl.room is None and r.erl.flr == "3"
    assert r.confidence == "degraded"
    assert r.degraded_reason is not None
    assert "switch_port" in r.degraded_reason
    assert f"{WINDOW}s freshness window" in r.degraded_reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("poll_interval", "age", "expect_rule"),
    [
        # Window is poll_interval * 2, so one 90 s-old observation is
        # stale for a 30 s poller and fresh for a 60 s one. The SAME age
        # against two intervals is what proves the window is derived
        # rather than a constant — a fixed 600 s default would call both
        # of these fresh and a fixed 60 s one would call both stale.
        (30, 90, "subnet"),
        (60, 90, "switch_port"),
    ],
)
async def test_the_window_is_two_poll_intervals_not_a_fixed_number(
    db_session: AsyncSession, poll_interval: int, age: int, expect_rule: str
) -> None:
    """One missed poll is tolerated; two is not. A fixed global window
    would be too tight for a 15-minute poller and uselessly loose for a
    60-second one.

    The first draft of this test was wrong in two ways at once and is worth
    recording: it used an age BELOW the window (45 s against 60 s, which is
    fresh) and it called the resolver with only an IP and no lease, so no
    MAC was ever resolved and the switch_port rule was skipped for want of
    an identity rather than for staleness. It passed its first assertion
    for entirely the wrong reason — the #1045 lesson, one layer up.
    """
    f = await _estate(db_session)
    f.device.poll_interval_seconds = poll_interval
    await db_session.flush()
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=age)
    await _bind(db_session, f.erls["room"], "switch_port", network_interface_id=f.iface.id)
    await _bind(db_session, f.erls["floor"], "subnet", subnet_id=f.subnet.id)

    r = await resolve_location(db_session, ip=PHONE_IP, mac=PHONE_MAC, now=f.now)
    assert r.rule_matched == expect_rule, r.degraded_reason
    if expect_rule == "subnet":
        assert f"{poll_interval * 2}s freshness window" in (r.degraded_reason or "")
        assert r.confidence == "degraded"
    else:
        assert r.confidence == "observed"


@pytest.mark.asyncio
async def test_exactly_at_the_window_is_still_fresh(db_session: AsyncSession) -> None:
    """The boundary is ``>``, not ``>=``. An off-by-one here degrades every
    answer on a device polled exactly on its interval."""
    f = await _estate(db_session)
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=WINDOW)
    await _bind(db_session, f.erls["room"], "switch_port", network_interface_id=f.iface.id)

    r = await resolve_location(db_session, mac=PHONE_MAC, now=f.now)
    assert r.rule_matched == "switch_port"
    assert r.confidence == "observed"


@pytest.mark.asyncio
async def test_an_lldp_disagreement_makes_the_fdb_row_stale_immediately(
    db_session: AsyncSession,
) -> None:
    """A different phone is plugged into that port NOW. The FDB row for the
    old one is fresh by age and wrong in fact — this is the case the age
    test cannot catch quickly, and LLDP is the device's own announcement."""
    f = await _estate(db_session)
    # The contradicting LLDP is NEWER than the FDB row — both well inside
    # the 600 s window, so this is the immediacy the age test cannot give.
    # Equal timestamps deliberately do NOT fire: that is a daisy chain, not
    # a swap (see test_a_daisy_chained_pc_still_gets_a_room).
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=20)
    await _lldp(db_session, f, chassis_id=OTHER_MAC, age_seconds=5)
    await _bind(db_session, f.erls["room"], "switch_port", network_interface_id=f.iface.id)
    await _bind(db_session, f.erls["floor"], "subnet", subnet_id=f.subnet.id)

    r = await resolve_location(db_session, ip=PHONE_IP, mac=PHONE_MAC, now=f.now)
    assert r.rule_matched == "subnet"
    assert r.confidence == "degraded"
    assert "contradicted by a MORE RECENT" in (r.degraded_reason or "")
    assert OTHER_MAC in (r.degraded_reason or "")


@pytest.mark.asyncio
async def test_an_agreeing_lldp_neighbour_does_not_degrade(
    db_session: AsyncSession,
) -> None:
    """Control for the test above. LLDP announcing the SAME MAC is
    corroboration, not contradiction — a resolver that degraded here would
    degrade every correctly-wired phone in the estate."""
    f = await _estate(db_session)
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=10)
    await _lldp(db_session, f, chassis_id=PHONE_MAC, age_seconds=10)
    await _bind(db_session, f.erls["room"], "switch_port", network_interface_id=f.iface.id)

    r = await resolve_location(db_session, mac=PHONE_MAC, now=f.now)
    assert r.rule_matched == "switch_port"
    assert r.confidence == "observed"


@pytest.mark.asyncio
async def test_lldp_is_preferred_over_the_fdb(db_session: AsyncSession) -> None:
    """The phone's own claim beats the switch's memory of a frame. Here the
    two name different ports; only the LLDP one carries a binding."""
    f = await _estate(db_session)
    other_iface = NetworkInterface(
        device_id=f.device.id, if_index=99, name="Gi3/0/99", alias="Bldg-A-Fl3-Rm399"
    )
    db_session.add(other_iface)
    await db_session.flush()
    # FDB says port 99, LLDP says port 12.
    db_session.add(
        NetworkFdbEntry(
            device_id=f.device.id,
            interface_id=other_iface.id,
            mac_address=PHONE_MAC,
            fdb_type="learned",
            first_seen=f.now - timedelta(seconds=70),
            last_seen=f.now - timedelta(seconds=10),
        )
    )
    await _lldp(db_session, f, chassis_id=PHONE_MAC, age_seconds=10)
    await _bind(db_session, f.erls["room"], "switch_port", network_interface_id=f.iface.id)
    await db_session.flush()

    r = await resolve_location(db_session, mac=PHONE_MAC, now=f.now)
    assert r.rule_matched == "switch_port"
    assert r.erl is not None and r.erl.room == "312"


# ══════════════════════════════════════════════════════════════════════
# Precedence
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_manual_pin_beats_the_subnet(db_session: AsyncSession) -> None:
    """#972 numbered the pin BELOW subnet, which would make it dead code:
    every device with a pin is also on some subnet, so a subnet rule would
    win every time and the pin could never fire. It exists for the phone on
    an unmonitored port."""
    f = await _estate(db_session)
    await _bind(db_session, f.erls["pinned"], "mac", mac_address=PHONE_MAC)
    await _bind(db_session, f.erls["floor"], "subnet", subnet_id=f.subnet.id)

    r = await resolve_location(db_session, ip=PHONE_IP, mac=PHONE_MAC, now=f.now)
    assert r.rule_matched == "mac"
    assert r.erl is not None and r.erl.room == "701"


@pytest.mark.asyncio
async def test_a_live_switch_port_beats_a_manual_pin(db_session: AsyncSession) -> None:
    """The other side of that boundary: a measured observation beats an
    operator's standing assertion, so the pin stays below switch_port."""
    f = await _estate(db_session)
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=30)
    await _bind(db_session, f.erls["room"], "switch_port", network_interface_id=f.iface.id)
    await _bind(db_session, f.erls["pinned"], "mac", mac_address=PHONE_MAC)

    r = await resolve_location(db_session, mac=PHONE_MAC, now=f.now)
    assert r.rule_matched == "switch_port"
    assert r.erl is not None and r.erl.room == "312"


@pytest.mark.asyncio
async def test_a_stale_port_degrades_to_the_pin_before_the_subnet(
    db_session: AsyncSession,
) -> None:
    """Degradation walks the precedence, it does not jump to the bottom."""
    f = await _estate(db_session)
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=WINDOW + 1)
    await _bind(db_session, f.erls["room"], "switch_port", network_interface_id=f.iface.id)
    await _bind(db_session, f.erls["pinned"], "mac", mac_address=PHONE_MAC)
    await _bind(db_session, f.erls["floor"], "subnet", subnet_id=f.subnet.id)

    r = await resolve_location(db_session, ip=PHONE_IP, mac=PHONE_MAC, now=f.now)
    assert r.rule_matched == "mac"
    assert r.confidence == "degraded"


@pytest.mark.asyncio
async def test_the_site_default_is_the_last_resort(db_session: AsyncSession) -> None:
    """What a PSAP gets when we know nothing better — the front door, which
    is still a dispatchable location and still better than nothing."""
    f = await _estate(db_session)
    await _bind(db_session, f.erls["site"], "site_default", site_id=f.site.id)

    r = await resolve_location(db_session, ip=PHONE_IP, now=f.now)
    assert r.rule_matched == "site_default"
    assert r.erl is not None and r.erl.name.endswith("front door")


@pytest.mark.asyncio
async def test_no_binding_anywhere_answers_none_not_a_guess(
    db_session: AsyncSession,
) -> None:
    f = await _estate(db_session)
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=30)

    r = await resolve_location(db_session, ip=PHONE_IP, mac=PHONE_MAC, now=f.now)
    assert r.erl is None
    assert r.rule_matched is None
    assert r.confidence == "none"
    assert r.found is False
    assert "no ERL binding matched" in (r.degraded_reason or "")


@pytest.mark.asyncio
async def test_a_binding_pointing_at_an_inactive_erl_keeps_walking(
    db_session: AsyncSession,
) -> None:
    """A coarser live answer beats a precise dead one."""
    f = await _estate(db_session)
    f.erls["room"].is_active = False
    await db_session.flush()
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=30)
    await _bind(db_session, f.erls["room"], "switch_port", network_interface_id=f.iface.id)
    await _bind(db_session, f.erls["floor"], "subnet", subnet_id=f.subnet.id)

    r = await resolve_location(db_session, ip=PHONE_IP, mac=PHONE_MAC, now=f.now)
    assert r.rule_matched == "subnet"
    assert "inactive ERL" in (r.degraded_reason or "")


@pytest.mark.asyncio
async def test_an_inactive_binding_is_ignored(db_session: AsyncSession) -> None:
    f = await _estate(db_session)
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=30)
    b = await _bind(db_session, f.erls["room"], "switch_port", network_interface_id=f.iface.id)
    b.is_active = False
    await _bind(db_session, f.erls["floor"], "subnet", subnet_id=f.subnet.id)
    await db_session.flush()

    r = await resolve_location(db_session, ip=PHONE_IP, mac=PHONE_MAC, now=f.now)
    assert r.rule_matched == "subnet"


# ══════════════════════════════════════════════════════════════════════
# Identity resolution
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_an_ip_resolves_to_a_mac_through_the_dhcp_lease(
    db_session: AsyncSession,
) -> None:
    """The caller's question is usually "where is 10.20.3.44" — the PBX
    knows the phone's IP and nothing else."""
    f = await _estate(db_session)
    await _lease(db_session, f)
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=30)
    await _bind(db_session, f.erls["room"], "switch_port", network_interface_id=f.iface.id)

    r = await resolve_location(db_session, ip=PHONE_IP, now=f.now)
    assert r.rule_matched == "switch_port"
    assert r.erl is not None and r.erl.room == "312"
    assert any(e.kind == "dhcp_lease" for e in r.evidence)


@pytest.mark.asyncio
async def test_a_mac_is_accepted_in_any_common_separator(
    db_session: AsyncSession,
) -> None:
    """Operators and PBXs spell MACs four different ways; a lookup that
    only accepts colons fails on exactly the caller we are building for."""
    f = await _estate(db_session)
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=30)
    await _bind(db_session, f.erls["room"], "switch_port", network_interface_id=f.iface.id)

    for spelling in ("AA:BB:CC:11:22:33", "aa-bb-cc-11-22-33", "aabb.cc11.2233", "aabbcc112233"):
        r = await resolve_location(db_session, mac=spelling, now=f.now)
        assert r.rule_matched == "switch_port", spelling


@pytest.mark.asyncio
async def test_a_chassis_id_that_is_not_a_mac_is_not_an_error(
    db_session: AsyncSession,
) -> None:
    """LLDP chassis-id subtype 7 is "locally assigned" — an opaque string.
    It cannot be joined against anything we hold, and that is legitimate:
    the resolver must fall through to the coarser rules rather than 500."""
    f = await _estate(db_session)
    await _bind(db_session, f.erls["site"], "site_default", site_id=f.site.id)

    r = await resolve_location(
        db_session, ip=PHONE_IP, chassis_id="switch-closet-3", port_id="1", now=f.now
    )
    assert r.rule_matched == "site_default"
    assert r.confidence in ("observed", "degraded")


@pytest.mark.asyncio
async def test_the_identity_is_reported_for_the_audit_trail(
    db_session: AsyncSession,
) -> None:
    """Every resolution is logged against the identity asked about, so the
    resolver has to say which one it understood the question to be."""
    f = await _estate(db_session)
    await _bind(db_session, f.erls["site"], "site_default", site_id=f.site.id)

    by_ip = await resolve_location(db_session, ip=PHONE_IP, now=f.now)
    assert (by_ip.identity_kind, by_ip.identity_value) == ("ip", PHONE_IP)

    by_mac = await resolve_location(db_session, mac=PHONE_MAC, now=f.now)
    assert by_mac.identity_kind == "mac"

    by_port = await resolve_location(
        db_session, chassis_id=PHONE_MAC, port_id="Gi3/0/12", now=f.now
    )
    assert by_port.identity_kind == "chassis_port"


@pytest.mark.asyncio
async def test_a_subnet_with_no_site_still_resolves_the_site_from_the_switch(
    db_session: AsyncSession,
) -> None:
    """An unsited voice subnet is common — the site is on the switch. The
    front door is still the right last-resort answer."""
    f = await _estate(db_session)
    f.subnet.site_id = None
    await db_session.flush()
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=30)
    await _bind(db_session, f.erls["site"], "site_default", site_id=f.site.id)

    r = await resolve_location(db_session, ip=PHONE_IP, mac=PHONE_MAC, now=f.now)
    assert r.rule_matched == "site_default"


@pytest.mark.asyncio
async def test_the_most_specific_subnet_wins(db_session: AsyncSession) -> None:
    """Overlapping subnets are normal in IPAM. A /28 carved out of the /24
    for a conference room is exactly the case where the coarser answer is
    the wrong floor."""
    f = await _estate(db_session)
    narrow = Subnet(
        space_id=f.subnet.space_id,
        block_id=f.subnet.block_id,
        network="10.20.3.32/28",
        name="voice-confroom",
        subnet_role="voice",
        site_id=f.site.id,
    )
    db_session.add(narrow)
    await db_session.flush()
    await _bind(db_session, f.erls["floor"], "subnet", subnet_id=f.subnet.id)
    await _bind(db_session, f.erls["room"], "subnet", subnet_id=narrow.id)

    r = await resolve_location(db_session, ip=PHONE_IP, now=f.now)
    assert r.rule_matched == "subnet"
    assert r.erl is not None and r.erl.room == "312"


# ══════════════════════════════════════════════════════════════════════
# Regressions from /code-review
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_overlapping_subnets_do_not_500_the_lookup(db_session: AsyncSession) -> None:
    """``ip_address.address`` is unique only PER SUBNET. With a /28 carved
    out of the /24 — which this suite already calls normal — an unscoped
    ``scalar_one_or_none()`` raised MultipleResultsFound and took the whole
    endpoint down *before* the audit row was written."""
    f = await _estate(db_session)
    narrow = Subnet(
        space_id=f.subnet.space_id,
        block_id=f.subnet.block_id,
        network="10.20.3.32/28",
        name="voice-confroom",
        subnet_role="voice",
        site_id=f.site.id,
    )
    db_session.add(narrow)
    await db_session.flush()
    # The SAME address in both subnets.
    db_session.add(IPAddress(subnet_id=narrow.id, address=PHONE_IP, status="allocated"))
    await db_session.flush()
    await _bind(db_session, f.erls["room"], "subnet", subnet_id=narrow.id)

    r = await resolve_location(db_session, ip=PHONE_IP, now=f.now)
    assert r.rule_matched == "subnet"
    assert r.erl is not None and r.erl.room == "312"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["10.20.3.4x", "not-an-ip", "", "10.20.3.4/24", "; DROP"])
async def test_a_malformed_ip_answers_none_rather_than_aborting(
    db_session: AsyncSession, bad: str
) -> None:
    """Unvalidated text reaching an INET comparison raises 22P02, and via
    the copilot tool the aborted transaction takes out every later tool call
    in the chat turn. A typo must be a "no location" answer."""
    f = await _estate(db_session)
    await _bind(db_session, f.erls["site"], "site_default", site_id=f.site.id)
    r = await resolve_location(db_session, ip=bad, now=f.now)
    # Nothing raised, and the identity still records what was asked.
    assert r.confidence in ("none", "observed")
    if bad:
        assert r.identity_value == bad


@pytest.mark.asyncio
async def test_a_chassis_id_alone_is_recorded_in_the_identity(
    db_session: AsyncSession,
) -> None:
    """Permitted on its own, and it used to log as ``unknown`` / "" — which
    silently lost the one thing the audit trail exists to record."""
    f = await _estate(db_session)
    await _bind(db_session, f.erls["site"], "site_default", site_id=f.site.id)
    r = await resolve_location(db_session, chassis_id=PHONE_MAC, now=f.now)
    assert r.identity_kind == "chassis_id"
    assert r.identity_value == PHONE_MAC


@pytest.mark.asyncio
async def test_a_config_rule_reports_no_evidence_age(db_session: AsyncSession) -> None:
    """A `subnet` match is as current as the moment it was saved. Reporting
    the port evidence's age beside it put an unrelated — possibly stale —
    number next to a green ``observed`` answer, in the response and in the
    log column documented as "the evidence the answer rests on"."""
    f = await _estate(db_session)
    # Port evidence exists and is old, but no switch_port binding does, so a
    # subnet rule wins on its own terms rather than by degradation.
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=WINDOW + 500)
    await _bind(db_session, f.erls["floor"], "subnet", subnet_id=f.subnet.id)

    r = await resolve_location(db_session, ip=PHONE_IP, mac=PHONE_MAC, now=f.now)
    assert r.rule_matched == "subnet"
    assert r.confidence == "observed"
    assert r.evidence_age_seconds is None
    assert r.observed_at is None
    # The observation is still REPORTED as evidence — it just does not
    # pretend to be what the answer rests on.
    assert any(e.kind == "fdb" for e in r.evidence)


@pytest.mark.asyncio
async def test_a_daisy_chained_pc_still_gets_a_room(db_session: AsyncSession) -> None:
    """A desk phone with a PC behind it is the commonest wiring in exactly
    the estates this serves: the PC's MAC is in the FDB and only the phone
    announces LLDP. Treating that as a contradiction made a room-level
    answer unreachable for every such PC, permanently."""
    f = await _estate(db_session)
    # Both current. The PC is in the FDB; the phone announces LLDP.
    await _fdb(db_session, f, mac=OTHER_MAC, age_seconds=30)
    await _lldp(db_session, f, chassis_id=PHONE_MAC, age_seconds=30)
    await _bind(db_session, f.erls["room"], "switch_port", network_interface_id=f.iface.id)

    r = await resolve_location(db_session, mac=OTHER_MAC, now=f.now)
    assert r.rule_matched == "switch_port"
    assert r.confidence == "observed"


@pytest.mark.asyncio
async def test_a_newer_lldp_neighbour_still_invalidates_the_fdb_row(
    db_session: AsyncSession,
) -> None:
    """Control for the test above, and the case the signal exists for: after
    a swap the new device's LLDP is NEWER than the old device's FDB row."""
    f = await _estate(db_session)
    await _fdb(db_session, f, mac=PHONE_MAC, age_seconds=300)
    await _lldp(db_session, f, chassis_id=OTHER_MAC, age_seconds=30)
    await _bind(db_session, f.erls["room"], "switch_port", network_interface_id=f.iface.id)
    await _bind(db_session, f.erls["floor"], "subnet", subnet_id=f.subnet.id)

    r = await resolve_location(db_session, ip=PHONE_IP, mac=PHONE_MAC, now=f.now)
    assert r.rule_matched == "subnet"
    assert r.confidence == "degraded"
    assert "MORE RECENT" in (r.degraded_reason or "")
