"""Security regression tests for the address-set delegation feature (#103).

These cover the seven security-review findings closed on ``issue-449-103``.
The read-model the fixes enforce:

* **CREATE / RANGE-RESIZE** of an address set is a *subnet-owner* operation →
  requires ``write`` (or ``admin``, which implies write) on the PARENT SUBNET.
* **READ** (get / list / MCP find / count) is scoped to the parent subnet →
  a caller may see a set only if they can read its parent subnet.
* **WRITE/ADMIN on a specific set** (the delegated grant) is unchanged — the
  per-IP gate handles "edit IPs in my set's range".

So a type-wide ``admin:address_set`` (the "Address Set Editor" role) can no
longer self-delegate write on an arbitrary subnet by carving a slice out of it.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.address_set import EXPLICIT_ADDRESSES_MAX, AddressSet
from app.models.auth import Group, Role, User
from app.models.ipam import IPBlock, IPSpace, Subnet
from app.services.ai.tools import REGISTRY

# ── Fixtures ──────────────────────────────────────────────────────────────────


async def _user(
    db: AsyncSession,
    *,
    name: str,
    permissions: list[dict] | None = None,
    superadmin: bool = False,
) -> tuple[User, str]:
    """Create a user with exactly ``permissions`` (attached via a fresh
    Group+Role) and return ``(user, bearer_token)``."""
    user = User(
        username=f"{name}-{uuid.uuid4().hex[:8]}",
        email=f"{name}-{uuid.uuid4().hex[:8]}@t.io",
        display_name=name,
        hashed_password=hash_password("password123"),
        is_superadmin=superadmin,
    )
    user.groups = []
    if permissions:
        role = Role(name=f"role-{uuid.uuid4().hex[:8]}", permissions=permissions)
        group = Group(name=f"grp-{uuid.uuid4().hex[:8]}")
        group.roles = [role]
        user.groups = [group]
        db.add_all([role, group])
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _subnet(db: AsyncSession, network: str = "10.0.5.0/24") -> Subnet:
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.0.0.0/16", name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=network, name="s")
    db.add(subnet)
    await db.flush()
    return subnet


async def _set(
    db: AsyncSession,
    subnet: Subnet,
    *,
    name: str = "delegated",
    start: str = "10.0.5.50",
    end: str = "10.0.5.99",
) -> AddressSet:
    row = AddressSet(
        name=name,
        subnet_id=subnet.id,
        range_kind="contiguous",
        start_address=start,
        end_address=end,
    )
    db.add(row)
    await db.flush()
    return row


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── Finding #1: CREATE requires write on the parent subnet ────────────────────


@pytest.mark.asyncio
async def test_create_denied_without_subnet_write(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A user with type-wide ``admin:address_set`` but NO subnet write gets 403
    on POST /address-sets for a subnet they can't write (self-escalation)."""
    subnet = await _subnet(db_session)
    _, token = await _user(
        db_session,
        name="addrset-editor",
        permissions=[{"action": "admin", "resource_type": "address_set"}],
    )
    r = await client.post(
        "/api/v1/address-sets",
        headers=_auth(token),
        json={
            "name": "mine",
            "subnet_id": str(subnet.id),
            "range_kind": "contiguous",
            "start_address": "10.0.5.50",
            "end_address": "10.0.5.99",
        },
    )
    assert r.status_code == 403, r.text
    assert "parent subnet" in r.json()["detail"]


