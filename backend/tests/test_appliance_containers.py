"""Workload→pod resolver + log-container picker for the Console log picker (#416).

``resolve_workload_pod`` lets the Cluster dashboard tail a stable workload
(deployment / daemonset) by its ``app.kubernetes.io/component`` label and have
the backend pick the current running pod AND the right container — so the
stream survives pod rolls, never exposes churny pod names, and doesn't 400 on
a multi-container pod (the redis pod is render-config + redis + sentinel).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.appliance import containers


def _pod(
    name: str,
    component: str,
    *,
    phase: str = "Running",
    start: str = "2026-06-14T10:00:00Z",
    container_names: list[str] | None = None,
) -> dict[str, Any]:
    names = container_names if container_names is not None else [component]
    return {
        "metadata": {
            "name": name,
            "uid": "u" * 12,
            "labels": {
                "app.kubernetes.io/part-of": "spatiumddi",
                "app.kubernetes.io/component": component,
            },
        },
        "spec": {"containers": [{"name": n, "image": "ghcr.io/x:dev"} for n in names]},
        "status": {
            "phase": phase,
            "startTime": start,
            "containerStatuses": [{"ready": True}],
        },
    }


@pytest.fixture(autouse=True)
def _appliance_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(containers.settings, "appliance_mode", True)


def test_resolve_picks_newest_running_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        containers.k8s,
        "list_pods",
        lambda *a, **k: [
            _pod("spatium-control-spatiumddi-api-old", "api", start="2026-06-14T09:00:00Z"),
            _pod("spatium-control-spatiumddi-api-new", "api", start="2026-06-14T10:30:00Z"),
            _pod("spatium-control-spatiumddi-worker-1", "worker"),
        ],
    )
    # Newest running pod for the component; single-container → no container
    # needed (kubeapi defaults to the only one).
    assert containers.resolve_workload_pod("api") == (
        "spatium-control-spatiumddi-api-new",
        None,
    )
    assert containers.resolve_workload_pod("worker") == (
        "spatium-control-spatiumddi-worker-1",
        None,
    )


def test_resolve_multi_container_picks_component_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The redis pod is render-config + redis + sentinel — kubeapi 400s without
    # a container; the resolver picks the one matching the component.
    monkeypatch.setattr(
        containers.k8s,
        "list_pods",
        lambda *a, **k: [
            _pod(
                "spatium-control-spatiumddi-redis-0",
                "redis",
                container_names=["render-config", "redis", "sentinel"],
            ),
        ],
    )
    assert containers.resolve_workload_pod("redis") == (
        "spatium-control-spatiumddi-redis-0",
        "redis",
    )


def test_resolve_skips_completed_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Succeeded one-shot Job pod is not "running" → no tailable pod.
    monkeypatch.setattr(
        containers.k8s,
        "list_pods",
        lambda *a, **k: [
            _pod("spatium-control-spatiumddi-migrate-xyz", "migrate", phase="Succeeded"),
        ],
    )
    assert containers.resolve_workload_pod("migrate") is None


def test_resolve_unknown_component(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        containers.k8s,
        "list_pods",
        lambda *a, **k: [_pod("spatium-control-spatiumddi-api-1", "api")],
    )
    assert containers.resolve_workload_pod("does-not-exist") is None


def test_resolve_component_from_name_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # No component label → derive from the chart-prefixed pod name.
    pod = _pod("spatium-control-spatiumddi-frontend-abc-1", "frontend")
    del pod["metadata"]["labels"]["app.kubernetes.io/component"]
    monkeypatch.setattr(containers.k8s, "list_pods", lambda *a, **k: [pod])
    assert containers.resolve_workload_pod("frontend") == (
        "spatium-control-spatiumddi-frontend-abc-1",
        None,
    )
