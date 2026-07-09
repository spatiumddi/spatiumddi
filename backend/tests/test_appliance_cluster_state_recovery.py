"""Stuck cluster-transition recovery (#590).

A 3-node HA cluster survived a hard node kill at every layer that was
supposed to (k3s, etcd, CNPG) but the dead-node REPLACE flow could not
restore it: the replacement sat in ``cluster_join_state="joining"`` for
50+ minutes while the dead member sat ``"evicting"``, because every
cluster transition converges only on a supervisor report and none of them
has a timeout or a failure ceiling.

Two backend behaviours close that:

1. A reported ``failed`` clears the promote/demote desired-state. Before,
   only ``ready``/``left`` cleared it — so the supervisor kept re-firing a
   DESTRUCTIVE k3s wipe-and-rejoin on every heartbeat, and the row read
   "joining" forever instead of surfacing a clean failure.
2. ``POST .../clear-cluster-state`` is the operator escape hatch for a
   transition whose reporter is never coming back (dead node mid-join,
   seed that can't reach the kubeapi).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.appliance import (
    APPLIANCE_STATE_APPROVED,
    CLUSTER_JOIN_STATE_EVICTING,
    CLUSTER_JOIN_STATE_FAILED,
    CLUSTER_JOIN_STATE_JOINING,
    CLUSTER_JOIN_STATE_READY,
    CLUSTER_ROLE_MEMBER,
    DESIRED_CLUSTER_ROLE_MEMBER,
    DESIRED_CLUSTER_ROLE_NONE,
    Appliance,
)
from app.models.auth import User

pytestmark = pytest.mark.asyncio


async def _admin(db: AsyncSession, *, superadmin: bool = True, username: str = "csadmin") -> str:
    user = User(
        username=username,
        email=f"{username}@example.com",
        display_name=username,
        hashed_password=hash_password("password123"),
        auth_source="local",
        is_superadmin=superadmin,
    )
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


async def _appliance(db: AsyncSession, hostname: str, **kw: object) -> Appliance:
    der = os.urandom(32)
    row = Appliance(
        id=uuid.uuid4(),
        hostname=hostname,
        public_key_der=der,
        public_key_fingerprint=hashlib.sha256(der).hexdigest(),
        state=APPLIANCE_STATE_APPROVED,
        deployment_kind="appliance",
        **kw,  # type: ignore[arg-type]
    )
    db.add(row)
    await db.flush()
    return row


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _url(row: Appliance) -> str:
    return f"/api/v1/appliance/fleet/control-plane/{row.id}/clear-cluster-state"


# ── operator escape hatch ────────────────────────────────────────────


async def test_clear_unsticks_a_wedged_joiner(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin(db_session)
    row = await _appliance(
        db_session,
        "wedged",
        cluster_join_state=CLUSTER_JOIN_STATE_JOINING,
        desired_cluster_role=DESIRED_CLUSTER_ROLE_MEMBER,
        desired_k3s_server_url="https://10.0.0.1:6443",
    )
    await db_session.commit()

    resp = await client.post(_url(row), headers=_hdr(token))
    assert resp.status_code == 200, resp.text

    await db_session.refresh(row)
    assert row.cluster_join_state is None
    assert row.desired_cluster_role is None
    assert row.desired_k3s_server_url is None
    assert row.desired_k3s_join_token_encrypted is None
    assert row.evict_requested is False


async def test_clear_refuses_a_young_transition(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """A k3s join legitimately takes minutes. Clearing a RUNNING join blanks
    the desired-state out from under it, and the joiner then comes up as a
    live control-plane member that cp-size scaling, MetalLB and quorum math
    all undercount. So an impatient click seconds in must be refused."""
    token = await _admin(db_session)
    row = await _appliance(
        db_session,
        "joining-now",
        cluster_join_state=CLUSTER_JOIN_STATE_JOINING,
        desired_cluster_role=DESIRED_CLUSTER_ROLE_MEMBER,
        cluster_join_state_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    await db_session.commit()

    resp = await client.post(_url(row), headers=_hdr(token))
    assert resp.status_code == 409
    assert "legitimately takes minutes" in resp.text

    await db_session.refresh(row)
    assert row.desired_cluster_role == DESIRED_CLUSTER_ROLE_MEMBER
    assert row.cluster_join_state == CLUSTER_JOIN_STATE_JOINING


async def test_clear_allows_a_stale_transition(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin(db_session)
    row = await _appliance(
        db_session,
        "joining-forever",
        cluster_join_state=CLUSTER_JOIN_STATE_JOINING,
        desired_cluster_role=DESIRED_CLUSTER_ROLE_MEMBER,
        cluster_join_state_at=datetime.now(UTC) - timedelta(minutes=45),
    )
    await db_session.commit()

    resp = await client.post(_url(row), headers=_hdr(token))
    assert resp.status_code == 200, resp.text
    await db_session.refresh(row)
    assert row.cluster_join_state is None
    assert row.cluster_join_state_at is None


async def test_clear_force_overrides_the_age_guard(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """For the operator who KNOWS the node is never coming back."""
    token = await _admin(db_session)
    row = await _appliance(
        db_session,
        "definitely-dead",
        cluster_join_state=CLUSTER_JOIN_STATE_JOINING,
        desired_cluster_role=DESIRED_CLUSTER_ROLE_MEMBER,
        cluster_join_state_at=datetime.now(UTC),
    )
    await db_session.commit()

    assert (await client.post(_url(row), headers=_hdr(token))).status_code == 409
    resp = await client.post(_url(row), headers=_hdr(token), json={"force": True})
    assert resp.status_code == 200, resp.text
    await db_session.refresh(row)
    assert row.cluster_join_state is None


async def test_clear_allows_when_the_age_is_unknown(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Rows written before the cluster_join_state_at column existed can't be
    proven young — the guard must not lock them out of the escape hatch."""
    token = await _admin(db_session)
    row = await _appliance(
        db_session,
        "legacy",
        cluster_join_state=CLUSTER_JOIN_STATE_JOINING,
        cluster_join_state_at=None,
    )
    await db_session.commit()

    resp = await client.post(_url(row), headers=_hdr(token))
    assert resp.status_code == 200, resp.text


