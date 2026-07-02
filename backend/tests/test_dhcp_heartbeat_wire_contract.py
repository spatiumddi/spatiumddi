"""#482 — DHCP agent heartbeat wire contract is hardened.

The DHCP ``AgentHeartbeatRequest`` was all-optional with no ``extra=forbid``,
so a wrong-envelope ACK batch (a typo'd key) validated into an all-default body
(ops_ack=[]), the ACK loop ran zero times, the endpoint returned 200, and the
agent cleared its ACK buffer — losing the ACK. That's the same defect #430 D4
fixed for the DNS agent; the structurally-identical DHCP model never got it.

These pin the forbid behaviour and prove the model stays a strict SUPERSET of
every DHCP agent shape (so forbid is backward-compatible).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1.dhcp.agents import AgentHeartbeatRequest


def test_current_dhcp_agent_heartbeat_body_validates() -> None:
    # The exact shape agent/dhcp/spatium_dhcp_agent/heartbeat.py ships today,
    # including the pid / status / lease_count_since_start telemetry the
    # handler doesn't read but forbid must still accept.
    body = {
        "agent_version": "1.2.3",
        "pid": 42,
        "status": "ok",
        "daemon": {"status": "ok"},
        "lease_count_since_start": 7,
        "ops_ack": [{"op_id": "x", "status": "applied"}],
    }
    m = AgentHeartbeatRequest.model_validate(body)
    assert m.agent_version == "1.2.3"
    assert m.pid == 42
    assert len(m.ops_ack) == 1


def test_wrong_envelope_is_rejected() -> None:
    # A typo'd ACK key ("ops_acks") used to silently validate into ops_ack=[].
    with pytest.raises(ValidationError):
        AgentHeartbeatRequest.model_validate({"ops_acks": [{"op_id": "x"}]})


def test_legacy_slot_fields_still_accepted() -> None:
    # Pre-Wave-C1 DHCP agents shipped slot / deployment telemetry directly to
    # this endpoint. forbid must NOT 422 them — the model is a deliberate
    # superset.
    body = {
        "agent_version": "old",
        "deployment_kind": "appliance",
        "current_slot": "A",
        "is_trial_boot": False,
        "last_upgrade_state": "ready",
    }
    AgentHeartbeatRequest.model_validate(body)


def test_ops_ack_over_cap_rejected() -> None:
    # The ACK list is bounded so a malformed / hostile heartbeat can't pin memory.
    with pytest.raises(ValidationError):
        AgentHeartbeatRequest.model_validate({"ops_ack": [{} for _ in range(5001)]})
