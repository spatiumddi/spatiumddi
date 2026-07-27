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
from app.services.dns_threat.dga import bigram_implausibility, score_dga
from app.services.dns_threat.scoring import (
    DEFAULT_BENIGN_PARENTS,
    MIN_QUERIES_TO_SCORE,
    extract_features,
    is_benign_parent,
    score_tunneling,
    shannon_entropy,
    split_qname,
)
from app.services.logs.bind9_parser import parse_query_line, parse_rpz_line


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


# ── DGA detection (issue #699) ────────────────────────────────────────
#
# Same philosophy as the tunneling tests above: pin the things that
# would make the detection *wrong*, not the exact numbers. The one
# extra concern here is that DGA and tunneling are structurally
# opposite (one sprays across parents, the other concentrates under
# one), so the tests that matter most are the ones proving each does
# NOT fire on the other's traffic.


def _dga_labels(n: int = 60, tld: str = "com", length: int = 12) -> dict[str, str]:
    """A DGA crop: many distinct fixed-width random-alphabet domains.

    Uses the FULL a-z alphabet, not consonants only. That distinction
    caught a real calibration bug: a consonants-only fixture measures
    0.80 bigram implausibility where a genuine Conficker/Necurs-shaped
    label measures 0.60, and the original thresholds were set against
    the fixture — so the scorer passed its tests while being unable to
    reach its own alerting threshold on real traffic.

    Deterministic rather than random so a failure is reproducible — a
    flaky security detector is one nobody trusts.
    """
    import random

    rng = random.Random(1337)
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    out: dict[str, str] = {}
    while len(out) < n:
        label = "".join(rng.choice(alphabet) for _ in range(length))
        out[f"{label}.{tld}"] = label
    return out


def _human_labels(n: int = 60) -> dict[str, str]:
    """Ordinary registrable domains — word-shaped and varied in length."""
    words = [
        "northwind",
        "contoso",
        "example",
        "warehouse",
        "printing",
        "logistics",
        "greenfield",
        "marketing",
        "supplier",
        "riverside",
        "bookstore",
        "hardware",
        "acme",
        "stationery",
        "mailchimp",
        "atlassian",
        "cloudflare",
        "wikipedia",
        "shopping",
        "newsletter",
    ]
    out: dict[str, str] = {}
    i = 0
    while len(out) < n:
        base = f"{words[i % len(words)]}{'' if i < len(words) else i}"
        out[f"{base}.com"] = base
        i += 1
    return out


def test_bigram_implausibility_separates_gibberish_from_words() -> None:
    assert bigram_implausibility("newsletter") < 0.2
    assert bigram_implausibility("xkqjfhwbzvmp") > 0.8


def test_a_real_dga_crop_clears_the_alerting_threshold() -> None:
    """The calibration guard. The first cut of these thresholds put
    _NGRAM_FLOOR (0.55) ABOVE where a random a-z label actually measures
    (0.60) and _NGRAM_CEIL at an unreachable 0.92, so a genuine crop
    scored ~64 against an alert bar of 80 — the detection could not fire
    on the thing it exists to detect, while its tests passed."""
    from app.services.dns_threat.aggregate import DGA_ALERTING_SCORE

    assert score_dga(_dga_labels(250)).score >= DGA_ALERTING_SCORE


def test_busy_but_innocent_client_does_not_score() -> None:
    """Fan-out alone must not score. A build server, a mail gateway or a
    downstream forwarder collapsing a whole office onto one client_ip
    legitimately touches hundreds of parents an hour; when the fan-out
    weight was additive such a client scored 36/100 with ZERO implausible
    names and outranked genuine smaller crops."""
    assert score_dga(_human_labels(300)).score < 10.0


def test_bigram_implausibility_ignores_digits() -> None:
    """``o365`` and friends are legitimate; digits must not read as
    gibberish or every Microsoft-shaped domain scores."""
    assert bigram_implausibility("office365") < 0.5


def test_dga_crop_outranks_ordinary_domains() -> None:
    """The ordering is the contract; absolute numbers will be retuned."""
    crop = score_dga(_dga_labels(120))
    human = score_dga(_human_labels(120))
    assert crop.score > human.score
    assert human.score < 10.0, "ordinary browsing must sit near zero"


