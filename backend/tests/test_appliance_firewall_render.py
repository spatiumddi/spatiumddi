"""Server-side firewall render (#285 Phase 2a + 3b) — render-identity tests.

The load-bearing contract is the THREE-WAY identity across an input matrix:

    render_drop_in (supervisor in-pod)
      == compile_firewall_body (frozen 2a port)
      == compile_firewall_from_policies (3b merge, fed the seeded builtins)

A one-byte divergence re-fires every node's host trigger on the
firewall_enabled flip (the merge runs when enabled; the in-pod renderer when
not). The pure-function legs run on the host venv; the supervisor leg loads
the in-pod renderer standalone via importlib and skips if it isn't on disk.
The async ``firewall_bundle`` legs use the empty-DB builtin fallback so they
need no seeding.
"""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys

import pytest

from app.services.appliance.firewall import compile_firewall_body, firewall_bundle
from app.services.appliance.firewall_merge import (
    builtin_policy_set,
    compile_firewall_from_policies,
    reset_policy_cache,
)

# The full input matrix the identity + smoke tests exercise.
_MATRIX: list[dict] = [
    {"role_assignment": {"roles": []}},  # idle
    {"role_assignment": {"roles": ["dns-bind9"]}},
    {"role_assignment": {"roles": ["dhcp"]}},
    {"role_assignment": {"roles": ["dns-powerdns", "dhcp"]}},
    # single-node CP: pod/service CIDR, no peers
    {
        "role_assignment": {"roles": []},
        "pod_cidrs": ["10.42.0.0/16"],
        "service_cidrs": ["10.43.0.0/16"],
    },
    # multi-node CP + VIP: peers, pod, svc, memberlist
    {
        "role_assignment": {"roles": ["dns-bind9"], "kubeapi_expose_cidrs": ["10.9.0.0/24"]},
        "cluster_peer_cidrs": ["192.168.0.133/32", "192.168.0.125/32"],
        "pod_cidrs": ["10.42.0.0/16"],
        "service_cidrs": ["10.43.0.0/16"],
        "cp_member_count": 3,
        "vip_configured": True,
    },
    # dual-stack peers
    {
        "role_assignment": {"roles": []},
        "cluster_peer_cidrs": ["192.168.0.10", "2001:db8::10", "2001:db8::11/128"],
        "pod_cidrs": ["10.42.0.0/16", "2001:cafe:42::/56"],
        "cp_member_count": 2,
    },
    # operator firewall_extra
    {"role_assignment": {"roles": ["dhcp"], "firewall_extra": 'udp dport 161 accept comment "x"'}},
    # Trap 1: junk pod CIDR — is_cp via RAW non-empty, but no valid union.
    {"role_assignment": {"roles": []}, "pod_cidrs": ["not-a-cidr"]},
    # multi-node but NO vip → memberlist guard fails (header + peer/api only)
    {
        "role_assignment": {"roles": []},
        "cluster_peer_cidrs": ["192.168.0.5/32"],
        "cp_member_count": 3,
        "vip_configured": False,
    },
    # #285 Phase 6 — Web-UI source-scoped (v4 only)
    {"role_assignment": {"roles": []}, "web_ui_allowed_cidrs": ["192.168.0.0/24", "10.0.0.0/8"]},
    # #285 Phase 6 — Web-UI source-scoped (dual-stack) on a DNS node
    {
        "role_assignment": {"roles": ["dns-bind9"]},
        "web_ui_allowed_cidrs": ["192.168.0.0/24", "2001:db8:f00d::/64"],
    },
]


@pytest.fixture(autouse=True)
def _reset_fw_cache():
    reset_policy_cache()
    yield
    reset_policy_cache()


def _call(fn, case: dict):
    return fn(
        case["role_assignment"],
        case.get("cluster_peer_cidrs"),
        pod_cidrs=case.get("pod_cidrs"),
        service_cidrs=case.get("service_cidrs"),
        cp_member_count=case.get("cp_member_count", 1),
        vip_configured=case.get("vip_configured", False),
        web_ui_allowed_cidrs=case.get("web_ui_allowed_cidrs"),
    )


def _call_merge(case: dict) -> str:
    return compile_firewall_from_policies(
        case["role_assignment"],
        case.get("cluster_peer_cidrs"),
        pod_cidrs=case.get("pod_cidrs"),
        service_cidrs=case.get("service_cidrs"),
        cp_member_count=case.get("cp_member_count", 1),
        vip_configured=case.get("vip_configured", False),
        policy_set=builtin_policy_set(),
        web_ui_allowed_cidrs=case.get("web_ui_allowed_cidrs"),
    )


