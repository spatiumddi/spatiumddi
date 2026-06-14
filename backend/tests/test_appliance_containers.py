"""Workload→pod resolver for the Console log picker (#416).

``resolve_workload_pod`` lets the Cluster dashboard tail a stable workload
(deployment / daemonset) by its ``app.kubernetes.io/component`` label and
have the backend pick the current running pod — so the stream survives pod
rolls and never exposes churny pod names.
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
) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "uid": "u" * 12,
            "labels": {
                "app.kubernetes.io/part-of": "spatiumddi",
                "app.kubernetes.io/component": component,
            },
        },
        "spec": {"containers": [{"image": "ghcr.io/x:dev"}]},
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
    # Newest running pod for the component wins (fresh after a roll).
    assert containers.resolve_workload_pod("api") == "spatium-control-spatiumddi-api-new"
    assert containers.resolve_workload_pod("worker") == "spatium-control-spatiumddi-worker-1"


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
    pod = _pod("spatium-control-spatiumddi-frontend-abc-1", "ignored")
    del pod["metadata"]["labels"]["app.kubernetes.io/component"]
    monkeypatch.setattr(containers.k8s, "list_pods", lambda *a, **k: [pod])
    assert (
        containers.resolve_workload_pod("frontend") == "spatium-control-spatiumddi-frontend-abc-1"
    )
