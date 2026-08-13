"""#734 — the control plane could never read a zone off agent-managed BIND9.

The #61 drift report and sync-with-servers both AXFR the live zone. The
agent grants ``allow-transfer`` to the group's TSIG **key**, never to a
source address, and the control plane transferred unsigned — so both
features failed with REFUSED on every zone of the flagship deployment,
100% of the time, for two releases.

Verified live against the dev BIND9 on a stock ``allow_transfer: ["none"]``
group while writing these: unsigned → ``REFUSED``; signed with the group
key → the zone came back. These tests pin the two halves that made that
work — picking a key the agent actually granted, and refusing to guess when
there isn't one.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_str
from app.drivers.dns import AXFR_TSIG_DRIVERS, register_driver
from app.drivers.dns.base import RecordData
from app.models.dns import DNSServer, DNSServerGroup, DNSTSIGKey, DNSZone
from app.services.dns.drift import compute_zone_drift
from app.services.dns.tsig import resolve_group_transfer_key, transfer_needs_tsig

_B64 = "c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0MDE="


async def _group(db: AsyncSession, **kw: Any) -> DNSServerGroup:
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:8]}", **kw)
    db.add(grp)
    await db.flush()
    return grp


# ── Which key gets used ─────────────────────────────────────────────────────


async def test_legacy_group_key_is_preferred(db_session: AsyncSession) -> None:
    """The auto-generated group key exists on every agent-managed group
    without operator action, so drift has to work out of the box rather
    than only after someone thinks to create a named key."""
    grp = await _group(
        db_session,
        tsig_key_name="spatium-default",
        tsig_key_secret=_B64,
        tsig_key_algorithm="hmac-sha512",
    )
    db_session.add(
        DNSTSIGKey(
            group_id=grp.id,
            name="aaa-operator-key",
            algorithm="hmac-sha256",
            secret_encrypted=encrypt_str(_B64),
        )
    )
    await db_session.flush()

    key = await resolve_group_transfer_key(db_session, grp.id)
    assert key is not None
    assert key.name == "spatium-default"
    # The group's own algorithm, not the default: signing with the wrong
    # algorithm fails as PeerBadKey, which reads like a permissions problem.
    assert key.algorithm == "hmac-sha512"


async def test_falls_back_to_first_operator_key_by_name(db_session: AsyncSession) -> None:
    """Without a legacy key, use the head of the same list the bundle ships.

    ``build_agent_bundle`` orders operator keys by name, and the agent grants
    every key in that list, so picking the first by name keeps both ends in
    agreement by construction.
    """
    grp = await _group(db_session)
    for name in ("zzz-last", "aaa-first"):
        db_session.add(
            DNSTSIGKey(
                group_id=grp.id,
                name=name,
                algorithm="hmac-sha256",
                secret_encrypted=encrypt_str(_B64),
            )
        )
    await db_session.flush()

    key = await resolve_group_transfer_key(db_session, grp.id)
    assert key is not None
    assert key.name == "aaa-first"


async def test_no_key_at_all_returns_none(db_session: AsyncSession) -> None:
    grp = await _group(db_session)
    assert await resolve_group_transfer_key(db_session, grp.id) is None


async def test_undecryptable_key_is_skipped_not_fatal(db_session: AsyncSession) -> None:
    """A row whose secret won't decrypt is also skipped from the bundle, so
    the agent never granted it — skip it here too and try the next one,
    rather than failing the whole report over one bad row."""
    grp = await _group(db_session)
    db_session.add(
        DNSTSIGKey(
            group_id=grp.id,
            name="aaa-broken",
            algorithm="hmac-sha256",
            secret_encrypted=b"not-a-valid-fernet-token",
        )
    )
    db_session.add(
        DNSTSIGKey(
            group_id=grp.id,
            name="bbb-good",
            algorithm="hmac-sha256",
            secret_encrypted=encrypt_str(_B64),
        )
    )
    await db_session.flush()

    key = await resolve_group_transfer_key(db_session, grp.id)
    assert key is not None
    assert key.name == "bbb-good"


async def test_partial_legacy_key_is_not_used(db_session: AsyncSession) -> None:
    """A name with no secret can't sign anything. Half a key is no key."""
    grp = await _group(db_session, tsig_key_name="spatium-default", tsig_key_secret=None)
    assert await resolve_group_transfer_key(db_session, grp.id) is None


# ── What the drift report does with it ──────────────────────────────────────


class _RecordingDriver:
    """Captures the ``tsig`` kwarg the drift service hands the driver."""

    seen: list[Any] = []

    async def pull_zone_records(
        self, server: Any, zone_name: str, *, tsig: Any = None
    ) -> list[RecordData]:
        type(self).seen.append(tsig)
        return []


async def _zone_with_server(
    db_session: AsyncSession,
    grp: DNSServerGroup,
    driver: str,
    *,
    agent_managed: bool = True,
) -> DNSZone:
    db_session.add(
        DNSServer(
            group_id=grp.id,
            name=f"srv-{uuid.uuid4().hex[:6]}",
            host="192.0.2.10",
            driver=driver,
            is_primary=True,
            # Set only by ``POST /dns/agents/register`` — the marker that an
            # agent actually rendered this server's named.conf, and therefore
            # granted the key-gated allow-transfer.
            agent_id=uuid.uuid4() if agent_managed else None,
        )
    )
    zone = DNSZone(group_id=grp.id, name="example.com.", zone_type="primary")
    db_session.add(zone)
    await db_session.flush()
    return zone