def test_few_domains_never_score() -> None:
    """A handful of odd domains is a person clicking a link, not a crop.

    This is the guard that keeps one hashed CDN bucket from firing the
    detection.
    """
    verdict = score_dga(dict(list(_dga_labels().items())[:5]))
    assert verdict.score == 0.0
    assert verdict.signals[0].name == "insufficient_domains"


def test_short_labels_do_not_dilute_a_real_crop() -> None:
    """Three-letter domains are unjudgeable, not innocent.

    Averaging them in as 0.0 implausibility was the obvious
    implementation and it let a crop hide behind a pile of short
    legitimate domains.
    """
    crop = _dga_labels(120)
    padded = {**crop, **{f"a{i}.com": f"a{i}" for i in range(200)}}
    assert score_dga(padded).score == pytest.approx(score_dga(crop).score, abs=1.0)


def test_tunneling_traffic_does_not_score_as_dga() -> None:
    """The structural discriminator, and the whole reason DGA is a
    separate scorer: a tunnel is thousands of subdomains under ONE
    parent, so it presents exactly one registrable domain here."""
    feats = extract_features(_tunnel_rows(400))
    assert score_dga(feats.parent_labels).score == 0.0


def test_dga_traffic_does_not_score_as_tunneling() -> None:
    """...and the converse. A crop fans out across parents, so the
    tunneling scorer's subdomain-fan-out signal never lights up."""
    rows = [(parent, "A", "server-1") for parent in _dga_labels(120)]
    verdict = score_tunneling(extract_features(rows))
    assert verdict.score < 20.0


def test_dga_score_is_bounded() -> None:
    assert 0.0 <= score_dga(_dga_labels(500)).score <= 100.0


def test_min_parents_zero_does_not_crash_the_rollup() -> None:
    """A caller lowering the gate must not reach fmean() on an empty
    set — StatisticsError would propagate out of the aggregator's flush
    and abort the hourly rollup for EVERY client, not just this one."""
    assert score_dga({}, min_parents=0).score == 0.0


def test_dga_evidence_names_the_domains() -> None:
    """A bare score is not actionable — hashed-CDN traffic shares the
    shape, so an operator has to see WHICH domains scored."""
    verdict = score_dga(_dga_labels(120))
    assert verdict.candidates_json(), "evidence must carry the domains"
    assert len(verdict.candidates_json()) <= 10, "evidence is capped for triage"
    assert verdict.detail
    assert all("parent" in c and "label" in c for c in verdict.candidates_json())


def test_dga_signals_carry_their_own_ceiling() -> None:
    """Same wire shape as tunnel_signals so one UI component renders
    both detections' evidence."""
    for sig in score_dga(_dga_labels(120)).signals_json():
        assert set(sig) >= {"name", "value", "contribution", "max_contribution", "detail"}
        assert sig["contribution"] <= sig["max_contribution"] + 1e-9


@pytest.mark.asyncio
async def test_aggregate_persists_dga_verdict(db_session: AsyncSession) -> None:
    """End-to-end through the rollup: a crop in the query log lands as a
    scored, evidenced row."""
    server = await _make_dns_server(db_session)
    bucket = floor_hour(datetime.now(UTC))
    rows = [(parent, "A", str(server.id)) for parent in _dga_labels(120)]
    await _seed_queries(db_session, server.id, rows, ts=bucket, client="10.0.0.44")
    await aggregate_window(db_session, window_start=bucket)

    win = (
        await db_session.execute(
            select(DNSClientWindow).where(DNSClientWindow.client_ip == "10.0.0.44")
        )
    ).scalar_one()
    assert win.dga_score > 0.0
    assert win.dga_candidates, "the crop must be persisted as evidence"
    assert win.dga_signals
    assert win.dga_detail


# ── RPZ hit attribution (issue #699) ──────────────────────────────────
#
# Pure reporting rather than a heuristic, so unlike the detections above
# these tests DO pin exact numbers — a hit is ground truth and an
# off-by-one in attribution is a real bug, not a tuning question.

_RPZ_LINE = (
    "27-Jul-2026 22:15:03.123 rpz: info: client @0x7f8 192.0.2.5#54321 "
    "(ads.tracker.example): view default: rpz QNAME NXDOMAIN rewrite "
    "ads.tracker.example via ads.tracker.example.spatium-blocklist.rpz"
)
_QUERY_LINE = (
    "27-Jul-2026 22:15:04.001 queries: info: client @0x7f8 192.0.2.5#54321 "
    "(www.example.com): query: www.example.com IN A +E(0)K (10.0.0.1)"
)


