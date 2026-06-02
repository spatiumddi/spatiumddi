"""Merge-engine internals (#285 Phase 3b).

The byte-identity of the BUILTINS is proven in test_appliance_firewall_render
(the three-way triangle). Here we test the parts the builtins don't exercise:
the operator-overlay pipeline (explode → deny-wins → source-union), derived-
source resolution, render guards, family filtering — plus a drift guard tying
the in-code canonical builtins to the seed migration.
"""

from __future__ import annotations

import importlib.util
import pathlib

from app.services.appliance.firewall_merge import (
    _BUILTIN_GUARD,
    _BUILTIN_SEED,
    MergeContext,
    PolicySet,
    _Alias,
    _guard_ok,
    _Policy,
    _Rule,
    builtin_policy_set,
    compile_firewall_from_policies,
)


def _rule(seq, action, proto, ports, **kw) -> _Rule:
    return _Rule(
        seq=seq,
        action=action,
        protocol=proto,
        ports=tuple(ports),
        source_kind=kw.get("source_kind", "any"),
        source_cidrs=tuple(kw.get("source_cidrs", ())),
        source_alias=kw.get("source_alias"),
        family=kw.get("family", "both"),
        comment=kw.get("comment"),
        render_guard=kw.get("render_guard"),
        enabled=kw.get("enabled", True),
    )


def _overlay_lines(fleet_rules=(), appliance_rules=(), aliases=None) -> list[str]:
    """Render an idle (non-CP) node so only the overlay block has content,
    and return just the overlay lines."""
    ps = PolicySet(
        fleet=_Policy("fleet", None, True, tuple(fleet_rules)),
        aliases=aliases or {},
    )
    appliance_policy = (
        _Policy("appliance", None, True, tuple(appliance_rules)) if appliance_rules else None
    )
    body = compile_firewall_from_policies(
        {"roles": []},
        None,
        policy_set=ps,
        appliance_policy=appliance_policy,
    )
    lines = body.splitlines()
    if "# ── Fleet / appliance overlay ──────────────────────────" not in lines:
        return []
    i = lines.index("# ── Fleet / appliance overlay ──────────────────────────")
    return [ln for ln in lines[i + 1 :] if ln and not ln.startswith("#")]


def test_overlay_deny_wins_emits_drops_first() -> None:
    # nft is first-match-wins: a DROP authored after an ACCEPT must still emit
    # before it so it actually blocks.
    out = _overlay_lines(
        fleet_rules=[
            _rule(10, "accept", "tcp", [8080], comment="app"),
            _rule(
                20,
                "drop",
                "tcp",
                [8080],
                source_kind="cidr",
                source_cidrs=["10.0.0.0/8"],
                comment="block",
            ),  # noqa: E501
        ]
    )
    drop_idx = next(i for i, ln in enumerate(out) if " drop" in ln)
    accept_idx = next(i for i, ln in enumerate(out) if " accept" in ln)
    assert drop_idx < accept_idx, out


def test_overlay_source_union_merges_same_target() -> None:
    out = _overlay_lines(
        fleet_rules=[
            _rule(
                10,
                "accept",
                "tcp",
                [443],
                source_kind="cidr",
                source_cidrs=["10.0.0.0/8"],
                comment="https",
            ),  # noqa: E501
            _rule(
                20,
                "accept",
                "tcp",
                [443],
                source_kind="cidr",
                source_cidrs=["192.168.0.0/16"],
                comment="https",
            ),  # noqa: E501
        ]
    )
    # One unioned line, both CIDRs in a single set (deterministic sort).
    assert len(out) == 1, out
    assert "10.0.0.0/8" in out[0] and "192.168.0.0/16" in out[0]
    assert (
        out[0] == 'ip saddr { 10.0.0.0/8, 192.168.0.0/16 } tcp dport 443 accept comment "https-v4"'
    )


def test_overlay_alias_resolution() -> None:
    aliases = {"mgmt": _Alias("mgmt", ("10.1.0.0/24",), ("2001:db8::/64",), ())}
    out = _overlay_lines(
        fleet_rules=[
            _rule(
                10,
                "accept",
                "tcp",
                [22],
                source_kind="alias",
                source_alias="mgmt",
                comment="ssh-net",
            )
        ],  # noqa: E501
        aliases=aliases,
    )
    assert any("10.1.0.0/24" in ln and "ssh-net-v4" in ln for ln in out), out
    assert any("2001:db8::/64" in ln and "ssh-net-v6" in ln for ln in out), out


def test_overlay_any_source_emits_bare() -> None:
    out = _overlay_lines(fleet_rules=[_rule(10, "accept", "udp", [123], comment="ntp")])
    assert out == ['udp dport 123 accept comment "ntp"'], out


