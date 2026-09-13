"""Fleet-wide interface-MTU consistency (#1017).

The appliance runs k3s with ``flannel-backend: host-gw``, which writes
plain Linux routes instead of encapsulating — so the pod MTU derives from
the node interface with **no tunnel headroom**, and a mixed-MTU cluster
black-holes pod-to-pod traffic while presenting as random timeouts. There
is nothing in the UI that would explain it, which is why the check exists
at all.

The properties pinned here are the ones that would be wrong in a way
nobody would notice, because every one of them fails SILENTLY — a wrong
answer here is a green screen over a broken cluster:

* **unset is compared as itself, never as 1500** — scoring an
  unconfigured node at the Ethernet default is a guess about hardware
  nobody read, and it would tell an operator whose switches are genuinely
  all-9000 that their cluster disagrees when it does not;
* **an unreported node is excluded, not assumed** — a supervisor too old
  to write the sidecar ships no reading, and folding that in as
  ``default`` would report a genuine mismatch as agreement on exactly the
  nodes that could not answer;
* **the pre-#1017 fleet is quiet** — every appliance installed before
  this feature reports ``default``, so one distinct answer, so no
  finding. An alarm that fires on every existing install is one operators
  learn to dismiss;
* **only approved nodes are compared** — a pending node is not in the k3s
  cluster and cannot black-hole anything.
"""

from __future__ import annotations

import pytest

from app.services.appliance.network_mtu import (
    DEFAULT_ANSWER,
    evaluate_node,
    fleet_summary,
    has_network_report,
    node_answer,
)
from app.services.appliance.storage_health import worst_severity


def health(mtu=None, applied="applied", requested=None):
    """A ``cluster_health`` dict shaped like the supervisor's."""
    return {
        "storage": {"md_supported": False},
        "network": {
            "interface": "eth0",
            "mode": "static",
            "mtu": mtu,
            "mtu_requested": requested if requested is not None else mtu,
            "mtu_applied": applied,
        },
    }


# ── what one node contributes ─────────────────────────────────────────


def test_a_configured_mtu_is_its_own_answer():
    assert node_answer(health(1400)) == "1400"


def test_an_unconfigured_node_answers_default_not_1500():
    """The load-bearing one. ``default`` and ``"1500"`` are different
    claims and only the second was read off a configuration; collapsing
    them would manufacture agreement between a node explicitly set to
    1500 and one nobody configured, and disagreement on an all-9000 L2.
    """
    answer = node_answer(health(None, applied="default"))
    assert answer == DEFAULT_ANSWER
    assert answer != "1500"


def test_a_node_that_never_reported_contributes_nothing():
    assert node_answer({"storage": {}}) is None
    assert node_answer({}) is None
    assert node_answer(None) is None
    assert has_network_report({"storage": {}}) is False


def test_a_node_with_no_keyfile_still_answers_the_link_default():
    """DHCP with no pinned port writes no keyfile, so nothing of OURS is
    in force — but the interface is still running the link default,
    which is the same real state as a ``default`` node, and that one is
    compared.

    Excluding it (the first cut) meant a cluster with one node pinned at
    1400 and one on any-port DHCP reported *consistent* and rendered no
    banner: the host-gw black hole this whole check exists to catch,
    scored as healthy.
    """
    assert node_answer(health(None, applied="n/a", requested="9000")) == DEFAULT_ANSWER


def test_a_pinned_node_and_an_any_port_node_are_compared():
    """The regression the exclusion caused, end to end."""
    rows = [("ddi1", health(1400)), ("ddi2", health(None, applied="n/a"))]
    summary = fleet_summary(rows)
    assert summary.consistent is False
    assert summary.reported == 2


def test_a_dropped_value_answers_default_because_that_is_what_runs():
    """The renderer refused the value, so the link default is what the
    interface is actually running at — which is what the comparison is
    about. The operator still gets told separately.
    """
    assert node_answer(health(None, applied="dropped", requested="9000")) == DEFAULT_ANSWER


# ── per-node findings ─────────────────────────────────────────────────