async def test_clear_unsticks_a_wedged_eviction(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The dead member the seed never managed to evict."""
    token = await _admin(db_session)
    row = await _appliance(
        db_session,
        "dead",
        cluster_join_state=CLUSTER_JOIN_STATE_EVICTING,
        evict_requested=True,
    )
    await db_session.commit()

    resp = await client.post(_url(row), headers=_hdr(token))
    assert resp.status_code == 200, resp.text

    await db_session.refresh(row)
    assert row.evict_requested is False
    assert row.cluster_join_state is None


async def test_clear_leaves_actual_membership_alone(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """This clears BOOKKEEPING, never the node. A member whose demote
    wedged is still a member — clearing must not silently drop it from
    the cluster accounting."""
    token = await _admin(db_session)
    row = await _appliance(
        db_session,
        "member1",
        cluster_role=CLUSTER_ROLE_MEMBER,
        cluster_join_state="leaving",
        desired_cluster_role=DESIRED_CLUSTER_ROLE_NONE,
    )
    await db_session.commit()

    resp = await client.post(_url(row), headers=_hdr(token))
    assert resp.status_code == 200, resp.text

    await db_session.refresh(row)
    assert row.cluster_role == CLUSTER_ROLE_MEMBER
    assert row.desired_cluster_role is None


async def test_clear_refuses_a_settled_row(db_session: AsyncSession, client: AsyncClient) -> None:
    """Nothing stuck → 409. Blanking a settled row would only destroy the
    trail of how it got there."""
    token = await _admin(db_session)
    row = await _appliance(
        db_session,
        "settled",
        cluster_role=CLUSTER_ROLE_MEMBER,
        cluster_join_state=CLUSTER_JOIN_STATE_READY,
    )
    await db_session.commit()

    resp = await client.post(_url(row), headers=_hdr(token))
    assert resp.status_code == 409
    assert "no in-flight cluster transition" in resp.text


async def test_clear_refuses_a_settled_failure(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """``failed`` is terminal and self-explanatory — the operator re-promotes
    rather than clearing it."""
    token = await _admin(db_session)
    row = await _appliance(db_session, "failed1", cluster_join_state=CLUSTER_JOIN_STATE_FAILED)
    await db_session.commit()

    resp = await client.post(_url(row), headers=_hdr(token))
    assert resp.status_code == 409


async def test_clear_404_on_unknown_appliance(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin(db_session)
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/appliance/fleet/control-plane/{uuid.uuid4()}/clear-cluster-state",
        headers=_hdr(token),
    )
    assert resp.status_code == 404


async def test_clear_requires_superadmin(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _admin(db_session, superadmin=False, username="plain_cs")
    row = await _appliance(db_session, "wedged2", cluster_join_state=CLUSTER_JOIN_STATE_JOINING)
    await db_session.commit()

    resp = await client.post(_url(row), headers=_hdr(token))
    assert resp.status_code == 403
    await db_session.refresh(row)
    assert row.cluster_join_state == CLUSTER_JOIN_STATE_JOINING


async def test_clear_writes_an_audit_row(db_session: AsyncSession, client: AsyncClient) -> None:
    from sqlalchemy import select

    from app.models.audit import AuditLog

    token = await _admin(db_session)
    row = await _appliance(
        db_session,
        "audited",
        cluster_join_state=CLUSTER_JOIN_STATE_EVICTING,
        evict_requested=True,
    )
    await db_session.commit()

    assert (await client.post(_url(row), headers=_hdr(token))).status_code == 200

    entry = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "appliance.control_plane_state_cleared")
        )
    ).scalar_one()
    assert entry.resource_id == str(row.id)
    # The pre-clear state is what an auditor needs; it's gone from the row.
    assert entry.old_value is not None
    assert entry.old_value["cluster_join_state"] == CLUSTER_JOIN_STATE_EVICTING
    assert entry.old_value["evict_requested"] is True
