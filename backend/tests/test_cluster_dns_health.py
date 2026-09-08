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
    _resolver_ip,
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
        "app.services.appliance.cluster_health._resolver_ip",
        lambda *a, **k: "10.43.0.10",
    )
    # The probe memoizes its verdict for 15 s so the 2 s SSE loop does not
    # hammer CoreDNS. conftest's ``_reset_global_caches`` clears it around
    # every test — this stub would otherwise be answered from a previous
    # test's verdict.


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


def test_the_probe_still_runs_when_pods_cannot_be_listed():
    """THE one that decides whether the alert exists at all.

    The alert evaluator runs in the Celery worker, whose ServiceAccount
    deliberately carries ``nodes`` + ``nodes/stats`` and NOT a
    cluster-wide pod list. With the probe behind the pods-listable gate
    the matcher raised ``AlertDataUnavailable`` on every tick forever, so
    CoreDNS could be entirely down while the rule sat silent and enabled
    — the exact "dead code that looked enabled" failure worker-rbac.yaml
    was written to prevent for its sibling rule.

    The probe needs no RBAC: it is a DNS query from the pod.
    """
    out = _cluster_dns_health([], nodes_total=3, pods_listable=False, from_node="n1")
    assert out["resolve_probe"] is not None
    assert out["resolve_probe"]["ok"] is True
    assert out["resolve_probe"]["from_node"] == "n1"
    assert out["resolver_ip"] == "10.43.0.10"


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
    assert _resolver_ip(str(conf)) == "10.43.0.10"


def test_resolver_ip_is_none_when_there_is_no_nameserver(tmp_path):
    conf = tmp_path / "resolv.conf"
    conf.write_text("search example.com\noptions ndots:5\n")
    assert _resolver_ip(str(conf)) is None


def test_resolver_ip_is_none_when_the_file_is_missing(tmp_path):
    assert _resolver_ip(str(tmp_path / "nope")) is None


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


# ── the alert matcher, which nothing covered before ───────────────────
#
# The co-location branch was unreachable for the entire life of the
# feature and no test noticed, because the suite exercised only the
# collector. These drive the matcher itself.


def _snapshot(**cluster_dns):
    base = {
        "available": True,
        "detail": None,
        "resolver_ip": "10.43.0.10",
        "replicas_ready": 2,
        "replicas_total": 2,
        "expected_replicas": 2,
        "nodes": ["n1", "n2"],
        "spread_ok": True,
        "resolve_probe": {"ok": True, "latency_ms": 1.0, "error": None, "from_node": "n1"},
        "checked_at": "2026-09-07T00:00:00+00:00",
    }
    base.update(cluster_dns)
    return {"available": True, "detail": None, "cluster_dns": base}


async def _match(monkeypatch, snap):
    from app.services import alerts as alerts_mod

    async def _snap():
        return snap

    monkeypatch.setattr(alerts_mod, "_cluster_health_snapshot", _snap)
    return await alerts_mod._matching_cluster_dns_subjects(None, None)


@pytest.mark.asyncio
async def test_two_replicas_on_one_node_raises_a_warning(monkeypatch):
    """#633's exact failure, and the reason this rule has a WARNING arm.

    The producer emits ``sorted(set(ready_nodes))``, so the old test
    ``len(set(nodes)) < len(nodes)`` was unsatisfiable and this branch
    could never fire — the dashboard rendered amber while the alert plane
    stayed silent. ``len(nodes) < ready`` survives the dedup.
    """
    matches = await _match(monkeypatch, _snapshot(nodes=["n1"], spread_ok=False, replicas_ready=2))
    assert len(matches) == 1
    _sid, _disp, message, severity = matches[0]
    assert severity == "warning"
    assert "same node" in message or "on 1 node" in message


@pytest.mark.asyncio
async def test_a_healthy_cluster_matches_nothing(monkeypatch):
    assert await _match(monkeypatch, _snapshot()) == []


@pytest.mark.asyncio
async def test_a_thin_deployment_warns(monkeypatch):
    matches = await _match(monkeypatch, _snapshot(replicas_ready=1, nodes=["n1"], spread_ok=False))
    assert matches and matches[0][3] == "warning"
    assert "1 of 2" in matches[0][2]


@pytest.mark.asyncio
async def test_no_ready_replicas_is_critical(monkeypatch):
    matches = await _match(monkeypatch, _snapshot(replicas_ready=0, nodes=[], spread_ok=False))
    assert matches and matches[0][3] == "critical"


@pytest.mark.asyncio
async def test_a_failed_probe_is_critical_even_with_healthy_replicas(monkeypatch):
    """The distinct state the card and the alert both have to keep
    legible: pods fine, path broken — kube-proxy or the CNI, not CoreDNS.
    """
    probe = {"ok": False, "latency_ms": 2000.0, "error": "timed out", "from_node": "n3"}
    matches = await _match(monkeypatch, _snapshot(resolve_probe=probe))
    assert matches and matches[0][3] == "critical"
    assert "probed from n3" in matches[0][2]


@pytest.mark.asyncio
async def test_an_unknown_replica_view_with_a_failed_probe_still_alerts(monkeypatch):
    """The worker cannot list pods, so this is the shape it actually sees.

    If the probe fails there, that is a fact worth alerting on by itself
    — independent of whether the replica view could be read.
    """
    snap = {
        "available": True,
        "detail": None,
        "cluster_dns": {
            "available": False,
            "detail": "pods could not be listed",
            "resolver_ip": "10.43.0.10",
            "replicas_ready": None,
            "replicas_total": None,
            "expected_replicas": None,
            "nodes": [],
            "spread_ok": None,
            "resolve_probe": {
                "ok": False,
                "latency_ms": 2000.0,
                "error": "timed out",
                "from_node": "n1",
            },
            "checked_at": "2026-09-07T00:00:00+00:00",
        },
    }
    matches = await _match(monkeypatch, snap)
    assert matches and matches[0][3] == "critical"


@pytest.mark.asyncio
async def test_an_unknown_view_with_a_passing_probe_raises_rather_than_resolving(monkeypatch):
    """Unknown must neither fire nor silently clear a real open event."""
    from app.services.alerts import AlertDataUnavailable

    snap = {
        "available": True,
        "detail": None,
        "cluster_dns": {
            "available": False,
            "detail": "pods could not be listed",
            "resolver_ip": "10.43.0.10",
            "replicas_ready": None,
            "replicas_total": None,
            "expected_replicas": None,
            "nodes": [],
            "spread_ok": None,
            "resolve_probe": {"ok": True, "latency_ms": 1.0, "error": None, "from_node": "n1"},
            "checked_at": "2026-09-07T00:00:00+00:00",
        },
    }
    with pytest.raises(AlertDataUnavailable):
        await _match(monkeypatch, snap)