def test_a_dropped_mtu_is_a_finding_on_its_own():
    findings = evaluate_node(health(None, applied="dropped", requested="1200"))
    assert len(findings) == 1
    assert findings[0].kind == "dropped"
    assert findings[0].severity == "warning"
    assert "1200" in findings[0].detail
    # The SHARED helper from storage_health — duck-typed on ``.severity``
    # and already what every other appliance surface calls. A local copy
    # would be a second severity ranking nobody consults, and it would
    # shadow the one ``supervisor.py`` already imports.
    assert worst_severity(findings) == "warning"


@pytest.mark.parametrize("applied", ["applied", "default", "n/a"])
def test_a_node_that_got_what_it_asked_for_has_no_finding(applied):
    assert evaluate_node(health(1400 if applied == "applied" else None, applied=applied)) == []


def test_a_node_that_never_reported_has_no_finding():
    """UNKNOWN is not a fault. Rendering a finding here would put a
    warning on every appliance whose supervisor predates the feature.
    """
    assert evaluate_node({"storage": {}}) == []


# ── the fleet verdict ─────────────────────────────────────────────────


def test_a_fleet_all_at_the_default_is_consistent():
    """Every appliance installed before #1017 is in this state. It must
    be silent, or the feature ships an alarm on the whole estate.
    """
    rows = [(f"ddi{n}", health(None, applied="default")) for n in (1, 2, 3)]
    summary = fleet_summary(rows)
    assert summary.consistent is True
    assert summary.detail is None
    assert summary.reported == 3


def test_a_fleet_all_at_the_same_explicit_mtu_is_consistent():
    rows = [(f"ddi{n}", health(1400)) for n in (1, 2, 3)]
    assert fleet_summary(rows).consistent is True


def test_one_node_out_of_step_is_a_mismatch_and_is_named():
    rows = [("ddi1", health(9000)), ("ddi2", health(None, applied="default"))]
    summary = fleet_summary(rows)
    assert summary.consistent is False
    assert summary.detail is not None
    # The hostnames are in the message: "MTU mismatch" without them sends
    # the operator to four screens.
    assert "ddi1" in summary.detail and "ddi2" in summary.detail
    assert "host-gw" in summary.detail
    assert summary.answers == {"9000": ["ddi1"], DEFAULT_ANSWER: ["ddi2"]}


def test_an_explicit_1500_and_an_unset_node_are_reported_as_disagreeing():
    """Honest rather than clever. We do not know the unset node's link
    MTU, so we cannot assert these agree — and the message says the
    default was not read from the node, so the operator can settle it.
    """
    rows = [("ddi1", health(1500)), ("ddi2", health(None, applied="default"))]
    summary = fleet_summary(rows)
    assert summary.consistent is False
    assert "not read from the node" in summary.detail


def test_unreported_nodes_do_not_create_or_mask_a_mismatch():
    """A supervisor too old to answer must not be counted as agreeing
    with anyone — that would report a genuine mismatch as consistent on
    exactly the nodes that could not answer.
    """
    rows = [
        ("ddi1", health(9000)),
        ("ddi2", {"storage": {}}),
        ("ddi3", None),
    ]
    summary = fleet_summary(rows)
    assert summary.reported == 1
    assert summary.consistent is True  # one answer; nothing to disagree with
    assert summary.answers == {"9000": ["ddi1"]}


def test_an_empty_fleet_is_consistent():
    summary = fleet_summary([])
    assert summary.consistent is True
    assert summary.reported == 0
    assert summary.answers == {}


def test_three_distinct_answers_are_all_named():
    rows = [
        ("ddi1", health(9000)),
        ("ddi2", health(1400)),
        ("ddi3", health(None, applied="default")),
    ]
    summary = fleet_summary(rows)
    assert summary.consistent is False
    assert set(summary.answers) == {"9000", "1400", DEFAULT_ANSWER}
    for name in ("ddi1", "ddi2", "ddi3"):
        assert name in summary.detail


def test_hostnames_are_sorted_so_the_message_is_stable():
    """The detail string lands in a UI banner and in an alert payload. An
    unstable ordering would make every poll look like a new event.
    """
    rows = [("zeta", health(9000)), ("alpha", health(9000)), ("mid", health(1400))]
    summary = fleet_summary(rows)
    assert summary.answers["9000"] == ["alpha", "zeta"]
    assert fleet_summary(rows).detail == summary.detail
