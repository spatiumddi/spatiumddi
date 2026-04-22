"""Tests for the BIND9 metrics poller.

Covers XML parsing + delta / counter-reset behavior. The HTTP path
is excluded (same pattern as the DHCP metrics tests).
"""

from __future__ import annotations

from pathlib import Path

from spatium_dns_agent.config import AgentConfig
from spatium_dns_agent.metrics import MetricsPoller, _parse_snapshot


SAMPLE_XML = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<statistics version="3.12.1">
 <server>
  <counters type="opcode">
   <counter name="QUERY">1000</counter>
   <counter name="IQUERY">0</counter>
  </counters>
  <counters type="nsstat">
   <counter name="QryAuthAns">700</counter>
   <counter name="QryNoauthAns">250</counter>
   <counter name="QryNXDOMAIN">30</counter>
   <counter name="QrySERVFAIL">5</counter>
   <counter name="QryRecursion">120</counter>
  </counters>
 </server>
</statistics>
"""


def test_parse_snapshot_extracts_expected_counters():
    out = _parse_snapshot(SAMPLE_XML)
    assert out["queries_total"] == 1000
    assert out["noerror"] == 950  # 700 + 250
    assert out["nxdomain"] == 30
    assert out["servfail"] == 5
    assert out["recursion"] == 120


def test_parse_snapshot_handles_malformed_xml():
    assert _parse_snapshot(b"<not-xml") == {}


def _poller(tmp_path: Path) -> MetricsPoller:
    cfg = AgentConfig(
        control_plane_url="http://api.invalid",
        dns_agent_key="unused",
        server_name="dns-test",
        driver="bind9",
        roles=["authoritative"],
        group_name=None,
        tls_ca_path=None,
        insecure_skip_tls_verify=True,
        state_dir=tmp_path,
    )
    return MetricsPoller(cfg, token_ref=["unused"])


def test_first_tick_no_delta(tmp_path):
    p = _poller(tmp_path)
    assert p._compute_delta(_parse_snapshot(SAMPLE_XML)) is None


def test_second_tick_emits_delta(tmp_path):
    p = _poller(tmp_path)
    p._compute_delta(_parse_snapshot(SAMPLE_XML))
    # Second XML with +50 queries total, +40 noerror, +10 recursion.
    second = SAMPLE_XML.replace(b"1000", b"1050").replace(
        b'QryAuthAns">700', b'QryAuthAns">740'
    ).replace(b'QryRecursion">120', b'QryRecursion">130')
    delta = p._compute_delta(_parse_snapshot(second))
    assert delta is not None
    assert delta["queries_total"] == 50
    assert delta["noerror"] == 40
    assert delta["recursion"] == 10


def test_counter_reset_returns_none(tmp_path):
    p = _poller(tmp_path)
    p._compute_delta(_parse_snapshot(SAMPLE_XML))
    # named restart — everything back to 0.
    reset_xml = SAMPLE_XML.replace(b">1000<", b">0<").replace(
        b">700<", b">0<"
    ).replace(b">250<", b">0<").replace(b">30<", b">0<").replace(
        b">5<", b">0<"
    ).replace(b">120<", b">0<")
    assert p._compute_delta(_parse_snapshot(reset_xml)) is None
