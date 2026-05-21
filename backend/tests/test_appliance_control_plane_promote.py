"""Control-plane promote/demote validation (#272 Phase 7).

Covers the batch-to-odd-target even-node guard, seed resolution, the
join-coordinate stamping on promote, and the leave stamping on demote.
The actual k3s reconfigure is the supervisor's host-side runner
(Phase 7b) — these exercise the backend contract / validation only.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_str
from app.core.security import create_access_token, hash_password
from app.models.appliance import (
    APPLIANCE_STATE_APPROVED,
    CLUSTER_ROLE_MEMBER,
    CLUSTER_ROLE_PRIMARY,
    Appliance,
)
from app.models.auth import User

pytestmark = pytest.mark.asyncio


async def _admin(db: AsyncSession, *, superadmin: bool = True, username: str = "cpadmin") -> str:
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


async def _seed(db: AsyncSession) -> Appliance:
    return await _appliance(
        db,
        "seed",
        appliance_variant="control-plane",
        cluster_role=CLUSTER_ROLE_PRIMARY,
        last_seen_ip="10.42.0.6",  # supervisor POD IP — must NOT be used
        node_ip="10.0.0.1",  # real routable node IP — the join target
        k3s_join_token_encrypted=encrypt_str("K10::servertoken"),
    )


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── promote ──────────────────────────────────────────────────────────


async def test_promote_to_three_ok(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _admin(db_session)
    await _seed(db_session)
    a = await _appliance(db_session, "app1", appliance_variant="appliance")
    b = await _appliance(db_session, "app2", appliance_variant="appliance")
    await db_session.commit()

    resp = await client.post(
        "/api/v1/appliance/fleet/control-plane/promote",
        json={"appliance_ids": [str(a.id), str(b.id)]},
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["appliances"]) == 2

    await db_session.refresh(a)
    assert a.desired_cluster_role == "member"
    assert a.desired_k3s_server_url == "https://10.0.0.1:6443"
    assert a.cluster_join_state == "joining"
    # The (sensitive) token is stamped encrypted, not in the row plaintext.
    assert a.desired_k3s_join_token_encrypted is not None


async def test_promote_even_target_refused(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _admin(db_session)
    await _seed(db_session)
    a = await _appliance(db_session, "app1", appliance_variant="appliance")
    await db_session.commit()

    # 1 seed + 1 promote = 2 → even → refused.
    resp = await client.post(
        "/api/v1/appliance/fleet/control-plane/promote",
        json={"appliance_ids": [str(a.id)]},
        headers=_hdr(token),
    )
    assert resp.status_code == 422
    assert "ODD" in resp.text


async def test_promote_requires_seed_token(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _admin(db_session)
    # Seed without a reported join token.
    await _appliance(
        db_session,
        "seed",
        appliance_variant="control-plane",
        cluster_role=CLUSTER_ROLE_PRIMARY,
        last_seen_ip="10.0.0.1",
    )
    a = await _appliance(db_session, "app1", appliance_variant="appliance")
    b = await _appliance(db_session, "app2", appliance_variant="appliance")
    await db_session.commit()

    resp = await client.post(
        "/api/v1/appliance/fleet/control-plane/promote",
        json={"appliance_ids": [str(a.id), str(b.id)]},
        headers=_hdr(token),
    )
    assert resp.status_code == 409
    assert "join token" in resp.text


async def test_promote_requires_seed_node_ip(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin(db_session)
    # Seed reported a token but only a pod IP (no node_ip) — the join URL
    # can't be built from a pod IP, so promote must refuse.
    await _appliance(
        db_session,
        "seed",
        appliance_variant="control-plane",
        cluster_role=CLUSTER_ROLE_PRIMARY,
        last_seen_ip="10.42.0.6",
        k3s_join_token_encrypted=encrypt_str("K10::servertoken"),
    )
    a = await _appliance(db_session, "app1", appliance_variant="appliance")
    b = await _appliance(db_session, "app2", appliance_variant="appliance")
    await db_session.commit()

    resp = await client.post(
        "/api/v1/appliance/fleet/control-plane/promote",
        json={"appliance_ids": [str(a.id), str(b.id)]},
        headers=_hdr(token),
    )
    assert resp.status_code == 409
    assert "node IP" in resp.text


async def test_promote_designates_lone_seed(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _admin(db_session)
    # Single control-plane node with no cluster_role yet → designated.
    seed = await _appliance(
        db_session,
        "seed",
        appliance_variant="control-plane",
        last_seen_ip="10.42.0.7",
        node_ip="10.0.0.9",
        k3s_join_token_encrypted=encrypt_str("K10::tok"),
    )
    a = await _appliance(db_session, "app1", appliance_variant="appliance")
    b = await _appliance(db_session, "app2", appliance_variant="appliance")
    await db_session.commit()

    resp = await client.post(
        "/api/v1/appliance/fleet/control-plane/promote",
        json={"appliance_ids": [str(a.id), str(b.id)]},
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(seed)
    assert seed.cluster_role == CLUSTER_ROLE_PRIMARY


async def test_promote_non_superadmin_403(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _admin(db_session, superadmin=False, username="regular")
    await _seed(db_session)
    a = await _appliance(db_session, "app1", appliance_variant="appliance")
    await db_session.commit()
    resp = await client.post(
        "/api/v1/appliance/fleet/control-plane/promote",
        json={"appliance_ids": [str(a.id)]},
        headers=_hdr(token),
    )
    assert resp.status_code == 403


# ── demote ───────────────────────────────────────────────────────────


async def test_demote_to_one_ok(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _admin(db_session)
    await _seed(db_session)
    m1 = await _appliance(db_session, "m1", cluster_role=CLUSTER_ROLE_MEMBER)
    m2 = await _appliance(db_session, "m2", cluster_role=CLUSTER_ROLE_MEMBER)
    await db_session.commit()

    # 3 members - 2 = 1 → odd → ok.
    resp = await client.post(
        "/api/v1/appliance/fleet/control-plane/demote",
        json={"appliance_ids": [str(m1.id), str(m2.id)]},
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(m1)
    assert m1.desired_cluster_role == "none"
    assert m1.cluster_join_state == "leaving"


async def test_demote_even_remaining_refused(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _admin(db_session)
    await _seed(db_session)
    m1 = await _appliance(db_session, "m1", cluster_role=CLUSTER_ROLE_MEMBER)
    await _appliance(db_session, "m2", cluster_role=CLUSTER_ROLE_MEMBER)
    await db_session.commit()

    # 3 members - 1 = 2 → even → refused.
    resp = await client.post(
        "/api/v1/appliance/fleet/control-plane/demote",
        json={"appliance_ids": [str(m1.id)]},
        headers=_hdr(token),
    )
    assert resp.status_code == 422
    assert "ODD" in resp.text


async def test_demote_refuses_primary(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _admin(db_session)
    seed = await _seed(db_session)
    await _appliance(db_session, "m1", cluster_role=CLUSTER_ROLE_MEMBER)
    await _appliance(db_session, "m2", cluster_role=CLUSTER_ROLE_MEMBER)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/appliance/fleet/control-plane/demote",
        json={"appliance_ids": [str(seed.id)]},
        headers=_hdr(token),
    )
    assert resp.status_code == 422
    assert "seed" in resp.text
