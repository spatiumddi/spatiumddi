"""#590 — reclaim_stranded_redis_storage frees node-affine Redis PVCs.

Evicting a dead node strands every local-path PVC provisioned on it (the
PV is node-affine), so the StatefulSet's replacement pod sits Pending
forever and the missing replica's sentinel silently drops the failover
quorum. These tests pin the contract: reclaim exactly the Redis claims
annotated with the deleted node (PVC first, then the Pending consumer
pod so the StatefulSet recreates both), never touch CNPG's claims or
other nodes' claims, and surface kubeapi errors instead of guessing.
"""

from __future__ import annotations

import json

from spatium_supervisor import k8s_api


class _Recorder:
    """Stand-in for k8s_api._request that scripts the PVC-list GET and
    records every follow-up DELETE."""

    def __init__(self, get_status: int, get_body: str, delete_status: int = 200):
        self._get = (get_status, get_body)
        self._delete_status = delete_status
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, path, body=None, content_type=None):
        self.calls.append((method, path))
        if method == "GET":
            return self._get
        return (self._delete_status, "{}")

    @property
    def deleted(self) -> list[str]:
        return [p for m, p in self.calls if m == "DELETE"]


def _pvc(name: str, node: str | None) -> dict:
    meta: dict = {"name": name}
    if node is not None:
        meta["annotations"] = {"volume.kubernetes.io/selected-node": node}
    return {"metadata": meta}


def _pvc_list(*items: dict) -> str:
    return json.dumps({"items": list(items)})


def test_reclaims_redis_pvc_and_pending_pod_on_dead_node(monkeypatch) -> None:
    rec = _Recorder(200, _pvc_list(
        _pvc("data-spatium-control-spatiumddi-redis-2", "ddipg-member-2"),
        _pvc("data-spatium-control-spatiumddi-redis-0", "ddipg-seed"),
    ))
    monkeypatch.setattr(k8s_api, "_request", rec)

    reclaimed, err = k8s_api.reclaim_stranded_redis_storage("ddipg-member-2")

    assert (reclaimed, err) == (["data-spatium-control-spatiumddi-redis-2"], None)
    # PVC first (pvc-protection holds it until its pod is gone), then the pod.
    assert rec.deleted == [
        "/api/v1/namespaces/spatium/persistentvolumeclaims/"
        "data-spatium-control-spatiumddi-redis-2",
        "/api/v1/namespaces/spatium/pods/spatium-control-spatiumddi-redis-2",
    ]


def test_never_touches_cnpg_or_other_nodes(monkeypatch) -> None:
    # CNPG deletes + recreates its own instance PVCs; reclaiming one out
    # from under the operator would fight its reconcile. And claims on
    # LIVE nodes are healthy — only the deleted node's are stranded.
    rec = _Recorder(200, _pvc_list(
        _pvc("spatium-control-spatiumddi-postgresql-2", "ddipg-member-2"),
        _pvc("data-spatium-control-spatiumddi-redis-1", "ddipg-member-1"),
    ))
    monkeypatch.setattr(k8s_api, "_request", rec)

    reclaimed, err = k8s_api.reclaim_stranded_redis_storage("ddipg-member-2")

    assert (reclaimed, err) == ([], None)
    assert rec.deleted == []


def test_unannotated_pvc_is_left_alone(monkeypatch) -> None:
    # No selected-node annotation = not a WaitForFirstConsumer local-path
    # claim (or not yet scheduled) — there is nothing node-affine to free.
    rec = _Recorder(200, _pvc_list(
        _pvc("data-spatium-control-spatiumddi-redis-2", None),
    ))
    monkeypatch.setattr(k8s_api, "_request", rec)

    reclaimed, err = k8s_api.reclaim_stranded_redis_storage("ddipg-member-2")

    assert (reclaimed, err) == ([], None)
    assert rec.deleted == []


def test_list_failure_reports_instead_of_guessing(monkeypatch) -> None:
    rec = _Recorder(500, "boom")
    monkeypatch.setattr(k8s_api, "_request", rec)

    reclaimed, err = k8s_api.reclaim_stranded_redis_storage("ddipg-member-2")

    assert reclaimed == []
    assert err is not None and "500" in err
    assert rec.deleted == []
