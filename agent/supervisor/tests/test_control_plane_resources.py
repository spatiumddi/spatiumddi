"""Appliance-sized api / worker limits ride the control-plane overrides.

The 2026-09 resource-floor campaign found the chart's BYO-cluster defaults
(api 512Mi; worker 1Gi at four prefork processes) give way on the appliance
long before the VM does — the api cannot build a 250k-record bundle under
512Mi, the worker OOMs under 20k-device churn with gigabytes free — and that
a ``kubectl set resources`` never survives the next k3s restart. The
supervisor sizes both from the node's RAM and writes them into the same
HelmChartConfig as the replica overrides, where they survive (#272).
"""

from __future__ import annotations

import json

import yaml

from spatium_supervisor import k8s_api


class _Recorder:
    """Stand-in for k8s_api._request (same shape as the mirror tests')."""

    def __init__(self, current: str | None = None):
        self._current = current
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(self, method, path, body=None, content_type=None):
        self.calls.append((method, path, body))
        if method == "GET":
            if self._current is None:
                return (404, "")
            return (200, json.dumps({"spec": {"valuesContent": self._current}}))
        return (200 if method == "PATCH" else 201, "{}")

    @property
    def doc(self) -> dict:
        for method, _, body in self.calls:
            if method in ("POST", "PATCH") and body is not None:
                return yaml.safe_load(json.loads(body)["spec"]["valuesContent"])
        raise AssertionError("no HelmChartConfig write recorded")


def test_sizing_scales_with_ram_inside_the_clamps() -> None:
    six = k8s_api.control_plane_resources(6144)
    assert six["api"]["resources"]["limits"]["memory"] == "3072Mi"
    assert six["worker"]["resources"]["limits"]["memory"] == "1536Mi"
    assert six["worker"]["concurrency"] == 2
    # Floors: a 2 GiB box still gets the minimum the bundle build needs.
    small = k8s_api.control_plane_resources(2048)
    assert small["api"]["resources"]["limits"]["memory"] == "1024Mi"
    assert small["worker"]["resources"]["limits"]["memory"] == "1024Mi"
    # Ceilings: a 64 GiB box does not hand the api half of it.
    big = k8s_api.control_plane_resources(65536)
    assert big["api"]["resources"]["limits"]["memory"] == "8192Mi"
    assert big["worker"]["resources"]["limits"]["memory"] == "4096Mi"
    assert big["worker"]["concurrency"] == 4
    # Requests are never set: scheduling on a small node is unchanged.
    assert "requests" not in six["api"]["resources"]
    assert "requests" not in six["worker"]["resources"]


def test_unknown_ram_leaves_the_chart_defaults_alone() -> None:
    assert k8s_api.control_plane_resources(None) == {}
    assert k8s_api.control_plane_resources(0) == {}


def test_the_overrides_carry_the_sizing_and_state_the_redis_kind(monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(k8s_api, "_request", rec)

    ok, err = k8s_api.apply_control_plane_overrides(1, "", mem_total_mib=8192)

    assert (ok, err) == (True, None)
    doc = rec.doc
    assert doc["api"]["replicas"] == 1
    assert doc["api"]["resources"] == {"limits": {"memory": "4096Mi"}}
    assert doc["worker"]["concurrency"] == 2
    assert doc["worker"]["resources"] == {"limits": {"memory": "2048Mi"}}
    assert doc["redis"] == {"kind": "sentinel", "sentinel": {"replicas": 1}}


def test_without_a_size_the_document_is_what_it_was(monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(k8s_api, "_request", rec)

    ok, _err = k8s_api.apply_control_plane_overrides(3, "10.0.0.9")

    assert ok
    doc = rec.doc
    assert "resources" not in doc["api"] and "resources" not in doc["worker"]
    assert "concurrency" not in doc["worker"]
    assert doc["redis"]["kind"] == "sentinel"


def test_an_operators_own_request_keys_survive_the_merge(monkeypatch) -> None:
    current = yaml.safe_dump(
        {
            "api": {"resources": {"requests": {"cpu": "250m"}, "limits": {"cpu": "2"}}},
            "image": {"tag": "dev-1"},
        },
        sort_keys=True,
    )
    rec = _Recorder(current)
    monkeypatch.setattr(k8s_api, "_request", rec)

    ok, _err = k8s_api.apply_control_plane_overrides(1, "", mem_total_mib=4096)

    assert ok
    doc = rec.doc
    # Sibling keys the supervisor does not own are kept, memory is set.
    assert doc["api"]["resources"] == {
        "requests": {"cpu": "250m"},
        "limits": {"cpu": "2", "memory": "2048Mi"},
    }
    assert doc["image"]["tag"] == "dev-1"


def test_node_memory_reads_meminfo(tmp_path, monkeypatch) -> None:
    mem = tmp_path / "meminfo"
    mem.write_text("MemTotal:        8123456 kB\nMemFree:  100 kB\n")
    real_open = open

    def fake_open(path, *a, **kw):
        if path == "/proc/meminfo":
            return real_open(mem, *a, **kw)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    assert k8s_api.node_memory_mib() == 8123456 // 1024
