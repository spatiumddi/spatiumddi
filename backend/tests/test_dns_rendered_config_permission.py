"""``GET /dns/servers/{id}/rendered-config`` is superadmin-only (#869).

The endpoint returns a DNS server's whole rendered configuration, pushed up
by the agent. The DNS router's own gate is satisfied by *read on any* of
``dns_group`` / ``dns_zone`` / ``dns_record``, so before this change the
builtin Viewer role reached it — and the rendered PowerDNS config carries
the REST ``api-key`` plus the TSIG secrets that authorise dynamic updates.

The agent now redacts known credential values before pushing, but that is a
denylist over whatever a driver happened to render; the gate is the part
that doesn't depend on having anticipated the format.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import Group, Role, User
from app.models.dns import DNSServer, DNSServerGroup

pytestmark = pytest.mark.asyncio


async def _viewer(db: AsyncSession) -> str:
    """A non-superadmin with read on everything — the builtin Viewer shape."""
    u = User(
        username=f"viewer-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@example.com",
        display_name="Viewer",
        hashed_password=hash_password("x"),
        is_superadmin=False,
    )
    db.add(u)
    await db.flush()
    role = Role(
        name=f"viewer-{uuid.uuid4().hex[:6]}",
        description="",
        permissions=[{"action": "read", "resource_type": "*"}],
    )
    db.add(role)
    await db.flush()
    group = Group(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    group.roles = [role]
    group.users = [u]
    db.add(group)
    await db.flush()
    return create_access_token(str(u.id))


async def _superadmin(db: AsyncSession) -> str:
    u = User(
        username=f"admin-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@example.com",
        display_name="Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(u)
    await db.flush()
    return create_access_token(str(u.id))


async def _server(db: AsyncSession) -> DNSServer:
    grp = DNSServerGroup(name=f"grp-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    srv = DNSServer(
        group_id=grp.id,
        name=f"srv-{uuid.uuid4().hex[:6]}",
        driver="powerdns",
        host="10.0.0.9",
    )
    db.add(srv)
    await db.flush()
    return srv


async def test_viewer_is_refused(client: AsyncClient, db_session: AsyncSession) -> None:
    srv = await _server(db_session)
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/dns/servers/{srv.id}/rendered-config",
        headers={"Authorization": f"Bearer {await _viewer(db_session)}"},
    )
    assert resp.status_code == 403, resp.text


async def test_superadmin_is_allowed(client: AsyncClient, db_session: AsyncSession) -> None:
    """The gate must not have broken the endpoint for the role that needs it.

    No snapshot has been pushed, so this is the documented empty answer
    rather than a 404 — which is also what proves we got past the gate.
    """
    srv = await _server(db_session)
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/dns/servers/{srv.id}/rendered-config",
        headers={"Authorization": f"Bearer {await _superadmin(db_session)}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["files"] == []
    assert body["rendered_at"] is None
