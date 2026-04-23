"""Tests for the block-overlap exception that allows creating a
supernet at the same level as existing sibling blocks and
auto-reparenting those siblings under the new block.

Exercises ``_assert_no_block_overlap`` and the ``create_block``
router handler through the shared ``client`` fixture so the
request's DB session is the same transactional session the fixtures
seeded into.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.ipam import IPBlock, IPSpace


async def _make_admin(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"ov-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Overlap Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_space(db: AsyncSession) -> IPSpace:
    space = IPSpace(name=f"ov-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    return space


@pytest.mark.asyncio
async def test_new_supernet_reparents_existing_siblings(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Create /12 at top level when two /16s already exist at top
    level: succeed and reparent both /16s under the new /12."""
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    db_session.add(IPBlock(space_id=space.id, network="172.20.0.0/16", name="existing-a"))
    db_session.add(IPBlock(space_id=space.id, network="172.21.0.0/16", name="existing-b"))
    await db_session.flush()

    resp = await client.post(
        "/api/v1/ipam/blocks",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "space_id": str(space.id),
            "network": "172.16.0.0/12",
            "name": "new-parent",
        },
    )
    assert resp.status_code == 201, resp.text
    parent_id = resp.json()["id"]

    for net in ("172.20.0.0/16", "172.21.0.0/16"):
        row = await db_session.execute(
            text("SELECT parent_block_id FROM ip_block WHERE network = :net"),
            {"net": net},
        )
        assert str(row.scalar_one()) == parent_id, f"{net} not reparented"


@pytest.mark.asyncio
async def test_duplicate_block_still_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    db_session.add(IPBlock(space_id=space.id, network="10.0.0.0/8", name="existing"))
    await db_session.flush()

    resp = await client.post(
        "/api/v1/ipam/blocks",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "space_id": str(space.id),
            "network": "10.0.0.0/8",
            "name": "duplicate",
        },
    )
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_strict_subset_rejected_with_hint(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A new block strictly contained in an existing sibling should
    still reject — operator should set parent_block_id to that
    sibling instead."""
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    db_session.add(IPBlock(space_id=space.id, network="10.0.0.0/8", name="existing"))
    await db_session.flush()

    resp = await client.post(
        "/api/v1/ipam/blocks",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "space_id": str(space.id),
            "network": "10.42.0.0/16",
            "name": "subset",
        },
    )
    assert resp.status_code == 409
    assert "contained in" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_supernet_reparent_only_touches_same_level(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A new supernet at top level should only reparent sibling blocks
    at top level — not children already nested under something else."""
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    # Existing chain: top-level /10 → /16 child.
    top = IPBlock(space_id=space.id, network="100.64.0.0/10", name="top")
    db_session.add(top)
    await db_session.flush()
    child = IPBlock(
        space_id=space.id, network="100.80.0.0/16", name="child", parent_block_id=top.id
    )
    db_session.add(child)
    await db_session.flush()

    # A separate /16 at top level — this one SHOULD reparent under the new /12.
    sibling = IPBlock(space_id=space.id, network="172.20.0.0/16", name="sibling")
    db_session.add(sibling)
    await db_session.flush()

    resp = await client.post(
        "/api/v1/ipam/blocks",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "space_id": str(space.id),
            "network": "172.16.0.0/12",
            "name": "new-parent",
        },
    )
    assert resp.status_code == 201, resp.text
    parent_id = resp.json()["id"]

    # Sibling at top level was reparented.
    row = await db_session.execute(
        text("SELECT parent_block_id FROM ip_block WHERE network = :n"),
        {"n": "172.20.0.0/16"},
    )
    assert str(row.scalar_one()) == parent_id

    # Child nested under the /10 is untouched.
    row = await db_session.execute(
        text("SELECT parent_block_id FROM ip_block WHERE network = :n"),
        {"n": "100.80.0.0/16"},
    )
    assert str(row.scalar_one()) == str(top.id)
