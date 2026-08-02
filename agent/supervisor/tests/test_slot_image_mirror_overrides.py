"""#787 — the slot-image mirror is enabled from the control-plane size.

An uploaded upgrade image lands on the api replica that served the upload;
the host runner's download round-robins through the Service, so on a
multi-replica control plane roughly half the downloads 404. The mirror
(#296 Phase B) is the fix but defaulted off and nothing on the appliance
ever turned it on.

These tests pin:

* the derivation — on at >= 2 nodes, off on a box that has never grown;
* the LATCH — a demote leaves it on, because turning it off reclaims
  nothing (PVC + Secret carry ``helm.sh/resource-policy: keep``) and would
  strand every image that exists only on the mirror;
* that the override write MERGES onto the live document instead of
  replacing it, so ``chart_bump``'s ``image.tag`` survives a heartbeat
  landing mid-upgrade.
"""

from __future__ import annotations

import json

import yaml

from spatium_supervisor import k8s_api


class _Recorder:
    """Stand-in for k8s_api._request.

    ``current`` is the HelmChartConfig's live ``valuesContent``; ``None``
    means the CR does not exist yet (GET 404 → the upsert creates it).
    Both the pre-render read and the upsert's own read are served from it.
    """

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
    def written(self) -> str:
        for method, _, body in self.calls:
            if method in ("POST", "PATCH") and body is not None:
                payload = json.loads(body)
                return payload["spec"]["valuesContent"]
        raise AssertionError("no HelmChartConfig write recorded")

    @property
    def doc(self) -> dict:
        return yaml.safe_load(self.written)

    @property
    def wrote_anything(self) -> bool:
        return any(m in ("POST", "PATCH") for m, _, _ in self.calls)


def _values(**overrides) -> str:
    return yaml.safe_dump(overrides, sort_keys=True, default_flow_style=False)


