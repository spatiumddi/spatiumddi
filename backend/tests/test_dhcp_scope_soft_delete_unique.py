"""DHCP scope uniqueness must ignore soft-deleted rows (#474).

A soft-deleted scope kept occupying the ``(group, subnet)`` slot under the
old non-partial ``UNIQUE`` constraint, so re-creating a scope for that
subnet raised a raw ``IntegrityError`` → 500. The constraint is now a
partial unique index (``WHERE deleted_at IS NULL``); live scopes stay
unique per ``(group, subnet)`` but trashed rows fall out of the index.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dhcp import DHCPScope, DHCPServerGroup
from app.models.ipam import IPBlock, IPSpace, Subnet

CIDR = "10.74.0.0/24"


async def _superadmin_token(db: AsyncSession) -> str:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


async def _subnet_and_group(db: AsyncSession) -> tuple[Subnet, DHCPServerGroup]:
    space = IPSpace(name=f"u474-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network=CIDR, name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=CIDR, name="s")
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add_all([subnet, grp])
    await db.flush()
    return subnet, grp


async def _create_scope(
    client: AsyncClient, token: str, subnet: Subnet, grp: DHCPServerGroup, name: str
):
    return await client.post(
        f"/api/v1/dhcp/subnets/{subnet.id}/dhcp-scopes",
        headers={"Authorization": f"Bearer {token}"},
        json={"group_id": str(grp.id), "name": name},
    )


async def test_recreate_scope_after_soft_delete(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _superadmin_token(db_session)
    subnet, grp = await _subnet_and_group(db_session)
    await db_session.commit()

    r = await _create_scope(client, token, subnet, grp, "first")
    assert r.status_code in (200, 201), r.text
    scope_id = uuid.UUID(r.json()["id"])

    # Soft-delete it (simulate Trash) directly, bypassing the approvals gate.
    scope = await db_session.get(DHCPScope, scope_id)
    assert scope is not None
    scope.deleted_at = datetime.now(UTC)
    await db_session.commit()

    # Re-creating a scope for the same (group, subnet) must succeed, not 500.
    r2 = await _create_scope(client, token, subnet, grp, "second")
    assert r2.status_code in (200, 201), r2.text
    assert uuid.UUID(r2.json()["id"]) != scope_id


async def test_duplicate_active_scope_still_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _superadmin_token(db_session)
    subnet, grp = await _subnet_and_group(db_session)
    await db_session.commit()

    r = await _create_scope(client, token, subnet, grp, "first")
    assert r.status_code in (200, 201), r.text

    # A second live scope for the same (group, subnet) is still rejected.
    r2 = await _create_scope(client, token, subnet, grp, "dup")
    assert r2.status_code == 409, r2.text
