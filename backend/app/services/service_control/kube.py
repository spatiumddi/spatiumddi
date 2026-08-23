"""Kubernetes lifecycle backend (issue #890).

Rollout-restart of the chart's own workloads through the api pod's
mounted ServiceAccount — the same mechanism the appliance's remote
``/appliances/{id}/k8s/restart`` already drives over the supervisor
proxy, applied locally.

Scope is the api pod's own namespace, and within it only workloads
labelled as ours (``app.kubernetes.io/name=spatiumddi`` or
``part-of=spatiumddi`` — the umbrella and appliance charts respectively).
A workload the operator dropped in the same namespace by hand is not
listed and therefore not actionable, because actions resolve against the
listing.

``restart`` is the only verb. ``start`` / ``stop`` would mean scaling to
zero and back, and restoring the previous replica count needs somewhere
durable to keep it; a control plane that forgets how many replicas a
workload had is worse than one that never offered to stop it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import structlog

from app.services.appliance import k8s

logger = structlog.get_logger(__name__)

# Workload kinds we list and can restart. Jobs are excluded on purpose:
# re-running the migrate Job is a different operation with different
# consequences, and "restart" on a completed Job means nothing.
WORKLOAD_KINDS: tuple[tuple[str, str], ...] = (
    ("Deployment", "deployments"),
    ("StatefulSet", "statefulsets"),
    ("DaemonSet", "daemonsets"),
)

_OURS_LABELS = ("app.kubernetes.io/name", "app.kubernetes.io/part-of")
_OURS_VALUE = "spatiumddi"

RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"


class KubeControlError(RuntimeError):
    """kubeapi refused, or is unreachable."""


def available() -> bool:
    return k8s.get_config() is not None


def namespace() -> str | None:
    cfg = k8s.get_config()
    return cfg.namespace if cfg else None


def is_spatium_workload(obj: dict[str, Any]) -> bool:
    """Is this workload one of ours?

    Both chart conventions count: the umbrella chart labels
    ``app.kubernetes.io/name=spatiumddi``, the appliance chart
    ``part-of=spatiumddi``. Anything else in the namespace is an
    operator's own object and is neither listed nor actionable.
    """
    labels = (obj.get("metadata") or {}).get("labels") or {}
    return any(labels.get(key) == _OURS_VALUE for key in _OURS_LABELS)


def summarise_workload(kind: str, obj: dict[str, Any]) -> dict[str, Any]:
    """Flatten a kubeapi workload object to the shared summary shape.

    Shared with the remote-appliance listing in
    ``api/v1/appliance/supervisor.py``, which reads the same objects
    through the supervisor proxy — so the local and remote Fleet views
    cannot disagree about what a workload's state is.
    """
    meta = obj.get("metadata") or {}
    st = obj.get("status") or {}
    spec = obj.get("spec") or {}
    if kind == "DaemonSet":
        desired = int(st.get("desiredNumberScheduled") or 0)
        ready = int(st.get("numberReady") or 0)
    else:
        # ``replicas`` is absent on a workload that has never been scaled
        # and explicitly null while a HorizontalPodAutoscaler owns it, so
        # the ``or 0`` covers both without treating either as a fault.
        desired = int(spec.get("replicas") or 0)
        ready = int(st.get("readyReplicas") or 0)
    containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
    image = str(containers[0].get("image", "")) if containers else ""
    labels = meta.get("labels") or {}
    return {
        "kind": kind,
        "name": str(meta.get("name") or ""),
        "component": str(labels.get("app.kubernetes.io/component") or ""),
        "image": image,
        "desired": desired,
        "ready": ready,
        # A workload is "running" once every desired replica is ready;
        # a scaled-to-zero one is reported as ``stopped`` rather than
        # ``degraded``, because zero-of-zero ready is not a fault.
        "state": ("stopped" if desired == 0 else ("running" if ready >= desired else "degraded")),
        "last_restarted_at": (
            ((spec.get("template") or {}).get("metadata") or {}).get("annotations") or {}
        ).get(RESTART_ANNOTATION),
    }


def list_workloads() -> list[dict[str, Any]]:
    """Every spatiumddi-labelled workload in the api pod's namespace."""
    ns = namespace()
    if ns is None:
        raise KubeControlError("ServiceAccount not mounted; kubeapi unreachable")
    out: list[dict[str, Any]] = []
    for kind, plural in WORKLOAD_KINDS:
        path = f"/apis/apps/v1/namespaces/{quote(ns)}/{plural}"
        try:
            status, body = k8s._request("GET", path)  # noqa: SLF001 — shared transport
        except k8s.KubeapiUnavailableError as exc:
            raise KubeControlError(str(exc)) from exc
        if status == 403:
            # The chart grants list on these only when service control is
            # enabled. Say so rather than reporting an empty inventory,
            # which reads as "nothing to restart".
            raise KubeControlError(
                f"kubeapi forbade listing {plural} in {ns}; enable "
                "api.serviceControlRBAC in the chart values"
            )
        if status != 200:
            raise KubeControlError(f"kubeapi status {status} listing {plural} in {ns}")
        try:
            items = (json.loads(bytes(body)) or {}).get("items") or []
        except (ValueError, TypeError) as exc:
            raise KubeControlError(f"unparseable kubeapi response for {plural}: {exc}") from exc
        out.extend(summarise_workload(kind, obj) for obj in items if is_spatium_workload(obj))
    out.sort(key=lambda r: (r["kind"], r["name"]))
    return out


def rollout_restart(kind: str, name: str) -> None:
    """Bump ``restartedAt`` on one workload's pod template.

    ``kind`` / ``name`` always come from a fresh ``list_workloads()``
    match, so neither reaches kubeapi as an unvalidated path segment.
    """
    ns = namespace()
    if ns is None:
        raise KubeControlError("ServiceAccount not mounted; kubeapi unreachable")
    plural = next((p for k, p in WORKLOAD_KINDS if k == kind), None)
    if plural is None:
        raise KubeControlError(f"unsupported workload kind {kind!r}")
    path = f"/apis/apps/v1/namespaces/{quote(ns)}/{plural}/{quote(name)}"
    payload = json.dumps(
        {
            "spec": {
                "template": {
                    "metadata": {"annotations": {RESTART_ANNOTATION: datetime.now(UTC).isoformat()}}
                }
            }
        }
    ).encode("utf-8")
    try:
        status, body = k8s._request(  # noqa: SLF001 — shared transport
            "PATCH",
            path,
            body=payload,
            content_type="application/strategic-merge-patch+json",
        )
    except k8s.KubeapiUnavailableError as exc:
        raise KubeControlError(str(exc)) from exc
    if status in (200, 201):
        logger.info("service_control_rollout_restart", kind=kind, name=name, namespace=ns)
        return
    if status == 403:
        raise KubeControlError(
            f"kubeapi forbade patching {kind.lower()}/{name}; enable "
            "api.serviceControlRBAC in the chart values"
        )
    raise KubeControlError(f"kubeapi status {status}: {bytes(body)[:200]!r}")


__all__ = [
    "RESTART_ANNOTATION",
    "WORKLOAD_KINDS",
    "KubeControlError",
    "available",
    "is_spatium_workload",
    "list_workloads",
    "namespace",
    "rollout_restart",
    "summarise_workload",
]
