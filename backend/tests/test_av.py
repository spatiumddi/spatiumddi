"""AV / Audio-Video-over-IP registry tests — issue #540 (part of #543).

Covers the reserved-range CRUD, the flow-descriptor upsert/delete
sidecar semantics (dropping the descriptor must NOT delete the parent
multicast group), the two server-side validators the router owns
(protocol vocabulary + multicast-only CIDR), the allocation-preview
verdicts, and the ``network.av`` feature-module gate.

Rows the AV surface hangs off (IPSpace, MulticastGroup) are inserted
directly as models rather than through their own APIs — this file is
testing ``/av``, not the multicast router.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import Group, Role, User
from app.models.av import AV_PROTOCOLS, AVFlowProfile, AVReservedRange
from app.models.ipam import IPSpace
from app.models.multicast import MulticastGroup


async def _make_superadmin(db: AsyncSession) -> tuple[User, str]:
    u = User(
        username=f"admin-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@x.com",
        display_name="Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(u)
    await db.flush()
    return u, create_access_token(str(u.id))


async def _make_user_with_perm(db: AsyncSession, perm: dict | None) -> tuple[User, str]:
    """Create a non-superadmin user; optionally grant ``perm`` via a
    role + group so the permission helper sees it."""
    u = User(
        username=f"user-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@x.com",
        display_name="User",
        hashed_password=hash_password("x"),
        is_superadmin=False,
    )
    db.add(u)
    await db.flush()
    if perm is not None:
        role = Role(name=f"r-{uuid.uuid4().hex[:6]}", description="", permissions=[perm])
        db.add(role)
        await db.flush()
        group = Group(name=f"g-{uuid.uuid4().hex[:6]}", description="")
        group.roles = [role]
        group.users = [u]
        db.add(group)
        await db.flush()
    return u, create_access_token(str(u.id))


async def _make_space(db: AsyncSession) -> IPSpace:
    space = IPSpace(name=f"av-space-{uuid.uuid4().hex[:8]}")
    db.add(space)
    await db.flush()
    return space


async def _make_group(db: AsyncSession, space: IPSpace, address: str) -> MulticastGroup:
    group = MulticastGroup(
        space_id=space.id,
        address=address,
        name=f"grp-{address}",
        application="AoIP",
    )
    db.add(group)
    await db.flush()
    return group


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── Reserved ranges ───────────────────────────────────────────────────


async def test_reserved_range_crud_round_trip(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_superadmin(db_session)
    space = await _make_space(db_session)
    headers = _auth(token)

    created = await client.post(
        "/api/v1/av/reserved-ranges",
        headers=headers,
        json={
            "space_id": str(space.id),
            "cidr": "239.69.0.0/16",
            "av_protocol": "dante",
            "name": "Dante default",
            "description": "Audinate transmit flows",
            "exclusive": True,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    range_id = body["id"]
    assert body["cidr"] == "239.69.0.0/16"
    assert body["av_protocol"] == "dante"
    assert body["exclusive"] is True

    listed = await client.get("/api/v1/av/reserved-ranges", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [r["id"] for r in listed.json()] == [range_id]

    fetched = await client.get(f"/api/v1/av/reserved-ranges/{range_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["name"] == "Dante default"

    updated = await client.put(
        f"/api/v1/av/reserved-ranges/{range_id}",
        headers=headers,
        json={"name": "Dante flows", "exclusive": False},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Dante flows"
    assert updated.json()["exclusive"] is False

    deleted = await client.delete(f"/api/v1/av/reserved-ranges/{range_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    gone = await client.get(f"/api/v1/av/reserved-ranges/{range_id}", headers=headers)
    assert gone.status_code == 404
    assert (await db_session.scalar(select(func.count()).select_from(AVReservedRange))) == 0


async def test_reserved_range_rejects_unknown_protocol(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_superadmin(db_session)
    space = await _make_space(db_session)

    resp = await client.post(
        "/api/v1/av/reserved-ranges",
        headers=_auth(token),
        json={
            "space_id": str(space.id),
            "cidr": "239.69.0.0/16",
            "av_protocol": "zigbee",
        },
    )
    assert resp.status_code == 422, resp.text


async def test_reserved_range_rejects_unicast_cidr(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The Postgres CHECK is the real guard; the router turns it into a
    422 that names the allowed ranges instead of an IntegrityError."""
    _, token = await _make_superadmin(db_session)
    space = await _make_space(db_session)

    resp = await client.post(
        "/api/v1/av/reserved-ranges",
        headers=_auth(token),
        json={
            "space_id": str(space.id),
            "cidr": "10.0.0.0/8",
            "av_protocol": "dante",
        },
    )
    assert resp.status_code == 422, resp.text
    assert any("224.0.0.0/4" in str(item) for item in resp.json()["detail"])
    assert (await db_session.scalar(select(func.count()).select_from(AVReservedRange))) == 0


