"""Cluster DNS (CoreDNS) health on the cluster-health snapshot (#985).

We have *acted* on cluster DNS since #590 / #750 — ``ensure_coredns_ha``
matches replica count and spread to the node count — and showed nothing
about it anywhere, so a CoreDNS that was down, single-replica or
co-located read as "everything healthy" until an unrelated pod restart
failed to resolve.

What is worth pinning here is mostly about **not lying**:

* an unlistable ``kube-system`` must not read as "no CoreDNS replicas";
* a cluster that labels its DNS differently (GKE calls the deployment
  ``kube-dns``) must not read as a missing deployment either;
* two replicas on the same node is not spread, which is #633's failure
  and the reason ``ensure_coredns_ha`` uses *required* anti-affinity;
* replicas existing and DNS answering are different facts, so a failed
  probe with healthy replicas must stay legible as its own diagnosis.

Nothing here talks to a real cluster; the pod fixtures are the shape
kubeapi returns.
"""

from __future__ import annotations

import pytest

from app.services.appliance.cluster_health import (
    _cluster_dns_health,
    _resolver_ip_from_resolv_conf,
)

# Bound at import time, before the autouse fixture below patches the
# module attribute — so the two tests that exercise the real probe get
# the real probe, not the stub every other test relies on.
from app.services.appliance.cluster_health import (  # isort: skip
    _cluster_dns_probe as _real_probe,
)


def _dns_pod(name: str, *, node: str, ready: bool = True, phase: str = "Running"):
    return {
        "metadata": {
            "name": name,
            "namespace": "kube-system",
            "labels": {"k8s-app": "kube-dns"},
        },
        "spec": {"nodeName": node},
        "status": {
            "phase": phase,
            "containerStatuses": [{"ready": ready}],
        },
    }


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """Never let these tests issue a real query.

    The probe is exercised on its own below; everywhere else it is
    stubbed so a test machine's resolver cannot change a verdict.
    """
    monkeypatch.setattr(
        "app.services.appliance.cluster_health._cluster_dns_probe",
        lambda ip: {"ok": True, "latency_ms": 0.4, "error": None},
    )
    monkeypatch.setattr(
        "app.services.appliance.cluster_health._resolver_ip_from_resolv_conf",
        lambda *a, **k: "10.43.0.10",
    )


# ── the two "unknown" cases, which must never read as zero ────────────


def test_unlistable_pods_report_unknown_not_zero_replicas():
    """A 403 on the cluster-wide pod list returns an EMPTY list, not an
    error — so without this branch the card would claim there are no
    CoreDNS replicas on a cluster we simply cannot see.
    """
    out = _cluster_dns_health([], nodes_total=3, pods_listable=False, from_node="n1")
    assert out["available"] is False
    assert out["replicas_ready"] is None
    assert out["replicas_total"] is None
    assert out["spread_ok"] is None
    assert "pods could not be listed" in out["detail"]


def test_no_matching_pods_still_reports_the_probe():
    """On a BYO cluster that labels its DNS differently, the replica view
    is unknown but the probe still answers the question that matters.
    """
    other = {
        "metadata": {"name": "x", "namespace": "kube-system", "labels": {"k8s-app": "other"}},
        "spec": {"nodeName": "n1"},
        "status": {"phase": "Running", "containerStatuses": [{"ready": True}]},
    }
    out = _cluster_dns_health([other], nodes_total=1, pods_listable=True, from_node="n1")
    assert out["available"] is False
    assert out["replicas_ready"] is None
    assert out["resolve_probe"]["ok"] is True
    assert out["resolver_ip"] == "10.43.0.10"


def test_the_label_not_the_deployment_name_is_what_matches():
    """GKE's deployment is called ``kube-dns``, k3s's is ``coredns``. The
    label is the same on both, which is why the label is the selector.
    """
    pod = _dns_pod("kube-dns-abc", node="n1")
    pod["metadata"]["name"] = "kube-dns-7f9c-xyz"  # nothing named "coredns"
    out = _cluster_dns_health([pod], nodes_total=1, pods_listable=True, from_node="n1")
    assert out["available"] is True
    assert out["replicas_ready"] == 1


def test_a_matching_label_in_another_namespace_is_ignored():
    pod = _dns_pod("coredns-a", node="n1")
    pod["metadata"]["namespace"] = "default"
    out = _cluster_dns_health([pod], nodes_total=1, pods_listable=True, from_node="n1")
    assert out["available"] is False


# ── replica counting + spread ─────────────────────────────────────────


def test_single_node_with_one_replica_is_healthy():
    """``ensure_coredns_ha`` targets STOCK on a single node — one replica
    is the correct state there, not a degraded one.
    """
    out = _cluster_dns_health(
        [_dns_pod("coredns-a", node="n1")], nodes_total=1, pods_listable=True, from_node="n1"
    )
    assert out["replicas_ready"] == 1
    assert out["expected_replicas"] == 1
    assert out["spread_ok"] is True


def test_two_replicas_on_two_nodes_is_healthy():
    pods = [_dns_pod("coredns-a", node="n1"), _dns_pod("coredns-b", node="n2")]
    out = _cluster_dns_health(pods, nodes_total=3, pods_listable=True, from_node="n1")
    assert out["replicas_ready"] == 2
    assert out["expected_replicas"] == 2, "the target is min(nodes, 2), not one-per-node"
    assert out["spread_ok"] is True
    assert out["nodes"] == ["n1", "n2"]