def test_rpz_line_parses_client_name_policy_and_feed() -> None:
    hit = parse_rpz_line(_RPZ_LINE)
    assert hit is not None
    assert hit.client_ip == "192.0.2.5"
    assert hit.qname == "ads.tracker.example"
    assert hit.trigger == "QNAME"
    assert hit.policy == "NXDOMAIN"
    # Attribution to the FEED is the point — "blocked by the malware
    # list" and "blocked by the ad list" are different findings.
    assert hit.rpz_zone == "spatium-blocklist.rpz"


def test_rpz_parser_returns_none_for_a_query_line() -> None:
    """The discriminator the ingest relies on to tell the two shapes
    apart on a shared channel."""
    assert parse_rpz_line(_QUERY_LINE) is None


def test_query_parser_still_handles_query_lines_after_rpz_category_added() -> None:
    """_CAT_SEV_RE grew an ``rpz`` alternative; the queries path must be
    untouched by it."""
    parsed = parse_query_line(_QUERY_LINE)
    assert parsed is not None
    assert parsed.client_ip == "192.0.2.5"
    assert parsed.qname == "www.example.com"


def test_rpz_drop_policy_parses() -> None:
    """DROP logs ``rewrite``/``via`` like every other policy on BIND
    9.20 — an earlier fixture here asserted a bare-action shape named
    never emits, so the test passed against invented input."""
    line = (
        "27-Jul-2026 22:15:03.123 rpz: info: client @0x7f8 192.0.2.9#5 "
        "(bad.example): view default: rpz QNAME DROP rewrite bad.example/A/IN "
        "via bad.example.spatium-blocklist.rpz"
    )
    hit = parse_rpz_line(line)
    assert hit is not None
    assert hit.policy == "DROP"
    assert hit.rpz_zone == "spatium-blocklist.rpz"


def test_rewrite_operand_strips_qtype_and_qclass() -> None:
    """BIND logs ``rewrite bad.example/A/IN``. Storing the raw operand
    put ``bad.example/a/in`` in the qname — a name matching no real
    domain, which would group as its own row in the blocked-names
    report and render that way to operators."""
    line = (
        "27-Jul-2026 22:15:03.123 rpz: info: rpz QNAME NXDOMAIN "
        "rewrite evil.example/AAAA/IN via evil.example.spatium-blocklist.rpz"
    )
    hit = parse_rpz_line(line)
    assert hit is not None
    assert hit.qname == "evil.example"


def test_tcp_only_policy_uses_the_underscore_log_form() -> None:
    """``tcp-only`` is the named.conf keyword; the LOG string is
    ``TCP_ONLY``. Matching only the hyphen silently dropped every hit
    from the most aggressive rate-limiting policy."""
    line = (
        "27-Jul-2026 22:15:03.123 rpz: info: rpz QNAME TCP_ONLY "
        "rewrite x.example/A/IN via x.example.spatium-blocklist.rpz"
    )
    hit = parse_rpz_line(line)
    assert hit is not None
    assert hit.policy == "TCP_ONLY"


def test_log_only_dry_run_hits_are_not_counted_as_blocks() -> None:
    """named prefixes ``disabled `` when a policy zone runs with
    ``policy disabled`` — the standard way to trial a new feed. Nothing
    was blocked, so counting it would fabricate hits for an operator who
    is explicitly evaluating rather than enforcing."""
    line = (
        "27-Jul-2026 22:15:03.123 rpz: info: client @0x7f8 192.0.2.9#5 "
        "(x.example): view default: disabled rpz QNAME NXDOMAIN rewrite "
        "x.example/A/IN via x.example.spatium-blocklist.rpz"
    )
    assert parse_rpz_line(line) is None


def test_passthru_category_prefix_is_stripped() -> None:
    """PASSTHRU rides its own ``rpz-passthru`` category. If the prefix
    is not stripped, ``_HEAD_RE``'s ``^client`` anchor fails and the hit
    lands unattributable — which is what the offender report filters
    out, so the allow would be invisible twice over."""
    line = (
        "27-Jul-2026 22:15:03.123 rpz-passthru: info: client @0x7f8 "
        "192.0.2.7#5 (ok.example): view default: rpz QNAME PASSTHRU "
        "rewrite ok.example/A/IN via ok.example.spatium-blocklist.rpz"
    )
    hit = parse_rpz_line(line)
    assert hit is not None
    assert hit.policy == "PASSTHRU"
    assert hit.client_ip == "192.0.2.7"
    assert hit.qname == "ok.example"


