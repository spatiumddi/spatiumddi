"""Unit tests for the appliance Web UI firewall self-check (#779).

``spatiumddi-webui-selfcheck`` is a standalone host script (pure stdlib)
living at ``appliance/mkosi.extra/usr/local/bin/`` — not part of the
``app`` package, so it is loaded by path here, same convention as
``test_spatium_console.py``. Skips cleanly when the script isn't present
in the checkout (the dev container copies only ``backend/``).

The interesting surface is ``evaluate_port`` — the ruleset-reasoning that
answers "would a NEW off-box TCP connection to 443 be accepted?" from
``nft -j`` JSON. The fixtures below mirror the real appliance ruleset
shapes: the base config (policy drop, ct rules, ``iif lo`` accept, ssh),
the ``00-spatium-webui.nft`` bootstrap sentinel, the supervisor drop-in's
scoped web-ui rule, and the #776 failure (no accept at all).

The one trap this file exists to pin: an ``iif lo accept`` must never
count as reachability — ``curl`` from the appliance to its own LAN IP
rides ``lo`` and succeeds against a firewalled port, which is exactly the
false-green #776's diagnostic session fell into.
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


def _chain(policy: str = "drop") -> dict[str, Any]:
    return {
        "chain": {
            "family": "inet",
            "table": "filter",
            "name": "input",
            "handle": 1,
            "type": "filter",
            "hook": "input",
            "prio": 0,
            "policy": policy,
        }
    }


def _rule(*exprs: dict[str, Any]) -> dict[str, Any]:
    return {
        "rule": {
            "family": "inet",
            "table": "filter",
            "chain": "input",
            "handle": 10,
            "expr": list(exprs),
        }
    }


def _dport_match(right: Any) -> dict[str, Any]:
    return {
        "match": {
            "op": "==",
            "left": {"payload": {"protocol": "tcp", "field": "dport"}},
            "right": right,
        }
    }


def _base_rules() -> list[dict[str, Any]]:
    """The always-present base-config rules that must all be skipped."""
    return [
        # ct state established,related accept
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
        # ct state invalid drop
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
        _rule(
            {"match": {"op": "==", "left": {"meta": {"key": "iif"}}, "right": "lo"}},
            {"accept": None},
        ),
        # ssh
        _rule(_dport_match(22), {"accept": None}),
    ]


def _doc(*entries: dict[str, Any], policy: str = "drop") -> dict[str, Any]:
    return {
        "nftables": [
            {"metainfo": {"version": "1.0.9", "json_schema_version": 1}},
            {"table": {"family": "inet", "name": "filter", "handle": 1}},
            _chain(policy),
            *_base_rules(),
            *entries,
        ]
    }


# ── evaluate_port ───────────────────────────────────────────────────────────


def test_sentinel_accept_is_open(m) -> None:
    """The 00-spatium-webui.nft shape: tcp dport { 80, 443 } accept."""
    doc = _doc(_rule(_dport_match({"set": [80, 443]}), {"accept": None}))
    status, _ = m.evaluate_port(doc, 443)
    assert status == "open"
    assert m.evaluate_port(doc, 80)[0] == "open"


def test_missing_accept_is_blocked(m) -> None:
    """The #776 failure: base rules only, policy drop, no web accept."""
    doc = _doc()
    status, detail = m.evaluate_port(doc, 443)
    assert status == "blocked"
    assert "policy drop" in detail


def test_iif_lo_accept_alone_is_still_blocked(m) -> None:
    """The trap this whole check exists for: an lo-guarded accept for 443
    must not read as reachable — off-box traffic never arrives on lo."""
    doc = _doc(
        _rule(
            {"match": {"op": "==", "left": {"meta": {"key": "iif"}}, "right": "lo"}},
            _dport_match({"set": [80, 443]}),
            {"accept": None},
        )
    )
    assert m.evaluate_port(doc, 443)[0] == "blocked"


def test_scoped_accept_reports_scoped(m) -> None:
    """web_ui_allowed_cidrs set (#285 Phase 6): hardened, not broken."""
    doc = _doc(
        _rule(
            {
                "match": {
                    "op": "==",
                    "left": {"payload": {"protocol": "ip", "field": "saddr"}},
                    "right": {"set": [{"prefix": {"addr": "10.20.0.0", "len": 16}}]},
                }
            },
            _dport_match({"set": [80, 443]}),
            {"accept": None},
        )
    )
    status, detail = m.evaluate_port(doc, 443)
    assert status == "scoped"
    assert "10.20.0.0/16" in detail


def test_explicit_drop_wins_first_match(m) -> None:
    doc = _doc(
        _rule(_dport_match(443), {"drop": None}),
        _rule(_dport_match({"set": [80, 443]}), {"accept": None}),
    )
    assert m.evaluate_port(doc, 443)[0] == "blocked"


def test_port_range_containing_443_is_open(m) -> None:
    doc = _doc(_rule(_dport_match({"range": [400, 500]}), {"accept": None}))
    assert m.evaluate_port(doc, 443)[0] == "open"
    assert m.evaluate_port(doc, 80)[0] == "blocked"


def test_policy_accept_with_no_rules_is_open(m) -> None:
    doc = _doc(policy="accept")
    status, detail = m.evaluate_port(doc, 443)
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


# ── run_check / main ────────────────────────────────────────────────────────


def test_run_check_indeterminate_when_nft_unavailable(m, monkeypatch) -> None:
    def _boom(*a: Any, **k: Any) -> None:
        raise FileNotFoundError("nft")

    monkeypatch.setattr(m.subprocess, "run", _boom)
    result = m.run_check()
    assert result["status"] == "indeterminate"
    assert "could not read ruleset" in result["detail"]


def test_main_writes_state_and_exits_by_verdict(m, monkeypatch, tmp_path) -> None:
    state = tmp_path / "webui-selfcheck.json"
    monkeypatch.setattr(m, "STATE_PATH", state)

    class _Proc:
        stdout = json.dumps(_doc(_rule(_dport_match({"set": [80, 443]}), {"accept": None})))

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _Proc())
    assert m.main() == 0
    saved = json.loads(state.read_text())
    assert saved["status"] == "open"
    assert saved["ports"]["443"]["status"] == "open"
    assert isinstance(saved["checked_at"], int)

    class _ProcBlocked:
        stdout = json.dumps(_doc())

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _ProcBlocked())
    assert m.main() == 1
    assert json.loads(state.read_text())["status"] == "blocked"

    def _boom(*a: Any, **k: Any) -> None:
        raise FileNotFoundError("nft")

    monkeypatch.setattr(m.subprocess, "run", _boom)
    assert m.main() == 2
    assert json.loads(state.read_text())["status"] == "indeterminate"
