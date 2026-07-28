"""BACnet/IP device-registry tests — issue #541 (part of #543).

The load-bearing test here is the duplicate ``device_instance`` case:
instance uniqueness is internetwork-wide by specification, and the
registry has to answer a collision with a 409 that names the device
already holding the number — not a raw ``IntegrityError`` surfacing as
a 500.

Also covers the device CRUD round-trip, the 22-bit instance bounds, the
per-address lookup the IP-detail surface reads, the BBMD topology list,
the commissioning form's next-instance suggestion, and the
``network.bacnet`` feature-module gate.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import Group, Role, User
from app.models.bacnet import BACnetDevice
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet


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


async def _make_subnet(db: AsyncSession, network: str = "10.0.0.0/24") -> Subnet:
    """Minimum IPSpace → IPBlock → Subnet chain for a device anchor."""
    space = IPSpace(name=f"bac-space-{uuid.uuid4().hex[:8]}")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, name="b", network="10.0.0.0/16")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, name="s", network=network)
    db.add(subnet)
    await db.flush()
    return subnet


async def _make_ip(db: AsyncSession, subnet: Subnet, addr: str) -> IPAddress:
    ip = IPAddress(subnet_id=subnet.id, address=addr, status="allocated")
    db.add(ip)
    await db.flush()
    return ip


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── Device CRUD ───────────────────────────────────────────────────────


async def test_device_crud_round_trip(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_superadmin(db_session)
    subnet = await _make_subnet(db_session)
    ip = await _make_ip(db_session, subnet, "10.0.0.5")
    headers = _auth(token)

    created = await client.post(
        "/api/v1/bacnet/devices",
        headers=headers,
        json={
            "ip_address_id": str(ip.id),
            "device_instance": 100,
            "vendor_id": 8,
            "device_name": "AHU-3",
            "model_name": "eBMGR",
            "location": "Roof plant room",
            "max_apdu": 1476,
            "segmentation_supported": "both",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    device_id = body["id"]
    assert body["device_instance"] == 100
    assert body["address"] == "10.0.0.5"
    # subnet_id is the denormalised copy the BBMD grouping reads.
    assert body["subnet_id"] == str(subnet.id)
    assert body["vendor_label"] == "Delta Controls"

    listed = await client.get("/api/v1/bacnet/devices", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == device_id

    fetched = await client.get(f"/api/v1/bacnet/devices/{device_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["device_name"] == "AHU-3"

    patched = await client.patch(
        f"/api/v1/bacnet/devices/{device_id}",
        headers=headers,
        json={"device_name": "AHU-3 (north)", "firmware_rev": "4.02"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["device_name"] == "AHU-3 (north)"
    assert patched.json()["firmware_rev"] == "4.02"

    deleted = await client.delete(f"/api/v1/bacnet/devices/{device_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    gone = await client.get(f"/api/v1/bacnet/devices/{device_id}", headers=headers)
    assert gone.status_code == 404
    assert (await db_session.scalar(select(func.count()).select_from(BACnetDevice))) == 0


async def test_duplicate_device_instance_409_names_conflicting_device(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """THE constraint: instance numbers are unique internetwork-wide, and
    the collision answer has to name who already holds the number — the
    conflicting device is very often on another subnet, in another
    building, owned by another trade."""
    _, token = await _make_superadmin(db_session)
    subnet = await _make_subnet(db_session)
    first_ip = await _make_ip(db_session, subnet, "10.0.0.5")
    second_ip = await _make_ip(db_session, subnet, "10.0.0.6")
    headers = _auth(token)

    first = await client.post(
        "/api/v1/bacnet/devices",
        headers=headers,
        json={
            "ip_address_id": str(first_ip.id),
            "device_instance": 100,
            "device_name": "AHU-3",
        },
    )
    assert first.status_code == 201, first.text

    clash = await client.post(
        "/api/v1/bacnet/devices",
        headers=headers,
        json={
            "ip_address_id": str(second_ip.id),
            "device_instance": 100,
            "device_name": "VAV-12",
        },
    )
    assert clash.status_code == 409, clash.text
    detail = clash.json()["detail"]
    assert "100" in detail
    assert "AHU-3" in detail
    assert clash.headers["X-Conflicting-Device-Id"] == first.json()["id"]
    # The losing create wrote nothing.
    assert (await db_session.scalar(select(func.count()).select_from(BACnetDevice))) == 1


async def test_patch_to_taken_instance_409(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_superadmin(db_session)
    subnet = await _make_subnet(db_session)
    first_ip = await _make_ip(db_session, subnet, "10.0.0.5")
    second_ip = await _make_ip(db_session, subnet, "10.0.0.6")
    headers = _auth(token)

    await client.post(
        "/api/v1/bacnet/devices",
        headers=headers,
        json={
            "ip_address_id": str(first_ip.id),
            "device_instance": 100,
            "device_name": "AHU-3",
        },
    )
    second = await client.post(
        "/api/v1/bacnet/devices",
        headers=headers,
        json={"ip_address_id": str(second_ip.id), "device_instance": 101},
    )
    assert second.status_code == 201, second.text

    clash = await client.patch(
        f"/api/v1/bacnet/devices/{second.json()['id']}",
        headers=headers,
        json={"device_instance": 100},
    )
    assert clash.status_code == 409, clash.text
    assert "AHU-3" in clash.json()["detail"]


async def test_device_instance_out_of_range_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """0 - 4194302: the 22-bit space minus 0x3FFFFF, which the standard
    reserves for ``Who-Is`` wildcards."""
    _, token = await _make_superadmin(db_session)
    subnet = await _make_subnet(db_session)
    ip = await _make_ip(db_session, subnet, "10.0.0.5")
    headers = _auth(token)

    for bad_instance in (-1, 4194303):
        resp = await client.post(
            "/api/v1/bacnet/devices",
            headers=headers,
            json={"ip_address_id": str(ip.id), "device_instance": bad_instance},
        )
        assert resp.status_code == 422, f"{bad_instance}: {resp.text}"

    ok = await client.post(
        "/api/v1/bacnet/devices",
        headers=headers,
        json={"ip_address_id": str(ip.id), "device_instance": 4194302},
    )
    assert ok.status_code == 201, ok.text


async def test_bdt_rejected_on_non_bbmd(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_superadmin(db_session)
    subnet = await _make_subnet(db_session)
    ip = await _make_ip(db_session, subnet, "10.0.0.5")

    resp = await client.post(
        "/api/v1/bacnet/devices",
        headers=_auth(token),
        json={
            "ip_address_id": str(ip.id),
            "device_instance": 100,
            "bdt": [{"address": "10.0.1.1"}],
        },
    )
    assert resp.status_code == 422, resp.text
    assert "is_bbmd" in resp.json()["detail"]


# ── Per-address lookup ────────────────────────────────────────────────


async def test_by_address_returns_device_or_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_superadmin(db_session)
    subnet = await _make_subnet(db_session)
    with_device = await _make_ip(db_session, subnet, "10.0.0.5")
    without_device = await _make_ip(db_session, subnet, "10.0.0.6")
    headers = _auth(token)

    created = await client.post(
        "/api/v1/bacnet/devices",
        headers=headers,
        json={
            "ip_address_id": str(with_device.id),
            "device_instance": 100,
            "device_name": "AHU-3",
        },
    )
    assert created.status_code == 201, created.text

    hit = await client.get(f"/api/v1/bacnet/by-address/{with_device.id}", headers=headers)
    assert hit.status_code == 200, hit.text
    assert hit.json()["id"] == created.json()["id"]

    # A 404 here is the normal "this IP isn't a BACnet device" answer the
    # IP-detail surface renders as nothing.
    miss = await client.get(f"/api/v1/bacnet/by-address/{without_device.id}", headers=headers)
    assert miss.status_code == 404


# ── BBMD topology ─────────────────────────────────────────────────────


async def test_bbmds_lists_only_bbmd_devices(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_superadmin(db_session)
    subnet = await _make_subnet(db_session)
    bbmd_ip = await _make_ip(db_session, subnet, "10.0.0.5")
    plain_ip = await _make_ip(db_session, subnet, "10.0.0.6")
    headers = _auth(token)

    bbmd = await client.post(
        "/api/v1/bacnet/devices",
        headers=headers,
        json={
            "ip_address_id": str(bbmd_ip.id),
            "device_instance": 100,
            "device_name": "BBMD-Floor1",
            "is_bbmd": True,
            "bdt": [{"address": "10.0.1.1"}, {"address": "10.0.2.1"}],
        },
    )
    assert bbmd.status_code == 201, bbmd.text
    plain = await client.post(
        "/api/v1/bacnet/devices",
        headers=headers,
        json={
            "ip_address_id": str(plain_ip.id),
            "device_instance": 101,
            "device_name": "VAV-12",
        },
    )
    assert plain.status_code == 201, plain.text

    resp = await client.get("/api/v1/bacnet/bbmds", headers=headers)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [r["id"] for r in rows] == [bbmd.json()["id"]]
    assert rows[0]["subnet_id"] == str(subnet.id)
    assert rows[0]["subnet_network"] == "10.0.0.0/24"
    assert rows[0]["bdt_entries"] == 2
    assert rows[0]["fdt_entries"] == 0


# ── Instance allocation helper ────────────────────────────────────────


async def test_next_instance_returns_free_number(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_superadmin(db_session)
    subnet = await _make_subnet(db_session)
    headers = _auth(token)

    empty = await client.get("/api/v1/bacnet/next-instance", headers=headers)
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"start": 0, "device_instance": 0}

    for offset, instance in enumerate((0, 1, 3)):
        ip = await _make_ip(db_session, subnet, f"10.0.0.{10 + offset}")
        created = await client.post(
            "/api/v1/bacnet/devices",
            headers=headers,
            json={"ip_address_id": str(ip.id), "device_instance": instance},
        )
        assert created.status_code == 201, created.text

    # 0 and 1 are taken, so the first gap is 2.
    resp = await client.get("/api/v1/bacnet/next-instance", headers=headers)
    assert resp.json()["device_instance"] == 2
    # Block-allocating from 3 skips the taken 3 and lands on 4.
    scoped = await client.get("/api/v1/bacnet/next-instance?start=3", headers=headers)
    assert scoped.json() == {"start": 3, "device_instance": 4}


# ── Permission gate ───────────────────────────────────────────────────


async def test_read_permission_does_not_grant_write(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user_with_perm(
        db_session, {"action": "read", "resource_type": "bacnet_device"}
    )
    subnet = await _make_subnet(db_session)
    ip = await _make_ip(db_session, subnet, "10.0.0.5")
    headers = _auth(token)

    assert (await client.get("/api/v1/bacnet/devices", headers=headers)).status_code == 200
    denied = await client.post(
        "/api/v1/bacnet/devices",
        headers=headers,
        json={"ip_address_id": str(ip.id), "device_instance": 100},
    )
    assert denied.status_code == 403, denied.text


# ── Feature-module gate ───────────────────────────────────────────────


async def test_disabled_module_returns_404(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_superadmin(db_session)

    await db_session.execute(
        text(
            "INSERT INTO feature_module (id, enabled) VALUES (:id, false) "
            "ON CONFLICT (id) DO UPDATE SET enabled = false"
        ).bindparams(id="network.bacnet")
    )
    await db_session.commit()

    from app.services.feature_modules import invalidate_cache

    invalidate_cache()
    try:
        resp = await client.get("/api/v1/bacnet/devices", headers=_auth(token))
        assert resp.status_code == 404
    finally:
        await db_session.execute(
            text("UPDATE feature_module SET enabled = true WHERE id = :id").bindparams(
                id="network.bacnet"
            )
        )
        await db_session.commit()
        invalidate_cache()


# ── Resource-scoped API token (#374 / #523) ───────────────────────────


async def test_list_endpoints_narrow_to_a_scoped_tokens_subnet(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A subnet-bound token must not enumerate the whole BACnet estate.

    The router-level ``bacnet_device`` grant is unscoped, so the list
    filter is the only thing standing between a token bound to one
    subnet and every controller in the plant. Regression test for that
    gap: ``/devices`` and ``/bbmds`` both shipped without the filter
    while the sibling OT router had it, so this pins both.
    """
    from app.api.deps import get_current_user

    user, _ = await _make_superadmin(db_session)

    mine = await _make_subnet(db_session, "10.0.0.0/24")
    theirs = await _make_subnet(db_session, "10.9.9.0/24")
    ip_mine = await _make_ip(db_session, mine, "10.0.0.5")
    ip_theirs = await _make_ip(db_session, theirs, "10.9.9.5")

    db_session.add_all(
        [
            BACnetDevice(
                ip_address_id=ip_mine.id, subnet_id=mine.id, device_instance=4001, is_bbmd=True
            ),
            BACnetDevice(
                ip_address_id=ip_theirs.id, subnet_id=theirs.id, device_instance=4002, is_bbmd=True
            ),
        ]
    )
    await db_session.commit()

    # Stand in for the auth dependency: it stamps the token's resource
    # grants onto the user, which is what the scope helper reads.
    # A realistic scoped token carries BOTH: broad access to the device
    # type (so it clears the router-level gate) and a binding to one
    # subnet (which is what must narrow the result set). A subnet-only
    # grant never reaches the filter — the coarse gate 403s first.
    user._api_token_resource_grants = [  # type: ignore[attr-defined]
        {"action": "read", "resource_type": "bacnet_device", "resource_id": "*"},
        {"action": "read", "resource_type": "subnet", "resource_id": str(mine.id)},
    ]
    client._transport.app.dependency_overrides[get_current_user] = lambda: user  # type: ignore[attr-defined]
    try:
        listed = await client.get("/api/v1/bacnet/devices")
        assert listed.status_code == 200
        body = listed.json()
        assert [d["device_instance"] for d in body["items"]] == [4001]
        # The count must be narrowed too, or the total leaks the size of
        # the estate even when the rows are hidden.
        assert body["total"] == 1

        bbmds = await client.get("/api/v1/bacnet/bbmds")
        assert bbmds.status_code == 200
        assert [b["device_instance"] for b in bbmds.json()] == [4001]
    finally:
        client._transport.app.dependency_overrides.pop(get_current_user, None)  # type: ignore[attr-defined]
