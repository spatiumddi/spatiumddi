"""Interface MTU — per-node reading and fleet-wide consistency (#1017).

The supervisor reports the MTU ``spatium-etc-render`` actually applied
to this node's NetworkManager profile, inside its ``cluster_health``
dict (``read_network_state`` in
``agent/supervisor/spatium_supervisor/appliance_state.py``). This module
is the ONLY place that reading is interpreted — the API schema and the
copilot tool both call it, so they cannot disagree about whether a
cluster is consistent.

**Why a fleet check and not a per-node one.** The appliance runs k3s with
``flannel-backend: host-gw``
(``appliance/mkosi.extra/etc/rancher/k3s/config.yaml``), which writes
plain Linux routes instead of encapsulating, so the pod MTU derives from
the node interface with **no tunnel headroom**. A mixed-MTU cluster — one
node at 9000, two at 1500 — produces pod-to-pod black holes that present
as random timeouts with nothing in the UI explaining them. A per-node
guard cannot see that; only something holding every row can. That is the
#1013 lesson applied before the fact rather than after it.

**"Unset" is compared as itself, never as 1500.** The obvious
implementation scores an unconfigured node at the Ethernet default and
compares numbers. That is a guess about hardware nobody read: an operator
whose switches are genuinely all-9000, who sets an explicit 9000 on one
node, would be told their cluster disagrees when it does not. So a node's
answer is either the number it was configured with or the token
``default``, a mismatch is more than one distinct answer, and the wording
says plainly that the default was not read off the wire. That also keeps
every appliance installed before this feature — all of them ``default``
— quiet, which is the population that must not be alarmed.

**An unreadable node is UNKNOWN and is excluded, not assumed.** A
supervisor too old to write the sidecar ships no ``network`` key.
Treating that as ``default`` would report a genuine mismatch as
agreement on exactly the nodes that could not answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Severity ranking, worst first. Only ``warning`` is produced today: a
#: mixed-MTU cluster is a real hazard but it is not data loss, and the
#: node is already installed so there is nothing left to refuse — the
#: issue's "refuse or loudly warn" resolves to the second here.
_SEVERITY_ORDER = ("critical", "warning")

#: The answer reported for a node with no MTU of ours in force.
#: Deliberately not a number — see the module docstring.
DEFAULT_ANSWER = "default"


@dataclass(frozen=True)
class MtuFinding:
    """One thing worth saying about a single node's MTU."""

    #: ``warning``.
    severity: str
    #: ``dropped`` — this node asked for an MTU the renderer refused, so
    #: it is running something the operator did not choose.
    kind: str
    #: Operator-facing sentence, carrying the values: "MTU mismatch"
    #: without them sends the reader to four screens.
    detail: str


@dataclass(frozen=True)
class MtuFleetSummary:
    """How the whole cluster's MTUs compare.

    Computed over the rows a caller already holds, so the Fleet list
    costs no extra query.
    """

    #: How many nodes contributed an answer.
    reported: int = 0
    #: Answer -> the hostnames giving it. ``{}`` when nobody reported.
    answers: dict[str, list[str]] = field(default_factory=dict)
    #: False only when there is a genuine disagreement to act on.
    #: True when nobody reported, which is the pre-#1017 fleet — the
    #: absence of a reading is not a fault.
    consistent: bool = True
    #: The operator sentence, or None when consistent.
    detail: str | None = None


def network_report(cluster_health: Any) -> dict[str, Any] | None:
    """This node's raw MTU reading, or None when it never reported one."""
    if not isinstance(cluster_health, dict):
        return None
    report = cluster_health.get("network")
    return report if isinstance(report, dict) else None


def has_network_report(cluster_health: Any) -> bool:
    """Has the supervisor on this node ever reported its MTU?

    A supervisor too old to collect it ships no ``network`` key, and that
    is UNKNOWN — never a clean bill of health. Every surface renders
    nothing in that case rather than a green tick.
    """
    return network_report(cluster_health) is not None