def test_rpz_zone_is_bounded_to_the_column_width() -> None:
    """An unbounded ``via`` fallback could exceed String(255); the ingest
    commits the whole batch in one statement, so one overlong value
    would abort every query-log row in the same POST."""
    long_name = ".".join(["a" * 60] * 5)
    line = (
        f"27-Jul-2026 22:15:03.123 rpz: info: rpz QNAME NXDOMAIN "
        f"rewrite x/A/IN via {long_name}.blocklist.local"
    )
    hit = parse_rpz_line(line)
    assert hit is not None
    assert hit.rpz_zone is not None
    assert len(hit.rpz_zone) <= 255


@pytest.mark.asyncio
async def test_rpz_attribution_ranks_clients_and_excludes_passthru(
    db_session: AsyncSession,
) -> None:
    """The headline question: which machine keeps hitting the blocklist.

    PASSTHRU is an explicit ALLOW, so counting it would both inflate
    every client and make a correctly-tuned allowlist look like an
    infection.
    """
    from app.models.dns_rpz_hit import DNSRPZHit
    from app.services.dns_threat import rpz as rpz_service

    server = await _make_dns_server(db_session)
    now = datetime.now(UTC)
    for i in range(30):
        db_session.add(
            DNSRPZHit(
                server_id=server.id,
                ts=now - timedelta(minutes=i),
                client_ip="10.0.0.99",
                qname="malware.example",
                trigger="QNAME",
                policy="NXDOMAIN",
                rpz_zone="spatium-blocklist.rpz",
                raw="x",
            )
        )
    db_session.add(
        DNSRPZHit(
            server_id=server.id,
            ts=now,
            client_ip="10.0.0.1",
            qname="allowed.example",
            trigger="QNAME",
            policy="PASSTHRU",
            rpz_zone="rpz-allow.rpz",
            raw="x",
        )
    )
    await db_session.commit()

    clients = await rpz_service.top_offending_clients(db_session)
    assert [c["client_ip"] for c in clients] == [
        "10.0.0.99"
    ], "PASSTHRU is an allow, not a block — it must not create an offender"
    assert clients[0]["hits"] == 30
    assert clients[0]["top_qname"] == "malware.example"

    summary = await rpz_service.summary(db_session)
    assert summary["blocked_hits"] == 30
    assert summary["passthru_hits"] == 1
    assert summary["has_data"] is True


@pytest.mark.asyncio
async def test_rpz_summary_distinguishes_no_data_from_no_threats(
    db_session: AsyncSession,
) -> None:
    """An idle feature rendering as an all-clear is the failure this
    whole area exists to avoid — and here zero blocks is a plausible
    real answer, so the UI must not have to guess."""
    from app.services.dns_threat import rpz as rpz_service

    empty = await rpz_service.summary(db_session)
    assert empty["has_data"] is False
    assert empty["blocked_hits"] == 0


@pytest.mark.asyncio
async def test_rpz_hits_survive_the_query_log_retention_window(
    db_session: AsyncSession,
) -> None:
    """Hits are kept 30 days, not the query log's 24 h — "has this host
    hit the malware feed all week" is unanswerable otherwise."""
    from app.models.dns_rpz_hit import DNSRPZHit
    from app.tasks.prune_logs import _sweep_with_session

    server = await _make_dns_server(db_session)
    db_session.add(
        DNSRPZHit(
            server_id=server.id,
            ts=datetime.now(UTC) - timedelta(days=3),
            client_ip="10.0.0.7",
            qname="old.example",
            trigger="QNAME",
            policy="NXDOMAIN",
            rpz_zone="spatium-blocklist.rpz",
            raw="x",
        )
    )
    await db_session.commit()

    await _sweep_with_session(db_session)
    remaining = (
        (await db_session.execute(select(DNSRPZHit).where(DNSRPZHit.client_ip == "10.0.0.7")))
        .scalars()
        .all()
    )
    assert len(remaining) == 1, "a 3-day-old hit must outlive the 24 h query-log sweep"
