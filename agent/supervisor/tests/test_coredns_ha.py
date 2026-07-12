"""#590 — ensure_coredns_ha keeps cluster DNS able to survive a node loss.

k3s ships CoreDNS as a single replica that deterministically lands on the
seed and takes the k8s-default 300s to evict — a hard seed kill silences
all Service/pod-FQDN resolution (the api readiness gate's Postgres and
Redis lookups included) for longer than any recovery budget. These tests
pin the reconciler's contract: patch a stock deployment to 2 replicas +
fast-evict tolerations (stock entries preserved) + REQUIRED anti-affinity;
converge idempotently; upgrade a preferred-affinity patch from an earlier
build; and surface kubeapi errors.
"""

from __future__ import annotations

import json

from spatium_supervisor import k8s_api


class _Recorder:
    def __init__(self, get_status: int, get_body: str, patch_status: int = 200):
        self._get = (get_status, get_body)
        self._patch_status = patch_status
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(self, method, path, body=None, content_type=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return self._get
        return (self._patch_status, "{}")

    @property
    def patched(self) -> bool:
        return any(m == "PATCH" for m, _, _ in self.calls)

    @property
    def patch_body(self) -> dict:
        for method, _, body in self.calls:
            if method == "PATCH" and body is not None:
                return json.loads(body)
        return {}


_STOCK_TOLERATIONS = [
    {"key": "CriticalAddonsOnly", "operator": "Exists"},
    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists",
     "effect": "NoSchedule"},
]

_FAST_TOLERATIONS = [
    {"key": "node.kubernetes.io/unreachable", "operator": "Exists",
     "effect": "NoExecute", "tolerationSeconds": 20},
    {"key": "node.kubernetes.io/not-ready", "operator": "Exists",
     "effect": "NoExecute", "tolerationSeconds": 20},
]

_REQUIRED_SPREAD = {
    "podAntiAffinity": {
        "requiredDuringSchedulingIgnoredDuringExecution": [{
            "topologyKey": "kubernetes.io/hostname",
            "labelSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        }],
    },
}


def _deploy(replicas: int, tolerations: list, affinity: dict | None = None) -> str:
    tmpl_spec: dict = {"tolerations": tolerations}
    if affinity is not None:
        tmpl_spec["affinity"] = affinity
    return json.dumps({"spec": {"replicas": replicas,
                                "template": {"spec": tmpl_spec}}})


def test_stock_manifest_gets_replicas_tolerations_and_required_spread(monkeypatch) -> None:
    rec = _Recorder(200, _deploy(1, _STOCK_TOLERATIONS))
    monkeypatch.setattr(k8s_api, "_request", rec)

    changed, err = k8s_api.ensure_coredns_ha()

    assert (changed, err) == (True, None)
    body = rec.patch_body
    assert body["spec"]["replicas"] == 2
    # Stock tolerations ride along — the merge-patch REPLACES the list.
    assert body["spec"]["template"]["spec"]["tolerations"] == (
        _STOCK_TOLERATIONS + _FAST_TOLERATIONS)
    assert body["spec"]["template"]["spec"]["affinity"] == _REQUIRED_SPREAD


def test_idempotent_when_converged(monkeypatch) -> None:
    rec = _Recorder(200, _deploy(
        2, _STOCK_TOLERATIONS + _FAST_TOLERATIONS, _REQUIRED_SPREAD))
    monkeypatch.setattr(k8s_api, "_request", rec)

    changed, err = k8s_api.ensure_coredns_ha()

    assert (changed, err) == (False, None)
    assert not rec.patched


def test_preferred_spread_from_earlier_build_is_upgraded(monkeypatch) -> None:
    # The first cut of this reconciler used preferred anti-affinity, which
    # parked BOTH replicas on the seed when it fired on the single-node
    # firstboot (k8s never rebalances running pods) — the "HA" DNS died
    # with the seed anyway. A cluster patched by that build must be
    # re-patched to the required form, not short-circuited as converged.
    preferred = {"podAntiAffinity": {
        "preferredDuringSchedulingIgnoredDuringExecution": [{"weight": 100}]}}
    rec = _Recorder(200, _deploy(
        2, _STOCK_TOLERATIONS + _FAST_TOLERATIONS, preferred))
    monkeypatch.setattr(k8s_api, "_request", rec)

    changed, err = k8s_api.ensure_coredns_ha()

    assert (changed, err) == (True, None)
    assert rec.patch_body["spec"]["template"]["spec"]["affinity"] == _REQUIRED_SPREAD


def test_missing_deployment_reports(monkeypatch) -> None:
    rec = _Recorder(404, "not found")
    monkeypatch.setattr(k8s_api, "_request", rec)

    changed, err = k8s_api.ensure_coredns_ha()

    assert changed is False
    assert err is not None and "404" in err
    assert not rec.patched