def node_answer(cluster_health: Any) -> str | None:
    """What this node contributes to the comparison.

    A string, so ``default`` and ``"1500"`` stay distinguishable: they
    are different claims and only one of them was read off a
    configuration.

    ``None`` only when the node has NOT reported, which is the one state
    that is genuinely unknown.

    Every other outcome answers, including ``n/a`` — the install shape
    (DHCP with no pinned port) where etc-render writes no keyfile at all.
    An earlier cut excluded that one on the reasoning that "nothing
    SpatiumDDI set is in play", which is true and irrelevant: the
    interface is still running the link default, which is exactly the
    state a ``default`` node is in, and ``default`` is compared. Dropping
    it meant a cluster with one node pinned at 1400 and one on any-port
    DHCP at 1500 reported *consistent* and rendered no banner — the
    host-gw black hole this check exists to catch, scored as healthy.
    """
    report = network_report(cluster_health)
    if report is None:
        return None
    applied = report.get("mtu_applied")
    mtu = report.get("mtu")
    if isinstance(mtu, int) and mtu > 0:
        return str(mtu)
    # "default", "dropped" and "n/a" are one ANSWER: in all three the
    # link default is what the interface runs at, whatever the reason.
    # "dropped" additionally earns its own finding below, because the
    # operator asked for something and did not get it.
    if applied in {"default", "dropped", "n/a"}:
        return DEFAULT_ANSWER
    return None


def evaluate_node(cluster_health: Any) -> list[MtuFinding]:
    """Findings about one node in isolation.

    Deliberately independent of the fleet: this runs on every row from
    every endpoint, and a finding that appeared only on the list response
    and not on the drilldown would be a per-endpoint inconsistency the UI
    would have to paper over. The fleet verdict is
    :func:`fleet_summary`, computed once where the rows are.
    """
    report = network_report(cluster_health)
    if report is None:
        return []
    if report.get("mtu_applied") != "dropped":
        return []
    requested = report.get("mtu_requested")
    return [
        MtuFinding(
            severity="warning",
            kind="dropped",
            detail=(
                f"MTU {requested} was configured but not applied — this node is "
                "running its link default. The value is outside 576-9000, or below "
                "1280 with a static IPv6 address (RFC 8200). The reason is in "
                "/var/log/spatiumddi/etc-render.log on the node."
            ),
        )
    ]


def fleet_summary(rows: list[tuple[str, Any]]) -> MtuFleetSummary:
    """Compare every node's answer.

    ``rows`` is ``(hostname, cluster_health)`` — a tuple rather than the
    ORM object so the alert evaluator and the copilot tool can call this
    with a bare query result instead of loading whole appliances.
    """
    answers: dict[str, list[str]] = {}
    for hostname, health in rows:
        answer = node_answer(health)
        if answer is not None:
            answers.setdefault(answer, []).append(hostname)
    for names in answers.values():
        names.sort()

    reported = sum(len(names) for names in answers.values())
    if len(answers) < 2:
        return MtuFleetSummary(reported=reported, answers=answers, consistent=True)

    parts = []
    for answer in sorted(answers):
        names = ", ".join(answers[answer])
        label = "the link default" if answer == DEFAULT_ANSWER else f"MTU {answer}"
        parts.append(f"{label}: {names}")
    return MtuFleetSummary(
        reported=reported,
        answers=answers,
        consistent=False,
        detail=(
            "Nodes in this cluster are not running the same interface MTU ("
            + "; ".join(parts)
            + "). k3s runs flannel in host-gw mode, so the pod network inherits the "
            "node MTU with no tunnel headroom — a mismatch black-holes pod-to-pod "
            'traffic and presents as random timeouts. "The link default" is whatever '
            "each interface negotiates; it is not read from the node."
        ),
    )


def worst_severity(findings: list[MtuFinding]) -> str | None:
    """The most serious severity among ``findings``, or None when empty."""
    for sev in _SEVERITY_ORDER:
        if any(f.severity == sev for f in findings):
            return sev
    return None
