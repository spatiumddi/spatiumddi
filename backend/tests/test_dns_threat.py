"""DNS tunneling analytics (issue #699).

The detection is a heuristic, so the tests pin the things that would
make it *wrong* rather than the exact numbers it produces — thresholds
will be retuned against real traffic and shouldn't have to drag a test
suite behind them.

What matters, in order of how badly it breaks trust if it regresses:

* **False positives on benign traffic.** SpatiumDDI's own DNSBL sweep
  is the sharpest case: it queries ``<ip>.zen.spamhaus.org``, which is
  high fan-out and high entropy under one parent by construction. A
  detector whose first finding is the platform itself gets ignored, and
  an ignored detector is worse than none.
* **False negatives from hiding.** The allowlist must not become cover:
  a tunnel mixed into allowlisted traffic still has to score.
* **Ordering.** Tunnels must outrank ordinary traffic, whatever the
  absolute numbers are.
* **The rollup contract** — idempotent upsert, correct bucketing.
* **"No data" never reading as "no threats"**, in every surface.
"""

from __future__ import annotations

import base64
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dns import DNSServer, DNSServerGroup
from app.models.dns_threat import DNSClientWindow
from app.models.logs import DNSQueryLogEntry
from app.services.dns_threat.aggregate import aggregate_window, floor_hour
from app.services.dns_threat.scoring import (
    DEFAULT_BENIGN_PARENTS,
    MIN_QUERIES_TO_SCORE,
    extract_features,
    is_benign_parent,
    score_tunneling,
    shannon_entropy,
    split_qname,
)


def _tunnel_rows(n: int = 300, parent: str = "t.evil.example") -> list[tuple]:
    """iodine-shaped: long base32 labels, unique per query, TXT-heavy."""
    rows = []
    for _ in range(n):
        payload = base64.b32encode(os.urandom(35)).decode().rstrip("=").lower()[:60]
        rows.append((f"{payload}.{parent}", "TXT", "server-1"))
    return rows


def _ordinary_rows(n: int = 300) -> list[tuple]:
    hosts = ["www", "mail", "intranet", "vpn", "wiki", "printer1"]
    return [(f"{hosts[i % len(hosts)]}.corp.example.com", "A", "server-1") for i in range(n)]


def _dnsbl_rows(n: int = 300) -> list[tuple]:
    """What SpatiumDDI's own reputation sweep looks like on the wire."""
    return [(f"{i}.2.0.192.zen.spamhaus.org", "A", "server-1") for i in range(n)]


# ── scoring primitives ────────────────────────────────────────────────


def test_entropy_separates_encoded_payload_from_words() -> None:
    # A repeated character carries no information at all, whatever its
    # length — which is why entropy is measured per character and the
    # length signal is scored separately rather than folded in.
    assert shannon_entropy("aaaaaaaa") == 0.0
    assert shannon_entropy("abcdefgh") > shannon_entropy("aaaaaaab")
    encoded = base64.b32encode(os.urandom(30)).decode().rstrip("=").lower()
    assert shannon_entropy(encoded) > shannon_entropy("intranet")


def test_split_qname_returns_every_subdomain_label() -> None:
    """All labels above the parent, not just the leftmost.

    dnscat2 and dnsteal chunk payload across several labels; scoring
    only ``labels[0]`` let them evade the length and entropy signals
    entirely while still fanning out.
    """
    assert split_qname("a.b.example.co.uk") == (["a", "b"], "example.co.uk")
    assert split_qname("x.example.com") == (["x"], "example.com")
    assert split_qname("single.com") == ([], "single.com")


def test_allowlist_matches_full_name_not_derived_parent() -> None:
    """The bug that made the platform flag its own DNSBL sweep.

    Allowlist entries are routinely deeper than the two-label parent
    (`zen.spamhaus.org`, `blob.core.windows.net`); matching the derived
    parent would never hit them.
    """
    assert is_benign_parent("1.2.0.192.zen.spamhaus.org", DEFAULT_BENIGN_PARENTS)
    assert is_benign_parent("abc.mycontainer.blob.core.windows.net", DEFAULT_BENIGN_PARENTS)
    assert not is_benign_parent("payload.t.evil.example", DEFAULT_BENIGN_PARENTS)


