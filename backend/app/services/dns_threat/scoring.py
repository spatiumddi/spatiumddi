"""DNS tunneling detection — feature extraction + scoring (issue #699).

DNS tunneling (iodine, dnscat2, dnsteal and friends) encodes payload
into the *names* a client asks for, under a domain whose authoritative
server the attacker controls. It is the exfiltration path that walks
past a firewall untouched, because to every device in between it is
just DNS.

The shape is unmistakable once you look for it, and nothing about it
requires seeing responses — which matters, because the BIND9 query log
records the question, never the answer:

* **Long labels.** Payload has to go somewhere, and the leftmost label
  is where it goes. A 63-character label (the DNS maximum) is a
  protocol edge case in ordinary traffic and routine in a tunnel.
* **High entropy.** Encoded payload is base32/base64-ish and looks
  random. ``www`` and ``mail`` do not.
* **Subdomain fan-out under one parent.** Every packet needs a unique
  name or it would be served from cache, so a tunnel generates
  thousands of distinct labels beneath a *single* parent domain.
* **Payload-bearing qtypes.** TXT / NULL / CNAME carry far more bytes
  per response than A does, so tunnels lean on them heavily.

No single signal is sufficient — which is the entire reason this is a
weighted score rather than a rule. A CDN emits high-entropy labels; a
mail server emits plenty of TXT; a busy host emits volume. Tunnels emit
all of it at once, sustained.

**On false positives.** Several entirely legitimate things look like
tunneling if you squint, and one of them is *SpatiumDDI itself*: the
DNSBL reputation sweep (#528) queries ``<reversed-ip>.zen.spamhaus.org``
and friends, which is a high-fan-out, high-entropy pattern under one
parent by construction. Antivirus and reputation lookups do the same.
:data:`DEFAULT_BENIGN_PARENTS` therefore ships with those suffixes
pre-cleared, and operators can extend it — a detector that pages on the
platform's own traffic teaches operators to ignore it, which is worse
than not shipping the detector.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

# The aggregator passes whatever the driver hands back for
# ``server_id`` (a UUID); scoring only ever puts it in a set to count
# distinct servers, so the alias stays deliberately loose.
UuidLike = Any

# Parent domains whose traffic is expected to look like this. Matched
# as suffixes, so ``zen.spamhaus.org`` clears
# ``2.0.0.127.zen.spamhaus.org``.
#
# The DNSBL entries are not optional politeness: SpatiumDDI's own
# reputation sweep generates exactly this pattern, so without them the
# platform's first finding would be itself.
DEFAULT_BENIGN_PARENTS: frozenset[str] = frozenset(
    {
        # DNSBL / reputation — including the zones our own sweep queries.
        "zen.spamhaus.org",
        "dbl.spamhaus.org",
        "bl.spamcop.net",
        "b.barracudacentral.org",
        "dnsbl.sorbs.net",
        "cbl.abuseat.org",
        # Antivirus / endpoint reputation lookups, textbook lookalikes.
        "avqs.mcafee.com",
        "avts.mcafee.com",
        "cloud.sophos.com",
        "spf.sophos.com",
        "clamav.net",
        # Certificate transparency + OCSP-ish infrastructure.
        "ocsp.digicert.com",
        "ocsp.sectigo.com",
        # Content delivery — long hashed labels are the norm.
        "akamaiedge.net",
        "akamai.net",
        "cloudfront.net",
        "edgekey.net",
        "edgesuite.net",
        "azureedge.net",
        "fastly.net",
        "cdn.cloudflare.net",
        # Cloud storage / telemetry with hashed bucket labels.
        "amazonaws.com",
        "blob.core.windows.net",
        "googleusercontent.com",
        # In-addr / ip6 reverse space: long, numeric, high fan-out, and
        # entirely ordinary.
        "in-addr.arpa",
        "ip6.arpa",
        # Internal service discovery. Kubernetes pod/service names are
        # high-entropy, high-fan-out and concentrated under one parent
        # by construction — and SpatiumDDI's own appliance IS a k3s
        # cluster, so without these the platform flags its own nodes.
        "svc.cluster.local",
        "pod.cluster.local",
        "cluster.local",
        "consul",
        "internal",
        "in-addr.arpa.local",
        # Active Directory locator records: many machine-named SRV/A
        # lookups under one zone.
        "_msdcs.local",
        "_tcp.local",
        "_udp.local",
        # mDNS / Bonjour.
        "local.arpa",
    }
)

# Weights sum to 100. Kept explicit (rather than folded into the maths)
# so the numbers an operator sees in the signals blob add up to the
# score they were shown, and so tuning one signal is a one-line change.
_W_LABEL_LENGTH = 30.0
_W_ENTROPY = 25.0
_W_FANOUT = 30.0
_W_PAYLOAD_QTYPE = 15.0

# Below these, a signal contributes nothing. Set from what ordinary
# traffic looks like, not from what tunnels look like — the goal is
# "boring traffic scores ~0", so anything non-zero is worth a glance.
_LABEL_LENGTH_FLOOR = 20  # chars; ordinary labels are short
_LABEL_LENGTH_CEIL = 55  # approaching the 63-char protocol maximum
_ENTROPY_FLOOR = 3.0  # bits/char; English-ish text sits well below
_ENTROPY_CEIL = 4.4  # base32/base64 payload approaches this
_FANOUT_FLOOR = 50  # distinct subdomains under ONE parent, per hour
_FANOUT_CEIL = 800
_PAYLOAD_RATIO_FLOOR = 0.10
_PAYLOAD_RATIO_CEIL = 0.60

# A handful of queries can hit every ratio threshold by accident. Below
# this, report the features but score zero — the maths is meaningless
# on three samples.
MIN_QUERIES_TO_SCORE = 25

_PAYLOAD_QTYPES = frozenset({"TXT", "NULL", "CNAME"})


def shannon_entropy(value: str) -> float:
    """Shannon entropy in bits per character.

    Encoded payload approaches ~4.5-5.0 for base32/base64 alphabets;
    ordinary hostnames sit nearer 2.5-3.5. The measure is
    length-independent, which is what lets it be combined with the
    separate length signal instead of double-counting it.
    """
    if not value:
        return 0.0
    counts = Counter(value)
    total = len(value)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def is_benign_parent(name: str, allowlist: frozenset[str] | set[str]) -> bool:
    """True when ``name`` is, or sits under, an allowlisted domain.

    Matched against the **full query name**, not the two-label parent
    the grouping logic derives. Most allowlist entries are deeper than
    two labels — ``zen.spamhaus.org``, ``blob.core.windows.net`` — and
    testing the derived parent (``spamhaus.org``,
    ``windows.net``) would never match them. That bug let SpatiumDDI's
    own DNSBL sweep score as suspicious, which was the first thing the
    scorer flagged when run against realistic traffic.
    """
    n = name.lower().rstrip(".")
    return any(n == b or n.endswith(f".{b}") for b in allowlist)


def split_qname(qname: str) -> tuple[list[str], str]:
    """Split into (subdomain labels, parent domain).

    "Parent" is the registrable-ish remainder — the last two labels for
    a plain TLD, three when the name sits under a two-part public
    suffix like ``co.uk``. Deliberately a heuristic and not a PSL
    lookup: getting ``example.co.uk`` grouped correctly matters, but
    shipping a public-suffix dependency (and its refresh problem) to
    sharpen an already-fuzzy signal does not.
    """
    labels = [label for label in qname.lower().rstrip(".").split(".") if label]
    if len(labels) <= 2:
        return ([], ".".join(labels))
    tail = labels[-2:]
    # Two-part public suffixes we actually see in practice.
    if len(labels) >= 3 and tail[0] in {"co", "com", "net", "org", "ac", "gov", "edu"}:
        if len(tail[-1]) == 2:  # country-code TLD, e.g. co.uk / com.au
            tail = labels[-3:]
    parent = ".".join(tail)
    # EVERY label above the parent, not just the leftmost: dnscat2 and
    # dnsteal chunk payload across several labels, and scoring only
    # ``labels[0]`` would let them evade the length and entropy signals
    # entirely while still fanning out.
    return (labels[: -len(tail)], parent)


def _ramp(value: float, floor: float, ceil: float) -> float:
    """Linear 0→1 ramp between floor and ceiling, clamped.

    A ramp rather than a threshold so a host just over the line scores
    just over zero, instead of a one-character difference flipping a
    finding on. It keeps the score sortable, which is what makes the
    Threat tab's "worst first" ordering meaningful.
    """
    if ceil <= floor:
        return 0.0
    return max(0.0, min(1.0, (value - floor) / (ceil - floor)))


@dataclass
class ClientFeatures:
    """Detection-agnostic description of one client's window."""

    query_count: int = 0
    # Queries that actually reached the signal maths: a parseable name
    # under a non-allowlisted parent. Every ratio and the minimum-sample
    # gate divide by THIS, not ``query_count`` — otherwise padding with
    # benign lookups dilutes the ratios, which is a suppression channel
    # an attacker controls for free.
    scored_query_count: int = 0
    # Rows whose ``qname`` was NULL/empty. The query-log column is
    # nullable by design (the parser keeps lines it doesn't fully
    # understand), so a parser regression shows up here rather than
    # masquerading as a clean result.
    unparseable_count: int = 0
    distinct_qnames: int = 0
    distinct_parents: int = 0
    top_parent: str | None = None
    top_parent_subdomains: int = 0
    max_label_length: int = 0
    avg_label_length: float = 0.0
    mean_label_entropy: float = 0.0
    payload_qtype_count: int = 0
    server_count: int = 1
    allowlisted: bool = False
    # Each distinct non-allowlisted parent domain this client queried,
    # mapped to its registrable label (``xkqjfhwbz.com`` →
    # ``xkqjfhwbz``). Populated here because this walk already derives
    # every parent, so the DGA scorer costs no second pass over what can
    # be tens of thousands of log lines. Not persisted as a column — the
    # DGA verdict it produces is.
    parent_labels: dict[str, str] = field(default_factory=dict)

    @property
    def payload_qtype_ratio(self) -> float:
        return (
            self.payload_qtype_count / self.scored_query_count if self.scored_query_count else 0.0
        )


