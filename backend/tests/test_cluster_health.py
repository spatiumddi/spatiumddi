"""Tests for the Cluster health snapshot service + endpoint (#402).

The gather reads nodes + pods cluster-wide and the kubelet Summary API via
the api pod's ServiceAccount. Here we monkeypatch the three ``k8s`` helpers
with realistic kubeapi-shaped fixtures and assert the rollup (KPIs, per-node
live usage, workload health, top pods) + the degraded paths (no nodes/proxy
grant → no live usage; nodes 403 → available=false; SA missing → 503).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.services.appliance import cluster_health

_HEALTH_URL = "/api/v1/appliance/cluster/health"


# ── kube-shaped fixtures ────────────────────────────────────────────────────


def _node(name: str = "ddi1", *, ready: bool = True, cpu: str = "4", mem: str = "8Gi") -> dict:
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": "2026-06-12T19:00:00Z",
            "labels": {
                "node-role.kubernetes.io/control-plane": "true",
                "node-role.kubernetes.io/etcd": "true",
            },
        },
        "spec": {"unschedulable": False},
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True" if ready else "False"},
                {"type": "MemoryPressure", "status": "False"},
                {"type": "DiskPressure", "status": "False"},
            ],
            "addresses": [{"type": "InternalIP", "address": "192.168.0.199"}],
            "nodeInfo": {
                "kubeletVersion": "v1.35.5+k3s1",
                "osImage": "Debian GNU/Linux 13 (trixie)",
                "kernelVersion": "6.12.90",
                "containerRuntimeVersion": "containerd://2.2.3-k3s1",
                "architecture": "amd64",
            },
            "capacity": {"cpu": cpu, "memory": mem, "pods": "110"},
        },
    }


def _pod(
    name: str,
    *,
    ns: str = "spatium",
    comp: str = "api",
    node: str = "ddi1",
    phase: str = "Running",
    ready: bool = True,
    restarts: int = 0,
    owner: str = "ReplicaSet",
    waiting: str | None = None,
) -> dict:
    state: dict = (
        {"running": {}} if ready else {"waiting": {"reason": waiting or "ContainerCreating"}}
    )
    return {
        "metadata": {
            "name": name,
            "namespace": ns,
            "creationTimestamp": "2026-06-12T19:00:00Z",
            "labels": {"app.kubernetes.io/component": comp},
            "ownerReferences": [{"kind": owner}],
        },
        "spec": {"nodeName": node},
        "status": {
            "phase": phase,
            "podIP": "10.42.0.5",
            "containerStatuses": [
                {"ready": ready, "restartCount": restarts, "state": state},
            ],
        },
    }


def _summary(node: str = "ddi1") -> dict:
    return {
        "node": {
            "nodeName": node,
            "cpu": {"usageNanoCores": 800_000_000},  # 0.8 cores
            "memory": {"workingSetBytes": 2 * 1024**3, "availableBytes": 6 * 1024**3},
            "fs": {"usedBytes": 5 * 1024**3, "capacityBytes": 20 * 1024**3},
        },
        "pods": [
            {
                "podRef": {"namespace": "spatium", "name": "spatium-control-spatiumddi-api-x"},
                "cpu": {"usageNanoCores": 500_000_000},
                "memory": {"workingSetBytes": 400 * 1024**2},
            },
            {
                "podRef": {"namespace": "spatium", "name": "spatium-control-spatiumddi-worker-y"},
                "cpu": {"usageNanoCores": 120_000_000},
                "memory": {"workingSetBytes": 250 * 1024**2},
            },
        ],
    }


def _patch_kube(monkeypatch, *, node_status=200, summary_status=200) -> None:
    pods = [
        _pod("spatium-control-spatiumddi-api-x", comp="api"),
        _pod("spatium-control-spatiumddi-worker-y", comp="worker"),
        _pod(
            "helm-install-spatium-bootstrap-z",
            ns="kube-system",
            comp="helm-install",
            phase="Succeeded",
            ready=False,
            owner="Job",
        ),
    ]
    monkeypatch.setattr(
        "app.services.appliance.k8s.list_nodes",
        lambda label_selector=None: (node_status, [_node()] if node_status == 200 else []),
    )
    monkeypatch.setattr(
        "app.services.appliance.k8s.list_all_pods",
        lambda: (200, pods),
    )
    monkeypatch.setattr(
        "app.services.appliance.k8s.get_node_stats_summary",
        lambda name: (summary_status, _summary(name) if summary_status == 200 else None),
    )


# ── service-level ───────────────────────────────────────────────────────────


def test_get_cluster_health_rollup(monkeypatch) -> None:
    _patch_kube(monkeypatch)
    snap = cluster_health.get_cluster_health()

    assert snap["available"] is True
    assert snap["nodes_total"] == 1
    assert snap["nodes_ready"] == 1
    assert snap["control_plane_nodes"] == 1
    assert snap["is_ha"] is False
    assert snap["metrics_available"] is True
    assert snap["kubelet_version"] == "v1.35.5+k3s1"

    # Live per-node usage flowed through from the kubelet summary.
    node = snap["nodes"][0]
    assert node["cpu_usage_cores"] == pytest.approx(0.8)
    assert node["cpu_capacity_cores"] == pytest.approx(4.0)
    assert node["memory_working_set_bytes"] == 2 * 1024**3
    assert node["fs_capacity_bytes"] == 20 * 1024**3
    assert "control-plane" in node["roles"]

    # Cluster aggregate.
    assert snap["cpu_usage_cores"] == pytest.approx(0.8)
    assert snap["cpu_capacity_cores"] == pytest.approx(4.0)

    # Workload rollup: api + worker present + healthy; the Completed Job pod
    # is excluded (not counted as "down").
    comps = {w["component"]: w for w in snap["workloads"]}
    assert comps["api"]["status"] == "healthy"
    assert comps["worker"]["status"] == "healthy"
    assert "helm-install" not in comps

    # Top pods by CPU — api (0.5) ahead of worker (0.12).
    assert snap["top_pods_cpu"][0]["component"] == "api"
    assert snap["top_pods_cpu"][0]["cpu_usage_cores"] == pytest.approx(0.5)


def test_cluster_health_degrades_without_kubelet_proxy(monkeypatch) -> None:
    # nodes/proxy not granted → 403 on the Summary API → no live usage, but
    # the node inventory + workload rollup still render.
    _patch_kube(monkeypatch, summary_status=403)
    snap = cluster_health.get_cluster_health()
    assert snap["available"] is True
    assert snap["metrics_available"] is False
    assert snap["cpu_usage_cores"] is None
    assert snap["nodes"][0]["cpu_usage_cores"] is None
    # Capacity (from the Node object) is still known even without live usage.
    assert snap["nodes"][0]["cpu_capacity_cores"] == pytest.approx(4.0)
    assert snap["nodes_total"] == 1


def test_cluster_health_unavailable_when_nodes_forbidden(monkeypatch) -> None:
    _patch_kube(monkeypatch, node_status=403)
    snap = cluster_health.get_cluster_health()
    assert snap["available"] is False
    assert "RBAC" in (snap["detail"] or "") or "ServiceAccount" in (snap["detail"] or "")


def test_quantity_parsers() -> None:
    assert cluster_health._cpu_cores("4") == pytest.approx(4.0)
    assert cluster_health._cpu_cores("500m") == pytest.approx(0.5)
    assert cluster_health._cpu_cores("250000000n") == pytest.approx(0.25)
    assert cluster_health._cpu_cores(None) is None
    assert cluster_health._mem_bytes("8Gi") == 8 * 1024**3
    assert cluster_health._mem_bytes("8113280Ki") == 8113280 * 1024
    assert cluster_health._mem_bytes("2000M") == 2_000_000_000
    assert cluster_health._mem_bytes(None) is None


# ── endpoint-level ──────────────────────────────────────────────────────────


async def _superadmin(db: AsyncSession) -> User:
    u = User(
        username=f"sa-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@x.com",
        display_name="SA",
        hashed_password=hash_password("OldPass123!"),
        auth_source="local",
        is_active=True,
        is_superadmin=True,
        force_password_change=False,
        password_changed_at=datetime.now(UTC),
    )
    db.add(u)
    await db.flush()
    await db.commit()
    return u


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def test_health_endpoint_returns_snapshot(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    _patch_kube(monkeypatch)
    admin = await _superadmin(db_session)
    r = await client.get(_HEALTH_URL, headers=_bearer(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["nodes_total"] == 1
    assert body["metrics_available"] is True
    assert body["nodes"][0]["cpu_usage_cores"] == pytest.approx(0.8)


async def test_health_endpoint_503_when_sa_missing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    # No monkeypatch of the SA volume → list_nodes raises KubeapiUnavailable.
    def _boom(label_selector=None):
        from app.services.appliance.k8s import KubeapiUnavailableError

        raise KubeapiUnavailableError("ServiceAccount not mounted; kubeapi unreachable")

    monkeypatch.setattr("app.services.appliance.k8s.list_nodes", _boom)
    admin = await _superadmin(db_session)
    r = await client.get(_HEALTH_URL, headers=_bearer(admin))
    assert r.status_code == 503, r.text


async def test_health_endpoint_requires_auth(client: AsyncClient) -> None:
    r = await client.get(_HEALTH_URL)
    assert r.status_code == 401, r.text
