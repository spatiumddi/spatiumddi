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
from typing import TYPE_CHECKING, Any

from app.models.appliance import (
    APPLIANCE_STATE_APPROVED,
    CLUSTER_ROLE_MEMBER,
    CLUSTER_ROLE_PRIMARY,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from app.models.appliance import Appliance

#: The answer reported for a node with no MTU of ours in force.
#: Deliberately not a number — see the module docstring.
DEFAULT_ANSWER = "default"


@dataclass(frozen=True)
class MtuFinding:
    """One thing worth saying about a single node's MTU."""

    #: Always ``warning`` today. A mixed-MTU cluster is a real hazard but
    #: it is not data loss, and the node is already installed so there is
    #: nothing left to refuse — the issue's "refuse or loudly warn"
    #: resolves to the second here. Deliberately NOT backed by a local
    #: severity-ordering table: ``storage_health.worst_severity`` is
    #: duck-typed on ``.severity`` and is the one every other surface
    #: already shares, so a second copy would be dead code that ships a
    #: ranking nobody consults.
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

    **Everything else answers, and that direction is deliberate.** An
    earlier cut allowlisted the tokens that mean "link default" and
    returned None for the rest, which excluded ``n/a`` — so a cluster
    with one node pinned at 1400 and one on any-port DHCP at 1500
    reported *consistent* and rendered no banner, the host-gw black hole
    this check exists to catch scored as healthy. Adding the missing
    token fixed that instance and left the shape: any token a newer slot
    invents, or a truncated field, would drop a node out of the
    comparison silently, because ``fleet_summary`` cannot tell "excluded"
    from "not present". So the test is inverted — every applied-state
    token except a real measurement means the link default — and an
    unrecognised one is COMPARED rather than dropped. A wrong warning is
    recoverable; a silent black hole is the thing being prevented.
    """
    report = network_report(cluster_health)
    if report is None:
        return None
    applied = report.get("mtu_applied")
    mtu = report.get("mtu")
    # ``bool`` is an ``int`` in Python, so a malformed ``{"mtu": true}``
    # would otherwise become the answer key "True" and be compared as a
    # distinct MTU against every real one.
    if isinstance(mtu, int) and not isinstance(mtu, bool) and mtu > 0:
        return str(mtu)
    if not applied:
        # A report that carried no applied-state at all says nothing.
        return None
    return DEFAULT_ANSWER


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


def in_cluster(row: Appliance) -> bool:
    """Is this appliance a node in the control plane's own k3s cluster?

    The load-bearing half of the fleet check, and it is NOT "approved".
    An approved-but-unpromoted Additional node has ``cluster_role IS
    NULL`` and runs **its own single-node k3s** — ``APPLIANCE.md`` says so
    outright ("Its own single-node k3s runs fine on the upstream
    defaults, and it inherits the seed's values on promotion"). It shares
    no flannel network with the control plane, so its MTU cannot
    black-hole anything there.

    Filtering on approval instead put a permanent, unclearable warning on
    this feature's own headline deployment: a branch DNS appliance
    reached over a 1400-MTU WireGuard tunnel, beside a control plane at
    the link default, was reported as a mixed-MTU cluster about to
    black-hole pod-to-pod traffic between two boxes with no shared pod
    network. ``find_cluster_health`` already uses this predicate.
    """
    return (
        row.state == APPLIANCE_STATE_APPROVED
        and row.revoked_at is None
        and row.cluster_role in (CLUSTER_ROLE_PRIMARY, CLUSTER_ROLE_MEMBER)
    )


def fleet_summary_for_appliances(rows: Sequence[Appliance]) -> MtuFleetSummary:
    """:func:`fleet_summary` over the cluster members among ``rows``.

    The one place the membership predicate and the projection live, so
    the REST list and the copilot tool cannot give different consistency
    verdicts for the same cluster.
    """
    members = [r for r in rows if in_cluster(r)]
    return fleet_summary(
        [(r.hostname or "", r.cluster_health) for r in members],
        backends={r.dataplane_backend for r in members if r.dataplane_backend},
    )


def fleet_summary(rows: list[tuple[str, Any]], backends: set[str] | None = None) -> MtuFleetSummary:
    """Compare every node's answer.

    ``rows`` is ``(hostname, cluster_health)`` rather than the ORM object
    so a caller can pass a narrow two-column query result instead of
    hydrating whole appliances. :func:`fleet_summary_for_appliances` is
    the adapter for callers that already hold rows.

    ``backends`` is the set of k3s data-plane backends those nodes report
    (``Appliance.dataplane_backend``), used only to word the explanation
    honestly. It matters because WHY a mismatch hurts is
    backend-specific: ``host-gw`` writes plain routes, so the pod network
    inherits the node MTU with no headroom, while an encapsulating
    backend subtracts its own header instead. Asserting host-gw
    unconditionally told an operator who had set ``wireguard-native`` —
    supported, and already accommodated by the #285 firewall renderer —
    a fact their own configuration contradicts, in the one sentence meant
    to explain why to act. Unknown falls back to naming the appliance
    default rather than claiming to have checked.
    """
    answers: dict[str, list[str]] = {}
    for hostname, health in rows:
        answer = node_answer(health)
        if answer is not None:
            # ``Appliance.hostname`` is nullable, and sorting a bucket
            # that mixes None with str raises TypeError.
            answers.setdefault(answer, []).append(hostname or "(unnamed)")
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
    known = backends or set()
    if known == {"host-gw"}:
        why = (
            "k3s runs flannel in host-gw mode here, so the pod network inherits "
            "the node MTU with no tunnel headroom"
        )
    elif known:
        why = (
            "the pod network derives its MTU from the node interface (flannel "
            f"backend: {', '.join(sorted(known))})"
        )
    else:
        why = (
            "the pod network derives its MTU from the node interface (these nodes "
            "did not report a flannel backend; the appliance default is host-gw, "
            "which adds no tunnel headroom)"
        )
    return MtuFleetSummary(
        reported=reported,
        answers=answers,
        consistent=False,
        detail=(
            "Nodes in this cluster are not running the same interface MTU ("
            + "; ".join(parts)
            + f"). {why} — a mismatch black-holes pod-to-pod traffic and presents "
            'as random timeouts. "The link default" is whatever each interface '
            "negotiates; it is not read from the node."
        ),
    )
