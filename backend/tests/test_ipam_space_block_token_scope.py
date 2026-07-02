"""#484 / #400 L4 — get_space / get_block enforce per-row API-token scope.

The by-id space/block read handlers had no ``token_scope_allows`` re-check, so a
resource-scoped token was not narrowed on them (it could read arbitrary space /
block metadata), and — the real risk — if ``ip_space`` / ``ip_block`` were ever
added to ``TOKEN_GRANT_RESOURCE_TYPES`` a space/block-scoped token would read any
space/block. The handlers now gate through the centralized ``_enforce_token_scope``.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, generate_api_token, hash_password
from app.models.auth import APIToken, User
from app.models.ipam import IPBlock, IPSpace, Subnet


async def _make_user(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"tok-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="T",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_token(db: AsyncSession, owner: User, grants: list[dict]) -> str:
    raw, _prefix, token_hash = generate_api_token()
    db.add(
        APIToken(
            name=f"t-{uuid.uuid4().hex[:6]}",
            token_hash=token_hash,
            prefix=raw[:10],
            scope="user",
            scopes=[],
            resource_grants=grants,
            user_id=owner.id,
            created_by_user_id=owner.id,
            is_active=True,
        )
    )
    await db.flush()
    return raw


async def _space_and_block(db: AsyncSession) -> tuple[IPSpace, IPBlock]:
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.0.0.0/16", name="blk")
    db.add(block)
    await db.flush()
    return space, block


@pytest.mark.asyncio
async def test_unscoped_session_reads_space_and_block(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # Regression guard: a normal (non-token) session is unaffected by the gate.
    _, token = await _make_user(db_session)
    space, block = await _space_and_block(db_session)
    await db_session.commit()
    hdr = {"Authorization": f"Bearer {token}"}
    assert (await client.get(f"/api/v1/ipam/spaces/{space.id}", headers=hdr)).status_code == 200
    assert (await client.get(f"/api/v1/ipam/blocks/{block.id}", headers=hdr)).status_code == 200


@pytest.mark.asyncio
async def test_subnet_scoped_token_denied_on_space_and_block(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # A subnet-scoped token is constrained to its subnet; cross-type space /
    # block reads are now denied (previously un-narrowed — it could read them).
    owner, _ = await _make_user(db_session)
    space, block = await _space_and_block(db_session)
    subnet = Subnet(space_id=space.id, block_id=block.id, network="10.0.0.0/24", name="s")
    db_session.add(subnet)
    await db_session.flush()
    raw = await _make_token(
        db_session,
        owner,
        [{"action": "read", "resource_type": "subnet", "resource_id": str(subnet.id)}],
    )
    await db_session.commit()
    hdr = {"Authorization": f"Bearer {raw}"}
    assert (await client.get(f"/api/v1/ipam/spaces/{space.id}", headers=hdr)).status_code == 403
    assert (await client.get(f"/api/v1/ipam/blocks/{block.id}", headers=hdr)).status_code == 403


@pytest.mark.asyncio
async def test_space_and_block_scoped_tokens_bound_to_their_own(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # Forward-looking defense-in-depth: ip_space / ip_block aren't grantable via
    # the create endpoint yet, but the gate already constrains such a token to
    # its own row — so the moment they become grantable, cross-row reads can't
    # leak. (Grants are stashed verbatim by the auth dep, so a directly-inserted
    # grant exercises the gate.)
    owner, _ = await _make_user(db_session)
    space_a, block_a = await _space_and_block(db_session)
    space_b, block_b = await _space_and_block(db_session)

    space_tok = await _make_token(
        db_session,
        owner,
        [{"action": "read", "resource_type": "ip_space", "resource_id": str(space_a.id)}],
    )
    block_tok = await _make_token(
        db_session,
        owner,
        [{"action": "read", "resource_type": "ip_block", "resource_id": str(block_a.id)}],
    )
    await db_session.commit()

    sh = {"Authorization": f"Bearer {space_tok}"}
    assert (await client.get(f"/api/v1/ipam/spaces/{space_a.id}", headers=sh)).status_code == 200
    assert (await client.get(f"/api/v1/ipam/spaces/{space_b.id}", headers=sh)).status_code == 403

    bh = {"Authorization": f"Bearer {block_tok}"}
    assert (await client.get(f"/api/v1/ipam/blocks/{block_a.id}", headers=bh)).status_code == 200
    assert (await client.get(f"/api/v1/ipam/blocks/{block_b.id}", headers=bh)).status_code == 403
