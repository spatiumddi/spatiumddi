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


async def test_promote_requires_seed_node_ip(db_session: AsyncSession, client: AsyncClient) -> None:
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


# ── dead-node replacement (Phase 9) ──────────────────────────────────


async def test_replace_member_evicts_and_mints_code(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin(db_session)
    await _seed(db_session)
    m1 = await _appliance(db_session, "m1", cluster_role=CLUSTER_ROLE_MEMBER, node_ip="10.0.0.2")
    await _appliance(db_session, "m2", cluster_role=CLUSTER_ROLE_MEMBER, node_ip="10.0.0.3")
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/appliance/fleet/control-plane/{m1.id}/replace",
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pairing_code"] and len(body["pairing_code"]) == 8
    assert body["evicted"]["hostname"] == "m1"

    await db_session.refresh(m1)
    assert m1.cluster_role is None
    assert m1.evict_requested is True
    assert m1.cluster_join_state == "evicting"


async def test_replace_refuses_primary(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _admin(db_session)
    seed = await _seed(db_session)
    await _appliance(db_session, "m1", cluster_role=CLUSTER_ROLE_MEMBER)
    await _appliance(db_session, "m2", cluster_role=CLUSTER_ROLE_MEMBER)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/appliance/fleet/control-plane/{seed.id}/replace",
        headers=_hdr(token),
    )
    assert resp.status_code == 422
    assert "seed" in resp.text


async def test_replace_refuses_non_member(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _admin(db_session)
    await _seed(db_session)
    # An approved appliance that never joined the control plane.
    plain = await _appliance(db_session, "agent1", appliance_variant="appliance")
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/appliance/fleet/control-plane/{plain.id}/replace",
        headers=_hdr(token),
    )
    assert resp.status_code == 409
    assert "member" in resp.text


# ── MetalLB control-plane VIP config (Phase 7c) ──────────────────────

_METALLB_URL = "/api/v1/appliance/fleet/control-plane/metallb"


async def test_metallb_get_default(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _admin(db_session)
    await db_session.commit()
    resp = await client.get(_METALLB_URL, headers=_hdr(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False
    assert body["pool_addresses"] == []
    assert body["control_plane_vip"] == ""
    # #272 — live-status fields default to false/0 when kubeapi is
    # unavailable (no ServiceAccount mounted under pytest).
    assert body["controller_ready"] is False
    assert body["speakers_ready"] == 0
    assert body["speakers_total"] == 0


async def test_metallb_put_and_get(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _admin(db_session)
    await db_session.commit()
    resp = await client.put(
        _METALLB_URL,
        json={
            "enabled": True,
            "pool_addresses": ["192.168.0.240/29"],
            "control_plane_vip": "192.168.0.241",
        },
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["control_plane_vip"] == "192.168.0.241"
    # Round-trips through the GET (config fields; status fields default
    # to false/0 in-test and are asserted in test_metallb_get_default).
    got = await client.get(_METALLB_URL, headers=_hdr(token))
    body = got.json()
    assert body["enabled"] is True
    assert body["pool_addresses"] == ["192.168.0.240/29"]
    assert body["control_plane_vip"] == "192.168.0.241"


async def test_metallb_vip_outside_pool_refused(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin(db_session)
    await db_session.commit()
    resp = await client.put(
        _METALLB_URL,
        json={
            "enabled": True,
            "pool_addresses": ["192.168.0.240/29"],
            "control_plane_vip": "10.0.0.5",
        },
        headers=_hdr(token),
    )
    assert resp.status_code == 422
    assert "not inside" in resp.text


async def test_metallb_enabled_requires_vip(db_session: AsyncSession, client: AsyncClient) -> None:
    # #272 — enabling MetalLB requires a VIP. The address pool is now
    # OPTIONAL (auto-derived as <vip>/32 when omitted), so the missing-VIP
    # case is what 422s, not a missing pool.
    token = await _admin(db_session)
    await db_session.commit()
    resp = await client.put(
        _METALLB_URL,
        json={"enabled": True, "pool_addresses": [], "control_plane_vip": ""},
        headers=_hdr(token),
    )
    assert resp.status_code == 422
    assert "VIP is required" in resp.text


async def test_metallb_vip_only_autoderives_pool(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    # #272 — enabled + VIP + no pool → succeeds, pool auto-set to <vip>/32.
    # This is the common single-VIP path the operator takes (one field).
    token = await _admin(db_session)
    await db_session.commit()
    resp = await client.put(
        _METALLB_URL,
        json={"enabled": True, "pool_addresses": [], "control_plane_vip": "192.168.0.250"},
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pool_addresses"] == ["192.168.0.250/32"]
    assert body["control_plane_vip"] == "192.168.0.250"


async def test_metallb_invalid_pool_entry(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _admin(db_session)
    await db_session.commit()
    resp = await client.put(
        _METALLB_URL,
        json={"enabled": False, "pool_addresses": ["not-an-ip"], "control_plane_vip": ""},
        headers=_hdr(token),
    )
    assert resp.status_code == 422
    assert "invalid pool entry" in resp.text


async def test_metallb_range_pool_ok(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _admin(db_session)
    await db_session.commit()
    resp = await client.put(
        _METALLB_URL,
        json={
            "enabled": True,
            "pool_addresses": ["192.168.0.240-192.168.0.247"],
            "control_plane_vip": "192.168.0.245",
        },
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text


async def test_metallb_non_superadmin_refused(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin(db_session, superadmin=False, username="plainuser")
    await db_session.commit()
    resp = await client.get(_METALLB_URL, headers=_hdr(token))
    assert resp.status_code == 403
