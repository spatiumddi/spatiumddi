"""BIND9 agent RRL + amplification rendering (issue #146).

Exercises the pure ``_render_rate_limit_block`` helper against the
``options`` dict shape ``app.services.dns.agent_config`` ships, asserting
the ``rate-limit { … }`` stanza + amplification directives appear only
when opted in (so existing groups render byte-identical named.conf).
"""

from __future__ import annotations

from spatium_dns_agent.drivers.bind9 import _render_rate_limit_block


def test_default_is_noop() -> None:
    assert _render_rate_limit_block({}) == ""
    assert _render_rate_limit_block({"rrl_enabled": False, "minimal_responses": False}) == ""


def test_rrl_full_block() -> None:
    out = _render_rate_limit_block(
        {
            "rrl_enabled": True,
            "rrl_responses_per_second": 20,
            "rrl_window": 10,
            "rrl_slip": 3,
            "rrl_qps_scale": 250,
            "rrl_exempt_clients": ["10.0.0.0/8", "  ", "192.168.0.0/16"],
            "rrl_log_only": True,
        }
    )
    assert "rate-limit {" in out
    assert "responses-per-second 20;" in out
    assert "window 10;" in out
    assert "slip 3;" in out
    assert "qps-scale 250;" in out
    # blank exempt entry dropped; valid ones joined
    assert "exempt-clients { 10.0.0.0/8; 192.168.0.0/16; };" in out
    assert "log-only yes;" in out


def test_rrl_optional_fields_omitted() -> None:
    out = _render_rate_limit_block(
        {
            "rrl_enabled": True,
            "rrl_responses_per_second": 15,
            "rrl_window": 15,
            "rrl_slip": 2,
        }
    )
    assert "rate-limit {" in out
    assert "qps-scale" not in out
    assert "exempt-clients" not in out
    assert "log-only" not in out


def test_amplification_without_rrl() -> None:
    out = _render_rate_limit_block(
        {
            "minimal_responses": True,
            "tcp_clients": 200,
            "clients_per_query": 12,
            "max_clients_per_query": 120,
        }
    )
    assert "minimal-responses yes;" in out
    assert "tcp-clients 200;" in out
    assert "clients-per-query 12;" in out
    assert "max-clients-per-query 120;" in out
    # no RRL stanza when rrl_enabled is absent/false
    assert "rate-limit {" not in out


def test_partial_amplification_omits_unset() -> None:
    out = _render_rate_limit_block({"tcp_clients": 150})
    assert "tcp-clients 150;" in out
    assert "minimal-responses" not in out
    assert "clients-per-query" not in out
