"""Unit tests for the appliance Web UI firewall self-check (#779).

``spatiumddi-webui-selfcheck`` is a standalone host script (pure stdlib)
living at ``appliance/mkosi.extra/usr/local/bin/`` — not part of the
``app`` package, so it is loaded by path here, same convention as
``test_spatium_console.py``. Skips cleanly when the script isn't present
in the checkout (the dev container copies only ``backend/``).

The interesting surface is the ruleset-reasoning: "would a NEW off-box
TCP connection to 443 be accepted?" answered from ``nft -j`` JSON. The
fixtures mirror real appliance ruleset shapes (verified against live
nft 1.1.x output during review): the base config (policy drop, ct rules,
``iif lo`` accept, ssh), the ``00-spatium-webui.nft`` bootstrap sentinel,
the supervisor drop-in's scoped web-ui rule, k3s's iptables-nft
``ip filter INPUT`` chain, and the #776 failure (no accept at all).

Three traps this file exists to pin, all found in review:

* ``iif lo accept`` must never count as reachability — ``curl`` from the
  appliance to its own LAN IP rides ``lo`` and succeeds against a
  firewalled port (the false-green #776's diagnostic session fell into).
* input-hooked chains are evaluated INDEPENDENTLY and combined
  worst-wins — an operator's stray ``iptables -A INPUT ... -j ACCEPT``
  lands in kube-proxy's accept-policy ``ip filter INPUT`` and must not
  mask a drop-policy ``inet filter input`` with no accept (false-open).
* qualified drops (saddr / interface / rate-limit) and unmodeled
  constructs (``!=`` ops, named sets, vmaps, jumps in drop-policy
  chains) must never produce a confident wrong verdict — the qualified
  drops are skipped, the unmodeled shapes degrade to ``indeterminate``
  (crying wolf on a healthy hardened box is the failure mode #779 names
  as worse than no check).
"""

from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "appliance"
    / "mkosi.extra"
    / "usr"
    / "local"
    / "bin"
    / "spatiumddi-webui-selfcheck"
)

pytestmark = pytest.mark.skipif(
    not _SCRIPT_PATH.exists(),
    reason="webui-selfcheck host script not present in this checkout",
)


