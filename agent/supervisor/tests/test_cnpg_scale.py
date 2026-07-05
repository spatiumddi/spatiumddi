"""#272 — patch_cnpg_instances directly scales the CNPG Cluster CR.

The Cluster carries ``helm.sh/resource-policy: keep`` so the helm-controller
won't patch its spec on upgrade; the supervisor scales it out of band with a
merge-patch. These tests pin the GET-then-PATCH contract: idempotent on a
matching size, a real PATCH on a change, a quiet no-op when the Cluster
isn't up yet (404), and the ``< 1`` guard.
"""

from __future__ import annotations

import json

from spatium_supervisor import k8s_api


class _Recorder:
    """Stand-in for k8s_api._request that scripts a GET response then
    records the follow-up PATCH (if any)."""

    def __init__(self, get_status: int, get_body: str, patch_status: int = 200):
        self._get = (get_status, get_body)
        self._patch_status = patch_status
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(self, method, path, body=None, content_type=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return self._get
        return (self._patch_status, "{}")

    @property
    def patched(self) -> bool:
        return any(m == "PATCH" for m, _, _ in self.calls)

    @property
    def patch_body(self) -> dict:
        for method, _, body in self.calls:
            if method == "PATCH" and body is not None:
                return json.loads(body)
        return {}


def test_scale_up_patches_when_size_differs(monkeypatch) -> None:
    rec = _Recorder(200, json.dumps({"spec": {"instances": 1}}))
    monkeypatch.setattr(k8s_api, "_request", rec)

    changed, err = k8s_api.patch_cnpg_instances(3)

    assert (changed, err) == (True, None)
    assert rec.patched
    assert rec.patch_body == {"spec": {"instances": 3}}


def test_idempotent_when_size_matches(monkeypatch) -> None:
    rec = _Recorder(200, json.dumps({"spec": {"instances": 3}}))
    monkeypatch.setattr(k8s_api, "_request", rec)

    changed, err = k8s_api.patch_cnpg_instances(3)

    # Already at target — no PATCH, no "applied" log on the next heartbeat.
    assert (changed, err) == (False, None)
    assert not rec.patched


def test_missing_cluster_is_quiet_noop(monkeypatch) -> None:
    rec = _Recorder(404, "not found")
    monkeypatch.setattr(k8s_api, "_request", rec)

    changed, err = k8s_api.patch_cnpg_instances(3)

    # Cluster not up yet (early boot / non-cnpg deploy) — not an error.
    assert (changed, err) == (False, None)
    assert not rec.patched


def test_rejects_sub_one_size(monkeypatch) -> None:
    rec = _Recorder(200, json.dumps({"spec": {"instances": 1}}))
    monkeypatch.setattr(k8s_api, "_request", rec)

    changed, err = k8s_api.patch_cnpg_instances(0)

    assert changed is False
    assert err == "instances < 1"
    assert not rec.calls  # guard short-circuits before any kubeapi call


# ── #272 Phase 10 — data-plane VIP overrides (HelmChartConfig render) ──


def test_dataplane_vip_overrides_render(monkeypatch) -> None:
    # No existing HelmChartConfig (404) → create POST carrying the values.
    rec = _Recorder(404, "")
    monkeypatch.setattr(k8s_api, "_request", rec)

    changed, err = k8s_api.apply_dataplane_vip_overrides(
        dns_vip="192.168.0.242", dhcp_relay_vip="192.168.0.243"
    )

    assert (changed, err) == (True, None)
    body = next(json.loads(b) for m, _, b in rec.calls if m == "POST" and b)
    vals = body["spec"]["valuesContent"]
    assert "useMetalLBVIP: true" in vals
    assert 'vip: "192.168.0.242"' in vals
    assert 'relayVIP: "192.168.0.243"' in vals


def test_dataplane_vip_overrides_empty_disables(monkeypatch) -> None:
    rec = _Recorder(404, "")
    monkeypatch.setattr(k8s_api, "_request", rec)

    k8s_api.apply_dataplane_vip_overrides(dns_vip="", dhcp_relay_vip="")

    body = next(json.loads(b) for m, _, b in rec.calls if m == "POST" and b)
    vals = body["spec"]["valuesContent"]
    assert "useMetalLBVIP: false" in vals
    assert 'vip: ""' in vals
    assert 'relayVIP: ""' in vals


# ── issue #566 decision D1 — MetalLB BGP mode overrides ────────────────


def test_metallb_bgp_overrides_render(monkeypatch) -> None:
    # No existing HelmChartConfig (404) → create POST carrying the values.
    rec = _Recorder(404, "")
    monkeypatch.setattr(k8s_api, "_request", rec)

    changed, err = k8s_api.apply_metallb_overrides(
        metallb_enabled=True,
        pool_addresses=["192.168.0.240/32"],
        bgp_enabled=True,
        bgp_peers=[
            {
                "my_asn": 65000,
                "peer_asn": 65001,
                "peer_address": "203.0.113.1",
                "peer_port": 1179,
                "hold_time": "90s",
            }
        ],
        bgp_advertisements=[{"ip_address_pools": ["spatium-control-plane"]}],
    )

    assert (changed, err) == (True, None)
    body = next(json.loads(b) for m, _, b in rec.calls if m == "POST" and b)
    vals = body["spec"]["valuesContent"]
    assert "frrk8s:\n    enabled: true" in vals
    assert "bgp:\n    enabled: true" in vals
    assert '"myASN": 65000' in vals
    assert '"peerASN": 65001' in vals
    assert '"peerAddress": "203.0.113.1"' in vals
    assert '"peerPort": 1179' in vals
    assert '"holdTime": "90s"' in vals
    assert '"ipAddressPools": ["spatium-control-plane"]' in vals


def test_metallb_bgp_overrides_disabled_shape(monkeypatch) -> None:
    # No peers / bgp_enabled=False (default) — L2-only shape, matching
    # today's behavior unchanged. Also the shape a heartbeat renders
    # when the operator hasn't configured BGP at all.
    rec = _Recorder(404, "")
    monkeypatch.setattr(k8s_api, "_request", rec)

    changed, err = k8s_api.apply_metallb_overrides(
        metallb_enabled=True, pool_addresses=["192.168.0.240/32"]
    )

    assert (changed, err) == (True, None)
    body = next(json.loads(b) for m, _, b in rec.calls if m == "POST" and b)
    vals = body["spec"]["valuesContent"]
    assert "frrk8s:\n    enabled: false" in vals
    assert "bgp:\n    enabled: false" in vals
    assert "peers: []" in vals
    assert "advertisements: []" in vals