def extract_features(
    rows: list[tuple[str | None, str | None, UuidLike]],
    *,
    allowlist: frozenset[str] | set[str] = DEFAULT_BENIGN_PARENTS,
) -> ClientFeatures:
    """Summarise one client's queries in a window.

    ``rows`` is ``(qname, qtype, server_id)`` — the three columns the
    scoring needs, kept narrow so the aggregator can select just those
    rather than hydrating whole ORM objects for what may be tens of
    thousands of log lines.

    Allowlisted and unparseable rows are excluded from every ratio and
    from the minimum-sample gate, via ``scored_query_count``. Counting
    them (as an earlier version did) meant a host could suppress its own
    payload-qtype ratio simply by emitting DNSBL-shaped noise alongside
    the tunnel, and could clear the 25-query floor on 24 real samples.
    ``query_count`` keeps the raw total for display.
    """
    feats = ClientFeatures()
    qnames: set[str] = set()
    servers: set[Any] = set()
    per_parent: dict[str, set[str]] = defaultdict(set)
    label_lengths: list[int] = []
    entropies: list[float] = []

    for qname, qtype, server_id in rows:
        feats.query_count += 1
        if server_id is not None:
            servers.add(server_id)
        if not qname:
            feats.unparseable_count += 1
            continue
        qnames.add(qname.lower())
        # Allowlist against the full name before deriving the parent —
        # entries are routinely deeper than the two-label parent.
        if is_benign_parent(qname, allowlist):
            continue
        sub_labels, parent = split_qname(qname)
        if not parent:
            # Root / single-label / otherwise unsplittable. Counted as
            # unparseable rather than silently skipped: skipping left
            # scored_query_count at 0 with unparseable_count also 0,
            # which the allowlisted guard below reads as "everything was
            # benign" — hiding the client from every surface, which is
            # precisely the failure the unparseable branch exists to
            # prevent.
            feats.unparseable_count += 1
            continue
        feats.scored_query_count += 1
        # The whole subdomain path is the fan-out key, so a tunnel that
        # chunks payload across labels still counts as one unique name
        # per packet rather than collapsing onto a shared first label.
        per_parent[parent].add(".".join(sub_labels))
        if (qtype or "").upper() in _PAYLOAD_QTYPES:
            feats.payload_qtype_count += 1
        for label in sub_labels:
            label_lengths.append(len(label))
        if sub_labels:
            # Entropy over the joined payload region rather than per
            # label: multi-label encodings are one payload split by
            # dots, and averaging per label would understate them.
            entropies.append(shannon_entropy("".join(sub_labels)))

    feats.distinct_qnames = len(qnames)
    feats.distinct_parents = len(per_parent)
    feats.server_count = len(servers) or 1
    # "Everything this client asked for was benign" — genuinely cleared.
    # Distinct from "nothing was parseable", which is a data problem and
    # must NOT read as cleared, because every surface filters allowlisted
    # rows out of view.
    feats.allowlisted = (
        feats.query_count > 0
        and feats.scored_query_count == 0
        and feats.unparseable_count < feats.query_count
    )

    # The parent's leftmost label is the registrable one — the part a
    # DGA actually generates. ``split_qname`` has already trimmed the
    # public suffix, so ``example.co.uk`` yields ``example`` rather than
    # ``co``.
    feats.parent_labels = {parent: parent.split(".")[0] for parent in per_parent}

    if per_parent:
        top_parent, subs = max(per_parent.items(), key=lambda kv: len(kv[1]))
        feats.top_parent = top_parent
        feats.top_parent_subdomains = len(subs)
    if label_lengths:
        feats.max_label_length = max(label_lengths)
        feats.avg_label_length = sum(label_lengths) / len(label_lengths)
    if entropies:
        feats.mean_label_entropy = sum(entropies) / len(entropies)
    return feats


