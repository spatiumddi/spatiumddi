"""DNSBL / RBL reputation monitoring (issue #528).

Unit tests over the sweep engine (reversed-octet query construction,
candidate-set derivation, listing-state persistence + latch) plus the
alert latch/auto-resolve through evaluate_all. DNS is mocked — no live
blocklist queries.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import AlertEvent, AlertRule
from app.models.dnsbl import (
    SOURCE_INTERNET_FACING,
    SOURCE_IPAM,
    SOURCE_NAT_EGRESS,
    SOURCE_PINNED,
    DNSBLList,
    DNSBLListing,
    DNSBLPinnedIP,
)
from app.models.ipam import IPAddress, IPBlock, IPSpace, NATMapping, Subnet
from app.services import alerts as alerts_svc
from app.services.dnsbl import sweep

pytestmark = pytest.mark.asyncio


# ── Pure helpers ───────────────────────────────────────────────────────


async def test_reversed_octets() -> None:
    assert sweep.reversed_octets("1.2.3.4") == "4.3.2.1"
    assert sweep.reversed_octets("203.0.113.5") == "5.113.0.203"


async def test_dnsbl_query_name() -> None:
    assert sweep.dnsbl_query_name("1.2.3.4", "zen.spamhaus.org") == "4.3.2.1.zen.spamhaus.org"
    # Trailing dot on the suffix is stripped.
    assert sweep.dnsbl_query_name("1.2.3.4", "bl.spamcop.net.") == "4.3.2.1.bl.spamcop.net"


async def test_is_ipv4() -> None:
    assert sweep.is_ipv4("8.8.8.8") is True
    assert sweep.is_ipv4("2001:db8::1") is False
    assert sweep.is_ipv4("not-an-ip") is False


# ── Fixtures / factories ───────────────────────────────────────────────


async def _list(
    db: AsyncSession, suffix: str = "zen.spamhaus.org", enabled: bool = True
) -> DNSBLList:
    row = DNSBLList(
        name=f"list-{suffix}",
        zone_suffix=suffix,
        enabled=enabled,
        return_codes={"127.0.0.2": "spam"},
    )
    db.add(row)
    await db.flush()
    return row


async def _subnet(db: AsyncSession, *, internet_facing: bool = False) -> Subnet:
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="0.0.0.0/0", name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network=f"203.0.{uuid.uuid4().int % 250}.0/24",
        name="sn",
        total_ips=254,
        internet_facing=internet_facing,
    )
    db.add(subnet)
    await db.flush()
    return subnet


# ── Candidate-set derivation ───────────────────────────────────────────


async def test_derive_candidates_public_vs_private(db_session: AsyncSession) -> None:
    subnet = await _subnet(db_session)
    db_session.add(IPAddress(subnet_id=subnet.id, address="8.8.8.8", status="allocated"))
    db_session.add(IPAddress(subnet_id=subnet.id, address="10.1.2.3", status="allocated"))
    await db_session.flush()

    cands = await sweep.derive_candidates(db_session)
    assert "8.8.8.8" in cands
    assert cands["8.8.8.8"] == SOURCE_IPAM
    # RFC1918 private is skipped.
    assert "10.1.2.3" not in cands


async def test_derive_candidates_skips_ipv6(db_session: AsyncSession) -> None:
    subnet = await _subnet(db_session)
    db_session.add(IPAddress(subnet_id=subnet.id, address="2001:db8::5", status="allocated"))
    await db_session.flush()
    cands = await sweep.derive_candidates(db_session)
    assert "2001:db8::5" not in cands


async def test_derive_candidates_internet_facing(db_session: AsyncSession) -> None:
    subnet = await _subnet(db_session, internet_facing=True)
    db_session.add(IPAddress(subnet_id=subnet.id, address="9.9.9.9", status="allocated"))
    await db_session.flush()
    cands = await sweep.derive_candidates(db_session)
    # internet_facing has higher precedence than plain ipam.
    assert cands.get("9.9.9.9") == SOURCE_INTERNET_FACING


async def test_derive_candidates_nat_egress(db_session: AsyncSession) -> None:
    subnet = await _subnet(db_session)
    nat = NATMapping(
        name="nat-hide",
        kind="hide",
        internal_subnet_id=subnet.id,
        external_ip="4.2.2.2",
    )
    db_session.add(nat)
    await db_session.flush()
    cands = await sweep.derive_candidates(db_session)
    assert cands.get("4.2.2.2") == SOURCE_NAT_EGRESS


async def test_derive_candidates_pinned(db_session: AsyncSession) -> None:
    db_session.add(DNSBLPinnedIP(ip="1.0.0.1", note="mail relay"))
    await db_session.flush()
    cands = await sweep.derive_candidates(db_session)
    assert cands.get("1.0.0.1") == SOURCE_PINNED


async def test_derive_candidates_source_precedence(db_session: AsyncSession) -> None:
    """Same IP from ipam + pinned -> pinned wins (highest precedence)."""
    subnet = await _subnet(db_session)
    db_session.add(IPAddress(subnet_id=subnet.id, address="208.67.222.222", status="allocated"))
    db_session.add(DNSBLPinnedIP(ip="208.67.222.222", note="pin"))
    await db_session.flush()
    cands = await sweep.derive_candidates(db_session)
    assert cands.get("208.67.222.222") == SOURCE_PINNED


# ── check_one parsing (mocked resolver) ────────────────────────────────


class _FakeRR:
    def __init__(self, text: str, strings: list[bytes] | None = None) -> None:
        self._text = text
        self.strings = strings

    def __str__(self) -> str:
        return self._text


class _FakeAnswer:
    def __init__(self, rrs: list[_FakeRR]) -> None:
        self._rrs = rrs
        self.rrset = rrs if rrs else None

    def __iter__(self):
        return iter(self._rrs)


def _patch_resolver(monkeypatch, *, a_rrs, txt_rrs=None, raise_exc=None) -> None:
    import dns.asyncresolver

    class _FakeResolver:
        def __init__(self, *a, **k) -> None:
            self.nameservers: list[str] = []
            self.timeout = 0.0
            self.lifetime = 0.0

        async def resolve(self, qname, rdtype, raise_on_no_answer=False):
            if raise_exc is not None:
                raise raise_exc
            if rdtype == "TXT":
                return _FakeAnswer(txt_rrs or [])
            return _FakeAnswer(a_rrs)

    monkeypatch.setattr(dns.asyncresolver, "Resolver", _FakeResolver)


async def test_check_one_listed(monkeypatch) -> None:
    _patch_resolver(
        monkeypatch,
        a_rrs=[_FakeRR("127.0.0.2")],
        txt_rrs=[_FakeRR("", strings=[b"https://delist.example/1.2.3.4"])],
    )
    lst = DNSBLList(name="x", zone_suffix="zen.spamhaus.org")
    res = await sweep.check_one("1.2.3.4", lst)
    assert res.listed is True
    assert res.return_codes == ["127.0.0.2"]
    assert res.txt_reason == "https://delist.example/1.2.3.4"
    assert res.error is None


async def test_check_one_not_listed_nxdomain(monkeypatch) -> None:
    import dns.resolver

    _patch_resolver(monkeypatch, a_rrs=[], raise_exc=dns.resolver.NXDOMAIN())
    lst = DNSBLList(name="x", zone_suffix="zen.spamhaus.org")
    res = await sweep.check_one("1.2.3.4", lst)
    assert res.listed is False
    assert res.error is None


async def test_check_one_timeout_is_error(monkeypatch) -> None:
    import dns.exception

    _patch_resolver(monkeypatch, a_rrs=[], raise_exc=dns.exception.Timeout())
    lst = DNSBLList(name="x", zone_suffix="zen.spamhaus.org")
    res = await sweep.check_one("1.2.3.4", lst)
    assert res.listed is False
    assert res.error is not None


# ── Listing persistence + latch ────────────────────────────────────────


async def _row(db: AsyncSession, ip: str, list_id) -> DNSBLListing | None:
    return await db.scalar(
        select(DNSBLListing).where(DNSBLListing.ip == ip, DNSBLListing.list_id == list_id)
    )


async def test_run_sweep_persists_listing(db_session: AsyncSession, monkeypatch) -> None:
    lst = await _list(db_session)
    subnet = await _subnet(db_session)
    db_session.add(IPAddress(subnet_id=subnet.id, address="8.8.4.4", status="allocated"))
    await db_session.flush()

    async def _fake_check(ip, list_row, resolvers=None, resolver=None):
        return sweep.CheckResult(listed=True, return_codes=["127.0.0.2"], txt_reason="spam")

    monkeypatch.setattr(sweep, "check_one", _fake_check)
    counters = await sweep.run_sweep(db_session)
    assert counters["listed"] >= 1

    row = await _row(db_session, "8.8.4.4", lst.id)
    assert row is not None
    assert row.listed is True
    assert row.first_listed_at is not None
    assert row.resolved_at is None
    assert row.return_codes == ["127.0.0.2"]


async def test_run_sweep_delist_resolves(db_session: AsyncSession, monkeypatch) -> None:
    lst = await _list(db_session)
    subnet = await _subnet(db_session)
    db_session.add(IPAddress(subnet_id=subnet.id, address="1.1.1.1", status="allocated"))
    await db_session.flush()

    async def _listed(ip, list_row, resolvers=None, resolver=None):
        return sweep.CheckResult(listed=True, return_codes=["127.0.0.2"])

    async def _clean(ip, list_row, resolvers=None, resolver=None):
        return sweep.CheckResult(listed=False)

    monkeypatch.setattr(sweep, "check_one", _listed)
    await sweep.run_sweep(db_session)
    row = await _row(db_session, "1.1.1.1", lst.id)
    assert row is not None and row.listed is True
    first_listed = row.first_listed_at

    monkeypatch.setattr(sweep, "check_one", _clean)
    await sweep.run_sweep(db_session)
    await db_session.refresh(row)
    assert row.listed is False
    assert row.resolved_at is not None
    # first_listed_at is preserved for history.
    assert row.first_listed_at == first_listed


async def test_check_error_preserves_prior_state(db_session: AsyncSession, monkeypatch) -> None:
    lst = await _list(db_session)
    subnet = await _subnet(db_session)
    db_session.add(IPAddress(subnet_id=subnet.id, address="216.58.192.1", status="allocated"))
    await db_session.flush()

    async def _listed(ip, list_row, resolvers=None, resolver=None):
        return sweep.CheckResult(listed=True, return_codes=["127.0.0.2"])

    async def _err(ip, list_row, resolvers=None, resolver=None):
        return sweep.CheckResult(error="resolver error: Timeout")

    monkeypatch.setattr(sweep, "check_one", _listed)
    await sweep.run_sweep(db_session)
    row = await _row(db_session, "216.58.192.1", lst.id)
    assert row is not None and row.listed is True

    # A transient error must NOT flip the listing off (would spuriously
    # auto-resolve the alert).
    monkeypatch.setattr(sweep, "check_one", _err)
    await sweep.run_sweep(db_session)
    await db_session.refresh(row)
    assert row.listed is True
    assert row.resolved_at is None
    assert row.check_error == "resolver error: Timeout"


# ── De-scoped / disabled-list reconciliation (finding #9) ──────────────


async def test_run_sweep_resolves_when_ip_leaves_candidates(
    db_session: AsyncSession, monkeypatch
) -> None:
    """An IP listed then removed from the candidate set auto-resolves on the
    next sweep — without ever being re-queried (it's no longer a candidate)."""
    lst = await _list(db_session)
    subnet = await _subnet(db_session)
    addr = IPAddress(subnet_id=subnet.id, address="8.8.8.8", status="allocated")
    db_session.add(addr)
    await db_session.flush()

    async def _listed(ip, list_row, resolvers=None, resolver=None):
        return sweep.CheckResult(listed=True, return_codes=["127.0.0.2"])

    monkeypatch.setattr(sweep, "check_one", _listed)
    await sweep.run_sweep(db_session)
    row = await _row(db_session, "8.8.8.8", lst.id)
    assert row is not None and row.listed is True

    # Drop the IP from the candidate set (delete its IPAM row).
    await db_session.delete(addr)
    await db_session.flush()

    # A de-scoped IP must NOT be re-checked — the reconcile pass resolves it.
    async def _boom(ip, list_row, resolvers=None, resolver=None):
        raise AssertionError("de-scoped IP must not be re-checked")

    monkeypatch.setattr(sweep, "check_one", _boom)
    counters = await sweep.run_sweep(db_session)
    await db_session.refresh(row)
    assert row.listed is False
    assert row.resolved_at is not None
    assert counters["resolved"] >= 1


async def test_run_sweep_resolves_when_list_disabled(db_session: AsyncSession, monkeypatch) -> None:
    """Disabling a list resolves the alerts it was holding open, even though
    the IP is still a monitored candidate (list is no longer swept)."""
    lst = await _list(db_session)
    subnet = await _subnet(db_session)
    db_session.add(IPAddress(subnet_id=subnet.id, address="8.8.4.4", status="allocated"))
    await db_session.flush()

    async def _listed(ip, list_row, resolvers=None, resolver=None):
        return sweep.CheckResult(listed=True, return_codes=["127.0.0.2"])

    monkeypatch.setattr(sweep, "check_one", _listed)
    await sweep.run_sweep(db_session)
    row = await _row(db_session, "8.8.4.4", lst.id)
    assert row is not None and row.listed is True

    # Disable the (only) list — no enabled lists remain, so the reconcile must
    # still run and resolve the open latch.
    lst.enabled = False
    await db_session.flush()
    counters = await sweep.run_sweep(db_session)
    await db_session.refresh(row)
    assert row.listed is False
    assert row.resolved_at is not None
    assert counters["resolved"] >= 1


async def test_run_sweep_still_listed_candidate_stays_open(
    db_session: AsyncSession, monkeypatch
) -> None:
    """A still-listed, still-candidate IP on an enabled list stays open across
    sweeps — the reconcile pass must not touch it."""
    lst = await _list(db_session)
    subnet = await _subnet(db_session)
    db_session.add(IPAddress(subnet_id=subnet.id, address="9.9.9.9", status="allocated"))
    await db_session.flush()

    async def _listed(ip, list_row, resolvers=None, resolver=None):
        return sweep.CheckResult(listed=True, return_codes=["127.0.0.2"])

    monkeypatch.setattr(sweep, "check_one", _listed)
    await sweep.run_sweep(db_session)
    row = await _row(db_session, "9.9.9.9", lst.id)
    assert row is not None and row.listed is True
    first_listed = row.first_listed_at

    # Second sweep — still a candidate, still listed → latch stays open.
    await sweep.run_sweep(db_session)
    await db_session.refresh(row)
    assert row.listed is True
    assert row.resolved_at is None
    assert row.first_listed_at == first_listed


# ── Alert latch / auto-resolve ─────────────────────────────────────────


async def test_alert_latch_and_auto_resolve(db_session: AsyncSession) -> None:
    lst = await _list(db_session)
    rule = AlertRule(
        name="IP on DNS blocklist",
        rule_type=alerts_svc.RULE_TYPE_IP_BLOCKLISTED,
        severity="warning",
        enabled=True,
    )
    db_session.add(rule)
    listing = DNSBLListing(
        ip="64.6.64.6",
        list_id=lst.id,
        listed=True,
        source=SOURCE_IPAM,
        first_listed_at=datetime.now(UTC),
    )
    db_session.add(listing)
    await db_session.flush()

    # First evaluation opens exactly one event for the listed IP.
    await alerts_svc.evaluate_all(db_session)
    events = (
        (await db_session.execute(select(AlertEvent).where(AlertEvent.rule_id == rule.id)))
        .scalars()
        .all()
    )
    open_events = [e for e in events if e.resolved_at is None]
    assert len(open_events) == 1
    assert open_events[0].subject_id == "64.6.64.6"

    # Re-evaluate while still listed — no duplicate event.
    await alerts_svc.evaluate_all(db_session)
    open_events = (
        (
            await db_session.execute(
                select(AlertEvent).where(
                    AlertEvent.rule_id == rule.id, AlertEvent.resolved_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(open_events) == 1

    # Delist -> the subject drops out of the match set -> auto-resolve.
    listing.listed = False
    listing.resolved_at = datetime.now(UTC)
    await db_session.flush()
    await alerts_svc.evaluate_all(db_session)
    open_events = (
        (
            await db_session.execute(
                select(AlertEvent).where(
                    AlertEvent.rule_id == rule.id, AlertEvent.resolved_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(open_events) == 0
