"""DGA (domain-generation algorithm) detection — name plausibility (issue #699).

Malware that resolves its C2 through a DGA generates a fresh crop of
domains on a schedule, tries them in turn, and keeps whichever one the
operator registered that day. The give-away is not any single name but
the *crop*: dozens of registrable domains, each looking like nothing a
human would choose.

**This is structurally the inverse of tunneling**, which is why it is a
separate scorer rather than more weights on
:mod:`app.services.dns_threat.scoring`. A tunnel concentrates — thousands
of unique subdomains beneath ONE parent it controls. A DGA sprays —
one or two lookups each across MANY distinct parents it hopes to
control. Scoring both from one weighted sum would mean each detection's
strongest signal is the other's evidence of innocence.

**On the NXDOMAIN prior.** Issue #699 originally specified scoring
"NXDOMAIN-heavy clients", because a DGA's crop is mostly unregistered
and so mostly fails. We cannot do that: ``dns_query_log_entry`` records
the question, never the answer — the BIND9 query log carries no rcode,
which is why the existing ``dns_nxdomain_spike`` rule reads
``dns_metric_sample`` instead. Three options were written up on the
issue; this implements the first — score on name characteristics alone.
That is weaker than a true NXDOMAIN prior and the thresholds are set
accordingly conservative, but it works on data we already collect and
needs no new agent plumbing. The per-server NXDOMAIN counts in
``dns_metric_sample`` remain available as a future multiplier; this
module deliberately keeps its signals additive so one can be layered on
without restructuring the score.

**On false positives.** Plenty of legitimate domains are unpronounceable
— hashed CDN buckets, shortlink services, IPv6-literal-ish labels, and
any brand that bought a consonant cluster. Those are handled the same
way tunneling handles them: the shared allowlist in
:data:`~app.services.dns_threat.scoring.DEFAULT_BENIGN_PARENTS` clears
them before scoring, and the fan-out gate means a client has to be
spraying *many* implausible parents before it scores at all. One weird
domain is not a DGA; forty in an hour is.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

# Bigrams that turn up constantly in English words and in the brandable
# labels people actually register. Deliberately a coarse membership set
# rather than a trained frequency model: the job is separating
# "pronounceable thing a human picked" from "output of a PRNG", and a
# 200-entry set does that without shipping a corpus, a model file, or a
# refresh problem. A label whose bigrams are mostly absent from this set
# is not necessarily malicious — it is merely not word-shaped.
_COMMON_BIGRAMS: frozenset[str] = frozenset(
    """th he in er an re on at en nd ti es or te of ed is it al ar st to nt ng se ha as ou io
    le ve co me de hi ri ro ic ne ea ra ce li ch ll be ma si om ur ca el ta la ns di fo ho pe
    ec pr no ct us ac ot il tr ly nc et ut ss so rs un lo wa ge ie wh ee wi em ad ol rt po we
    na ul ni ts mo ow pa im mi ai sh ir su id os iv ia am fi ci vi pl ig tu ev ld ry mp fe bl
    ab gh ty op wo sa ay ex ky fr oo av ag if ap gr od bo sp rd do uc bu ei ov by rm ep tt oc
    fa ef cu rn sc gi da yo cr cl du ga qu ue ff ba ey ls va um pp ua up lu go ht ru ug ds lt
    pi rc rr eg au ck ew mu br bi pt ak pu ui rg ib tl ny ki rk ys ob mm fu ph og ms ye ud mb
    ip ub oi rl gu dr cc tw ft wn nu af hu nn eo vo nf sm fl ok nl my gl aw ju oa sy sl ps jo
    lf nv je nk kn gs dy hy ze ks bs ik dd cy rp sk oe ws eu wr""".split()
)

_VOWELS = frozenset("aeiouy")

# Weights sum to 100, mirroring the tunneling and beaconing scorers so
# all three produce comparable numbers on the same 0-100 scale.
_W_NGRAM = 35.0
_W_PARENT_FANOUT = 30.0
_W_PRONOUNCEABILITY = 20.0
_W_LENGTH_UNIFORMITY = 15.0

# Fraction of a label's bigrams absent from _COMMON_BIGRAMS. Real
# brandable labels still miss plenty (brand names are odd on purpose),
# so the floor is high — this only starts contributing well past what
# ordinary domains score.
_NGRAM_FLOOR = 0.55
_NGRAM_CEIL = 0.92

# Distinct non-allowlisted parent domains in the hour. A browsing human
# comfortably clears 50; a DGA crop is typically tried in bulk. Set so
# ordinary traffic scores ~0 on this signal and only genuine spraying
# registers.
_FANOUT_FLOOR = 40
_FANOUT_CEIL = 250

# Vowel fraction of the label. Human-chosen labels sit near 0.35-0.40;
# consonant soup sits far below. Inverted ramp — LOW is the signal.
_VOWEL_RATIO_IMPLAUSIBLE = 0.12
_VOWEL_RATIO_NORMAL = 0.34

# Coefficient of variation of label lengths across the client's parents.
# Many DGA families emit a fixed or narrow length (all 12 chars, all
# 16). Organic browsing has wildly varying label lengths, so a LOW
# spread across many domains is itself suspicious. Inverted ramp.
_LENGTH_CV_UNIFORM = 0.05
_LENGTH_CV_ORGANIC = 0.45

# Below this many distinct scoreable parents the maths says nothing: a
# handful of odd domains is a person clicking a link, not a crop.
MIN_PARENTS_TO_SCORE = 12

# Labels shorter than this carry too few bigrams to judge (``go``,
# ``bit``), and are overwhelmingly legitimate short brandables.
_MIN_LABEL_LENGTH = 6


def bigram_implausibility(label: str) -> float:
    """Fraction of a label's character bigrams absent from the common set.

    1.0 = every adjacent pair is unusual (``xkqjfhwbz``); 0.0 = every
    pair is word-shaped (``newsletter``). Digits are stripped first
    rather than counted as unusual — plenty of legitimate domains carry
    them, and leaving them in made every ``o365``-shaped label score as
    gibberish.
    """
    cleaned = "".join(ch for ch in label.lower() if ch.isalpha())
    if len(cleaned) < 3:
        return 0.0
    pairs = [cleaned[i : i + 2] for i in range(len(cleaned) - 1)]
    if not pairs:
        return 0.0
    missing = sum(1 for p in pairs if p not in _COMMON_BIGRAMS)
    return missing / len(pairs)


def vowel_ratio(label: str) -> float:
    """Fraction of alphabetic characters that are vowels (``y`` counts).

    Returns the normal-looking 0.34 for labels with no letters at all,
    so an all-numeric label contributes nothing rather than reading as
    maximally implausible.
    """
    letters = [ch for ch in label.lower() if ch.isalpha()]
    if not letters:
        return _VOWEL_RATIO_NORMAL
    return sum(1 for ch in letters if ch in _VOWELS) / len(letters)


def _ramp(value: float, floor: float, ceil: float) -> float:
    if ceil <= floor:
        return 0.0
    return max(0.0, min(1.0, (value - floor) / (ceil - floor)))


def _inverse_ramp(value: float, low: float, high: float) -> float:
    """1.0 at or below ``low``, 0.0 at or above ``high``.

    For signals where a SMALL number is the suspicious one (vowel
    deficiency, length uniformity).
    """
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (high - value) / (high - low)))


@dataclass
class DGACandidate:
    """One registrable domain that scored as algorithmically generated."""

    parent: str
    label: str
    implausibility: float
    vowel_ratio: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "parent": self.parent,
            "label": self.label,
            "implausibility": round(self.implausibility, 3),
            "vowel_ratio": round(self.vowel_ratio, 3),
        }


@dataclass
class DGAVerdict:
    score: float = 0.0
    candidates: list[DGACandidate] = field(default_factory=list)
    detail: str = ""
    signals: list[dict[str, Any]] = field(default_factory=list)

    def candidates_json(self) -> list[dict[str, Any]]:
        # Worst first, capped. The evidence blob is for an operator
        # triaging one finding — showing all 200 domains of a crop would
        # bury the point rather than make it.
        ranked = sorted(self.candidates, key=lambda c: c.implausibility, reverse=True)
        return [c.as_dict() for c in ranked[:10]]


def _signal(
    name: str, value: float, contribution: float, max_contribution: float, detail: str
) -> dict[str, Any]:
    """Same wire shape as the tunneling scorer's ``Signal.as_dict`` so the
    UI renders both detections' evidence with one component."""
    return {
        "name": name,
        "value": round(value, 3),
        "contribution": round(contribution, 1),
        "max_contribution": round(max_contribution, 1),
        "detail": detail,
    }


