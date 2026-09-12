"""#1058 — reclaim_stranded_postgres_storage frees a CNPG claim pinned to a
node that no longer exists.

After a dead-node replace the evicted member's CloudNativePG instance claim
stays Bound to a local-path PV whose nodeAffinity names the deleted node; the
operator re-creates the pod against that claim and it sits Pending forever
("didn't match PersistentVolume's node affinity"), Postgres runs 2 of 3 and
the replacement member never hosts an instance. These tests pin the contract:
reclaim exactly the CNPG claims whose PV is pinned to a hostname no Node
carries (the claim first, then the Pending pod, so the operator joins a fresh
instance elsewhere); NEVER the current or target primary's (deferred instead);
never a claim on a live or merely NotReady node; never Redis's or any other
component's; a cheap no-op while Postgres is whole unless a node was evicted
this tick; and kubeapi errors are surfaced, never guessed around — a PV read that is
forbidden or failing is an error, only a PV that does not exist falls back to the
scheduler's selected-node annotation, and the journal records which one decided.
"""

from __future__ import annotations

import json

from spatium_supervisor import k8s_api

CLUSTER = "spatium-control-spatiumddi-postgresql"
CR_PATH = f"/apis/postgresql.cnpg.io/v1/namespaces/spatium/clusters/{CLUSTER}"
PVC_PATH = "/api/v1/namespaces/spatium/persistentvolumeclaims"
POD_PATH = "/api/v1/namespaces/spatium/pods"


class _Recorder:
    """Stand-in for k8s_api._request keyed by path: scripts every GET and
    records every DELETE, in order."""

    def __init__(self, gets: dict[str, tuple[int, object]], delete_status: int = 200):
        self._gets = gets
        self._delete_status = delete_status
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, path, body=None, content_type=None, timeout=None):
        self.calls.append((method, path))
        if method == "GET":
            status, payload = self._gets.get(path, (404, {}))
            raw = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
            return status, raw.encode() if isinstance(raw, str) else raw
        return self._delete_status, b"{}"

    @property
    def deleted(self) -> list[str]:
        return [p for m, p in self.calls if m == "DELETE"]

    @property
    def gets(self) -> list[str]:
        return [p for m, p in self.calls if m == "GET"]


def _cr(ready: int, want: int = 3, current: str = f"{CLUSTER}-1", target: str | None = None):
    return {
        "spec": {"instances": want},
        "status": {
            "readyInstances": ready,
            "currentPrimary": current,
            "targetPrimary": target or current,
        },
    }


def _nodes(*names: str):
    return {"items": [{"metadata": {"name": n}} for n in names]}


def _pvc(name: str, volume: str, *, labels: dict | None = None, selected: str | None = None):
    meta: dict = {"name": name}
    if labels is not None:
        meta["labels"] = labels
    if selected:
        meta["annotations"] = {"volume.kubernetes.io/selected-node": selected}
    return {"metadata": meta, "spec": {"volumeName": volume}}


def _pv(host: str | None):
    if host is None:
        return {"spec": {}}
    return {
        "spec": {
            "nodeAffinity": {
                "required": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {"key": "kubernetes.io/hostname", "operator": "In", "values": [host]}
                            ]
                        }
                    ]
                }
            }
        }
    }


def _cnpg_labels(instance: str, role: str = "PG_DATA") -> dict:
    return {"cnpg.io/cluster": CLUSTER, "cnpg.io/instanceName": instance, "cnpg.io/pvcRole": role}


def _world(
    *,
    ready: int = 2,
    nodes=("ddipg-seed", "ddipg-member-1", "ddipg-member-3"),
    current: str = f"{CLUSTER}-1",
    target: str | None = None,
    pvcs=(),
    pvs=None,
) -> dict:
    gets: dict[str, tuple[int, object]] = {
        CR_PATH: (200, _cr(ready, current=current, target=target)),
        "/api/v1/nodes": (200, _nodes(*nodes)),
        PVC_PATH: (200, {"items": list(pvcs)}),
    }
    for vol, host in (pvs or {}).items():
        gets[f"/api/v1/persistentvolumes/{vol}"] = (200, _pv(host))
    return gets