@pytest.fixture
def recording_driver() -> type[_RecordingDriver]:
    _RecordingDriver.seen = []
    register_driver("bind9", _RecordingDriver)  # type: ignore[arg-type]
    register_driver("windows_dns", _RecordingDriver)  # type: ignore[arg-type]
    yield _RecordingDriver
    # Restore the real drivers so later tests in the session aren't affected.
    from app.drivers.dns.bind9 import BIND9Driver
    from app.drivers.dns.windows import WindowsDNSDriver

    register_driver("bind9", BIND9Driver)
    register_driver("windows_dns", WindowsDNSDriver)


async def test_drift_signs_for_an_agent_managed_server(
    db_session: AsyncSession, recording_driver: type[_RecordingDriver]
) -> None:
    """The fix, end to end through the service: bind9 gets the group key."""
    grp = await _group(db_session, tsig_key_name="spatium-default", tsig_key_secret=_B64)
    zone = await _zone_with_server(db_session, grp, "bind9")

    report = await compute_zone_drift(db_session, group_id=grp.id, zone=zone)

    assert [s.status for s in report.servers] == ["ok"]
    assert recording_driver.seen[0] is not None
    assert recording_driver.seen[0].name == "spatium-default"


async def test_drift_does_not_sign_for_windows(
    db_session: AsyncSession, recording_driver: type[_RecordingDriver]
) -> None:
    """Windows authorises transfers by address and knows nothing of our
    group key. Handing it one turns a WORKING unsigned pull into BADKEY —
    a regression introduced by the fix, in a driver the fix isn't about."""
    grp = await _group(db_session, tsig_key_name="spatium-default", tsig_key_secret=_B64)
    zone = await _zone_with_server(db_session, grp, "windows_dns")

    await compute_zone_drift(db_session, group_id=grp.id, zone=zone)

    assert recording_driver.seen == [None]


async def test_drift_fails_closed_and_names_the_missing_key(
    db_session: AsyncSession, recording_driver: type[_RecordingDriver]
) -> None:
    """No key means the transfer cannot succeed. Say which thing is missing.

    The generic REFUSED error points at allow-transfer and the firewall —
    neither is the problem, and neither is reachable anyway, because the
    agent owns named.conf. That dead end is what made this issue's symptom
    unactionable for the operator.
    """
    grp = await _group(db_session)
    zone = await _zone_with_server(db_session, grp, "bind9")

    report = await compute_zone_drift(db_session, group_id=grp.id, zone=zone)

    (entry,) = report.servers
    assert entry.status == "unsupported"
    assert entry.error is not None
    assert "TSIG key" in entry.error
    assert "allow-transfer" not in entry.error
    # And it must not have attempted a transfer that could only fail.
    assert recording_driver.seen == []


def test_windows_is_deliberately_not_a_tsig_driver() -> None:
    """Pin the taxonomy itself — adding windows_dns here would silently
    break every working Windows Path A pull."""
    assert AXFR_TSIG_DRIVERS == {"bind9", "technitium"}


# ── Agent-managed vs operator-run ───────────────────────────────────────────
#
# The driver name alone does NOT decide this. A ``bind9`` row can be an
# operator's own BIND9 that SpatiumDDI never deployed to — pointed at by
# host, authorised the ordinary way by address. That server has no group key
# and never granted one, so signing its transfer turns a working unsigned
# pull into NOTAUTH, and refusing to pull for want of a key breaks it a
# different way. ``agent_id`` separates the two.


def _srv(driver: str, agent_id: uuid.UUID | None) -> DNSServer:
    return DNSServer(name="s", host="192.0.2.10", driver=driver, agent_id=agent_id)


@pytest.mark.parametrize(
    ("driver", "agent_managed", "expected"),
    [
        ("bind9", True, True),
        ("technitium", True, True),
        # The regression this guards: an operator-run BIND9 must keep its
        # unsigned, address-authorised pull.
        ("bind9", False, False),
        ("technitium", False, False),
        # Windows authorises by address even when agent-adjacent.
        ("windows_dns", True, False),
        ("cloudflare", False, False),
    ],
)
def test_only_agent_managed_axfr_drivers_need_signing(
    driver: str, agent_managed: bool, expected: bool
) -> None:
    server = _srv(driver, uuid.uuid4() if agent_managed else None)
    assert transfer_needs_tsig(server) is expected


async def test_drift_leaves_an_operator_run_bind9_unsigned(
    db_session: AsyncSession, recording_driver: type[_RecordingDriver]
) -> None:
    """End to end: a keyed group must not sign for a server no agent owns."""
    grp = await _group(db_session, tsig_key_name="spatium-default", tsig_key_secret=_B64)
    zone = await _zone_with_server(db_session, grp, "bind9", agent_managed=False)

    report = await compute_zone_drift(db_session, group_id=grp.id, zone=zone)

    assert [s.status for s in report.servers] == ["ok"]
    assert recording_driver.seen == [None]


async def test_operator_run_bind9_is_not_blocked_by_a_missing_key(
    db_session: AsyncSession, recording_driver: type[_RecordingDriver]
) -> None:
    """A keyless group is fine here — this server never needed a key. Failing
    closed would be the fix breaking a pull that worked before it."""
    grp = await _group(db_session)
    zone = await _zone_with_server(db_session, grp, "bind9", agent_managed=False)

    report = await compute_zone_drift(db_session, group_id=grp.id, zone=zone)

    assert [s.status for s in report.servers] == ["ok"]
    assert recording_driver.seen == [None]
