"""Regression test for #453 — IPAM "Sync → DHCP" against an agent-based Kea
server returned 400 Bad Request.

The IPAM subnet sync modal fans ``POST /dhcp/servers/{id}/sync-leases`` out to
every server backing the subnet, agent-based ones included. ``sync-leases`` is
an agentless-only (Windows DHCP) operation, so it used to hard-reject Kea with
a 400 — which read as a broken sync to the operator. The endpoint now returns a
no-op result with an explanatory ``note`` (and nudges the agent to re-poll)
instead of erroring.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dhcp import DHCPServer, DHCPServerGroup


async def _superadmin(db: AsyncSession) -> str:
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


async def _kea_server(db: AsyncSession, *, grouped: bool = True) -> DHCPServer:
    group_id = None
    if grouped:
        grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", description="")
        db.add(grp)
        await db.flush()
        group_id = grp.id
    srv = DHCPServer(
        name=f"s-{uuid.uuid4().hex[:6]}",
        driver="kea",  # agent-based
        host="127.0.0.1",
        port=67,
        server_group_id=group_id,
    )
    db.add(srv)
    await db.flush()
    return srv


@pytest.mark.asyncio
async def test_sync_leases_on_kea_returns_note_not_400(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _superadmin(db_session)
    srv = await _kea_server(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dhcp/servers/{srv.id}/sync-leases",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    # No-op shape: nothing pulled, no errors, and a human-readable note.
    assert body["note"]
    assert "agent-based" in body["note"]
    assert body["server_leases"] == 0
    assert body["imported"] == 0
    assert body["errors"] == []


@pytest.mark.asyncio
async def test_sync_leases_on_ungrouped_kea_still_ok(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # ``server_group_id`` is nullable; the wake-channel build must not assume a
    # group. A standalone Kea server should still return the no-op note.
    token = await _superadmin(db_session)
    srv = await _kea_server(db_session, grouped=False)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dhcp/servers/{srv.id}/sync-leases",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["note"]


@pytest.mark.asyncio
async def test_sync_leases_missing_server_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _superadmin(db_session)
    await db_session.commit()
    r = await client.post(
        f"/api/v1/dhcp/servers/{uuid.uuid4()}/sync-leases",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404