def test_single_node_leaves_the_mirror_off(monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(k8s_api, "_request", rec)

    ok, err = k8s_api.apply_control_plane_overrides(1, "")

    assert (ok, err) == (True, None)
    assert rec.doc["slotImageMirror"] == {"enabled": False}


def test_multi_node_turns_the_mirror_on(monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(k8s_api, "_request", rec)

    ok, err = k8s_api.apply_control_plane_overrides(3, "10.0.0.9")

    assert (ok, err) == (True, None)
    assert rec.doc["slotImageMirror"] == {"enabled": True}
    assert rec.doc["api"]["replicas"] == 3


def test_demote_latches_the_mirror_on(monkeypatch) -> None:
    # Turning it off would reclaim nothing (keep-annotated PVC + Secret) and
    # would strand every image that lives only on the mirror's PVC, which is
    # the same 404 this change exists to remove.
    rec = _Recorder(_values(slotImageMirror={"enabled": True}))
    monkeypatch.setattr(k8s_api, "_request", rec)

    k8s_api.apply_control_plane_overrides(1, "")

    assert rec.doc["slotImageMirror"] == {"enabled": True}


def test_latch_is_explicit_not_an_omission(monkeypatch) -> None:
    # helm-controller MERGES valuesContent rather than diffing it, so the
    # state has to be written as a value. An absent key would silently keep
    # whatever was there before, which is not a latch — it is a coin flip.
    rec = _Recorder()
    monkeypatch.setattr(k8s_api, "_request", rec)

    k8s_api.apply_control_plane_overrides(1, "")

    assert "slotImageMirror" in rec.doc


def test_an_unreadable_current_state_aborts_the_write(monkeypatch) -> None:
    """Unknown != empty, and conflating them is how #792 comes back.

    Merging onto ``{}`` produces a document holding only supervisor-owned
    keys. If the READ failed but the WRITE then succeeds — a blip between
    two calls milliseconds apart — that write deletes ``image.tag``, the one
    key a cluster rolling upgrade is depending on mid-flight. So the write
    is abandoned and retried on the next heartbeat.
    """

    def _boom(method, path, body=None, content_type=None):
        if method == "GET":
            raise RuntimeError("kubeapi down")
        raise AssertionError("must not write when the current state is unknown")

    monkeypatch.setattr(k8s_api, "_request", _boom)

    ok, err = k8s_api.apply_control_plane_overrides(2, "")

    assert ok is False
    assert err is not None


def test_an_absent_cr_is_empty_not_unknown(monkeypatch) -> None:
    # A 404 is a real answer — there is no document to preserve — so the
    # first write on a fresh install must still go through.
    rec = _Recorder()
    monkeypatch.setattr(k8s_api, "_request", rec)

    assert k8s_api._helmchartconfig_doc("spatium-control") == {}

    ok, err = k8s_api.apply_control_plane_overrides(2, "")
    assert (ok, err) == (True, None)


def test_the_latch_still_derives_from_the_node_count_when_nothing_is_written(
    monkeypatch,
) -> None:
    assert k8s_api._slot_image_mirror_enabled(2, {}) is True
    assert k8s_api._slot_image_mirror_enabled(1, {}) is False


def test_malformed_current_values_do_not_crash_the_latch() -> None:
    # An operator hand-edit (or a half-written CR) must not take the
    # control-plane override write down with it.
    for junk in ({"slotImageMirror": "yes"}, {"slotImageMirror": None}, {}):
        assert k8s_api._slot_image_mirror_enabled(1, junk) is False
        assert k8s_api._slot_image_mirror_enabled(2, junk) is True


# ── The override write must not clobber keys it does not own ─────────


def test_image_tag_survives_the_override_write(monkeypatch) -> None:
    """The regression this exists for.

    ``chart_bump._patch_image_tag`` stamps ``image.tag`` on this same CR to
    roll the control plane to a new version. The override write used to
    replace ``spec.valuesContent`` wholesale, so the next heartbeat — at
    most 30 s later, i.e. mid-upgrade — deleted the tag and helm-controller
    re-applied the chart at its default.
    """
    rec = _Recorder(_values(image={"tag": "2026.08.01-1"}, api={"replicas": 3}))
    monkeypatch.setattr(k8s_api, "_request", rec)

    k8s_api.apply_control_plane_overrides(3, "")

    assert rec.doc["image"] == {"tag": "2026.08.01-1"}
    assert rec.doc["api"]["replicas"] == 3


def test_foreign_nested_keys_are_preserved_not_replaced(monkeypatch) -> None:
    # The merge recurses: owning ``api.replicas`` must not take an operator's
    # ``api.resources`` with it.
    rec = _Recorder(_values(api={"replicas": 1, "resources": {"limits": {"cpu": "2"}}}))
    monkeypatch.setattr(k8s_api, "_request", rec)

    k8s_api.apply_control_plane_overrides(2, "")

    assert rec.doc["api"]["resources"] == {"limits": {"cpu": "2"}}
    assert rec.doc["api"]["replicas"] == 2


def test_rewrite_is_byte_stable_so_the_upsert_stays_idempotent(monkeypatch) -> None:
    # Two agents write this CR. They now share one normalisation, so a
    # settled document produces an identical string and the upsert skips the
    # PATCH — no helm re-apply, no Deployment roll, no fight.
    first = _Recorder()
    monkeypatch.setattr(k8s_api, "_request", first)
    k8s_api.apply_control_plane_overrides(2, "10.0.0.9")

    second = _Recorder(first.written)
    monkeypatch.setattr(k8s_api, "_request", second)
    changed, err = k8s_api.apply_control_plane_overrides(2, "10.0.0.9")

    assert (changed, err) == (False, None)
    assert not second.wrote_anything


def test_owned_scaling_still_lands_on_every_workload(monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(k8s_api, "_request", rec)

    k8s_api.apply_control_plane_overrides(
        3, "10.0.0.9", web_ui_allowed_cidrs=["10.0.0.0/8"]
    )

    doc = rec.doc
    for component in ("api", "frontend", "worker"):
        assert doc[component]["replicas"] == 3
        assert doc[component]["podAntiAffinity"] == "hard"
        assert [t["tolerationSeconds"] for t in doc[component]["tolerations"]] == [
            20,
            20,
        ]
    assert doc["postgresql"]["cnpg"]["instances"] == 3
    assert doc["redis"]["sentinel"]["replicas"] == 3
    assert doc["frontend"]["controlPlaneVIP"] == "10.0.0.9"
    assert doc["frontend"]["loadBalancerSourceRanges"] == ["10.0.0.0/8"]


# ── unknown current state vs genuinely-empty current state ───────────
#
# The whole #792 fix rests on this distinction, so each branch is pinned
# individually rather than through one happy-path call.


def _reader(monkeypatch, status: int, body: str):
    def _fake(method, path, body_=None, content_type=None):
        return (status, body)

    monkeypatch.setattr(k8s_api, "_request", _fake)


def test_absent_cr_reads_as_empty(monkeypatch) -> None:
    _reader(monkeypatch, 404, "")
    assert k8s_api._helmchartconfig_doc("spatium-control") == {}


def test_empty_values_read_as_empty(monkeypatch) -> None:
    _reader(monkeypatch, 200, json.dumps({"spec": {"valuesContent": "   "}}))
    assert k8s_api._helmchartconfig_doc("spatium-control") == {}


def test_unexpected_status_reads_as_unknown(monkeypatch) -> None:
    _reader(monkeypatch, 500, "boom")
    assert k8s_api._helmchartconfig_doc("spatium-control") is None


def test_unparseable_body_reads_as_unknown(monkeypatch) -> None:
    _reader(monkeypatch, 200, "{not json")
    assert k8s_api._helmchartconfig_doc("spatium-control") is None


def test_unparseable_values_read_as_unknown(monkeypatch) -> None:
    # "there is a document and I cannot read it" is not "there is no
    # document" — merging onto {} here would delete the operator's keys.
    _reader(monkeypatch, 200, json.dumps({"spec": {"valuesContent": "a: [unclosed"}}))
    assert k8s_api._helmchartconfig_doc("spatium-control") is None


def test_non_mapping_values_read_as_unknown(monkeypatch) -> None:
    _reader(monkeypatch, 200, json.dumps({"spec": {"valuesContent": "- a\n- b\n"}}))
    assert k8s_api._helmchartconfig_doc("spatium-control") is None


def test_unparseable_values_abort_the_write(monkeypatch) -> None:
    writes: list[str] = []

    def _fake(method, path, body=None, content_type=None):
        if method == "GET":
            return (200, json.dumps({"spec": {"valuesContent": "a: [unclosed"}}))
        writes.append(method)
        return (200, "{}")

    monkeypatch.setattr(k8s_api, "_request", _fake)

    ok, err = k8s_api.apply_control_plane_overrides(2, "")

    assert ok is False
    assert err is not None
    assert writes == [], "a document we cannot read must not be overwritten"