def _load_supervisor_renderer():
    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "agent/supervisor/spatium_supervisor/firewall_renderer.py"
    )
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("_sup_firewall_renderer", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # The renderer defines a @dataclass; dataclasses resolves the class's
    # module via sys.modules, so register it before exec_module.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_merge_subsumes_legacy_renderer() -> None:
    # The 3b merge fed the seeded builtins reproduces the frozen 2a renderer
    # BYTE-FOR-BYTE across the matrix (the half of the triangle that lives in
    # this repo without the supervisor file on disk).
    for case in _MATRIX:
        legacy = _call(compile_firewall_body, case)
        merged = _call_merge(case)
        assert (
            merged == legacy
        ), f"merge drift for {case!r}\n--legacy--\n{legacy}\n--merge--\n{merged}"


def test_byte_identical_with_supervisor_renderer() -> None:
    sup = _load_supervisor_renderer()
    if sup is None:
        pytest.skip("supervisor firewall_renderer not on disk (backend-only checkout)")
    for case in _MATRIX:
        backend_body = _call(compile_firewall_body, case)
        supervisor_body = _call(sup.render_drop_in, case).body
        assert backend_body == supervisor_body, f"render drift for {case!r}"
        # Close the triangle: supervisor == merge too.
        assert _call_merge(case) == supervisor_body, f"merge≠supervisor for {case!r}"


def test_no_dataplane_rule_any_renderer() -> None:
    # No renderer may emit a flannel/wireguard INPUT rule (#285 — VXLAN
    # bypasses our chain). Guards against a one-sided data-plane-floor add.
    sup = _load_supervisor_renderer()
    for case in _MATRIX:
        for body in (_call(compile_firewall_body, case), _call_merge(case)):
            assert "8472" not in body and "51820" not in body and "dataplane" not in body
        if sup is not None:
            assert "8472" not in _call(sup.render_drop_in, case).body


async def test_bundle_disabled_shape() -> None:
    # Disabled path short-circuits before any DB read → db can be None.
    b = await firewall_bundle(
        None,
        role_assignment={"roles": []},
        cluster_peer_cidrs=[],
        pod_cidrs=[],
        service_cidrs=[],
        cp_member_count=1,
        vip_configured=False,
        firewall_enabled=False,
    )
    assert b == {"enabled": False, "config_hash": "", "firewall_conf": ""}


async def test_bundle_enabled_shape(db_session) -> None:
    # Empty DB → builtin fallback → byte-identical to the legacy render.
    b = await firewall_bundle(
        db_session,
        role_assignment={"roles": ["dns-bind9"]},
        cluster_peer_cidrs=[],
        pod_cidrs=[],
        service_cidrs=[],
        cp_member_count=1,
        vip_configured=False,
        firewall_enabled=True,
    )
    assert b["enabled"] is True
    assert b["firewall_conf"].startswith("# Auto-generated by spatium-supervisor")
    assert "tcp dport 53 accept" in b["firewall_conf"]
    assert b["config_hash"] == hashlib.sha256(b["firewall_conf"].encode()).hexdigest()
    legacy = compile_firewall_body({"roles": ["dns-bind9"]}, [])
    assert b["firewall_conf"] == legacy


async def test_bundle_multinode_retire_directive(db_session) -> None:
    single = await firewall_bundle(
        db_session,
        role_assignment={"roles": []},
        cluster_peer_cidrs=[],
        pod_cidrs=["10.42.0.0/16"],
        service_cidrs=[],
        cp_member_count=1,
        vip_configured=False,
        firewall_enabled=True,
    )
    multi = await firewall_bundle(
        db_session,
        role_assignment={"roles": []},
        cluster_peer_cidrs=["192.168.0.2/32"],
        pod_cidrs=["10.42.0.0/16"],
        service_cidrs=[],
        cp_member_count=3,
        vip_configured=False,
        firewall_enabled=True,
    )
    assert "# spatium-bootstrap: keep" in single["firewall_conf"]
    assert "# spatium-bootstrap: retire" in multi["firewall_conf"]


def test_web_ui_default_open_all_renderers() -> None:
    # #285 Phase 6 — with no scope set, EVERY renderer must emit the un-scoped
    # `tcp dport { 80, 443 } accept` (the base /etc/nftables.conf no longer
    # opens it, so the default-open behaviour now lives in the drop-in). This
    # is the anti-lockout floor for a fresh install.
    sup = _load_supervisor_renderer()
    case = {"role_assignment": {"roles": []}}
    expect = 'tcp dport { 80, 443 } accept comment "web-ui"'
    bodies = [_call(compile_firewall_body, case), _call_merge(case)]
    if sup is not None:
        bodies.append(_call(sup.render_drop_in, case).body)
    for body in bodies:
        assert expect in body
        assert "ip saddr" not in body.split('comment "web-ui"')[0].rsplit("\n", 1)[-1]


def test_web_ui_scoped_drops_open_accept() -> None:
    # When scoped, the un-scoped open accept must be GONE (replaced by a
    # source-matched accept); policy-drop then denies everything else.
    case = {
        "role_assignment": {"roles": []},
        "web_ui_allowed_cidrs": ["192.168.0.0/24", "2001:db8:f00d::/64"],
    }
    for body in (_call(compile_firewall_body, case), _call_merge(case)):
        assert 'tcp dport { 80, 443 } accept comment "web-ui"' not in body
        assert (
            'ip saddr { 192.168.0.0/24 } tcp dport { 80, 443 } accept comment "web-ui-v4"' in body
        )
        assert (
            'ip6 saddr { 2001:db8:f00d::/64 } tcp dport { 80, 443 } accept comment "web-ui-v6"'
            in body
        )
