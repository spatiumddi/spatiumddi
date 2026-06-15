"""#430 (D4 / D8) — DNS agent heartbeat wire contract is hardened.

The DNS ``AgentHeartbeatRequest`` was all-optional with no ``extra=forbid``,
so a wrong-envelope ACK batch (a typo'd key) validated into an all-default
body (ops_ack=[]), the ACK loop ran zero times, the endpoint returned 200,
and the agent cleared its ACK buffer — losing the ACK. These tests pin the
forbid behaviour and prove the model stays a superset of every agent shape
(so forbid is backward-compatible).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1.dns.agents import AgentHeartbeatRequest


def test_current_dns_agent_heartbeat_body_validates() -> None:
    # The exact shape agent/dns/spatium_dns_agent/heartbeat.py ships today.
    body = {
        "agent_version": "1.2.3",
        "daemon": {"status": "ok"},
        "config": {},
        "ops_ack": [{"op_id": "x", "status": "applied"}],
        "failed_ops_count": 0,
    }
    m = AgentHeartbeatRequest.model_validate(body)
    assert m.agent_version == "1.2.3"
    assert len(m.ops_ack) == 1


def test_wrong_envelope_is_rejected() -> None:
    # A typo'd ACK key ("ops_acks") used to silently validate into ops_ack=[].
    with pytest.raises(ValidationError):
        AgentHeartbeatRequest.model_validate({"ops_acks": [{"op_id": "x"}]})


def test_legacy_agent_fields_still_accepted() -> None:
    # Pre-Wave-C1 agents shipped slot/deployment telemetry + zone_serials.
    # forbid must NOT 422 them — the model is a deliberate superset.
    body = {
        "agent_version": "old",
        "zone_serials": {"example.com.": 5},
        "deployment_kind": "appliance",
        "current_slot": "A",
        "is_trial_boot": False,
    }
    AgentHeartbeatRequest.model_validate(body)


def test_ops_ack_over_cap_rejected() -> None:
    # The ACK list is bounded so a malformed/hostile heartbeat can't pin memory.
    with pytest.raises(ValidationError):
        AgentHeartbeatRequest.model_validate({"ops_ack": [{} for _ in range(5001)]})