# The shape the live evidence had (spatiumddi#1058): instance 3's claim on the
# evicted ddipg-member-2, the primary on the seed, the replacement member-3
# hosting nothing.
_LIVE_PVCS = (
    _pvc(f"{CLUSTER}-1", "pv-1", labels=_cnpg_labels(f"{CLUSTER}-1"), selected="ddipg-seed"),
    _pvc(f"{CLUSTER}-2", "pv-2", labels=_cnpg_labels(f"{CLUSTER}-2"), selected="ddipg-member-1"),
    _pvc(f"{CLUSTER}-3", "pv-3", labels=_cnpg_labels(f"{CLUSTER}-3"), selected="ddipg-member-2"),
    _pvc("data-spatium-control-spatiumddi-redis-2", "pv-r2", selected="ddipg-member-2"),
    _pvc("spatium-control-spatiumddi-slot-image-mirror-data", "pv-m", selected="ddipg-member-2"),
)
_LIVE_PVS = {
    "pv-1": "ddipg-seed",
    "pv-2": "ddipg-member-1",
    "pv-3": "ddipg-member-2",
    "pv-r2": "ddipg-member-2",
    "pv-m": "ddipg-member-2",
}


def test_reclaims_the_replica_claim_pinned_to_a_missing_node_then_its_pod(monkeypatch) -> None:
    rec = _Recorder(_world(pvcs=_LIVE_PVCS, pvs=_LIVE_PVS))
    monkeypatch.setattr(k8s_api, "_request", rec)

    reclaimed, deferred, err = k8s_api.reclaim_stranded_postgres_storage()

    assert (reclaimed, deferred, err) == ([f"{CLUSTER}-3"], [], None)
    # The claim first (pvc-protection holds it until its pod is gone), then
    # the Pending pod — and nothing that is not CNPG's, whatever node it is on.
    assert rec.deleted == [f"{PVC_PATH}/{CLUSTER}-3", f"{POD_PATH}/{CLUSTER}-3"]


def test_the_primary_is_deferred_never_deleted(monkeypatch) -> None:
    # The dead node hosted the primary and the operator has not failed over
    # yet: leave the claim alone and say so; the next tick tries again.
    rec = _Recorder(_world(pvcs=_LIVE_PVCS, pvs=_LIVE_PVS, current=f"{CLUSTER}-3"))
    monkeypatch.setattr(k8s_api, "_request", rec)

    reclaimed, deferred, err = k8s_api.reclaim_stranded_postgres_storage()

    assert (reclaimed, deferred, err) == ([], [f"{CLUSTER}-3"], None)
    assert rec.deleted == []


def test_a_target_primary_counts_as_the_primary(monkeypatch) -> None:
    # Mid-failover the operator has already chosen the stranded instance as
    # the next primary (targetPrimary) while currentPrimary still says the old
    # one: touching it now would race the promotion.
    rec = _Recorder(
        _world(pvcs=_LIVE_PVCS, pvs=_LIVE_PVS, current=f"{CLUSTER}-1", target=f"{CLUSTER}-3")
    )
    monkeypatch.setattr(k8s_api, "_request", rec)

    reclaimed, deferred, err = k8s_api.reclaim_stranded_postgres_storage()

    assert (reclaimed, deferred, err) == ([], [f"{CLUSTER}-3"], None)
    assert rec.deleted == []