async def test_reserved_range_rejects_host_bits_set(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_superadmin(db_session)
    space = await _make_space(db_session)

    resp = await client.post(
        "/api/v1/av/reserved-ranges",
        headers=_auth(token),
        json={
            "space_id": str(space.id),
            "cidr": "239.69.0.5/16",
            "av_protocol": "dante",
        },
    )
    assert resp.status_code == 422, resp.text


# ── Flow descriptors ──────────────────────────────────────────────────


async def test_flow_upsert_then_delete_keeps_group(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The descriptor is a sidecar: dropping it demotes the row back to a
    plain multicast group rather than deleting the address record."""
    _, token = await _make_superadmin(db_session)
    space = await _make_space(db_session)
    group = await _make_group(db_session, space, "239.69.1.5")
    group_id = group.id
    headers = _auth(token)

    created = await client.put(
        f"/api/v1/av/flows/{group_id}",
        headers=headers,
        json={
            "av_protocol": "dante",
            "av_flow_label": "Cam7-Studio-B HD",
            "ptp_domain": 3,
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["group_address"] == "239.69.1.5"
    assert created.json()["ptp_domain"] == 3

    # PUT is a full-document upsert — a second call updates in place.
    updated = await client.put(
        f"/api/v1/av/flows/{group_id}",
        headers=headers,
        json={"av_protocol": "aes67", "av_flow_label": "FOH-Mix L/R"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["av_protocol"] == "aes67"
    # Omitted ptp_domain clears it — documented full-document semantics.
    assert updated.json()["ptp_domain"] is None

    listed = await client.get("/api/v1/av/flows", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["group_id"] == str(group_id)

    deleted = await client.delete(f"/api/v1/av/flows/{group_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    after = await client.get("/api/v1/av/flows", headers=headers)
    assert after.json()["total"] == 0
    # Counted in SQL rather than via db.get() so the session identity map
    # can't answer from a cached instance.
    assert (await db_session.scalar(select(func.count()).select_from(AVFlowProfile))) == 0
    assert (
        await db_session.scalar(
            select(func.count()).select_from(MulticastGroup).where(MulticastGroup.id == group_id)
        )
    ) == 1


async def test_flow_upsert_rejects_unknown_protocol(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_superadmin(db_session)
    space = await _make_space(db_session)
    group = await _make_group(db_session, space, "239.69.1.6")

    resp = await client.put(
        f"/api/v1/av/flows/{group.id}",
        headers=_auth(token),
        json={"av_protocol": "zigbee"},
    )
    assert resp.status_code == 422, resp.text


async def test_flow_delete_unknown_group_404(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_superadmin(db_session)

    resp = await client.delete(f"/api/v1/av/flows/{uuid.uuid4()}", headers=_auth(token))
    assert resp.status_code == 404


# ── Allocation preview ────────────────────────────────────────────────


async def test_allocation_preview_conflict_only_when_exclusive(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A foreign protocol landing in an exclusive range is a conflict;
    the same allocation in a shared range is informational only."""
    _, token = await _make_superadmin(db_session)
    space = await _make_space(db_session)
    headers = _auth(token)

    created = await client.post(
        "/api/v1/av/reserved-ranges",
        headers=headers,
        json={
            "space_id": str(space.id),
            "cidr": "239.69.0.0/16",
            "av_protocol": "dante",
            "exclusive": True,
        },
    )
    assert created.status_code == 201, created.text
    range_id = created.json()["id"]

    preview_body = {
        "space_id": str(space.id),
        "address_or_cidr": "239.69.1.5",
        "av_protocol": "aes67",
    }
    conflicted = await client.post(
        "/api/v1/av/allocation-preview", headers=headers, json=preview_body
    )
    assert conflicted.status_code == 200, conflicted.text
    body = conflicted.json()
    assert body["status"] == "conflict"
    assert [c["av_protocol"] for c in body["conflicts"]] == ["dante"]
    assert body["conflicts"][0]["relation"] == "inside"
    assert body["advisories"] == []

    # Drop exclusivity: same address, same protocols, no conflict.
    relaxed = await client.put(
        f"/api/v1/av/reserved-ranges/{range_id}",
        headers=headers,
        json={"exclusive": False},
    )
    assert relaxed.status_code == 200, relaxed.text

    shared = await client.post("/api/v1/av/allocation-preview", headers=headers, json=preview_body)
    assert shared.status_code == 200, shared.text
    shared_body = shared.json()
    assert shared_body["conflicts"] == []
    assert shared_body["status"] == "informational"
    assert [a["av_protocol"] for a in shared_body["advisories"]] == ["dante"]


async def test_allocation_preview_ok_for_own_protocol(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_superadmin(db_session)
    space = await _make_space(db_session)
    headers = _auth(token)

    await client.post(
        "/api/v1/av/reserved-ranges",
        headers=headers,
        json={
            "space_id": str(space.id),
            "cidr": "239.69.0.0/16",
            "av_protocol": "dante",
            "exclusive": True,
        },
    )
    resp = await client.post(
        "/api/v1/av/allocation-preview",
        headers=headers,
        json={
            "space_id": str(space.id),
            "address_or_cidr": "239.69.1.5",
            "av_protocol": "dante",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["conflicts"] == []
    assert [r["av_protocol"] for r in body["own_ranges"]] == ["dante"]
    assert body["suggested_range"] is None


async def test_allocation_preview_rejects_unparseable_target(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_superadmin(db_session)
    space = await _make_space(db_session)

    resp = await client.post(
        "/api/v1/av/allocation-preview",
        headers=_auth(token),
        json={
            "space_id": str(space.id),
            "address_or_cidr": "not-an-address",
            "av_protocol": "dante",
        },
    )
    assert resp.status_code == 422, resp.text


async def test_presets_cover_every_protocol(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_superadmin(db_session)
    resp = await client.get("/api/v1/av/presets", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {p["av_protocol"] for p in body["protocols"]} == set(AV_PROTOCOLS)
    assert body["ptp_domain_max"] == 255


# ── Permission gate ───────────────────────────────────────────────────


async def test_read_permission_does_not_grant_write(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user_with_perm(
        db_session, {"action": "read", "resource_type": "av_flow"}
    )
    space = await _make_space(db_session)
    headers = _auth(token)

    assert (await client.get("/api/v1/av/reserved-ranges", headers=headers)).status_code == 200
    denied = await client.post(
        "/api/v1/av/reserved-ranges",
        headers=headers,
        json={
            "space_id": str(space.id),
            "cidr": "239.69.0.0/16",
            "av_protocol": "dante",
        },
    )
    assert denied.status_code == 403, denied.text


# ── Feature-module gate ───────────────────────────────────────────────


async def test_disabled_module_returns_404(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_superadmin(db_session)

    await db_session.execute(
        text(
            "INSERT INTO feature_module (id, enabled) VALUES (:id, false) "
            "ON CONFLICT (id) DO UPDATE SET enabled = false"
        ).bindparams(id="network.av")
    )
    await db_session.commit()

    from app.services.feature_modules import invalidate_cache

    invalidate_cache()
    try:
        resp = await client.get("/api/v1/av/reserved-ranges", headers=_auth(token))
        assert resp.status_code == 404
    finally:
        await db_session.execute(
            text("UPDATE feature_module SET enabled = true WHERE id = :id").bindparams(
                id="network.av"
            )
        )
        await db_session.commit()
        invalidate_cache()
