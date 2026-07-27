"""C2 beaconing detection (issue #699, second detection).

Beaconing is a timing measure, so the tests pin timing properties
rather than exact scores — the thresholds will move once this meets
real traffic.

The load-bearing one is ``test_monitoring_agent_also_scores``: a health
check IS beaconing by any timing measure, and no threshold separates
it from a callback. The design answer is to name the qname in the
evidence so an operator recognises their own infrastructure, and to
ship the alert disabled. If someone later "fixes" that false positive
by tuning it away, they will have tuned away real beacons too — the
test exists to make that trade-off explicit rather than accidental.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dns_threat import DNSClientWindow
from app.services.dns_threat.aggregate import aggregate_window, floor_hour
from app.services.dns_threat.beaconing import (
    MIN_SAMPLES,
    coefficient_of_variation,
    score_beaconing,
)
from tests.test_dns_threat import _make_dns_server, _seed_queries

_BASE = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _stamps(period: float, n: int, jitter: float = 0.0, seed: int = 3) -> list[datetime]:
    rng = random.Random(seed)
    return [_BASE + timedelta(seconds=i * period + rng.uniform(-jitter, jitter)) for i in range(n)]


def test_cv_is_zero_for_perfect_regularity() -> None:
    assert coefficient_of_variation([60.0] * 10) == 0.0


def test_cv_degenerate_input_reads_as_irregular() -> None:
    """Never 0.0 for empty/single input — a zero would read as MAXIMUM
    regularity and invert the signal."""
    assert coefficient_of_variation([]) == 1.0
    assert coefficient_of_variation([60.0]) == 1.0
    assert coefficient_of_variation([0.0, 0.0]) == 1.0


def test_metronomic_callback_scores_high() -> None:
    v = score_beaconing({"c2.evil.example": _stamps(60, 40)})
    assert v.score >= 85
    assert v.candidates[0].qname == "c2.evil.example"
    assert 59 <= v.candidates[0].period_seconds <= 61


def test_jitter_does_not_defeat_detection() -> None:
    """Real beacons jitter slightly; a couple of seconds on a 60 s
    period must not hide one."""
    assert score_beaconing({"c2.evil.example": _stamps(60, 40, jitter=2)}).score >= 80


def test_irregular_traffic_scores_zero() -> None:
    rng = random.Random(9)
    stamps = [_BASE + timedelta(seconds=s) for s in sorted(rng.sample(range(3600), 30))]
    assert score_beaconing({"news.example.com": stamps}).score == 0.0


def test_sub_second_retry_storm_is_not_a_beacon() -> None:
    """A resolver loop or retry storm repeats perfectly regularly but is
    not a callback — the period floor rejects it."""
    assert score_beaconing({"retry.example.com": _stamps(0.4, 40)}).score == 0.0


def test_too_few_samples_scores_zero() -> None:
    v = score_beaconing({"rare.example.com": _stamps(60, MIN_SAMPLES - 1)})
    assert v.score == 0.0
    assert "no name repeated" in v.detail


def test_client_scores_its_worst_name_not_an_average() -> None:
    """One metronomic callback among noisy lookups is exactly what
    should surface; averaging would bury it."""
    rng = random.Random(4)
    noisy = {
        f"host{i}.corp.example.com": [
            _BASE + timedelta(seconds=s) for s in sorted(rng.sample(range(3600), 12))
        ]
        for i in range(25)
    }
    v = score_beaconing({"c2.evil.example": _stamps(60, 40), **noisy})
    assert v.score >= 85
    assert v.candidates_json()[0]["qname"] == "c2.evil.example"


def test_monitoring_agent_also_scores() -> None:
    """A health check every 30 s IS beaconing. This is not a bug to be
    tuned away — tuning it out would tune out real callbacks too.

    The mitigations are elsewhere: the evidence names the qname so an
    operator recognises it instantly, the alert ships disabled, and the
    mute workflow exists to clear known pollers.
    """
    poller = "metrics.corp.example.com"
    v = score_beaconing({poller: _stamps(30, 60)})
    assert v.score >= 85, "monitoring genuinely scores — see the docstring"
    # Assert on the structured evidence and on the detail's leading
    # token rather than substring-matching the prose: it pins the
    # contract more precisely (the name must BE the candidate and must
    # LEAD the message, not merely appear somewhere), and a bare
    # `"host.name" in text` is the broken-host-check idiom CodeQL flags
    # as py/incomplete-url-substring-sanitization.
    assert v.candidates[0].qname == poller
    assert v.detail.split(" ", 1)[0] == poller
    assert "monitoring agents" in v.detail, "the detail must warn about this class"


def test_evidence_is_capped() -> None:
    """The blob is for triaging one finding, not exporting every
    periodic lookup a busy host makes."""
    timings = {f"beacon{i}.example.com": _stamps(60, 20, seed=i) for i in range(12)}
    assert len(score_beaconing(timings).candidates_json()) <= 5


def test_non_fqdn_names_are_not_beacons() -> None:
    """Our own BIND9 healthcheck queries `.` every 30 s from loopback
    and scored a perfect 100 the first time this ran live.

    A callback has to resolve to something the attacker controls, so it
    uses a real FQDN — the root zone and bare labels cannot be that,
    but they ARE queried metronomically by health checks and resolver
    priming. Excluded structurally: no threshold separates "every 30 s
    exactly" from "every 30 s exactly".
    """
    assert score_beaconing({".": _stamps(30, 120)}).score == 0.0
    assert score_beaconing({"localhost": _stamps(30, 120)}).score == 0.0
    # ...while a real FQDN on the same cadence still scores.
    assert score_beaconing({"c2.evil.example": _stamps(30, 120)}).score >= 85


@pytest.mark.asyncio
async def test_aggregator_persists_beacon_score(db_session: AsyncSession) -> None:
    """End-to-end through the real rollup: query-log rows in, scored
    window row out."""
    server = await _make_dns_server(db_session)
    bucket = floor_hour(datetime.now(UTC))
    rows = [("beacon.evil.example", "A", "s")] * 40
    for offset, (qname, qtype, _s) in enumerate(rows):
        await _seed_queries(
            db_session,
            server.id,
            [(qname, qtype, "s")],
            ts=bucket + timedelta(seconds=offset * 60),
            client="10.5.5.5",
        )
    await aggregate_window(db_session, window_start=bucket)

    row = (
        await db_session.execute(
            select(DNSClientWindow).where(DNSClientWindow.client_ip == "10.5.5.5")
        )
    ).scalar_one()
    assert row.beacon_score >= 85
    assert row.beacon_candidates
    assert row.beacon_candidates[0]["qname"] == "beacon.evil.example"
    # Timing and content are independent measures: a short ordinary
    # name is not a tunnel.
    assert row.tunnel_score == 0.0