def test_overlay_family_filter() -> None:
    out = _overlay_lines(
        fleet_rules=[
            _rule(
                10,
                "accept",
                "tcp",
                [9000],
                source_kind="cidr",
                source_cidrs=["10.0.0.0/8", "2001:db8::/64"],
                family="v4",
                comment="v4only",
            ),
        ]
    )
    assert len(out) == 1 and "10.0.0.0/8" in out[0] and "2001:db8" not in out[0], out


def test_overlay_appliance_after_fleet() -> None:
    out = _overlay_lines(
        fleet_rules=[_rule(10, "accept", "udp", [100], comment="fleet")],
        appliance_rules=[_rule(10, "accept", "udp", [200], comment="appl")],
    )
    assert out == [
        'udp dport 100 accept comment "fleet"',
        'udp dport 200 accept comment "appl"',
    ], out


def test_overlay_disabled_policy_skipped() -> None:
    ps = PolicySet(
        fleet=_Policy("fleet", None, False, (_rule(10, "accept", "udp", [9], comment="x"),))
    )
    body = compile_firewall_from_policies({"roles": []}, None, policy_set=ps)
    assert "overlay" not in body


def test_guard_ok_matrix() -> None:
    ctx_lo = MergeContext.build(
        {"roles": []},
        None,
        pod_cidrs=None,
        service_cidrs=None,
        cp_member_count=1,
        vip_configured=False,
    )
    ctx_hi = MergeContext.build(
        {"roles": []},
        None,
        pod_cidrs=None,
        service_cidrs=None,
        cp_member_count=3,
        vip_configured=True,
    )
    assert _guard_ok(None, ctx_lo) is True
    assert _guard_ok(_BUILTIN_GUARD, ctx_lo) is False  # 1 member, no vip
    assert _guard_ok(_BUILTIN_GUARD, ctx_hi) is True
    assert _guard_ok({"min_cp_members": 2}, ctx_hi) is True
    assert _guard_ok({"requires_vip": True}, ctx_lo) is False


def test_resolve_source_kubeapi_union() -> None:
    ctx = MergeContext.build(
        {"roles": [], "kubeapi_expose_cidrs": ["10.9.0.0/24"]},
        ["192.168.0.1/32"],
        pod_cidrs=["10.42.0.0/16"],
        service_cidrs=["10.43.0.0/16"],
        cp_member_count=3,
        vip_configured=True,
    )
    v4, v6 = ctx.resolve_source(_rule(1, "accept", "tcp", [6443], source_kind="kubeapi"))
    assert set(v4) == {"192.168.0.1/32", "10.42.0.0/16", "10.43.0.0/16", "10.9.0.0/24"}
    assert v6 == []
    # cluster_peers is the narrower set (no pod/svc/kubeapi).
    pv4, _ = ctx.resolve_source(_rule(1, "accept", "tcp", [2379], source_kind="cluster_peers"))
    assert pv4 == ["192.168.0.1/32"]


def test_unknown_alias_resolves_empty() -> None:
    ctx = MergeContext.build(
        {"roles": []},
        None,
        pod_cidrs=None,
        service_cidrs=None,
        cp_member_count=1,
        vip_configured=False,
    )
    assert ctx.resolve_source(
        _rule(1, "accept", "tcp", [22], source_kind="alias", source_alias="nope")
    ) == ([], [])


def test_builtin_set_shape() -> None:
    ps = builtin_policy_set()
    assert ps.fleet is not None and ps.fleet.rules == ()
    assert set(ps.roles) == {
        "dns-bind9",
        "dns-powerdns",
        "dhcp",
        "control-plane",
        "observer",
        "custom",
    }
    assert ps.roles["observer"].enabled is False
    cp = ps.roles["control-plane"]
    assert [r.seq for r in cp.rules] == [10, 20, 30, 40]
    assert cp.rules[2].render_guard == _BUILTIN_GUARD


def _load_seed_migration():
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "alembic/versions/f5b8d2c91a06_firewall_builtin_seed.py"
    )
    spec = importlib.util.spec_from_file_location("_fw_seed_mig", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_builtin_seed_matches_migration() -> None:
    """The in-code canonical builtins (the render source on an unseeded DB)
    must match the seed migration row-for-row, else a node renders one set
    while the DB holds another."""
    mig = _load_seed_migration()
    assert mig._GUARD == _BUILTIN_GUARD

    def norm_merge(entry):
        sk, sr, enabled, rules = entry
        return (
            sk,
            sr,
            enabled,
            [(s, a, p, tuple(po), k, f, c, g) for (s, a, p, po, k, f, c, g) in rules],
        )  # noqa: E501

    def norm_mig(entry):
        sk, sr, _name, enabled, rules = entry
        return (
            sk,
            sr,
            enabled,
            [(s, a, p, tuple(po), k, f, c, g) for (s, a, p, po, k, f, c, g) in rules],
        )  # noqa: E501

    assert [norm_merge(e) for e in _BUILTIN_SEED] == [norm_mig(e) for e in mig._POLICIES]
