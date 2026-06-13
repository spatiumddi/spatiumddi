"""Cluster health snapshot for the appliance "Cluster → Overview" screen (#402).

Aggregates a live picture of the k3s cluster *underneath* the appliance from
data the api pod's ServiceAccount can already read (nodes + pods cluster-wide)
plus the kubelet Summary API via the apiserver proxy (``nodes/proxy [get]``,
added in the same PR). This is the same data source the TTY console uses — the
appliance ships **no** metrics-server / Prometheus, so live CPU / memory comes
from the kubelet Summary API, not ``metrics.k8s.io``.

``get_cluster_health()`` is a synchronous gather (a handful of stdlib kubeapi
calls); the router runs it in a worker thread so the event loop never blocks,
and the SSE stream re-runs it every couple of seconds for the near-real-time
dashboard. The return value is a JSON-safe dict matching the ``ClusterHealth``
Pydantic model in ``app.api.v1.appliance.cluster`` (so the SSE loop can
``json.dumps`` it directly without re-validating each tick).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.services.appliance import k8s

logger = structlog.get_logger(__name__)

# Components we recognise for the workload-health rollup. Anything else still
# rolls up under its own ``app.kubernetes.io/component`` label (or a name-
# derived fallback) — this list only drives nice display ordering.
_COMPONENT_ORDER = [
    "api",
    "worker",
    "beat",
    "frontend",
    "postgresql",
    "redis",
    "supervisor",
    "dns-bind9",
    "dns-powerdns",
    "dhcp-kea",
]

_TOP_POD_LIMIT = 8


# ── quantity parsers ───────────────────────────────────────────────────────


def _cpu_cores(q: str | None) -> float | None:
    """k8s CPU quantity → cores. Handles ``n`` / ``u`` / ``m`` / plain."""
    if not q:
        return None
    q = q.strip()
    try:
        if q.endswith("n"):
            return float(q[:-1]) / 1e9
        if q.endswith("u"):
            return float(q[:-1]) / 1e6
        if q.endswith("m"):
            return float(q[:-1]) / 1e3
        return float(q)
    except ValueError:
        return None


_MEM_BIN = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5, "Ei": 1024**6}
_MEM_DEC = {"k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}


def _mem_bytes(q: str | None) -> int | None:
    """k8s memory quantity → bytes. Handles Ki/Mi/Gi… (1024) + K/M/G… (1000)."""
    if not q:
        return None
    q = q.strip()
    for suf, mult in _MEM_BIN.items():
        if q.endswith(suf):
            try:
                return int(float(q[:-2]) * mult)
            except ValueError:
                return None
    for suf, mult in _MEM_DEC.items():
        if q.endswith(suf):
            try:
                return int(float(q[:-1]) * mult)
            except ValueError:
                return None
    try:
        return int(float(q))
    except ValueError:
        return None


def _age_seconds(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((datetime.now(UTC) - dt).total_seconds())


# ── per-resource extractors ────────────────────────────────────────────────


_ROLE_LABEL_NS = "node-role.kubernetes.io"


def _node_roles(node: dict[str, Any]) -> list[str]:
    """Roles from ``node-role.kubernetes.io/<role>`` label keys (k3s sets
    control-plane / etcd / master). Match the label *namespace* exactly via
    ``partition`` rather than a prefix ``startswith`` — the latter is an
    incomplete-substring check (CodeQL py/incomplete-url-substring-sanitization)
    and an exact equality is also more precise."""
    labels = (node.get("metadata") or {}).get("labels") or {}
    roles: list[str] = []
    for key in labels:
        ns, sep, role = key.partition("/")
        if sep and ns == _ROLE_LABEL_NS and role:
            roles.append(role)
    return sorted(roles) or ["worker"]


def _node_condition(node: dict[str, Any], kind: str) -> bool:
    for c in (node.get("status") or {}).get("conditions") or []:
        if c.get("type") == kind:
            return c.get("status") == "True"
    return False


def _internal_ip(node: dict[str, Any]) -> str | None:
    for a in (node.get("status") or {}).get("addresses") or []:
        if a.get("type") == "InternalIP":
            return a.get("address")
    return None


def _parse_node_stats(summary: dict[str, Any]) -> dict[str, Any]:
    node = summary.get("node") or {}
    cpu = node.get("cpu") or {}
    mem = node.get("memory") or {}
    fs = node.get("fs") or {}
    nano = cpu.get("usageNanoCores")
    return {
        "cpu_usage_cores": (nano / 1e9) if isinstance(nano, (int, float)) else None,
        "memory_working_set_bytes": mem.get("workingSetBytes"),
        "memory_available_bytes": mem.get("availableBytes"),
        "fs_used_bytes": fs.get("usedBytes"),
        "fs_capacity_bytes": fs.get("capacityBytes"),
    }


def _parse_pod_stats(summary: dict[str, Any]) -> dict[tuple[str, str], tuple[float, int]]:
    """``{(namespace, pod): (cpu_cores, mem_bytes)}`` from a kubelet summary."""
    out: dict[tuple[str, str], tuple[float, int]] = {}
    for pod in summary.get("pods") or []:
        ref = pod.get("podRef") or {}
        ns, nm = ref.get("namespace", ""), ref.get("name", "")
        if not nm:
            continue
        nano = (pod.get("cpu") or {}).get("usageNanoCores") or 0
        mem = (pod.get("memory") or {}).get("workingSetBytes") or 0
        out[(ns, nm)] = (nano / 1e9, int(mem))
    return out


def _pod_component(pod: dict[str, Any]) -> str:
    labels = (pod.get("metadata") or {}).get("labels") or {}
    comp = labels.get("app.kubernetes.io/component")
    if comp:
        return comp
    # Fallback: strip the chart's release prefixes off the pod name.
    name = (pod.get("metadata") or {}).get("name") or "?"
    for pre in ("spatium-control-spatiumddi-", "spatium-bootstrap-", "spatium-"):
        if name.startswith(pre):
            name = name[len(pre) :]
            break
    # Drop the random replica suffix (…-abc123-x9y2z / …-0).
    parts = name.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else name


def _pod_state(pod: dict[str, Any]) -> str:
    """Human state — surfaces a waiting reason (CrashLoopBackOff) over phase."""
    status = pod.get("status") or {}
    for cs in status.get("containerStatuses") or []:
        waiting = (cs.get("state") or {}).get("waiting") or {}
        reason = waiting.get("reason")
        if reason:
            return reason
    return status.get("phase") or "Unknown"


def _pod_owner_kind(pod: dict[str, Any]) -> str | None:
    owners = (pod.get("metadata") or {}).get("ownerReferences") or []
    return owners[0].get("kind") if owners else None


def _ready_counts(pod: dict[str, Any]) -> tuple[int, int]:
    css = (pod.get("status") or {}).get("containerStatuses") or []
    ready = sum(1 for c in css if c.get("ready"))
    return ready, len(css)


# ── assembly ───────────────────────────────────────────────────────────────


def _unavailable(detail: str) -> dict[str, Any]:
    return {
        "available": False,
        "detail": detail,
        "nodes_total": 0,
        "nodes_ready": 0,
        "pods_total": 0,
        "pods_running": 0,
        "pods_by_phase": {},
        "kubelet_version": None,
        "is_ha": False,
        "control_plane_nodes": 0,
        "metrics_available": False,
        "cpu_usage_cores": None,
        "cpu_capacity_cores": None,
        "memory_working_set_bytes": None,
        "memory_capacity_bytes": None,
        "nodes": [],
        "workloads": [],
        "top_pods_cpu": [],
        "top_pods_mem": [],
    }


def cluster_unavailable(detail: str) -> dict[str, Any]:
    """Public unavailable snapshot — used by the SSE stream when kubeapi is
    momentarily unreachable so the live dashboard shows a reason, not a stall."""
    return _unavailable(detail)


def get_cluster_health() -> dict[str, Any]:
    """Gather a full cluster-health snapshot. Synchronous (stdlib kubeapi).

    Raises ``k8s.KubeapiUnavailableError`` only when the ServiceAccount isn't
    mounted (non-k8s / non-appliance) — the router maps that to 503. A 403 on
    the node read (RBAC not granted) is returned as ``available=False`` with a
    diagnostic ``detail`` rather than an error, so the UI can explain it.
    """
    nstatus, nodes_raw = k8s.list_nodes()
    if nstatus == 403:
        return _unavailable(
            "The api ServiceAccount can't read Nodes — enable "
            "api.upgradeOrchestratorRBAC (the appliance default) and re-apply the chart."
        )
    if nstatus != 200:
        return _unavailable(f"kubeapi node list returned HTTP {nstatus}")

    try:
        _pstatus, pods_raw = k8s.list_all_pods()
    except k8s.KubeapiUnavailableError:
        pods_raw = []

    # Per-node kubelet Summary API (CPU / mem / fs + per-pod usage). Degrades
    # cleanly to "no live usage" when nodes/proxy isn't granted (403) or a
    # kubelet is briefly unreachable.
    node_stats: dict[str, dict[str, Any]] = {}
    pod_usage: dict[tuple[str, str], tuple[float, int]] = {}
    metrics_available = False
    for n in nodes_raw:
        nm = (n.get("metadata") or {}).get("name")
        if not nm:
            continue
        try:
            sstatus, summary = k8s.get_node_stats_summary(nm)
        except k8s.KubeapiUnavailableError:
            continue
        if sstatus == 200 and summary:
            metrics_available = True
            node_stats[nm] = _parse_node_stats(summary)
            pod_usage.update(_parse_pod_stats(summary))

    # ── nodes ──
    nodes: list[dict[str, Any]] = []
    nodes_ready = 0
    control_plane_nodes = 0
    cluster_cpu_used = 0.0
    cluster_cpu_cap = 0.0
    cluster_mem_used = 0
    cluster_mem_cap = 0
    kubelet_version: str | None = None
    pods_on_node: dict[str, int] = {}
    for p in pods_raw:
        nn = (p.get("spec") or {}).get("nodeName")
        if nn:
            pods_on_node[nn] = pods_on_node.get(nn, 0) + 1

    for n in nodes_raw:
        meta = n.get("metadata") or {}
        name = meta.get("name") or "?"
        info = (n.get("status") or {}).get("nodeInfo") or {}
        cap = (n.get("status") or {}).get("capacity") or {}
        roles = _node_roles(n)
        ready = k8s.is_node_ready(n)
        if ready:
            nodes_ready += 1
        if "control-plane" in roles or "master" in roles:
            control_plane_nodes += 1
        kubelet_version = kubelet_version or info.get("kubeletVersion")
        stats = node_stats.get(name) or {}
        cpu_cap = _cpu_cores(cap.get("cpu"))
        mem_cap = _mem_bytes(cap.get("memory"))
        if stats.get("cpu_usage_cores") is not None:
            cluster_cpu_used += stats["cpu_usage_cores"]
        if cpu_cap:
            cluster_cpu_cap += cpu_cap
        if stats.get("memory_working_set_bytes"):
            cluster_mem_used += int(stats["memory_working_set_bytes"])
        if mem_cap:
            cluster_mem_cap += mem_cap
        nodes.append(
            {
                "name": name,
                "ready": ready,
                "roles": roles,
                "schedulable": not (n.get("spec") or {}).get("unschedulable", False),
                "kubelet_version": info.get("kubeletVersion"),
                "os_image": info.get("osImage"),
                "kernel": info.get("kernelVersion"),
                "container_runtime": info.get("containerRuntimeVersion"),
                "architecture": info.get("architecture"),
                "internal_ip": _internal_ip(n),
                "age_seconds": _age_seconds(meta.get("creationTimestamp")),
                "memory_pressure": _node_condition(n, "MemoryPressure"),
                "disk_pressure": _node_condition(n, "DiskPressure"),
                "pid_pressure": _node_condition(n, "PIDPressure"),
                "cpu_capacity_cores": cpu_cap,
                "memory_capacity_bytes": mem_cap,
                "pods_capacity": int(cap["pods"]) if cap.get("pods") else None,
                "pods_running": pods_on_node.get(name, 0),
                "cpu_usage_cores": stats.get("cpu_usage_cores"),
                "memory_working_set_bytes": stats.get("memory_working_set_bytes"),
                "memory_available_bytes": stats.get("memory_available_bytes"),
                "fs_used_bytes": stats.get("fs_used_bytes"),
                "fs_capacity_bytes": stats.get("fs_capacity_bytes"),
                # #402 — host disk partitions are merged in by the router from
                # the supervisor's cluster_health JSONB (the api pod can't see
                # host partitions itself); empty here so the shape is stable.
                "host_disk_partitions": [],
            }
        )

    # ── pods + workload rollup + top pods ──
    pods_by_phase: dict[str, int] = {}
    pods_running = 0
    pod_rows: list[dict[str, Any]] = []
    rollup: dict[str, dict[str, Any]] = {}
    for p in pods_raw:
        meta = p.get("metadata") or {}
        status = p.get("status") or {}
        ns = meta.get("namespace") or ""
        name = meta.get("name") or "?"
        phase = status.get("phase") or "Unknown"
        pods_by_phase[phase] = pods_by_phase.get(phase, 0) + 1
        if phase == "Running":
            pods_running += 1
        ready_n, total_n = _ready_counts(p)
        restarts = sum(c.get("restartCount", 0) for c in status.get("containerStatuses") or [])
        cpu_u, mem_u = pod_usage.get((ns, name), (None, None))
        comp = _pod_component(p)
        owner = _pod_owner_kind(p)
        terminal = phase in ("Succeeded", "Failed")
        row = {
            "name": name,
            "namespace": ns,
            "component": comp,
            "node": (p.get("spec") or {}).get("nodeName"),
            "phase": phase,
            "state": _pod_state(p),
            "ready": f"{ready_n}/{total_n}" if total_n else "0/0",
            "restarts": restarts,
            "age_seconds": _age_seconds(meta.get("creationTimestamp")),
            "cpu_usage_cores": cpu_u,
            "memory_working_set_bytes": mem_u,
        }
        pod_rows.append(row)

        # Workload rollup — skip done Job pods (helm-install Completed etc.).
        if terminal and owner == "Job":
            continue
        agg = rollup.setdefault(
            comp,
            {"component": comp, "kind": owner, "ready": 0, "total": 0, "restarts": 0},
        )
        agg["total"] += 1
        agg["restarts"] += restarts
        fully_ready = (total_n > 0 and ready_n == total_n) or phase == "Succeeded"
        if fully_ready:
            agg["ready"] += 1
        if agg["kind"] is None:
            agg["kind"] = owner

    workloads: list[dict[str, Any]] = []
    for comp, agg in rollup.items():
        if agg["ready"] == agg["total"] and agg["total"] > 0:
            wstatus = "healthy"
        elif agg["ready"] > 0:
            wstatus = "degraded"
        else:
            wstatus = "down"
        workloads.append({**agg, "status": wstatus})
    workloads.sort(
        key=lambda w: (
            (
                _COMPONENT_ORDER.index(w["component"])
                if w["component"] in _COMPONENT_ORDER
                else len(_COMPONENT_ORDER)
            ),
            w["component"],
        )
    )

    with_cpu = [r for r in pod_rows if r["cpu_usage_cores"] is not None]
    with_mem = [r for r in pod_rows if r["memory_working_set_bytes"] is not None]
    top_pods_cpu = sorted(with_cpu, key=lambda r: r["cpu_usage_cores"], reverse=True)[
        :_TOP_POD_LIMIT
    ]
    top_pods_mem = sorted(with_mem, key=lambda r: r["memory_working_set_bytes"], reverse=True)[
        :_TOP_POD_LIMIT
    ]

    return {
        "available": True,
        "detail": None,
        "nodes_total": len(nodes_raw),
        "nodes_ready": nodes_ready,
        "pods_total": len(pod_rows),
        "pods_running": pods_running,
        "pods_by_phase": pods_by_phase,
        "kubelet_version": kubelet_version,
        "is_ha": control_plane_nodes > 1,
        "control_plane_nodes": control_plane_nodes,
        "metrics_available": metrics_available,
        "cpu_usage_cores": round(cluster_cpu_used, 4) if metrics_available else None,
        "cpu_capacity_cores": round(cluster_cpu_cap, 4) if cluster_cpu_cap else None,
        "memory_working_set_bytes": cluster_mem_used if metrics_available else None,
        "memory_capacity_bytes": cluster_mem_cap or None,
        "nodes": nodes,
        "workloads": workloads,
        "top_pods_cpu": top_pods_cpu,
        "top_pods_mem": top_pods_mem,
    }