@pytest.mark.asyncio
async def test_create_allowed_with_subnet_write(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The same user who ALSO holds write on the subnet succeeds."""
    subnet = await _subnet(db_session)
    _, token = await _user(
        db_session,
        name="addrset-owner",
        permissions=[
            {"action": "admin", "resource_type": "address_set"},
            {"action": "write", "resource_type": "subnet"},
        ],
    )
    r = await client.post(
        "/api/v1/address-sets",
        headers=_auth(token),
        json={
            "name": "mine",
            "subnet_id": str(subnet.id),
            "range_kind": "contiguous",
            "start_address": "10.0.5.50",
            "end_address": "10.0.5.99",
        },
    )
    assert r.status_code == 201, r.text


# ── Finding #2: RANGE-resize requires subnet write; non-range edits don't ─────


@pytest.mark.asyncio
async def test_update_range_widen_denied_without_subnet_write(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An Address Set Editor (type-wide ``admin:address_set``, no subnet write)
    gets 403 when the PUT widens the range, but succeeds when only editing
    name/description. Type-wide admin is what actually reaches the PUT handler —
    the coarse router gate maps PUT → write on ``address_set``, which a purely
    set-scoped grant can't satisfy, so the realistic finding-#2 threat is the
    type-wide editor widening a slice they don't own the subnet of."""
    subnet = await _subnet(db_session)
    row = await _set(db_session, subnet)
    await db_session.flush()
    _, token = await _user(
        db_session,
        name="set-delegate",
        permissions=[
            # Address Set Editor role shape: type-wide admin on address sets,
            # plus subnet READ so the read-model lets them see the row.
            {"action": "admin", "resource_type": "address_set"},
            {"action": "read", "resource_type": "subnet"},
        ],
    )

    # Widening the range is a subnet-owner op → 403.
    r = await client.put(
        f"/api/v1/address-sets/{row.id}",
        headers=_auth(token),
        json={"end_address": "10.0.5.200"},
    )
    assert r.status_code == 403, r.text
    assert "parent subnet" in r.json()["detail"]

    # Name / description edit (no range change) → allowed for the address-set admin.
    r = await client.put(
        f"/api/v1/address-sets/{row.id}",
        headers=_auth(token),
        json={"name": "renamed", "description": "ok"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "renamed"
    # Range is untouched.
    assert r.json()["end_address"] == "10.0.5.99"


@pytest.mark.asyncio
async def test_update_range_noop_resend_allowed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Resending the SAME range values is not a resize — no subnet write needed."""
    subnet = await _subnet(db_session)
    row = await _set(db_session, subnet)
    await db_session.flush()
    _, token = await _user(
        db_session,
        name="set-delegate2",
        permissions=[
            {"action": "admin", "resource_type": "address_set"},
            {"action": "read", "resource_type": "subnet"},
        ],
    )
    r = await client.put(
        f"/api/v1/address-sets/{row.id}",
        headers=_auth(token),
        json={"start_address": "10.0.5.50", "end_address": "10.0.5.99", "description": "same"},
    )
    assert r.status_code == 200, r.text


# ── Finding #4: MCP find/count scoped to readable parent subnets ──────────────


@pytest.mark.asyncio
async def test_find_address_sets_scoped_to_subnet_read(db_session: AsyncSession) -> None:
    """find_address_sets returns nothing for a user with no subnet read, and the
    set for a user who can read the subnet."""
    subnet = await _subnet(db_session)
    await _set(db_session, subnet, name="visible-only-to-readers")
    await db_session.flush()

    no_read, _ = await _user(
        db_session,
        name="no-read",
        permissions=[{"action": "admin", "resource_type": "address_set"}],
    )
    can_read, _ = await _user(
        db_session,
        name="can-read",
        permissions=[{"action": "read", "resource_type": "subnet"}],
    )

    out_none = await REGISTRY.call("find_address_sets", {}, db=db_session, user=no_read)
    assert out_none == []

    out_some = await REGISTRY.call("find_address_sets", {}, db=db_session, user=can_read)
    assert isinstance(out_some, list)
    names = {s["name"] for s in out_some}
    assert "visible-only-to-readers" in names


@pytest.mark.asyncio
async def test_count_address_sets_scoped_to_subnet_read(db_session: AsyncSession) -> None:
    subnet = await _subnet(db_session)
    await _set(db_session, subnet, name="counted")
    await db_session.flush()

    no_read, _ = await _user(
        db_session,
        name="no-read-c",
        permissions=[{"action": "admin", "resource_type": "address_set"}],
    )
    can_read, _ = await _user(
        db_session,
        name="can-read-c",
        permissions=[{"action": "read", "resource_type": "subnet"}],
    )

    out_none = await REGISTRY.call("count_address_sets", {}, db=db_session, user=no_read)
    assert out_none["address_sets"] == 0

    out_some = await REGISTRY.call("count_address_sets", {}, db=db_session, user=can_read)
    assert out_some["address_sets"] >= 1


# ── Finding #5: REST get/list read-model (404 / filtered) ─────────────────────


@pytest.mark.asyncio
async def test_get_address_set_404_without_subnet_read(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    subnet = await _subnet(db_session)
    row = await _set(db_session, subnet)
    await db_session.flush()
    # Type-wide address_set read, but NO subnet read → must not confirm existence.
    _, token = await _user(
        db_session,
        name="addrset-reader",
        permissions=[{"action": "read", "resource_type": "address_set"}],
    )
    r = await client.get(f"/api/v1/address-sets/{row.id}", headers=_auth(token))
    assert r.status_code == 404, r.text

    # The list is filtered to nothing for the same caller.
    r = await client.get("/api/v1/address-sets", headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == []


# ── Finding #6: explicit_addresses is capped ──────────────────────────────────


@pytest.mark.asyncio
async def test_explicit_addresses_over_cap_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An explicit_addresses list over the cap is rejected (422) — the gate
    re-parses these on every IPAM mutation, so an unbounded list is a DoS."""
    subnet = await _subnet(db_session, network="10.9.0.0/16")
    _, token = await _user(
        db_session,
        name="addrset-dos",
        permissions=[
            {"action": "admin", "resource_type": "address_set"},
            {"action": "write", "resource_type": "subnet"},
        ],
    )
    # One more than the cap → rejected by the Pydantic schema before the handler.
    too_many = [f"10.9.{i // 254}.{(i % 254) + 1}" for i in range(EXPLICIT_ADDRESSES_MAX + 1)]
    r = await client.post(
        "/api/v1/address-sets",
        headers=_auth(token),
        json={
            "name": "huge",
            "subnet_id": str(subnet.id),
            "range_kind": "explicit",
            "explicit_addresses": too_many,
        },
    )
    assert r.status_code == 422, r.text


def test_validate_shape_rejects_over_cap() -> None:
    """The shared validator (AI path + any direct caller) also enforces the
    cap, so no surface can exceed it."""
    from app.models.address_set import validate_address_set_shape

    over = [f"10.9.0.{i}" for i in range(EXPLICIT_ADDRESSES_MAX + 1)]
    err = validate_address_set_shape("explicit", None, None, over)
    assert err is not None
    assert str(EXPLICIT_ADDRESSES_MAX) in err
    # At-cap is fine (shape-wise).
    at_cap = [f"10.9.0.{i % 256}" for i in range(EXPLICIT_ADDRESSES_MAX)]
    assert validate_address_set_shape("explicit", None, None, at_cap) is None


# ── The core delegation case: a SCOPED grant clears the coarse IPAM gate ──────
# Both the code review and the security review tested only the type-wide
# "Address Set Editor" role (unscoped admin:address_set, which passes the
# coarse gate's unscoped check). The ACTUAL #103 delegation hands out a scoped
# ``{write, address_set, <id>}`` grant — which the unscoped coarse check would
# 403 before the per-IP gate ever runs. These two tests prove the scoped
# delegate works end to end (admit at the coarse gate via require_any_resource_
# or_scoped, real boundary enforced by the per-IP gate).


@pytest.mark.asyncio
async def test_scoped_delegate_can_write_ip_inside_set(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A delegate holding ONLY ``{write, address_set, <set_id>}`` (no subnet
    write) can allocate an IP that falls inside that set's range."""
    subnet = await _subnet(db_session)
    row = await _set(db_session, subnet, start="10.0.5.50", end="10.0.5.59")
    await db_session.flush()
    _, token = await _user(
        db_session,
        name="printers-admin",
        permissions=[
            {"action": "write", "resource_type": "address_set", "resource_id": str(row.id)}
        ],
    )
    r = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers=_auth(token),
        json={"address": "10.0.5.55", "hostname": "printer-1"},
    )
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_scoped_delegate_denied_ip_outside_set(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The same scoped delegate is 403'd for an IP outside their set's range —
    the per-IP gate, not the coarse gate, draws the boundary."""
    subnet = await _subnet(db_session)
    row = await _set(db_session, subnet, start="10.0.5.50", end="10.0.5.59")
    await db_session.flush()
    _, token = await _user(
        db_session,
        name="printers-admin",
        permissions=[
            {"action": "write", "resource_type": "address_set", "resource_id": str(row.id)}
        ],
    )
    r = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers=_auth(token),
        json={"address": "10.0.5.10", "hostname": "not-mine"},
    )
    assert r.status_code == 403, r.text


# ── Finding #7: gate merges intervals + bounds work ───────────────────────────


def test_writable_ranges_merge_and_membership() -> None:
    """Merged/sorted intervals give correct membership via binary search."""
    from app.services.ipam.address_set_gate import WritableSetRanges

    r = WritableSetRanges()
    # Deliberately unsorted + overlapping + adjacent.
    r.intervals = [(20, 30), (1, 10), (11, 15), (25, 40)]
    r.finalize()
    # (1,10)+(11,15) merge (adjacent), (20,30)+(25,40) merge (overlap).
    assert r.intervals == [(1, 15), (20, 40)]
    assert r.contains(1) and r.contains(15) and r.contains(40)
    assert not r.contains(0) and not r.contains(16) and not r.contains(41)
    assert r.contains(25)