def score_dga(
    parent_labels: dict[str, str],
    *,
    min_parents: int = MIN_PARENTS_TO_SCORE,
) -> DGAVerdict:
    """Score one client's window for domain-generation-algorithm traffic.

    ``parent_labels`` maps each distinct non-allowlisted parent domain
    the client queried to its registrable label — ``xkqjfhwbz.com`` →
    ``xkqjfhwbz``. The aggregator builds it while it is already walking
    the client's rows, so this costs no extra pass.

    Scored across the crop rather than per-domain: a single
    unpronounceable name is a hashed CDN bucket or an odd brand, and
    scoring it alone would flag half the internet. The signal is *many*
    implausible parents at once.
    """
    # Labels too short to judge are dropped rather than scored as
    # plausible — averaging in a pile of 0.0s from three-letter domains
    # would dilute a real crop's implausibility toward nothing.
    judged = {
        parent: label for parent, label in parent_labels.items() if len(label) >= _MIN_LABEL_LENGTH
    }
    if len(judged) < min_parents:
        return DGAVerdict(
            score=0.0,
            detail=(
                f"{len(judged)} distinct judgeable domains in the window; "
                f"{min_parents} needed before a crop can be distinguished from "
                f"ordinary browsing"
            ),
            signals=[
                _signal(
                    "insufficient_domains",
                    float(len(judged)),
                    0.0,
                    0.0,
                    "a DGA is identifiable by the size of its crop, not by any one name",
                )
            ],
        )

    implausibilities = {p: bigram_implausibility(lbl) for p, lbl in judged.items()}
    vowels = {p: vowel_ratio(lbl) for p, lbl in judged.items()}
    lengths = [len(lbl) for lbl in judged.values()]

    mean_implaus = statistics.fmean(implausibilities.values())
    mean_vowel = statistics.fmean(vowels.values())
    mean_len = statistics.fmean(lengths)
    length_cv = (statistics.pstdev(lengths) / mean_len) if mean_len > 0 else 1.0

    ngram_r = _ramp(mean_implaus, _NGRAM_FLOOR, _NGRAM_CEIL)
    fanout_r = _ramp(float(len(judged)), _FANOUT_FLOOR, _FANOUT_CEIL)
    vowel_r = _inverse_ramp(mean_vowel, _VOWEL_RATIO_IMPLAUSIBLE, _VOWEL_RATIO_NORMAL)
    uniform_r = _inverse_ramp(length_cv, _LENGTH_CV_UNIFORM, _LENGTH_CV_ORGANIC)

    signals = [
        _signal(
            "ngram_implausibility",
            mean_implaus,
            ngram_r * _W_NGRAM,
            _W_NGRAM,
            f"{mean_implaus:.0%} of character pairs across {len(judged)} domains are "
            f"not word-shaped (algorithmic labels score high; brandable ones, even "
            f"odd ones, sit well below)",
        ),
        _signal(
            "domain_fanout",
            float(len(judged)),
            fanout_r * _W_PARENT_FANOUT,
            _W_PARENT_FANOUT,
            f"{len(judged)} distinct registrable domains in one hour (a DGA tries a "
            f"whole crop; this is the signal that separates it from tunneling, which "
            f"concentrates under one parent)",
        ),
        _signal(
            "pronounceability",
            mean_vowel,
            vowel_r * _W_PRONOUNCEABILITY,
            _W_PRONOUNCEABILITY,
            f"mean vowel fraction {mean_vowel:.0%} (human-chosen labels sit near 34%; "
            f"consonant soup sits far below)",
        ),
        _signal(
            "length_uniformity",
            length_cv,
            uniform_r * _W_LENGTH_UNIFORMITY,
            _W_LENGTH_UNIFORMITY,
            f"label lengths vary by {length_cv:.0%} around a mean of {mean_len:.0f} "
            f"chars (many DGA families emit a fixed width; organic browsing does not)",
        ),
    ]
    score = sum(s["contribution"] for s in signals)

    candidates = [
        DGACandidate(
            parent=parent,
            label=judged[parent],
            implausibility=implausibilities[parent],
            vowel_ratio=vowels[parent],
        )
        for parent in judged
    ]
    worst = max(candidates, key=lambda c: c.implausibility)
    return DGAVerdict(
        score=score,
        candidates=candidates,
        signals=signals,
        detail=(
            f"{len(judged)} distinct domains queried, mean bigram implausibility "
            f"{mean_implaus:.0%} (worst: {worst.parent}) — this is what a "
            f"domain-generation algorithm's crop looks like; confirm against the "
            f"host's software before treating it as an infection, since hashed CDN "
            f"and shortlink traffic shares the shape"
        ),
    )
