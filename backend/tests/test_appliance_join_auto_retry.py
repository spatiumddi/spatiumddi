"""A transient control-plane join failure is retried, a permanent one is not.

The 3-node formations of the appliance sizing campaign (2026-09-02/03) lost
one member in three: two members promoted together race each other into the
seed's etcd, the loser dies with "could not reach the seed" / a readiness
timeout, and the backend's clear-on-failed (#590) turned that flake into an
operator round-trip — the row read "failed", the desired-state was gone, and
nothing retried until somebody re-promoted by hand.

Now a reported ``failed`` keeps the desired-state — so the supervisor
re-fires the join on its next heartbeat, bounded by its own per-target
attempt ceiling — unless the runner's classifier says no retry can fix it
(a stale etcd member under this hostname, a bootstrap-token mismatch) or the
latest failure is older than the auto-retry window.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.appliance import supervisor as sup
from app.models.appliance import (
    APPLIANCE_STATE_APPROVED,
    CLUSTER_JOIN_STATE_FAILED,
    CLUSTER_JOIN_STATE_JOINING,
    DESIRED_CLUSTER_ROLE_MEMBER,
    Appliance,
)
from app.models.settings import PlatformSettings
from app.services.appliance.ca import generate_session_token

_TRANSIENT = "could not reach the seed — check the firewall and tcp/6443 + tcp/2379-2380"
_PERMANENT = (
    "this hostname is already an etcd member of the target cluster — evict it "
    "(Fleet → Replace, or delete its k8s Node) before re-joining"
)


async def _joiner(
    db: AsyncSession, *, failed_since: timedelta | None = None
) -> tuple[Appliance, str]:
    settings_row = await db.get(PlatformSettings, 1)
    if settings_row is None:
        settings_row = PlatformSettings(id=1)
        db.add(settings_row)
    settings_row.supervisor_registration_enabled = True
    token, token_hash = generate_session_token()
    row = Appliance(
        id=uuid.uuid4(),
        hostname=f"m-{uuid.uuid4().hex[:6]}",
        state=APPLIANCE_STATE_APPROVED,
        public_key_der=b"fake-key",
        public_key_fingerprint="ee" * 32,
        cert_serial="0001",
        deployment_kind="appliance",
        appliance_variant="appliance",
        desired_cluster_role=DESIRED_CLUSTER_ROLE_MEMBER,
        desired_k3s_server_url="https://10.0.0.1:6443",
        desired_k3s_join_token_encrypted=b"enc",
        cluster_join_state=CLUSTER_JOIN_STATE_JOINING,
        cluster_join_state_at=datetime.now(UTC) - (failed_since or timedelta(0)),
        session_token_hash=token_hash,
    )
    db.add(row)
    await db.flush()
    return row, token


async def _report_failed(client: AsyncClient, row: Appliance, token: str, reason: str) -> None:
    resp = await client.post(
        "/api/v1/appliance/supervisor/heartbeat",
        json={
            "appliance_id": str(row.id),
            "session_token": token,
            "cluster_join_state": CLUSTER_JOIN_STATE_FAILED,
            "cluster_join_reason": reason,
        },
    )
    assert resp.status_code == 200, resp.text


# ── the pure helpers ────────────────────────────────────────────────────────


def test_the_classifier_names_the_two_permanent_failures() -> None:
    assert sup._join_failure_is_permanent(_PERMANENT)
    assert sup._join_failure_is_permanent(
        "bootstrap data already found and encrypted with a different token"
    )
    assert not sup._join_failure_is_permanent(_TRANSIENT)
    assert not sup._join_failure_is_permanent("k3s did not come Ready within 180s")
    assert not sup._join_failure_is_permanent(None)


def test_the_retry_window_is_measured_from_the_latest_failure() -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    assert not sup._join_retry_window_elapsed(now - timedelta(minutes=3), now)
    assert sup._join_retry_window_elapsed(now - timedelta(minutes=16), now)
    assert sup._join_retry_window_elapsed(None, now)  # unknown clock: never retry blind
    # A naive stamp is read as UTC rather than blowing up the heartbeat.
    assert not sup._join_retry_window_elapsed(datetime(2026, 9, 3, 11, 58), now)


# ── the heartbeat path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_transient_failure_keeps_the_desired_state_for_a_retry(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row, token = await _joiner(db_session)
    await db_session.commit()

    await _report_failed(client, row, token, _TRANSIENT)

    await db_session.refresh(row)
    assert row.cluster_join_state == CLUSTER_JOIN_STATE_FAILED
    assert row.cluster_join_reason == _TRANSIENT
    # Still asked to join: the supervisor re-fires on its next heartbeat.
    assert row.desired_cluster_role == DESIRED_CLUSTER_ROLE_MEMBER
    assert row.desired_k3s_server_url == "https://10.0.0.1:6443"
    assert row.desired_k3s_join_token_encrypted == b"enc"


@pytest.mark.asyncio
async def test_a_permanent_failure_clears_the_desired_state_at_once(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row, token = await _joiner(db_session)
    await db_session.commit()

    await _report_failed(client, row, token, _PERMANENT)

    await db_session.refresh(row)
    assert row.cluster_join_state == CLUSTER_JOIN_STATE_FAILED
    assert row.desired_cluster_role is None
    assert row.desired_k3s_server_url is None
    assert row.desired_k3s_join_token_encrypted is None
    # The reason survives so the Fleet UI can say WHY (and what to do).
    assert "evict" in (row.cluster_join_reason or "")


@pytest.mark.asyncio
async def test_a_transient_failure_older_than_the_window_clears(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # The row has already been `failed` for longer than the window (the
    # supervisor's own attempt ceiling stopped the re-fires): give up the
    # way #590 does, and let the operator re-promote.
    row, token = await _joiner(db_session, failed_since=timedelta(minutes=20))
    row.cluster_join_state = CLUSTER_JOIN_STATE_FAILED
    await db_session.commit()

    await _report_failed(client, row, token, _TRANSIENT)

    await db_session.refresh(row)
    assert row.desired_cluster_role is None
    assert row.desired_k3s_server_url is None