# ── the failure modes that matter ─────────────────────────────────────


def test_our_own_dnsbl_sweep_does_not_score() -> None:
    feats = extract_features(_dnsbl_rows())
    verdict = score_tunneling(feats)
    assert feats.allowlisted is True
    assert verdict.score == 0.0


def test_ordinary_traffic_scores_zero() -> None:
    assert score_tunneling(extract_features(_ordinary_rows())).score == 0.0


def test_tunnel_outranks_ordinary_traffic() -> None:
    """The ordering property, not the absolute numbers — thresholds get
    retuned against real traffic and shouldn't drag tests with them."""
    tunnel = score_tunneling(extract_features(_tunnel_rows())).score
    ordinary = score_tunneling(extract_features(_ordinary_rows())).score
    assert tunnel > ordinary
    assert tunnel >= 60, "an iodine-shaped tunnel must clear the default alert threshold"


def test_allowlist_is_not_a_hiding_place() -> None:
    """A tunnel mixed into allowlisted traffic must still score.

    Otherwise an attacker who also generates DNSBL-shaped noise gets a
    free pass, and the allowlist becomes an attack surface.
    """
    mixed = _tunnel_rows(200) + _dnsbl_rows(400)
    feats = extract_features(mixed)
    assert feats.allowlisted is False
    assert score_tunneling(feats).score >= 60


def test_small_samples_do_not_score() -> None:
    """Ratios are meaningless on a handful of queries; a single long
    TXT lookup must not read as an exfil tunnel."""
    feats = extract_features(_tunnel_rows(MIN_QUERIES_TO_SCORE - 1))
    verdict = score_tunneling(feats)
    assert verdict.score == 0.0
    assert verdict.signals[0].name == "insufficient_data"


def test_every_signal_is_reported_even_at_zero() -> None:
    """The UI shows what was *ruled out*, not only what fired."""
    verdict = score_tunneling(extract_features(_tunnel_rows()))
    names = {s.name for s in verdict.signals}
    assert names == {
        "max_label_length",
        "label_entropy",
        "subdomain_fanout",
        "payload_qtypes",
    }
    assert all(isinstance(s.detail, str) and s.detail for s in verdict.signals)


def test_score_is_bounded() -> None:
    """Weights sum to 100; nothing may exceed it however extreme."""
    extreme = [(f"{'z' * 63}.{i}.evil.example", "NULL", "s") for i in range(5000)]
    assert score_tunneling(extract_features(extreme)).score <= 100.0


def test_multi_label_payload_is_not_evaded() -> None:
    """Payload split across labels must still register on length."""
    name = "_acme-challenge.some-really-long-generated-hostname-123456.corp.example.com"
    feats = extract_features([(name, "TXT", "s")] * 40)
    assert feats.max_label_length == 42, "must measure the longest SUBDOMAIN label"


def test_benign_padding_cannot_dilute_the_signal() -> None:
    """Ratios divide by scoreable queries, not the raw total.

    Otherwise an attacker suppresses their own payload-qtype ratio for
    free by emitting DNSBL-shaped noise alongside the tunnel.
    """
    tunnel = _tunnel_rows(200)
    alone = extract_features(tunnel)
    padded = extract_features(tunnel + _dnsbl_rows(400))
    assert alone.payload_qtype_ratio == padded.payload_qtype_ratio == 1.0
    assert score_tunneling(alone).score == score_tunneling(padded).score


def test_minimum_sample_gate_counts_only_scoreable_queries() -> None:
    """24 real queries padded with 1000 benign ones is still 24 samples."""
    rows = _tunnel_rows(MIN_QUERIES_TO_SCORE - 1) + _dnsbl_rows(1000)
    verdict = score_tunneling(extract_features(rows))
    assert verdict.score == 0.0
    assert verdict.signals[0].name == "insufficient_data"


def test_unparseable_queries_never_read_as_cleared() -> None:
    """A query-log parser regression must not render as a clean bill of
    health — every surface hides allowlisted rows by default."""
    feats = extract_features([(None, "A", "s")] * 100)
    assert feats.allowlisted is False, "unparseable is a data problem, not a clearance"
    verdict = score_tunneling(feats)
    assert verdict.score == 0.0
    assert verdict.signals[0].name == "unparseable_queries"