@dataclass
class Signal:
    name: str
    value: float
    contribution: float
    detail: str
    # This signal's maximum possible contribution. Carried through to
    # the UI so a bar can be drawn as a true percentage of what the
    # signal could contribute — weights differ (30/25/30/15), so a
    # fully-saturated payload-qtype signal and a fully-saturated
    # label-length signal must both render as full.
    max_contribution: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": round(self.value, 3),
            "contribution": round(self.contribution, 1),
            "max_contribution": round(self.max_contribution, 1),
            "detail": self.detail,
        }


@dataclass
class TunnelVerdict:
    score: float
    signals: list[Signal] = field(default_factory=list)

    def signals_json(self) -> list[dict[str, Any]]:
        return [s.as_dict() for s in self.signals]


def score_tunneling(feats: ClientFeatures) -> TunnelVerdict:
    """Weighted 0-100 tunneling score from a client's window features.

    Every signal is reported even when it contributes nothing, so the
    UI can show what was considered rather than only what fired — "your
    entropy is fine, it's the fan-out that's odd" is a more useful
    answer than a bare 62.
    """
    if feats.allowlisted:
        return TunnelVerdict(
            score=0.0,
            signals=[
                Signal(
                    "allowlisted",
                    1.0,
                    0.0,
                    "every parent domain queried is on the benign allowlist "
                    "(DNSBL / CDN / reputation traffic looks like this by design)",
                )
            ],
        )
    if feats.query_count > 0 and feats.unparseable_count == feats.query_count:
        # NOT a clean bill of health: we could not read a single name.
        # Left un-allowlisted on purpose so it stays visible — every
        # surface hides allowlisted rows, and a parser regression that
        # rendered as "all clear" is the exact failure this feature
        # exists to avoid.
        return TunnelVerdict(
            score=0.0,
            signals=[
                Signal(
                    "unparseable_queries",
                    float(feats.unparseable_count),
                    0.0,
                    f"none of this client's {feats.query_count} logged queries had a "
                    f"readable name, so nothing could be scored — check the query-log "
                    f"parser rather than reading this as clear",
                )
            ],
        )
    if feats.scored_query_count < MIN_QUERIES_TO_SCORE:
        return TunnelVerdict(
            score=0.0,
            signals=[
                Signal(
                    "insufficient_data",
                    float(feats.scored_query_count),
                    0.0,
                    f"{feats.scored_query_count} scoreable queries in the window "
                    f"(of {feats.query_count} total); {MIN_QUERIES_TO_SCORE} needed "
                    f"before the ratios mean anything",
                )
            ],
        )

    # Blend max with mean. On max alone a SINGLE long lookup — one
    # _acme-challenge TXT, a DKIM selector, an un-allowlisted CDN
    # hostname — handed a client the full 30 points for the hour no
    # matter how ordinary its other 10,000 queries were. A tunnel's
    # labels are long *on average*; a false positive's are long once.
    # Keeping some weight on max means a short-label tunnel padded with
    # normal traffic still registers.
    length_r = 0.4 * _ramp(
        feats.max_label_length, _LABEL_LENGTH_FLOOR, _LABEL_LENGTH_CEIL
    ) + 0.6 * _ramp(feats.avg_label_length, _LABEL_LENGTH_FLOOR, _LABEL_LENGTH_CEIL)
    entropy_r = _ramp(feats.mean_label_entropy, _ENTROPY_FLOOR, _ENTROPY_CEIL)
    fanout_r = _ramp(feats.top_parent_subdomains, _FANOUT_FLOOR, _FANOUT_CEIL)
    payload_r = _ramp(feats.payload_qtype_ratio, _PAYLOAD_RATIO_FLOOR, _PAYLOAD_RATIO_CEIL)

    signals = [
        Signal(
            "max_label_length",
            float(feats.max_label_length),
            length_r * _W_LABEL_LENGTH,
            f"longest subdomain label {feats.max_label_length} chars, mean "
            f"{feats.avg_label_length:.0f} (payload has to live somewhere; 63 is "
            f"the protocol maximum — the mean is weighted higher so one long "
            f"lookup can't carry the signal)",
            max_contribution=_W_LABEL_LENGTH,
        ),
        Signal(
            "label_entropy",
            feats.mean_label_entropy,
            entropy_r * _W_ENTROPY,
            f"mean {feats.mean_label_entropy:.2f} bits/char across the subdomain "
            f"payload (encoded data approaches 4.5; ordinary names sit near 3)",
            max_contribution=_W_ENTROPY,
        ),
        Signal(
            "subdomain_fanout",
            float(feats.top_parent_subdomains),
            fanout_r * _W_FANOUT,
            f"{feats.top_parent_subdomains} distinct subdomains under "
            f"{feats.top_parent or 'n/a'} (a tunnel needs a unique name per "
            f"packet or the cache would answer it)",
            max_contribution=_W_FANOUT,
        ),
        Signal(
            "payload_qtypes",
            feats.payload_qtype_ratio,
            payload_r * _W_PAYLOAD_QTYPE,
            f"{feats.payload_qtype_ratio:.0%} TXT/NULL/CNAME of "
            f"{feats.scored_query_count} scoreable queries "
            f"(these carry the most bytes per response)",
            max_contribution=_W_PAYLOAD_QTYPE,
        ),
    ]
    return TunnelVerdict(score=sum(s.contribution for s in signals), signals=signals)
