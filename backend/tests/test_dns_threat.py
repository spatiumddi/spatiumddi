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

import pytest
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


def test_split_qname_handles_two_part_public_suffixes() -> None:
    assert split_qname("a.b.example.co.uk") == ("a", "example.co.uk")
    assert split_qname("x.example.com") == ("x", "example.com")
    assert split_qname("single.com") == ("", "single.com")


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


# ── rollup ────────────────────────────────────────────────────────────


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