def test_kubernetes_service_discovery_is_allowlisted() -> None:
    """SpatiumDDI's own appliance is a k3s cluster; without this the
    platform flags its own nodes."""
    rows = [(f"pod-{i}-abc123def456.svc.cluster.local", "A", "s") for i in range(900)]
    feats = extract_features(rows)
    assert feats.allowlisted is True
    assert score_tunneling(feats).score == 0.0


def test_signals_carry_their_own_ceiling() -> None:
    """The UI draws each bar against its signal's maximum, and weights
    differ (30/25/30/15)."""
    verdict = score_tunneling(extract_features(_tunnel_rows()))
    for sig in verdict.signals:
        assert sig.max_contribution > 0
        assert sig.contribution <= sig.max_contribution + 1e-9
    assert sum(s.max_contribution for s in verdict.signals) == 100.0


# ── rollup ────────────────────────────────────────────────────────────


async def _make_user(db: AsyncSession, *, username: str) -> tuple[Any, str]:
    from app.core.security import create_access_token, hash_password
    from app.models.auth import User

    user = User(
        username=username,
        email=f"{username}@example.test",
        display_name=username,
        hashed_password=hash_password("x"),
        auth_source="local",
        is_superadmin=True,
    )
    user.groups = []  # mark loaded — is_effective_superadmin walks .groups (#351)
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_dns_server(db: AsyncSession) -> DNSServer:
    g = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:8]}", description="")
    db.add(g)
    await db.flush()
    s = DNSServer(
        name=f"s-{uuid.uuid4().hex[:8]}",
        host="127.0.0.1",
        port=53,
        driver="bind9",
        group_id=g.id,
        is_primary=True,
        is_enabled=True,
    )
    db.add(s)
    await db.flush()
    return s


async def _seed_queries(
    db: AsyncSession, server_id, rows: list[tuple], *, ts: datetime, client: str
) -> None:
    for qname, qtype, _srv in rows:
        db.add(
            DNSQueryLogEntry(
                server_id=server_id,
                ts=ts,
                client_ip=client,
                qname=qname,
                qtype=qtype,
                raw="",
            )
        )
    await db.flush()


@pytest.mark.asyncio
async def test_aggregate_writes_one_row_per_client(db_session: AsyncSession) -> None:
    server = await _make_dns_server(db_session)
    bucket = floor_hour(datetime.now(UTC))
    await _seed_queries(db_session, server.id, _tunnel_rows(120), ts=bucket, client="10.0.0.9")
    await _seed_queries(db_session, server.id, _ordinary_rows(60), ts=bucket, client="10.0.0.10")

    written = await aggregate_window(db_session, window_start=bucket)
    assert written == 2

    rows = (
        (await db_session.execute(select(DNSClientWindow).order_by(DNSClientWindow.client_ip)))
        .scalars()
        .all()
    )
    by_ip = {str(r.client_ip): r for r in rows}
    assert by_ip["10.0.0.9"].tunnel_score > by_ip["10.0.0.10"].tunnel_score
    assert by_ip["10.0.0.9"].top_parent == "evil.example"
    assert by_ip["10.0.0.10"].tunnel_score == 0.0


@pytest.mark.asyncio
async def test_aggregate_is_idempotent(db_session: AsyncSession) -> None:
    """Re-running a bucket updates in place rather than duplicating —
    the aggregator recomputes recent buckets on every tick to pick up
    late-arriving log lines."""
    server = await _make_dns_server(db_session)
    bucket = floor_hour(datetime.now(UTC))
    await _seed_queries(db_session, server.id, _tunnel_rows(120), ts=bucket, client="10.0.0.9")

    await aggregate_window(db_session, window_start=bucket)
    await aggregate_window(db_session, window_start=bucket)

    rows = (await db_session.execute(select(DNSClientWindow))).scalars().all()
    assert len(rows) == 1, "second pass must upsert, not insert"


@pytest.mark.asyncio
async def test_aggregate_ignores_queries_outside_the_bucket(
    db_session: AsyncSession,
) -> None:
    server = await _make_dns_server(db_session)
    bucket = floor_hour(datetime.now(UTC))
    await _seed_queries(
        db_session,
        server.id,
        _tunnel_rows(120),
        ts=bucket - timedelta(hours=3),
        client="10.0.0.9",
    )
    written = await aggregate_window(db_session, window_start=bucket)
    assert written == 0