def test_two_replicas_on_the_same_node_is_not_spread():
    """#633's failure: preferred anti-affinity parked both replicas on the
    seed, and Kubernetes never rebalances running pods — so "2 ready" was
    reported as HA while losing one node took cluster DNS with it.
    """
    pods = [_dns_pod("coredns-a", node="n1"), _dns_pod("coredns-b", node="n1")]
    out = _cluster_dns_health(pods, nodes_total=3, pods_listable=True, from_node="n1")
    assert out["replicas_ready"] == 2
    assert out["spread_ok"] is False
    assert out["nodes"] == ["n1"]


def test_a_single_replica_on_a_multi_node_cluster_is_not_spread():
    out = _cluster_dns_health(
        [_dns_pod("coredns-a", node="n1")], nodes_total=3, pods_listable=True, from_node="n1"
    )
    assert out["replicas_ready"] == 1
    assert out["expected_replicas"] == 2
    assert out["spread_ok"] is False


def test_a_pod_that_exists_but_is_not_ready_is_counted_separately():
    """``replicas_total > replicas_ready`` is how a crash-looping replica
    stays visible instead of just vanishing from the count.
    """
    pods = [
        _dns_pod("coredns-a", node="n1"),
        _dns_pod("coredns-b", node="n2", ready=False),
    ]
    out = _cluster_dns_health(pods, nodes_total=2, pods_listable=True, from_node="n1")
    assert out["replicas_ready"] == 1
    assert out["replicas_total"] == 2


def test_terminal_pods_are_not_counted_at_all():
    pods = [
        _dns_pod("coredns-a", node="n1"),
        _dns_pod("coredns-old", node="n2", phase="Succeeded"),
        _dns_pod("coredns-dead", node="n2", phase="Failed"),
    ]
    out = _cluster_dns_health(pods, nodes_total=2, pods_listable=True, from_node="n1")
    assert out["replicas_ready"] == 1
    assert out["replicas_total"] == 1


def test_zero_ready_replicas_is_reported_as_zero_not_unknown():
    """The one place a 0 is right: we listed the pods, and none is ready."""
    pods = [_dns_pod("coredns-a", node="n1", ready=False)]
    out = _cluster_dns_health(pods, nodes_total=1, pods_listable=True, from_node="n1")
    assert out["available"] is True
    assert out["replicas_ready"] == 0
    assert out["spread_ok"] is False


def test_the_probe_is_labelled_with_the_vantage_node():
    """On a multi-node control plane the snapshot is served by whichever
    api replica took the request, so a pass is a statement about one
    vantage — the card and the alert both say which.
    """
    out = _cluster_dns_health(
        [_dns_pod("coredns-a", node="n1")], nodes_total=1, pods_listable=True, from_node="node-3"
    )
    assert out["resolve_probe"]["from_node"] == "node-3"


# ── resolver discovery ────────────────────────────────────────────────


def test_resolver_ip_is_read_from_resolv_conf(tmp_path):
    conf = tmp_path / "resolv.conf"
    conf.write_text(
        "# generated by kubelet\nsearch spatium.svc.cluster.local\n"
        "nameserver 10.43.0.10\nnameserver 10.43.0.11\noptions ndots:5\n"
    )
    assert _resolver_ip_from_resolv_conf(str(conf)) == "10.43.0.10"


def test_resolver_ip_is_none_when_there_is_no_nameserver(tmp_path):
    conf = tmp_path / "resolv.conf"
    conf.write_text("search example.com\noptions ndots:5\n")
    assert _resolver_ip_from_resolv_conf(str(conf)) is None


def test_resolver_ip_is_none_when_the_file_is_missing(tmp_path):
    assert _resolver_ip_from_resolv_conf(str(tmp_path / "nope")) is None


# ── the probe itself ──────────────────────────────────────────────────


def test_probe_without_a_resolver_reports_that_rather_than_failing_silently():
    out = _real_probe(None)
    assert out["ok"] is False
    assert "resolv.conf" in out["error"]


def test_probe_error_is_never_the_empty_string(monkeypatch):
    """dnspython raises exceptions whose ``str()`` is sometimes empty —
    the #735 lesson. An error of "" tells the operator nothing.
    """
    import dns.exception

    class _Boom(dns.exception.DNSException):
        def __str__(self):
            return ""

    class _Resolver:
        nameservers: list[str] = []
        timeout = 0.0
        lifetime = 0.0

        def resolve(self, *a, **k):
            raise _Boom()

    monkeypatch.setattr("dns.resolver.Resolver", lambda **kw: _Resolver())
    out = _real_probe("10.43.0.10")
    assert out["ok"] is False
    assert out["error"] and "_Boom" in out["error"]
    assert out["latency_ms"] is not None


# ── snapshot integration ──────────────────────────────────────────────


def test_the_unavailable_snapshot_still_carries_a_cluster_dns_block():
    """The key must always exist so no caller has to probe for it — and
    every count inside it is unknown, not zero.
    """
    from app.services.appliance.cluster_health import cluster_unavailable

    snap = cluster_unavailable("kubeapi node list returned HTTP 403")
    assert "cluster_dns" in snap
    assert snap["cluster_dns"]["available"] is False
    assert snap["cluster_dns"]["replicas_ready"] is None
