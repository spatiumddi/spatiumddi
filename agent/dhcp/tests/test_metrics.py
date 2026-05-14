"""Tests for the Kea metrics poller.

Covers the three interesting behaviors:
  • first tick after startup establishes a baseline, doesn't report;
  • second tick with a positive delta gets reported upstream;
  • counter-reset (Kea restart) is detected and the bucket is dropped.

We test ``_parse_snapshot`` + ``_compute_delta`` directly so the HTTP
client stays out of the test path.
"""

from __future__ import annotations

from spatium_dhcp_agent.config import AgentConfig
from spatium_dhcp_agent.metrics import MetricsPoller, _parse_snapshot


def _snapshot(values: dict[str, int]) -> dict:
    """Build a fake ``statistic-get-all`` response."""
    args: dict = {}
    for stat, v in values.items():
        args[stat] = [[v, "2026-04-22 09:00:00.000"]]
    return {"arguments": args}


def test_parse_snapshot_picks_expected_columns():
    resp = _snapshot(
        {
            "pkt4-discover-received": 10,
            "pkt4-offer-sent": 9,
            "pkt4-request-received": 9,
            "pkt4-ack-sent": 8,
            "pkt4-nak-sent": 1,
            "pkt4-decline-received": 0,
            "pkt4-release-received": 2,
            "pkt4-inform-received": 0,
        }
    )
    assert _parse_snapshot(resp) == {
        "discover": 10,
        "offer": 9,
        "request": 9,
        "ack": 8,
        "nak": 1,
        "decline": 0,
        "release": 2,
        "inform": 0,
    }


def test_parse_snapshot_missing_counter_defaults_zero():
    # Fresh Kea that hasn't seen traffic yet won't have every counter
    # in its response. We must still produce a full row.
    resp = _snapshot({"pkt4-ack-sent": 5})
    out = _parse_snapshot(resp)
    assert out["ack"] == 5
    assert out["discover"] == 0
    assert out["request"] == 0


def _poller(tmp_path) -> MetricsPoller:
    cfg = AgentConfig(
        control_plane_url="http://api.invalid",
        agent_key="unused",
        bootstrap_pairing_code="",
        server_name="dhcp-test",
        state_dir=tmp_path,
        kea_config_path=tmp_path / "kea.conf",
        kea_control_socket=tmp_path / "sock",
        kea_lease_file=tmp_path / "leases.csv",
        group_name=None,
        roles=["primary"],
        tls_ca_path=None,
        insecure_skip_tls_verify=True,
    )
    return MetricsPoller(cfg, token_ref=["unused"])


def test_first_tick_establishes_baseline(tmp_path):
    p = _poller(tmp_path)
    # First snapshot — no previous baseline, poller should not emit.
    delta = p._compute_delta(
        {
            "discover": 5,
            "offer": 5,
            "request": 5,
            "ack": 5,
            "nak": 0,
            "decline": 0,
            "release": 0,
            "inform": 0,
        }
    )
    assert delta is None


def test_second_tick_emits_delta(tmp_path):
    p = _poller(tmp_path)
    p._compute_delta(
        {
            "discover": 5,
            "offer": 5,
            "request": 5,
            "ack": 5,
            "nak": 0,
            "decline": 0,
            "release": 0,
            "inform": 0,
        }
    )
    delta = p._compute_delta(
        {
            "discover": 8,
            "offer": 8,
            "request": 8,
            "ack": 8,
            "nak": 1,
            "decline": 0,
            "release": 1,
            "inform": 0,
        }
    )
    assert delta == {
        "discover": 3,
        "offer": 3,
        "request": 3,
        "ack": 3,
        "nak": 1,
        "decline": 0,
        "release": 1,
        "inform": 0,
    }


def test_counter_reset_drops_bucket(tmp_path):
    p = _poller(tmp_path)
    p._compute_delta(
        {
            "discover": 100,
            "offer": 100,
            "request": 100,
            "ack": 100,
            "nak": 0,
            "decline": 0,
            "release": 0,
            "inform": 0,
        }
    )
    # Kea restart: counters back to zero.
    delta = p._compute_delta(
        {
            "discover": 0,
            "offer": 0,
            "request": 0,
            "ack": 0,
            "nak": 0,
            "decline": 0,
            "release": 0,
            "inform": 0,
        }
    )
    assert delta is None
    # Next normal tick produces a delta against the restart baseline.
    delta2 = p._compute_delta(
        {
            "discover": 2,
            "offer": 2,
            "request": 2,
            "ack": 2,
            "nak": 0,
            "decline": 0,
            "release": 0,
            "inform": 0,
        }
    )
    assert delta2 == {
        "discover": 2,
        "offer": 2,
        "request": 2,
        "ack": 2,
        "nak": 0,
        "decline": 0,
        "release": 0,
        "inform": 0,
    }