# ── mute (operator triage) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_muted_client_does_not_alert(db_session: AsyncSession) -> None:
    """A reviewed-and-cleared host must stop paging.

    Without this the only way to silence one noisy client is disabling
    the rule, which silences every other client too.
    """
    from app.models.alerts import AlertRule
    from app.models.dns_threat_mute import DNSThreatMute
    from app.services.alerts import (
        RULE_TYPE_DNS_TUNNELING,
        _matching_dns_tunneling_subjects,
    )

    server = await _make_dns_server(db_session)
    bucket = floor_hour(datetime.now(UTC))
    await _seed_queries(db_session, server.id, _tunnel_rows(200), ts=bucket, client="10.9.9.9")
    await aggregate_window(db_session, window_start=bucket)

    rule = AlertRule(
        name="tunnel",
        rule_type=RULE_TYPE_DNS_TUNNELING,
        severity="critical",
        enabled=True,
    )
    assert [d for _, d, _ in await _matching_dns_tunneling_subjects(db_session, rule)] == [
        "10.9.9.9"
    ]

    db_session.add(DNSThreatMute(client_ip="10.9.9.9", reason="backup agent", muted_until=None))
    await db_session.flush()
    assert await _matching_dns_tunneling_subjects(db_session, rule) == []


@pytest.mark.asyncio
async def test_expired_mute_alerts_again(db_session: AsyncSession) -> None:
    """An expiring mute is a decision that ages out — the point of
    offering a dated mute at all."""
    from app.models.alerts import AlertRule
    from app.models.dns_threat_mute import DNSThreatMute
    from app.services.alerts import (
        RULE_TYPE_DNS_TUNNELING,
        _matching_dns_tunneling_subjects,
    )

    server = await _make_dns_server(db_session)
    bucket = floor_hour(datetime.now(UTC))
    await _seed_queries(db_session, server.id, _tunnel_rows(200), ts=bucket, client="10.9.9.8")
    await aggregate_window(db_session, window_start=bucket)
    db_session.add(
        DNSThreatMute(
            client_ip="10.9.9.8",
            reason="temporary",
            muted_until=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await db_session.flush()

    rule = AlertRule(
        name="tunnel",
        rule_type=RULE_TYPE_DNS_TUNNELING,
        severity="critical",
        enabled=True,
    )
    assert [d for _, d, _ in await _matching_dns_tunneling_subjects(db_session, rule)] == [
        "10.9.9.8"
    ], "a lapsed mute must not keep suppressing"


def test_mute_is_active_semantics() -> None:
    from app.models.dns_threat_mute import DNSThreatMute

    now = datetime.now(UTC)
    assert DNSThreatMute(client_ip="1.1.1.1", muted_until=None).is_active(now) is True
    assert (
        DNSThreatMute(client_ip="1.1.1.1", muted_until=now + timedelta(days=1)).is_active(now)
        is True
    )
    assert (
        DNSThreatMute(client_ip="1.1.1.1", muted_until=now - timedelta(days=1)).is_active(now)
        is False
    )


@pytest.mark.asyncio
async def test_mute_requires_a_reason(client: AsyncClient, db_session: AsyncSession) -> None:
    """An unexplained mute is how a real incident gets buried by someone
    tidying a dashboard."""
    from app.models.feature_module import FeatureModule

    # The whole /dns-threat prefix is module-gated and the module is
    # default-off, so without this the request 404s before validation.
    db_session.add(FeatureModule(id="security.dns_threat", enabled=True))
    _, token = await _make_user(db_session, username="mutereason")
    await db_session.flush()
    res = await client.post(
        "/api/v1/dns-threat/mutes",
        json={"client_ip": "10.0.0.5", "reason": ""},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 422


async def _enable_module(db: AsyncSession) -> None:
    """The /dns-threat prefix is module-gated and default-off."""
    from app.models.feature_module import FeatureModule

    db.add(FeatureModule(id="security.dns_threat", enabled=True))
    await db.flush()


@pytest.mark.asyncio
async def test_mute_create_then_update_is_an_upsert(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Re-muting revises the decision rather than stacking rows that
    disagree about when the mute ends."""
    from app.models.dns_threat_mute import DNSThreatMute

    await _enable_module(db_session)
    _, token = await _make_user(db_session, username="muteupsert")
    h = {"Authorization": f"Bearer {token}"}

    r1 = await client.post(
        "/api/v1/dns-threat/mutes",
        json={"client_ip": "10.0.0.5", "reason": "first pass"},
        headers=h,
    )
    assert r1.status_code == 201
    r2 = await client.post(
        "/api/v1/dns-threat/mutes",
        json={"client_ip": "10.0.0.5", "reason": "actually the backup agent"},
        headers=h,
    )
    assert r2.status_code == 201
    rows = (await db_session.execute(select(DNSThreatMute))).scalars().all()
    assert len(rows) == 1, "second mute must update, not stack"
    assert rows[0].reason == "actually the backup agent"


@pytest.mark.asyncio
async def test_mute_and_unmute_are_audited(client: AsyncClient, db_session: AsyncSession) -> None:
    """Non-negotiable #4 — muting suppresses a critical alert, so the
    decision has to be reviewable afterwards."""
    from app.models.audit import AuditLog

    await _enable_module(db_session)
    _, token = await _make_user(db_session, username="muteaudit")
    h = {"Authorization": f"Bearer {token}"}

    await client.post(
        "/api/v1/dns-threat/mutes",
        json={"client_ip": "10.0.0.6", "reason": "reviewed, benign"},
        headers=h,
    )
    await client.delete("/api/v1/dns-threat/mutes/10.0.0.6", headers=h)

    actions = (
        (
            await db_session.execute(
                select(AuditLog.action).where(AuditLog.resource_type == "dns_client")
            )
        )
        .scalars()
        .all()
    )
    assert "dns_threat_mute_create" in actions
    assert "dns_threat_mute_delete" in actions


@pytest.mark.asyncio
async def test_unmute_missing_returns_404(client: AsyncClient, db_session: AsyncSession) -> None:
    await _enable_module(db_session)
    _, token = await _make_user(db_session, username="mutemissing")
    res = await client.delete(
        "/api/v1/dns-threat/mutes/10.0.0.7",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_mute_rejects_a_hostname(client: AsyncClient, db_session: AsyncSession) -> None:
    """The column is INET; an unvalidated string is a 500, not a 422."""
    await _enable_module(db_session)
    _, token = await _make_user(db_session, username="mutehostname")
    res = await client.post(
        "/api/v1/dns-threat/mutes",
        json={"client_ip": "web-01.corp.example.com", "reason": "not an ip"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_muted_client_drops_out_of_summary_and_windows(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Muting must clear the client everywhere an operator looks, not
    only in the alert — the mute dialog promises exactly that."""
    await _enable_module(db_session)
    _, token = await _make_user(db_session, username="mutesurfaces")
    h = {"Authorization": f"Bearer {token}"}

    server = await _make_dns_server(db_session)
    bucket = floor_hour(datetime.now(UTC))
    await _seed_queries(db_session, server.id, _tunnel_rows(200), ts=bucket, client="10.0.0.8")
    await aggregate_window(db_session, window_start=bucket)

    before = (await client.get("/api/v1/dns-threat/summary", headers=h)).json()
    assert before["suspicious_clients"] >= 1
    assert before["worst_client_ip"] == "10.0.0.8"

    await client.post(
        "/api/v1/dns-threat/mutes",
        json={"client_ip": "10.0.0.8", "reason": "reviewed"},
        headers=h,
    )

    after = (await client.get("/api/v1/dns-threat/summary", headers=h)).json()
    assert after["worst_client_ip"] != "10.0.0.8"
    listed = (await client.get("/api/v1/dns-threat/windows", headers=h)).json()
    assert all(
        w["client_ip"] != "10.0.0.8" for w in listed
    ), "a muted client must not appear in the default findings list"
    # ...but stays reachable when explicitly asked for.
    shown = (await client.get("/api/v1/dns-threat/windows?include_muted=true", headers=h)).json()
    assert any(w["client_ip"] == "10.0.0.8" and w["muted"] for w in shown)