def test_a_cluster_that_names_no_primary_defers_everything(monkeypatch) -> None:
    # No currentPrimary/targetPrimary in the status (none yet, or wiped by an
    # operator restart): the primary is UNKNOWN, not absent. The first cut
    # read an empty primary set as "everything is a replica" and would have
    # deleted a stranded primary's PGDATA (review, point 2).
    gets = _world(pvcs=_LIVE_PVCS, pvs=_LIVE_PVS)
    gets[CR_PATH] = (200, {"spec": {"instances": 3}, "status": {"readyInstances": 2}})
    rec = _Recorder(gets)
    monkeypatch.setattr(k8s_api, "_request", rec)

    out = k8s_api.reclaim_stranded_postgres_storage()

    assert (out.reclaimed, out.deferred, out.error) == ([], [f"{CLUSTER}-3"], None)
    assert "no current or target primary" in out.deferred_reason
    assert rec.deleted == []


def test_a_cluster_with_no_status_at_all_defers_everything(monkeypatch) -> None:
    gets = _world(pvcs=_LIVE_PVCS, pvs=_LIVE_PVS)
    gets[CR_PATH] = (200, {"spec": {"instances": 3}})
    rec = _Recorder(gets)
    monkeypatch.setattr(k8s_api, "_request", rec)

    out = k8s_api.reclaim_stranded_postgres_storage()

    assert (out.reclaimed, out.deferred) == ([], [f"{CLUSTER}-3"])
    assert rec.deleted == []


def test_the_primary_deferral_says_why(monkeypatch) -> None:
    rec = _Recorder(_world(pvcs=_LIVE_PVCS, pvs=_LIVE_PVS, current=f"{CLUSTER}-3"))
    monkeypatch.setattr(k8s_api, "_request", rec)

    out = k8s_api.reclaim_stranded_postgres_storage()

    assert out.deferred == [f"{CLUSTER}-3"]
    assert "current or target primary" in out.deferred_reason


def test_a_whole_postgres_is_one_cheap_read(monkeypatch) -> None:
    rec = _Recorder(_world(ready=3, pvcs=_LIVE_PVCS, pvs=_LIVE_PVS))
    monkeypatch.setattr(k8s_api, "_request", rec)

    assert tuple(k8s_api.reclaim_stranded_postgres_storage()) == ([], [], None)
    assert rec.gets == [CR_PATH]
    assert rec.deleted == []


def test_an_eviction_this_tick_forces_the_scan_past_a_stale_status(monkeypatch) -> None:
    # Right after the Node deletion the Cluster status may still count the
    # dead instance ready (its pod is not garbage-collected yet); the tick
    # that evicted the node scans anyway.
    rec = _Recorder(_world(ready=3, pvcs=_LIVE_PVCS, pvs=_LIVE_PVS))
    monkeypatch.setattr(k8s_api, "_request", rec)

    reclaimed, deferred, err = k8s_api.reclaim_stranded_postgres_storage(
        evicted_nodes=["ddipg-member-2"]
    )

    assert (reclaimed, deferred, err) == ([f"{CLUSTER}-3"], [], None)


def test_a_claim_on_a_node_that_still_exists_is_left_alone(monkeypatch) -> None:
    # NotReady is not gone: the Node object is still registered, so the node
    # (and the data on it) can come back. Only a hostname absent from the
    # Node list counts as stranded.
    rec = _Recorder(
        _world(
            nodes=("ddipg-seed", "ddipg-member-1", "ddipg-member-2"),
            pvcs=_LIVE_PVCS,
            pvs=_LIVE_PVS,
        )
    )
    monkeypatch.setattr(k8s_api, "_request", rec)

    assert tuple(k8s_api.reclaim_stranded_postgres_storage()) == ([], [], None)
    assert rec.deleted == []


def test_the_instances_wal_claim_goes_with_its_data_claim(monkeypatch) -> None:
    pvcs = (
        *_LIVE_PVCS,
        _pvc(f"{CLUSTER}-3-wal", "pv-3w", labels=_cnpg_labels(f"{CLUSTER}-3", "PG_WAL")),
    )
    rec = _Recorder(_world(pvcs=pvcs, pvs={**_LIVE_PVS, "pv-3w": "ddipg-member-2"}))
    monkeypatch.setattr(k8s_api, "_request", rec)

    reclaimed, deferred, err = k8s_api.reclaim_stranded_postgres_storage()

    assert (deferred, err) == ([], None)
    assert sorted(reclaimed) == [f"{CLUSTER}-3", f"{CLUSTER}-3-wal"]
    assert rec.deleted[-1] == f"{POD_PATH}/{CLUSTER}-3"