@pytest.fixture(scope="module")
def m():
    loader = SourceFileLoader("webui_selfcheck_under_test", str(_SCRIPT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ── nft -j fixture builders ─────────────────────────────────────────────────


def _chain(
    policy: str = "drop",
    *,
    family: str = "inet",
    table: str = "filter",
    name: str = "input",
) -> dict[str, Any]:
    return {
        "chain": {
            "family": family,
            "table": table,
            "name": name,
            "handle": 1,
            "type": "filter",
            "hook": "input",
            "prio": 0,
            "policy": policy,
        }
    }


def _rule(
    *exprs: dict[str, Any],
    family: str = "inet",
    table: str = "filter",
    chain: str = "input",
) -> dict[str, Any]:
    return {
        "rule": {
            "family": family,
            "table": table,
            "chain": chain,
            "handle": 10,
            "expr": list(exprs),
        }
    }


def _dport_match(right: Any, *, proto: str = "tcp", op: str = "==") -> dict[str, Any]:
    return {
        "match": {
            "op": op,
            "left": {"payload": {"protocol": proto, "field": "dport"}},
            "right": right,
        }
    }


def _saddr_match(prefixes: list[tuple[str, int]], *, proto: str = "ip") -> dict[str, Any]:
    return {
        "match": {
            "op": "==",
            "left": {"payload": {"protocol": proto, "field": "saddr"}},
            "right": {"set": [{"prefix": {"addr": a, "len": ln}} for a, ln in prefixes]},
        }
    }


def _iif_match(name: str) -> dict[str, Any]:
    return {"match": {"op": "==", "left": {"meta": {"key": "iif"}}, "right": name}}


def _base_rules() -> list[dict[str, Any]]:
    """The always-present base-config rules that must all be skipped."""
    return [
        # ct state established,related accept (real nft: bare list RHS)
        _rule(
            {
                "match": {
                    "op": "in",
                    "left": {"ct": {"key": "state"}},
                    "right": ["established", "related"],
                }
            },
            {"accept": None},
        ),
        # ct state invalid drop (real nft: bare string RHS)
        _rule(
            {
                "match": {
                    "op": "in",
                    "left": {"ct": {"key": "state"}},
                    "right": "invalid",
                }
            },
            {"drop": None},
        ),
        # iif lo accept — the #776 trap; must never count as reachability
        _rule(_iif_match("lo"), {"accept": None}),
        # ssh
        _rule(_dport_match(22), {"accept": None}),
    ]


def _doc(*entries: dict[str, Any], policy: str = "drop") -> dict[str, Any]:
    """A single-chain doc: the appliance's ``inet filter input``."""
    return {
        "nftables": [
            {"metainfo": {"version": "1.0.9", "json_schema_version": 1}},
            {"table": {"family": "inet", "name": "filter", "handle": 1}},
            _chain(policy),
            *_base_rules(),
            *entries,
        ]
    }


_SENTINEL = {"set": [80, 443]}  # 00-spatium-webui.nft's dport form


# ── single-chain shapes ─────────────────────────────────────────────────────


def test_sentinel_accept_is_open(m) -> None:
    """The 00-spatium-webui.nft shape: tcp dport { 80, 443 } accept."""
    doc = _doc(_rule(_dport_match(_SENTINEL), {"accept": None}))
    status, _ = m.evaluate_port(doc, 443)
    assert status == "open"
    assert m.evaluate_port(doc, 80)[0] == "open"


def test_missing_accept_is_blocked(m) -> None:
    """The #776 failure: base rules only, policy drop, no web accept."""
    status, detail = m.evaluate_port(_doc(), 443)
    assert status == "blocked"
    assert "policy drop" in detail


def test_iif_lo_accept_alone_is_still_blocked(m) -> None:
    """The trap this whole check exists for: an lo-guarded accept for 443
    must not read as reachable — off-box traffic never arrives on lo."""
    doc = _doc(_rule(_iif_match("lo"), _dport_match(_SENTINEL), {"accept": None}))
    assert m.evaluate_port(doc, 443)[0] == "blocked"


def test_scoped_accept_reports_scoped(m) -> None:
    """web_ui_allowed_cidrs set (#285 Phase 6): hardened, not broken."""
    doc = _doc(_rule(_saddr_match([("10.20.0.0", 16)]), _dport_match(_SENTINEL), {"accept": None}))
    status, detail = m.evaluate_port(doc, 443)
    assert status == "scoped"
    assert "10.20.0.0/16" in detail


def test_v6_only_scoped_accept_reports_scoped(m) -> None:
    """A v6-only web_ui_allowed_cidrs renders only an ip6-saddr rule; that
    is the operator's configured intent — scoped, never a false blocked."""
    doc = _doc(
        _rule(
            _saddr_match([("2001:db8::", 32)], proto="ip6"),
            _dport_match(_SENTINEL),
            {"accept": None},
        )
    )
    status, detail = m.evaluate_port(doc, 443)
    assert status == "scoped"
    assert "2001:db8::/32" in detail


def test_explicit_unqualified_drop_wins_first_match(m) -> None:
    doc = _doc(
        _rule(_dport_match(443), {"drop": None}),
        _rule(_dport_match(_SENTINEL), {"accept": None}),
    )
    assert m.evaluate_port(doc, 443)[0] == "blocked"


def test_port_range_containing_443_is_open(m) -> None:
    doc = _doc(_rule(_dport_match({"range": [400, 500]}), {"accept": None}))
    assert m.evaluate_port(doc, 443)[0] == "open"
    assert m.evaluate_port(doc, 80)[0] == "blocked"


def test_policy_accept_with_no_rules_is_open(m) -> None:
    status, detail = m.evaluate_port(_doc(policy="accept"), 443)
    assert status == "open"
    assert "policy accept" in detail


def test_no_input_chain_is_open(m) -> None:
    doc = {"nftables": [{"metainfo": {}}, {"table": {"family": "inet", "name": "filter"}}]}
    assert m.evaluate_port(doc, 443)[0] == "open"


def test_garbage_doc_is_indeterminate(m) -> None:
    assert m.evaluate_port({"bogus": True}, 443)[0] == "indeterminate"


def test_other_port_accept_does_not_leak(m) -> None:
    """An accept for 8443 must not read as 443 reachability."""
    doc = _doc(_rule(_dport_match(8443), {"accept": None}))
    assert m.evaluate_port(doc, 443)[0] == "blocked"


# ── qualified drops: skipped, never a blanket block ─────────────────────────


def test_saddr_scoped_drop_does_not_block(m) -> None:
    """Blocking one abusive /24 doesn't make the UI LAN-unreachable."""
    doc = _doc(
        _rule(_saddr_match([("203.0.113.0", 24)]), _dport_match(_SENTINEL), {"drop": None}),
        _rule(_dport_match(_SENTINEL), {"accept": None}),
    )
    assert m.evaluate_port(doc, 443)[0] == "open"


def test_interface_scoped_drop_does_not_block(m) -> None:
    """No UI on the DMZ NIC ≠ no UI at all."""
    doc = _doc(
        _rule(_iif_match("eth1"), _dport_match(443), {"drop": None}),
        _rule(_dport_match(_SENTINEL), {"accept": None}),
    )
    assert m.evaluate_port(doc, 443)[0] == "open"


def test_rate_limit_drop_does_not_block(m) -> None:
    """A `limit rate over ... drop` SYN-flood protector isn't a block."""
    doc = _doc(
        _rule(
            _dport_match(443),
            {"limit": {"rate": 10, "per": "second", "inv": True}},
            {"drop": None},
        ),
        _rule(_dport_match(_SENTINEL), {"accept": None}),
    )
    assert m.evaluate_port(doc, 443)[0] == "open"


# ── unmodeled constructs: indeterminate, never a confident wrong answer ─────


def test_negated_drop_is_never_credited_as_explicit_drop(m) -> None:
    """`tcp dport != 443 drop` never touches 443. Under a drop policy with
    no accept the verdict is still blocked — but by the POLICY, not by a
    phantom 'explicit drop rule for 443' (the old model's misattribution
    that survived even when an accept followed)."""
    doc = _doc(_rule(_dport_match(443, op="!="), {"drop": None}))
    status, detail = m.evaluate_port(doc, 443)
    assert status == "blocked"
    assert "policy drop" in detail
    assert "explicit" not in detail


def test_negated_drop_before_accept_is_indeterminate(m) -> None:
    """Ordering can't be trusted around an unmodeled rule — even with a
    modeled accept after it, the negation may have dropped first."""
    doc = _doc(
        _rule(_dport_match(80, op="!="), {"drop": None}),  # really drops 443!
        _rule(_dport_match(_SENTINEL), {"accept": None}),
    )
    assert m.evaluate_port(doc, 443)[0] == "indeterminate"


def test_negated_drop_under_policy_accept_is_indeterminate(m) -> None:
    """`tcp dport != 80 drop` + policy accept really blocks 443; claiming
    open would be a false-green."""
    doc = _doc(_rule(_dport_match(80, op="!="), {"drop": None}), policy="accept")
    assert m.evaluate_port(doc, 443)[0] == "indeterminate"


def test_named_set_accept_is_indeterminate(m) -> None:
    """`tcp dport @webports accept` — we don't resolve named sets; under
    policy drop the old model reported a confident false 'blocked'."""
    doc = _doc(_rule(_dport_match("@webports"), {"accept": None}))
    assert m.evaluate_port(doc, 443)[0] == "indeterminate"


def test_dport_vmap_is_indeterminate(m) -> None:
    doc = _doc(
        _rule(
            _dport_match({"vmap": [[443, {"accept": None}]]}),
        )
    )
    assert m.evaluate_port(doc, 443)[0] == "indeterminate"


def test_jump_in_drop_policy_chain_is_indeterminate(m) -> None:
    """The accept may live in the jumped-to sub-chain we don't follow."""
    doc = _doc(_rule({"jump": {"target": "websvc"}}))
    assert m.evaluate_port(doc, 443)[0] == "indeterminate"


def test_modeled_drop_before_unmodeled_rule_still_blocks(m) -> None:
    """First-match ordering IS trustworthy up to the unmodeled rule."""
    doc = _doc(
        _rule(_dport_match(443), {"drop": None}),
        _rule(_dport_match("@webports"), {"accept": None}),
    )
    assert m.evaluate_port(doc, 443)[0] == "blocked"


def test_meta_l4proto_set_th_dport_is_open(m) -> None:
    """`meta l4proto { tcp, udp } th dport { 80, 443 } accept` — the meta
    set must unwrap, and the protocol-agnostic `th` payload must not
    clobber the tcp answer (used to be skipped → false blocked)."""
    doc = _doc(
        _rule(
            {
                "match": {
                    "op": "==",
                    "left": {"meta": {"key": "l4proto"}},
                    "right": {"set": ["tcp", "udp"]},
                }
            },
            _dport_match(_SENTINEL, proto="th"),
            {"accept": None},
        )
    )
    assert m.evaluate_port(doc, 443)[0] == "open"


# ── multi-chain: independent evaluation, worst wins ─────────────────────────


def _k3s_input_chain(*extra: dict[str, Any]) -> list[dict[str, Any]]:
    """kube-proxy's iptables-nft `ip filter INPUT`: policy accept, ct+jump."""
    return [
        {"table": {"family": "ip", "name": "filter", "handle": 2}},
        _chain("accept", family="ip", table="filter", name="INPUT"),
        _rule(
            {
                "match": {
                    "op": "in",
                    "left": {"ct": {"key": "state"}},
                    "right": "new",
                }
            },
            {"jump": {"target": "KUBE-EXTERNAL-SERVICES"}},
            family="ip",
            table="filter",
            chain="INPUT",
        ),
        *extra,
    ]


def test_accept_in_other_chain_does_not_mask_blocked_inet(m) -> None:
    """The reproduced false-open: an operator's `iptables -A INPUT -p tcp
    --dport 443 -j ACCEPT` lands in accept-policy `ip filter INPUT` and
    must NOT mask the drop-policy inet chain that still has no accept —
    the packet dies in the inet chain regardless."""
    doc = _doc()  # inet: policy drop, no web accept (the #776 state)
    doc["nftables"].extend(
        _k3s_input_chain(
            _rule(
                _dport_match(443),
                {"accept": None},
                family="ip",
                table="filter",
                chain="INPUT",
            )
        )
    )
    status, detail = m.evaluate_port(doc, 443)
    assert status == "blocked"
    assert "inet" in detail  # names the chain that drops


def test_stock_k3s_chains_do_not_disturb_open_verdict(m) -> None:
    """Real appliance shape: inet chain with the sentinel + kube's
    ct/jump-only accept-policy INPUT chains. Jumps in accept-policy
    chains are skipped (service-layer rejects are out of frame)."""
    doc = _doc(_rule(_dport_match(_SENTINEL), {"accept": None}))
    doc["nftables"].extend(_k3s_input_chain())
    assert m.evaluate_port(doc, 443)[0] == "open"


def test_blocked_chain_outranks_indeterminate_chain(m) -> None:
    doc = _doc()  # inet: blocked
    doc["nftables"].extend(
        _k3s_input_chain(
            _rule(
                _dport_match(443, op="!="),
                {"drop": None},
                family="ip",
                table="filter",
                chain="INPUT",
            )
        )
    )
    assert m.evaluate_port(doc, 443)[0] == "blocked"


# ── run_check / main ────────────────────────────────────────────────────────


def test_run_check_indeterminate_when_nft_unavailable(m, monkeypatch) -> None:
    def _boom() -> None:
        raise FileNotFoundError("nft")

    monkeypatch.setattr(m, "_read_ruleset", _boom)
    result = m.run_check()
    assert result["status"] == "indeterminate"
    assert "could not read ruleset" in result["detail"]
    assert result["consecutive_indeterminate"] == 1
    assert result["ttl_s"] == m.TTL_S


def test_run_check_indeterminate_streak_accumulates(m, monkeypatch) -> None:
    """The persistent-indeterminate counter is what lets the console say
    'the checker itself is broken' instead of dying silently."""
    monkeypatch.setattr(m, "_read_ruleset", lambda: (_ for _ in ()).throw(FileNotFoundError("nft")))
    result = m.run_check(previous={"consecutive_indeterminate": 4})
    assert result["consecutive_indeterminate"] == 5

    # A successful run resets the streak.
    monkeypatch.setattr(
        m, "_read_ruleset", lambda: _doc(_rule(_dport_match(_SENTINEL), {"accept": None}))
    )
    result = m.run_check(previous={"consecutive_indeterminate": 4})
    assert result["status"] == "open"
    assert result["consecutive_indeterminate"] == 0


def test_main_writes_state_and_exits_by_verdict(m, monkeypatch, tmp_path) -> None:
    state = tmp_path / "webui-selfcheck.json"
    monkeypatch.setattr(m, "STATE_PATH", state)

    monkeypatch.setattr(
        m, "_read_ruleset", lambda: _doc(_rule(_dport_match(_SENTINEL), {"accept": None}))
    )
    assert m.main() == 0
    saved = json.loads(state.read_text())
    assert saved["status"] == "open"
    assert saved["ports"]["443"]["status"] == "open"
    assert saved["ports"]["80"]["status"] == "open"
    assert isinstance(saved["checked_at"], int)
    assert saved["ttl_s"] == m.TTL_S

    monkeypatch.setattr(m, "_read_ruleset", _doc)
    assert m.main() == 1
    assert json.loads(state.read_text())["status"] == "blocked"

    monkeypatch.setattr(m, "_read_ruleset", lambda: (_ for _ in ()).throw(FileNotFoundError("nft")))
    assert m.main() == 2
    saved = json.loads(state.read_text())
    assert saved["status"] == "indeterminate"
    # main() reads the previous state file, so the streak persists across
    # process invocations.
    assert saved["consecutive_indeterminate"] == 1
    assert m.main() == 2
    assert json.loads(state.read_text())["consecutive_indeterminate"] == 2
