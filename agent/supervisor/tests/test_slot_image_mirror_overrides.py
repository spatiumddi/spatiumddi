"""#787 — the slot-image mirror is enabled from the control-plane size.

An uploaded upgrade image lands on the api replica that served the upload;
the host runner's download round-robins through the Service, so on a
multi-replica control plane roughly half the downloads 404. The mirror
(#296 Phase B) is the fix but defaulted off and nothing on the appliance
ever turned it on.

These tests pin the derivation — enabled at >= 2, disabled at 1, and
emitted in BOTH states so a demote actually releases the PVC (a
HelmChartConfig is merged, not diffed, so an omitted key keeps its last
value) — and that it rides the same HelmChartConfig write as cp-size
rather than a second one that could disagree with it.
"""

from __future__ import annotations

import json

from spatium_supervisor import k8s_api


class _Recorder:
    """Stand-in for k8s_api._request: 404 on the GET (no HelmChartConfig
    yet) so the upsert takes its create path, then records the POST."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(self, method, path, body=None, content_type=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return (404, "")
        return (201, "{}")

    @property
    def values(self) -> str:
        for method, _, body in self.calls:
            if method == "POST" and body is not None:
                return json.loads(body)["spec"]["valuesContent"]
        raise AssertionError("no HelmChartConfig POST recorded")


def test_single_node_leaves_the_mirror_off(monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(k8s_api, "_request", rec)

    ok, err = k8s_api.apply_control_plane_overrides(1, "")

    assert (ok, err) == (True, None)
    assert "slotImageMirror:\n  enabled: false\n" in rec.values


def test_multi_node_turns_the_mirror_on(monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(k8s_api, "_request", rec)

    ok, err = k8s_api.apply_control_plane_overrides(3, "10.0.0.9")

    assert (ok, err) == (True, None)
    assert "slotImageMirror:\n  enabled: true\n" in rec.values


def test_demote_writes_the_off_state_rather_than_omitting_it(monkeypatch) -> None:
    # The regression this guards: emitting the key only when true. A
    # HelmChartConfig's valuesContent is merged over the HelmChart's, so a
    # dropped key is not "back to the chart default" — the previously
    # written ``true`` is what helm-controller keeps applying, and a
    # 3-node cluster demoted to 1 would hold the PVC forever.
    rec = _Recorder()
    monkeypatch.setattr(k8s_api, "_request", rec)

    k8s_api.apply_control_plane_overrides(1, "")

    assert "slotImageMirror:" in rec.values


def test_mirror_state_rides_the_cp_size_write(monkeypatch) -> None:
    # One HelmChartConfig, one write: the mirror cannot end up describing a
    # different cluster size than the replica counts it was derived from.
    rec = _Recorder()
    monkeypatch.setattr(k8s_api, "_request", rec)

    k8s_api.apply_control_plane_overrides(2, "")

    writes = [m for m, _, _ in rec.calls if m in ("POST", "PATCH")]
    assert writes == ["POST"]
    values = rec.values
    assert "api:\n  replicas: 2\n" in values
    assert "slotImageMirror:\n  enabled: true\n" in values


def test_values_stay_parseable_yaml(monkeypatch) -> None:
    # The stanza is string-concatenated into a YAML document; a missing
    # newline would silently glue it onto the worker block above.
    yaml = __import__("yaml")
    rec = _Recorder()
    monkeypatch.setattr(k8s_api, "_request", rec)

    k8s_api.apply_control_plane_overrides(2, "10.0.0.9")

    parsed = yaml.safe_load(rec.values)
    assert parsed["slotImageMirror"] == {"enabled": True}
    assert parsed["worker"]["replicas"] == 2