def test_unlabelled_claims_are_recognised_by_cnpg_naming(monkeypatch) -> None:
    # Older CNPG releases label less; the documented <cluster>-<ordinal>
    # naming (with -wal / -tbs-<name> suffixes) still identifies the instance.
    pvcs = (
        _pvc(f"{CLUSTER}-1", "pv-1"),
        _pvc(f"{CLUSTER}-3", "pv-3"),
        _pvc(f"{CLUSTER}-3-wal", "pv-3w"),
        _pvc(f"{CLUSTER}-3-tbs-fast", "pv-3t"),
        _pvc("data-spatium-control-spatiumddi-redis-2", "pv-r2"),
    )
    pvs = {
        "pv-1": "ddipg-seed",
        "pv-3": "ddipg-member-2",
        "pv-3w": "ddipg-member-2",
        "pv-3t": "ddipg-member-2",
        "pv-r2": "ddipg-member-2",
    }
    rec = _Recorder(_world(pvcs=pvcs, pvs=pvs))
    monkeypatch.setattr(k8s_api, "_request", rec)

    reclaimed, deferred, err = k8s_api.reclaim_stranded_postgres_storage()

    assert (deferred, err) == ([], None)
    assert sorted(reclaimed) == [f"{CLUSTER}-3", f"{CLUSTER}-3-tbs-fast", f"{CLUSTER}-3-wal"]
    assert f"{PVC_PATH}/data-spatium-control-spatiumddi-redis-2" not in rec.deleted


def test_another_clusters_claims_are_not_ours(monkeypatch) -> None:
    pvcs = (
        _pvc("other-postgres-3", "pv-o3", labels={"cnpg.io/cluster": "other-postgres"}),
    )
    rec = _Recorder(_world(pvcs=pvcs, pvs={"pv-o3": "ddipg-member-2"}))
    monkeypatch.setattr(k8s_api, "_request", rec)

    assert tuple(k8s_api.reclaim_stranded_postgres_storage()) == ([], [], None)
    assert rec.deleted == []


def test_network_storage_is_never_stranded(monkeypatch) -> None:
    # A readable PV with no hostname affinity is not node-local; the
    # scheduler's selected-node annotation must not make it look stranded.
    pvcs = (_pvc(f"{CLUSTER}-3", "pv-3", selected="ddipg-member-2"),)
    rec = _Recorder(_world(pvcs=pvcs, pvs={"pv-3": None}))
    monkeypatch.setattr(k8s_api, "_request", rec)

    assert tuple(k8s_api.reclaim_stranded_postgres_storage()) == ([], [], None)
    assert rec.deleted == []


def test_a_missing_pv_falls_back_to_the_selected_node_annotation(monkeypatch) -> None:
    # 404 = there is no PV object to read (unbound, or already released and
    # gone): the scheduler's annotation is the only anchor left, and the
    # journal says so.
    pvcs = (
        _pvc(f"{CLUSTER}-3", "pv-3", labels=_cnpg_labels(f"{CLUSTER}-3"), selected="ddipg-member-2"),
    )
    rec = _Recorder(_world(pvcs=pvcs, pvs={}))  # no PV scripted → 404
    monkeypatch.setattr(k8s_api, "_request", rec)

    out = k8s_api.reclaim_stranded_postgres_storage()

    assert tuple(out) == ([f"{CLUSTER}-3"], [], None)
    assert out.sources == {f"{CLUSTER}-3": "annotation"}


