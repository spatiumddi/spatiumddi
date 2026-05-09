"""Multicast group registry tests — issue #126 Phase 1.

Covers the registry CRUD surface + the multicast-class address
validation + the feature-module gate. Membership tests do a
direct model insert for the IPAddress prerequisite to avoid
plumbing a full IPSpace → IPBlock → Subnet → IPAddress chain
through the IPAM API in every test.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.multicast import MulticastDomain, MulticastGroup, MulticastMembership


async def _make_admin(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"mc-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Multicast Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_space(db: AsyncSession, name: str | None = None) -> IPSpace:
    space = IPSpace(name=name or f"mc-space-{uuid.uuid4().hex[:8]}")
    db.add(space)
    await db.flush()
    return space


# ── Group CRUD ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_group_v4_inside_multicast_range(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "space_id": str(space.id),
            "address": "239.5.7.42",
            "name": "Cam7 Studio-B HD",
            "application": "SMPTE 2110-20 video",
            "rtp_payload_type": 96,
            "bandwidth_mbps_estimate": "1485.000",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["address"] == "239.5.7.42"
    assert body["application"] == "SMPTE 2110-20 video"
    assert body["rtp_payload_type"] == 96


@pytest.mark.asyncio
async def test_create_group_v6_inside_multicast_range(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "space_id": str(space.id),
            "address": "ff05::1:3",
            "name": "site-local-DHCP-relay",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["address"] == "ff05::1:3"


@pytest.mark.asyncio
async def test_create_group_rejects_unicast_address(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "space_id": str(space.id),
            "address": "10.0.0.5",
            "name": "should-fail",
        },
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert any("224.0.0.0/4" in str(item) for item in detail)


@pytest.mark.asyncio
async def test_create_group_rejects_unknown_space(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "space_id": str(uuid.uuid4()),
            "address": "239.1.2.3",
            "name": "stray",
        },
    )
    assert resp.status_code == 422
    assert "space_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_groups_filters_by_space_and_search(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space_a = await _make_space(db_session, "mc-A")
    space_b = await _make_space(db_session, "mc-B")
    db_session.add_all(
        [
            MulticastGroup(
                space_id=space_a.id, address="239.1.1.1", name="cam1", application="video"
            ),
            MulticastGroup(
                space_id=space_a.id, address="239.1.1.2", name="cam2", application="audio"
            ),
            MulticastGroup(
                space_id=space_b.id, address="239.9.9.9", name="other", application="ndi"
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/multicast/groups?space_id={space_a.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {item["name"] for item in body["items"]} == {"cam1", "cam2"}

    # Substring search also looks at application.
    resp = await client.get(
        f"/api/v1/multicast/groups?space_id={space_a.id}&search=audio",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "cam2"


@pytest.mark.asyncio
async def test_update_and_delete_group(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers=headers,
        json={
            "space_id": str(space.id),
            "address": "239.5.5.5",
            "name": "before",
        },
    )
    group_id = resp.json()["id"]

    resp = await client.put(
        f"/api/v1/multicast/groups/{group_id}",
        headers=headers,
        json={"name": "after", "application": "trade-feed"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "after"
    assert body["application"] == "trade-feed"

    resp = await client.delete(f"/api/v1/multicast/groups/{group_id}", headers=headers)
    assert resp.status_code == 204

    resp = await client.get(f"/api/v1/multicast/groups/{group_id}", headers=headers)
    assert resp.status_code == 404


# ── Ports ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_port_crud_and_range_validation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers=headers,
        json={"space_id": str(space.id), "address": "239.6.6.6", "name": "ports"},
    )
    group_id = resp.json()["id"]

    # Single port (port_end null).
    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/ports",
        headers=headers,
        json={"port_start": 5000, "transport": "rtp"},
    )
    assert resp.status_code == 201
    port_id = resp.json()["id"]

    # Range.
    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/ports",
        headers=headers,
        json={"port_start": 5004, "port_end": 5008, "transport": "rtp"},
    )
    assert resp.status_code == 201

    # port_end < port_start rejected at the schema layer.
    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/ports",
        headers=headers,
        json={"port_start": 6000, "port_end": 5999},
    )
    assert resp.status_code == 422

    # Invalid transport rejected.
    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/ports",
        headers=headers,
        json={"port_start": 7000, "transport": "bogus"},
    )
    assert resp.status_code == 422

    resp = await client.get(f"/api/v1/multicast/groups/{group_id}/ports", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    resp = await client.delete(f"/api/v1/multicast/ports/{port_id}", headers=headers)
    assert resp.status_code == 204


# ── Memberships ───────────────────────────────────────────────────────


async def _make_ip(db: AsyncSession, space: IPSpace, addr: str) -> IPAddress:
    """Build the minimum IPSpace → IPBlock → Subnet → IPAddress chain
    so a membership test can attach a real IP. Cheaper than going
    through the IPAM API for every test."""
    block = IPBlock(space_id=space.id, name="b", network="10.0.0.0/16")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, name="s", network="10.0.0.0/24")
    db.add(subnet)
    await db.flush()
    ip = IPAddress(subnet_id=subnet.id, address=addr, status="allocated")
    db.add(ip)
    await db.flush()
    return ip


@pytest.mark.asyncio
async def test_membership_add_and_unique_triplet(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    ip = await _make_ip(db_session, space, "10.0.0.5")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers=headers,
        json={"space_id": str(space.id), "address": "239.7.7.7", "name": "memb"},
    )
    group_id = resp.json()["id"]

    # First add succeeds.
    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/memberships",
        headers=headers,
        json={"ip_address_id": str(ip.id), "role": "producer"},
    )
    assert resp.status_code == 201, resp.text
    membership_id = resp.json()["id"]

    # Same (group, ip, role) → 409.
    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/memberships",
        headers=headers,
        json={"ip_address_id": str(ip.id), "role": "producer"},
    )
    assert resp.status_code == 409

    # Different role on same (group, ip) → succeeds (RP + producer
    # is a real configuration).
    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/memberships",
        headers=headers,
        json={"ip_address_id": str(ip.id), "role": "rendezvous_point"},
    )
    assert resp.status_code == 201

    resp = await client.get(f"/api/v1/multicast/groups/{group_id}/memberships", headers=headers)
    assert len(resp.json()) == 2

    resp = await client.delete(f"/api/v1/multicast/memberships/{membership_id}", headers=headers)
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_membership_rejects_unknown_ip(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers=headers,
        json={"space_id": str(space.id), "address": "239.8.8.8", "name": "x"},
    )
    group_id = resp.json()["id"]

    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/memberships",
        headers=headers,
        json={"ip_address_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_membership_rejects_invalid_role(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    ip = await _make_ip(db_session, space, "10.0.0.6")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers=headers,
        json={"space_id": str(space.id), "address": "239.4.4.4", "name": "r"},
    )
    group_id = resp.json()["id"]

    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/memberships",
        headers=headers,
        json={"ip_address_id": str(ip.id), "role": "bogus"},
    )
    assert resp.status_code == 422


# ── Bulk allocate ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_allocate_happy_path(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    await db_session.commit()

    body = {
        "space_id": str(space.id),
        "count": 4,
        "name_template": "stream-{n:02d}",
        "start_address": "239.10.0.0",
        "application": "smpte 2110",
    }

    resp = await client.post(
        "/api/v1/multicast/groups/bulk-allocate/preview",
        headers=headers,
        json=body,
    )
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["conflict_count"] == 0
    assert [it["address"] for it in preview["items"]] == [
        "239.10.0.0",
        "239.10.0.1",
        "239.10.0.2",
        "239.10.0.3",
    ]
    assert [it["name"] for it in preview["items"]] == [
        "stream-01",
        "stream-02",
        "stream-03",
        "stream-04",
    ]

    resp = await client.post(
        "/api/v1/multicast/groups/bulk-allocate/commit",
        headers=headers,
        json=body,
    )
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["created"] == 4

    # Listing the space now returns the four created rows.
    resp = await client.get(
        f"/api/v1/multicast/groups?space_id={space.id}",
        headers=headers,
    )
    assert resp.json()["total"] == 4


@pytest.mark.asyncio
async def test_bulk_allocate_preview_surfaces_conflicts(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    # Pre-existing group at 239.20.0.1 — second slot of the run will
    # collide.
    db_session.add(MulticastGroup(space_id=space.id, address="239.20.0.1", name="prior"))
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    body = {
        "space_id": str(space.id),
        "count": 3,
        "name_template": "x-{n}",
        "start_address": "239.20.0.0",
    }

    resp = await client.post(
        "/api/v1/multicast/groups/bulk-allocate/preview",
        headers=headers,
        json=body,
    )
    assert resp.status_code == 200
    preview = resp.json()
    assert preview["conflict_count"] == 1
    flagged = [it for it in preview["items"] if it["conflict"]]
    assert flagged == [{"address": "239.20.0.1", "name": "x-2", "conflict": "in_use"}]

    # Commit refuses while a conflict is in the run.
    resp = await client.post(
        "/api/v1/multicast/groups/bulk-allocate/commit",
        headers=headers,
        json=body,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["conflicts"] == ["239.20.0.1"]


@pytest.mark.asyncio
async def test_bulk_allocate_count_cap_is_enforced(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    await db_session.commit()

    resp = await client.post(
        "/api/v1/multicast/groups/bulk-allocate/preview",
        headers=headers,
        json={
            "space_id": str(space.id),
            "count": 9999,
            "name_template": "x-{n}",
            "start_address": "239.5.0.0",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bulk_allocate_rejects_unicast_start(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    await db_session.commit()

    resp = await client.post(
        "/api/v1/multicast/groups/bulk-allocate/preview",
        headers=headers,
        json={
            "space_id": str(space.id),
            "count": 2,
            "name_template": "x-{n}",
            "start_address": "10.0.0.0",
        },
    )
    assert resp.status_code == 422
    assert any("224.0.0.0/4" in str(item) for item in resp.json()["detail"])


# ── PIM domains (Phase 2 Wave 1) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_create_domain_sparse_with_rp(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/multicast/domains",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "Studio-A PIM",
            "pim_mode": "sparse",
            "rendezvous_point_address": "10.0.0.1",
            "ssm_range": "232.0.0.0/8",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Studio-A PIM"
    assert body["pim_mode"] == "sparse"
    assert body["group_count"] == 0


@pytest.mark.asyncio
async def test_create_domain_sparse_without_rp_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/multicast/domains",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Bare sparse", "pim_mode": "sparse"},
    )
    assert resp.status_code == 422
    assert "rendezvous_point" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_domain_ssm_no_rp_required(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """SSM doesn't need an RP — sources signal directly."""
    _, token = await _make_admin(db_session)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/multicast/domains",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Trade-Floor SSM", "pim_mode": "ssm"},
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_update_domain_pim_mode_revalidates_rp(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/multicast/domains",
        headers=headers,
        json={"name": "Switchable", "pim_mode": "ssm"},
    )
    domain_id = resp.json()["id"]

    # Flip to sparse without an RP — should fail.
    resp = await client.put(
        f"/api/v1/multicast/domains/{domain_id}",
        headers=headers,
        json={"pim_mode": "sparse"},
    )
    assert resp.status_code == 422

    # Flip with RP set succeeds.
    resp = await client.put(
        f"/api/v1/multicast/domains/{domain_id}",
        headers=headers,
        json={"pim_mode": "sparse", "rendezvous_point_address": "10.1.1.1"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_group_with_unknown_domain_id_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "space_id": str(space.id),
            "address": "239.30.30.30",
            "name": "x",
            "domain_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 422
    assert "domain_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_group_with_real_domain_id_round_trips(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    domain = MulticastDomain(name="Round-trip", pim_mode="ssm")
    db_session.add(domain)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "space_id": str(space.id),
            "address": "239.40.40.40",
            "name": "with-domain",
            "domain_id": str(domain.id),
        },
    )
    assert resp.status_code == 201
    assert resp.json()["domain_id"] == str(domain.id)

    # Domain detail endpoint reflects the new group_count.
    resp = await client.get(
        f"/api/v1/multicast/domains/{domain.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.json()["group_count"] == 1


@pytest.mark.asyncio
async def test_delete_domain_orphans_groups(client: AsyncClient, db_session: AsyncSession) -> None:
    """ON DELETE SET NULL on multicast_group.domain_id."""
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    domain = MulticastDomain(name="Decom-me", pim_mode="ssm")
    db_session.add(domain)
    await db_session.flush()
    group = MulticastGroup(
        space_id=space.id,
        address="239.55.55.55",
        name="orphan-after",
        domain_id=domain.id,
    )
    db_session.add(group)
    await db_session.commit()

    resp = await client.delete(
        f"/api/v1/multicast/domains/{domain.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204

    # Group survives, domain_id orphans to NULL.
    await db_session.refresh(group)
    assert group.domain_id is None


# ── Operator Copilot tools (Phase 4 Wave 1) ─────────────────────────


@pytest.mark.asyncio
async def test_find_multicast_group_tool(
    db_session: AsyncSession,
) -> None:
    from app.services.ai.tools.multicast import (
        FindMulticastGroupArgs,
        find_multicast_group,
    )

    space = await _make_space(db_session)
    db_session.add_all(
        [
            MulticastGroup(
                space_id=space.id,
                address="239.10.10.1",
                name="cam-1",
                application="video",
            ),
            MulticastGroup(
                space_id=space.id,
                address="239.10.10.2",
                name="cam-2",
                application="audio",
            ),
        ]
    )
    await db_session.commit()
    user = User(
        username="ai-user",
        email="ai-user@example.test",
        display_name="AI",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db_session.add(user)
    await db_session.flush()

    rows = await find_multicast_group(
        db_session,
        user,
        FindMulticastGroupArgs(space_id=str(space.id)),
    )
    assert len(rows) == 2
    addresses = {r["address"] for r in rows}
    assert addresses == {"239.10.10.1", "239.10.10.2"}

    # Substring search hits the application column.
    rows = await find_multicast_group(
        db_session,
        user,
        FindMulticastGroupArgs(space_id=str(space.id), search="audio"),
    )
    assert len(rows) == 1
    assert rows[0]["name"] == "cam-2"


@pytest.mark.asyncio
async def test_find_multicast_membership_tool(
    db_session: AsyncSession,
) -> None:
    from app.services.ai.tools.multicast import (
        FindMulticastMembershipArgs,
        find_multicast_membership,
    )

    space = await _make_space(db_session)
    ip = await _make_ip(db_session, space, "10.0.0.42")
    group = MulticastGroup(space_id=space.id, address="239.20.0.1", name="copilot-test")
    db_session.add(group)
    await db_session.flush()
    db_session.add(
        MulticastMembership(
            group_id=group.id,
            ip_address_id=ip.id,
            role="consumer",
            seen_via="igmp_snooping",
        )
    )
    user = User(
        username="ai-user-2",
        email="ai-user-2@example.test",
        display_name="AI",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db_session.add(user)
    await db_session.commit()

    # Filter by role.
    rows = await find_multicast_membership(
        db_session, user, FindMulticastMembershipArgs(role="consumer")
    )
    assert any(r["role"] == "consumer" and r["seen_via"] == "igmp_snooping" for r in rows)

    # Invalid role surfaces as an error dict (not an exception).
    rows = await find_multicast_membership(
        db_session, user, FindMulticastMembershipArgs(role="bogus")
    )
    assert "error" in rows[0]


@pytest.mark.asyncio
async def test_count_multicast_groups_by_vrf_tool(
    db_session: AsyncSession,
) -> None:
    from app.services.ai.tools.multicast import (
        CountGroupsByVRFArgs,
        count_multicast_groups_by_vrf,
    )

    # Two groups without a domain → bucket as ``no_domain``.
    space = await _make_space(db_session)
    db_session.add_all(
        [
            MulticastGroup(space_id=space.id, address="239.30.0.1", name="g1"),
            MulticastGroup(space_id=space.id, address="239.30.0.2", name="g2"),
        ]
    )
    user = User(
        username="ai-user-3",
        email="ai-user-3@example.test",
        display_name="AI",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db_session.add(user)
    await db_session.commit()

    rows = await count_multicast_groups_by_vrf(db_session, user, CountGroupsByVRFArgs())
    no_domain = next((r for r in rows if r["vrf_name"] == "no_domain"), None)
    assert no_domain is not None
    assert no_domain["group_count"] >= 2


@pytest.mark.asyncio
async def test_multicast_tools_filtered_when_module_disabled() -> None:
    """``effective_tool_names`` drops every tool whose ``module``
    isn't in the enabled set. Operators who turn ``network.multicast``
    off see the registry shrink rather than the tools 404."""
    from app.services.ai.tools import REGISTRY, effective_tool_names

    # ``effective_tool_names`` only considers default-enabled reads,
    # so the propose tool (default_enabled=False) is naturally
    # excluded — the assertion narrows to the four read tools.
    multicast_read_tool_names = {
        tool.name
        for tool in REGISTRY.all()
        if tool.module == "network.multicast" and tool.default_enabled and not tool.writes
    }
    assert multicast_read_tool_names, "expected multicast read tools to be registered"

    # When ``network.multicast`` is in the enabled set, our tools survive.
    enabled = effective_tool_names(
        platform_enabled=None,
        provider_enabled=None,
        enabled_modules={
            "network.multicast",
            "ai.copilot",
        },
    )
    assert multicast_read_tool_names.issubset(enabled)

    # When ``network.multicast`` is absent from the enabled set, our
    # tools drop out entirely.
    enabled = effective_tool_names(
        platform_enabled=None,
        provider_enabled=None,
        enabled_modules={"ai.copilot"},
    )
    assert not multicast_read_tool_names.intersection(enabled)


# ── IGMP-snooping populator (Phase 3 Wave 1) ────────────────────────


@pytest.mark.asyncio
async def test_igmp_xref_creates_membership_when_group_and_ip_match(
    db_session: AsyncSession,
) -> None:
    """The cross-reference matcher creates a consumer membership
    when both the group address and reporter IP resolve."""
    from app.models.network import NetworkDevice
    from app.services.snmp.igmp import (
        IGMPCacheRow,
        cross_reference_igmp_memberships,
    )

    space = await _make_space(db_session)
    await _make_ip(db_session, space, "10.0.0.99")
    group = MulticastGroup(space_id=space.id, address="239.50.50.50", name="cam")
    db_session.add(group)
    device = NetworkDevice(
        name="sw-1",
        hostname="sw-1.lab",
        ip_address="10.0.0.1",
        ip_space_id=space.id,
    )
    db_session.add(device)
    await db_session.flush()

    rows = [
        IGMPCacheRow(
            group_address="239.50.50.50",
            if_index=1,
            last_reporter_ip="10.0.0.99",
            up_time_seconds=42,
            status=1,
        )
    ]
    counts = await cross_reference_igmp_memberships(db_session, device, rows)
    assert counts["created"] == 1
    assert counts["updated"] == 0
    assert counts["skipped_no_group"] == 0
    assert counts["skipped_no_ip"] == 0

    # Re-running the matcher only refreshes last_seen_at — the
    # unique (group, ip, role) triplet keeps it idempotent.
    counts = await cross_reference_igmp_memberships(db_session, device, rows)
    assert counts["created"] == 0
    assert counts["updated"] == 1


@pytest.mark.asyncio
async def test_igmp_xref_skips_when_group_unknown(
    db_session: AsyncSession,
) -> None:
    from app.models.network import NetworkDevice
    from app.services.snmp.igmp import (
        IGMPCacheRow,
        cross_reference_igmp_memberships,
    )

    space = await _make_space(db_session)
    await _make_ip(db_session, space, "10.0.0.50")
    device = NetworkDevice(
        name="sw-skip-group",
        hostname="sw-skip-group.lab",
        ip_address="10.0.0.2",
        ip_space_id=space.id,
    )
    db_session.add(device)
    await db_session.flush()

    counts = await cross_reference_igmp_memberships(
        db_session,
        device,
        [
            IGMPCacheRow(
                group_address="239.99.99.99",
                if_index=1,
                last_reporter_ip="10.0.0.50",
                up_time_seconds=None,
                status=1,
            )
        ],
    )
    assert counts["skipped_no_group"] == 1
    assert counts["created"] == 0


@pytest.mark.asyncio
async def test_igmp_xref_promotes_manual_to_igmp_snooping(
    db_session: AsyncSession,
) -> None:
    """When the matcher finds an existing manual membership, it
    refreshes ``last_seen_at`` AND promotes the ``seen_via`` tag
    to ``igmp_snooping``. Operators can see when discovery
    confirmed a manual entry."""
    from app.models.network import NetworkDevice
    from app.services.snmp.igmp import (
        IGMPCacheRow,
        cross_reference_igmp_memberships,
    )

    space = await _make_space(db_session)
    ip = await _make_ip(db_session, space, "10.0.0.77")
    group = MulticastGroup(space_id=space.id, address="239.77.77.77", name="cam77")
    db_session.add(group)
    await db_session.flush()
    membership = MulticastMembership(
        group_id=group.id,
        ip_address_id=ip.id,
        role="consumer",
        seen_via="manual",
    )
    db_session.add(membership)
    device = NetworkDevice(
        name="sw-promote",
        hostname="sw-promote.lab",
        ip_address="10.0.0.3",
        ip_space_id=space.id,
    )
    db_session.add(device)
    await db_session.flush()

    counts = await cross_reference_igmp_memberships(
        db_session,
        device,
        [
            IGMPCacheRow(
                group_address="239.77.77.77",
                if_index=2,
                last_reporter_ip="10.0.0.77",
                up_time_seconds=10,
                status=1,
            )
        ],
    )
    assert counts["updated"] == 1
    await db_session.refresh(membership)
    assert membership.seen_via == "igmp_snooping"
    assert membership.last_seen_at is not None


# ── Subnet.kind discriminator (Phase 2 Wave 3) ───────────────────────


@pytest.mark.asyncio
async def test_create_subnet_in_multicast_range_auto_kinds_multicast(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A subnet whose CIDR sits inside the IANA multicast range
    auto-stamps ``kind='multicast'`` and skips the network /
    broadcast / gateway placeholder rows."""
    _, token = await _make_admin(db_session)
    space = IPSpace(name=f"mc-kind-{uuid.uuid4().hex[:8]}")
    db_session.add(space)
    await db_session.flush()
    block = IPBlock(space_id=space.id, name="mc-block", network="239.0.0.0/8")
    db_session.add(block)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/ipam/subnets",
        headers=headers,
        json={
            "space_id": str(space.id),
            "block_id": str(block.id),
            "network": "239.10.10.0/24",
            "name": "studio-streams",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "multicast"
    subnet_id = body["id"]

    # No placeholder rows on a multicast subnet.
    resp = await client.get(
        f"/api/v1/ipam/subnets/{subnet_id}/addresses",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_create_unicast_subnet_default_kind_unicast(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = IPSpace(name=f"uc-{uuid.uuid4().hex[:8]}")
    db_session.add(space)
    await db_session.flush()
    block = IPBlock(space_id=space.id, name="uc", network="10.0.0.0/8")
    db_session.add(block)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/ipam/subnets",
        headers=headers,
        json={
            "space_id": str(space.id),
            "block_id": str(block.id),
            "network": "10.0.0.0/24",
            "name": "regular",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["kind"] == "unicast"


@pytest.mark.asyncio
async def test_multicast_subnet_refuses_ipam_allocation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``POST /subnets/{id}/addresses`` and ``/subnets/{id}/next``
    both 422 when the subnet is multicast — the operator is
    redirected to the multicast group registry."""
    _, token = await _make_admin(db_session)
    space = IPSpace(name=f"mc-refuse-{uuid.uuid4().hex[:8]}")
    db_session.add(space)
    await db_session.flush()
    block = IPBlock(space_id=space.id, name="mc-r", network="239.0.0.0/8")
    db_session.add(block)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/ipam/subnets",
        headers=headers,
        json={
            "space_id": str(space.id),
            "block_id": str(block.id),
            "network": "239.20.20.0/24",
            "name": "refuses",
        },
    )
    subnet_id = resp.json()["id"]

    # Create-address rejected. The body includes ``hostname`` so we
    # get past Pydantic body-validation and into our kind=multicast
    # short-circuit (which surfaces a string detail, not the list
    # shape FastAPI uses for body-validation errors).
    resp = await client.post(
        f"/api/v1/ipam/subnets/{subnet_id}/addresses",
        headers=headers,
        json={"address": "239.20.20.1", "hostname": "x"},
    )
    assert resp.status_code == 422
    assert "multicast" in resp.json()["detail"].lower()

    # next-ip-preview rejected.
    resp = await client.get(
        f"/api/v1/ipam/subnets/{subnet_id}/next-ip-preview",
        headers=headers,
    )
    assert resp.status_code == 422


# ── Memberships-by-IP cross-group lookup ──────────────────────────────


@pytest.mark.asyncio
async def test_memberships_by_ip_returns_joined_group_info(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    ip = await _make_ip(db_session, space, "10.0.0.50")

    g1 = MulticastGroup(
        space_id=space.id,
        address="239.10.10.1",
        name="cam-1",
        application="video",
    )
    g2 = MulticastGroup(
        space_id=space.id,
        address="239.10.10.2",
        name="cam-2",
        application="audio",
    )
    db_session.add_all([g1, g2])
    await db_session.flush()

    db_session.add_all(
        [
            MulticastMembership(group_id=g1.id, ip_address_id=ip.id, role="producer"),
            MulticastMembership(group_id=g2.id, ip_address_id=ip.id, role="consumer"),
        ]
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.get(
        f"/api/v1/multicast/memberships?ip_address_id={ip.id}",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 2
    # Ordered by address asc — cam-1 first.
    assert rows[0]["group_address"] == "239.10.10.1"
    assert rows[0]["group_name"] == "cam-1"
    assert rows[0]["group_application"] == "video"
    assert rows[0]["role"] == "producer"
    assert rows[1]["group_address"] == "239.10.10.2"
    assert rows[1]["role"] == "consumer"


@pytest.mark.asyncio
async def test_memberships_by_ip_for_unseen_ip_returns_empty(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/multicast/memberships?ip_address_id={uuid.uuid4()}",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ── Conformity: no_multicast_collision ────────────────────────────────


@pytest.mark.asyncio
async def test_no_multicast_collision_check_passes_for_unique_address(
    db_session: AsyncSession,
) -> None:
    from datetime import UTC
    from datetime import datetime as _dt

    from app.services.conformity.checks import check_no_multicast_collision

    space = await _make_space(db_session)
    group = MulticastGroup(space_id=space.id, address="239.50.50.50", name="lonely")
    db_session.add(group)
    await db_session.flush()

    outcome = await check_no_multicast_collision(
        db_session,
        target=group,
        target_kind="multicast_group",
        args={},
        now=_dt.now(UTC),
    )
    assert outcome.status == "pass"


@pytest.mark.asyncio
async def test_no_multicast_collision_check_fails_when_dup_in_same_space(
    db_session: AsyncSession,
) -> None:
    from datetime import UTC
    from datetime import datetime as _dt

    from app.services.conformity.checks import check_no_multicast_collision

    space = await _make_space(db_session)
    g1 = MulticastGroup(space_id=space.id, address="239.99.99.99", name="a")
    g2 = MulticastGroup(space_id=space.id, address="239.99.99.99", name="b")
    db_session.add_all([g1, g2])
    await db_session.flush()

    outcome = await check_no_multicast_collision(
        db_session,
        target=g1,
        target_kind="multicast_group",
        args={},
        now=_dt.now(UTC),
    )
    assert outcome.status == "fail"
    assert "239.99.99.99" in outcome.detail
    assert str(g2.id) in outcome.diagnostic["colliding_group_ids"]


@pytest.mark.asyncio
async def test_no_multicast_collision_check_passes_across_spaces(
    db_session: AsyncSession,
) -> None:
    """Same address in two *different* spaces is allowed — the
    collision rule is scoped per-space."""
    from datetime import UTC
    from datetime import datetime as _dt

    from app.services.conformity.checks import check_no_multicast_collision

    space_a = await _make_space(db_session, "A")
    space_b = await _make_space(db_session, "B")
    g_a = MulticastGroup(space_id=space_a.id, address="239.42.0.1", name="a")
    g_b = MulticastGroup(space_id=space_b.id, address="239.42.0.1", name="b")
    db_session.add_all([g_a, g_b])
    await db_session.flush()

    outcome = await check_no_multicast_collision(
        db_session,
        target=g_a,
        target_kind="multicast_group",
        args={},
        now=_dt.now(UTC),
    )
    assert outcome.status == "pass"


# ── Feature-module gate ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_module_returns_404(client: AsyncClient, db_session: AsyncSession) -> None:
    """When the operator turns ``network.multicast`` off the entire
    surface 404s — same shape as a not-installed plugin would behave
    in NetBox / Grafana."""
    _, token = await _make_admin(db_session)

    # Toggle the module off (default is enabled). Bypass the cache
    # since we're poking the row directly.
    await db_session.execute(
        text(
            "INSERT INTO feature_module (id, enabled) VALUES (:id, false) "
            "ON CONFLICT (id) DO UPDATE SET enabled = false"
        ).bindparams(id="network.multicast")
    )
    await db_session.commit()

    from app.services.feature_modules import invalidate_cache

    invalidate_cache()
    try:
        resp = await client.get(
            "/api/v1/multicast/groups",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404
    finally:
        # Re-enable for any subsequent tests in the session.
        await db_session.execute(
            text("UPDATE feature_module SET enabled = true WHERE id = :id").bindparams(
                id="network.multicast"
            )
        )
        await db_session.commit()
        invalidate_cache()
