"""Tests for the atomic next-available-subnet carve endpoint (#372).

``POST /ipam/blocks/{id}/allocate-subnet`` picks the lowest free child CIDR
of the requested prefix and creates it in one block-locked transaction.

The shared ``client`` fixture runs the handler on the same transactional
session ``db_session`` seeded into, so successive allocations within one test
see each other's committed rows — which is exactly what proves the free-space
recompute (and therefore the no-double-allocate invariant the block row-lock
enforces under real concurrency).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.ipam import IPBlock, IPSpace


async def _make_admin(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"alloc-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Allocate Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_space(db: AsyncSession) -> IPSpace:
    space = IPSpace(name=f"alloc-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    return space


async def _make_block(db: AsyncSession, space: IPSpace, network: str) -> IPBlock:
    block = IPBlock(space_id=space.id, network=network, name=f"blk-{uuid.uuid4().hex[:6]}")
    db.add(block)
    await db.flush()
    return block


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_allocate_carves_lowest_then_next(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Two sequential carves return the lowest free /24, then the next one."""
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    block = await _make_block(db_session, space, "10.50.0.0/16")

    r1 = await client.post(
        f"/api/v1/ipam/blocks/{block.id}/allocate-subnet",
        headers=_auth(token),
        json={"prefix_len": 24, "name": "first"},
    )
    assert r1.status_code == 201, r1.text
    assert r1.json()["network"] == "10.50.0.0/24"

    r2 = await client.post(
        f"/api/v1/ipam/blocks/{block.id}/allocate-subnet",
        headers=_auth(token),
        json={"prefix_len": 24, "name": "second"},
    )
    assert r2.status_code == 201, r2.text
    # Distinct, adjacent CIDR — the second carve saw the first's commit.
    assert r2.json()["network"] == "10.50.1.0/24"
    assert r1.json()["id"] != r2.json()["id"]


@pytest.mark.asyncio
async def test_allocate_skips_preexisting_subnet(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A subnet already occupying the low end is skipped."""
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    block = await _make_block(db_session, space, "10.51.0.0/16")

    # Pre-create 10.51.0.0/24 via the normal create path.
    pre = await client.post(
        "/api/v1/ipam/subnets",
        headers=_auth(token),
        json={"space_id": str(space.id), "block_id": str(block.id), "network": "10.51.0.0/24"},
    )
    assert pre.status_code == 201, pre.text

    r = await client.post(
        f"/api/v1/ipam/blocks/{block.id}/allocate-subnet",
        headers=_auth(token),
        json={"prefix_len": 24},
    )
    assert r.status_code == 201, r.text
    assert r.json()["network"] == "10.51.1.0/24"


@pytest.mark.asyncio
async def test_allocate_409_when_full(client: AsyncClient, db_session: AsyncSession) -> None:
    """A /30 block holds exactly one /30 — the second carve 409s."""
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    block = await _make_block(db_session, space, "10.52.0.0/30")

    r1 = await client.post(
        f"/api/v1/ipam/blocks/{block.id}/allocate-subnet",
        headers=_auth(token),
        json={"prefix_len": 30},
    )
    assert r1.status_code == 201, r1.text
    assert r1.json()["network"] == "10.52.0.0/30"

    r2 = await client.post(
        f"/api/v1/ipam/blocks/{block.id}/allocate-subnet",
        headers=_auth(token),
        json={"prefix_len": 30},
    )
    assert r2.status_code == 409, r2.text


@pytest.mark.asyncio
async def test_allocate_422_invalid_prefix(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    block = await _make_block(db_session, space, "10.53.0.0/24")

    # prefix_len <= block prefix
    r1 = await client.post(
        f"/api/v1/ipam/blocks/{block.id}/allocate-subnet",
        headers=_auth(token),
        json={"prefix_len": 24},
    )
    assert r1.status_code == 422, r1.text

    # prefix_len > family max for IPv4
    r2 = await client.post(
        f"/api/v1/ipam/blocks/{block.id}/allocate-subnet",
        headers=_auth(token),
        json={"prefix_len": 33},
    )
    assert r2.status_code == 422, r2.text


@pytest.mark.asyncio
async def test_allocate_avoids_child_block(client: AsyncClient, db_session: AsyncSession) -> None:
    """Free space excludes a direct child block's range."""
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    block = await _make_block(db_session, space, "10.54.0.0/16")
    # Reserve 10.54.0.0/24 as a child block — the carve must skip it.
    db_session.add(
        IPBlock(
            space_id=space.id,
            network="10.54.0.0/24",
            name="child",
            parent_block_id=block.id,
        )
    )
    await db_session.flush()

    r = await client.post(
        f"/api/v1/ipam/blocks/{block.id}/allocate-subnet",
        headers=_auth(token),
        json={"prefix_len": 24},
    )
    assert r.status_code == 201, r.text
    assert r.json()["network"] == "10.54.1.0/24"


@pytest.mark.asyncio
async def test_allocate_ipv6(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    block = await _make_block(db_session, space, "2001:db8:abcd::/48")

    r = await client.post(
        f"/api/v1/ipam/blocks/{block.id}/allocate-subnet",
        headers=_auth(token),
        json={"prefix_len": 64},
    )
    assert r.status_code == 201, r.text
    assert r.json()["network"] == "2001:db8:abcd::/64"


@pytest.mark.asyncio
async def test_allocate_404_unknown_block(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    r = await client.post(
        f"/api/v1/ipam/blocks/{uuid.uuid4()}/allocate-subnet",
        headers=_auth(token),
        json={"prefix_len": 24},
    )
    assert r.status_code == 404, r.text
