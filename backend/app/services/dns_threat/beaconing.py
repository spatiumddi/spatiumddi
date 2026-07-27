"""C2 beaconing detection — inter-arrival regularity (issue #699).

Malware that phones home does so on a timer. Whatever it asks for, and
however innocuous the name looks, the *rhythm* gives it away: a
callback every 60 seconds produces inter-arrival intervals that barely
vary, which almost nothing a human drives ever does.

This is a different question from tunneling, and deliberately a
separate module: tunneling asks "what do these names look like?"
(content), beaconing asks "when did they arrive?" (timing). A beacon
can use a single short, ordinary-looking name and score zero for
tunneling; a tunnel can be bursty and score zero here. Keeping them
apart means neither heuristic's thresholds drag on the other.

**The measure** is the coefficient of variation (stddev / mean) of the
intervals between one client's repeated queries for one name. Low CV =
metronomic. CV is used rather than raw stddev because it is
scale-free: a 60-second beacon jittering ±2 s and an hourly beacon
jittering ±2 min are equally suspicious, and a raw standard deviation
would rank the second far worse.

**On false positives.** Plenty of legitimate software polls on a timer,
and this catches all of it — monitoring agents, health checks, NTP-
adjacent lookups, update checkers, anything on a cron. That is why the
score is reported per (client, name) with the name attached rather than
as a bare client verdict: "10.0.0.5 queries metrics.corp.example.com
every 60 s" is instantly recognisable as benign by an operator who
would have had no idea what to make of "10.0.0.5 scored 80".
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Below this many queries for one name there aren't enough intervals to
# say anything about regularity — three samples can look metronomic by
# chance. Six queries gives five intervals, which is where the CV
# starts to mean something over an hour-long window.
MIN_SAMPLES = 6

# A beacon's intervals barely move. Anything above this is human- or
# event-driven traffic and scores nothing.
_CV_PERFECT = 0.05  # at or below → maximum regularity signal
_CV_FLOOR = 0.40  # at or above → no signal at all

# Sub-second repeats are a retry storm or a resolver loop, not a
# callback. Beyond an hour we cannot see a full period inside one
# hourly window anyway.
_MIN_PERIOD_SECONDS = 2.0
_MAX_PERIOD_SECONDS = 3600.0

# Weights sum to 100, mirroring the tunneling scorer so the two produce
# comparable numbers on the same 0-100 scale.
_W_REGULARITY = 70.0
_W_PERSISTENCE = 30.0

# Persistence: how much of the window the pattern actually covers. A
# beacon runs continuously; a burst of six identical lookups in ten
# seconds does not.
_PERSISTENCE_FLOOR = 6
_PERSISTENCE_CEIL = 60


def _ramp(value: float, floor: float, ceil: float) -> float:
    if ceil <= floor:
        return 0.0
    return max(0.0, min(1.0, (value - floor) / (ceil - floor)))


def coefficient_of_variation(intervals: list[float]) -> float:
    """stddev / mean of the intervals. 0.0 = perfectly metronomic.

    Returns 1.0 (i.e. "irregular") for degenerate input rather than
    raising or returning 0.0 — a zero here would read as *maximum*
    regularity and invert the signal.
    """
    if len(intervals) < 2:
        return 1.0
    mean = statistics.fmean(intervals)
    if mean <= 0:
        return 1.0
    return statistics.pstdev(intervals) / mean


@dataclass
class BeaconCandidate:
    """One (client, name) pair that repeated on a regular cadence."""

    qname: str
    samples: int
    period_seconds: float
    cv: float
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "qname": self.qname,
            "samples": self.samples,
            "period_seconds": round(self.period_seconds, 1),
            "cv": round(self.cv, 3),
            "score": round(self.score, 1),
        }


@dataclass
class BeaconVerdict:
    score: float = 0.0
    candidates: list[BeaconCandidate] = field(default_factory=list)
    detail: str = ""

    def candidates_json(self) -> list[dict[str, Any]]:
        # Worst first, capped: the evidence blob is for an operator
        # triaging one finding, not an export of every periodic lookup
        # a busy host makes.
        ranked = sorted(self.candidates, key=lambda c: c.score, reverse=True)
        return [c.as_dict() for c in ranked[:5]]


def score_beaconing(
    timings: dict[str, list[datetime]],
    *,
    min_samples: int = MIN_SAMPLES,
) -> BeaconVerdict:
    """Score one client's window for periodic callback behaviour.

    ``timings`` maps a query name to the timestamps that client asked
    for it during the window. The client's score is its *worst* name,
    not an average: one metronomic callback among a thousand ordinary
    lookups is exactly the thing worth surfacing, and averaging would
    bury it.
    """
    candidates: list[BeaconCandidate] = []

    for qname, stamps in timings.items():
        if not _is_beaconable_name(qname):
            continue
        if len(stamps) < min_samples:
            continue
        ordered = sorted(stamps)
        intervals = [(b - a).total_seconds() for a, b in zip(ordered, ordered[1:], strict=False)]
        intervals = [i for i in intervals if i > 0]
        if len(intervals) < min_samples - 1:
            continue

        period = statistics.fmean(intervals)
        if not (_MIN_PERIOD_SECONDS <= period <= _MAX_PERIOD_SECONDS):
            continue

        cv = coefficient_of_variation(intervals)
        # Inverted ramp: LOW variation is the signal, so a CV at or
        # below _CV_PERFECT scores 1.0 and one at _CV_FLOOR scores 0.
        regularity = 1.0 - _ramp(cv, _CV_PERFECT, _CV_FLOOR)
        if regularity <= 0:
            continue
        persistence = _ramp(len(ordered), _PERSISTENCE_FLOOR, _PERSISTENCE_CEIL)
        score = regularity * _W_REGULARITY + persistence * _W_PERSISTENCE
        candidates.append(
            BeaconCandidate(
                qname=qname,
                samples=len(ordered),
                period_seconds=period,
                cv=cv,
                score=score,
            )
        )

    if not candidates:
        return BeaconVerdict(score=0.0, detail="no name repeated on a regular cadence")

    worst = max(candidates, key=lambda c: c.score)
    return BeaconVerdict(
        score=worst.score,
        candidates=candidates,
        detail=(
            f"{worst.qname} queried {worst.samples}× at a steady "
            f"{_humanise_period(worst.period_seconds)} interval "
            f"(variation {worst.cv:.0%}) — periodic callbacks look like this; "
            f"so do monitoring agents and update checkers, so confirm what the "
            f"host runs before treating it as malicious"
        ),
    )


def _is_beaconable_name(qname: str) -> bool:
    """False for names a callback could never use.

    A beacon has to resolve to something the attacker controls, so it
    queries a real FQDN. The root zone (``.``) and bare single labels
    cannot be that — but they ARE queried on a metronomic cadence by
    health checks and resolver priming.

    This is not hypothetical: SpatiumDDI's own BIND9 container
    healthcheck queries ``.`` every 30 s from loopback, which scored a
    perfect 100 the first time this ran against the dev stack. Same
    class of self-inflicted false positive as the DNSBL sweep in the
    tunneling scorer, and worth excluding structurally rather than by
    threshold — no amount of tuning separates "every 30 s exactly" from
    "every 30 s exactly".
    """
    name = qname.strip().rstrip(".")
    return bool(name) and "." in name


def _humanise_period(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"