def test_the_live_shape_records_the_pv_as_the_source(monkeypatch) -> None:
    rec = _Recorder(_world(pvcs=_LIVE_PVCS, pvs=_LIVE_PVS))
    monkeypatch.setattr(k8s_api, "_request", rec)

    out = k8s_api.reclaim_stranded_postgres_storage()

    assert out.reclaimed == [f"{CLUSTER}-3"]
    assert out.sources == {f"{CLUSTER}-3": "pv"}


def test_a_forbidden_pv_read_is_an_error_not_a_guess(monkeypatch) -> None:
    # The first cut fell back to the annotation on ANY non-200 — and the
    # supervisor's ClusterRole had no persistentvolumes grant, so every
    # production read was a 403 that silently decided on the annotation
    # (review of the first cut). A 403 is a misconfiguration to surface.
    gets = _world(pvcs=_LIVE_PVCS, pvs=_LIVE_PVS)
    gets["/api/v1/persistentvolumes/pv-1"] = (403, "forbidden")
    rec = _Recorder(gets)
    monkeypatch.setattr(k8s_api, "_request", rec)

    out = k8s_api.reclaim_stranded_postgres_storage()

    assert out.reclaimed == [] and out.deferred == []
    assert out.error is not None and "403" in out.error and "persistentvolumes/pv-1" in out.error
    assert rec.deleted == []


def test_a_pv_server_error_is_an_error_not_a_guess(monkeypatch) -> None:
    gets = _world(pvcs=_LIVE_PVCS, pvs=_LIVE_PVS)
    gets["/api/v1/persistentvolumes/pv-3"] = (500, "boom")
    rec = _Recorder(gets)
    monkeypatch.setattr(k8s_api, "_request", rec)

    out = k8s_api.reclaim_stranded_postgres_storage()

    assert out.reclaimed == []
    assert out.error is not None and "500" in out.error
    assert rec.deleted == []


def test_a_pv_transport_failure_is_an_error_not_a_guess(monkeypatch) -> None:
    inner = _Recorder(_world(pvcs=_LIVE_PVCS, pvs=_LIVE_PVS))

    def flaky(method, path, **kw):
        if path == "/api/v1/persistentvolumes/pv-3":
            raise RuntimeError("kubeapi GET /api/v1/persistentvolumes/pv-3: timed out")
        return inner(method, path, **kw)

    monkeypatch.setattr(k8s_api, "_request", flaky)

    out = k8s_api.reclaim_stranded_postgres_storage()

    assert out.reclaimed == []
    assert out.error is not None and "timed out" in out.error
    assert inner.deleted == []


def test_no_cnpg_cluster_is_a_quiet_no_op(monkeypatch) -> None:
    rec = _Recorder({CR_PATH: (404, {})})
    monkeypatch.setattr(k8s_api, "_request", rec)

    assert tuple(k8s_api.reclaim_stranded_postgres_storage(evicted_nodes=["x"])) == ([], [], None)
    assert rec.deleted == []


def test_kubeapi_failures_are_reported_not_guessed_around(monkeypatch) -> None:
    gets = _world(pvcs=_LIVE_PVCS, pvs=_LIVE_PVS)
    gets[PVC_PATH] = (500, "boom")
    rec = _Recorder(gets)
    monkeypatch.setattr(k8s_api, "_request", rec)

    reclaimed, deferred, err = k8s_api.reclaim_stranded_postgres_storage()

    assert (reclaimed, deferred) == ([], [])
    assert err is not None and "500" in err
    assert rec.deleted == []


def test_a_refused_delete_stops_and_reports(monkeypatch) -> None:
    rec = _Recorder(_world(pvcs=_LIVE_PVCS, pvs=_LIVE_PVS), delete_status=403)
    monkeypatch.setattr(k8s_api, "_request", rec)

    reclaimed, _deferred, err = k8s_api.reclaim_stranded_postgres_storage()

    assert reclaimed == []
    assert err is not None and "403" in err
    # No pod delete after a refused claim delete — the order is the contract.
    assert rec.deleted == [f"{PVC_PATH}/{CLUSTER}-3"]
