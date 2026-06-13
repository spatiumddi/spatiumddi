"""Regression: dns_zone-scoped API token must not read/enumerate/export
foreign zones (issue #400 / GHSA-mj4g-hw3m-62rm finding C3 — IDOR).

Before the fix the coarse router gate passed a dns_zone-scoped token on type
match (req_rid=None) and the single-zone READ handlers (get_zone / export_zone /
server-state / dnssec-info / delegation-preview / drift) plus the list/export-all
handlers never re-checked the per-row binding, so a token bound to zone A could
read or bulk-export ANY zone in the group. The record handlers already enforced
``_enforce_zone_token_scope``; this mirrors that for every zone READ surface.

The fix:
* ``_require_zone`` now runs ``_enforce_zone_token_scope`` when the caller is
  supplied, so every single-zone consumer inherits the per-row check.
* ``list_zones`` / ``export_all_zones`` narrow their result set to the token's
  bound zone(s) via ``_zone_token_id_filter``.
"""

from __future__ import annotations

import io
import uuid
import zipfile

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, generate_api_token, hash_password
from app.models.auth import APIToken, User
from app.models.dns import DNSServerGroup, DNSZone


async def _make_user(db: AsyncSession, *, superadmin: bool) -> tuple[User, str]:
    user = User(
        username=f"tok-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Token User",
        hashed_password=hash_password("x"),
        is_superadmin=superadmin,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_token(db: AsyncSession, owner: User, grants: list[dict] | None) -> str:
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


async def _group_with_two_zones(
    db: AsyncSession,
) -> tuple[DNSServerGroup, DNSZone, DNSZone]:
    group = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    db.add(group)
    await db.flush()
    zone_a = DNSZone(group_id=group.id, name=f"a-{uuid.uuid4().hex[:6]}.test", kind="forward")
    zone_b = DNSZone(group_id=group.id, name=f"b-{uuid.uuid4().hex[:6]}.test", kind="forward")
    db.add_all([zone_a, zone_b])
    await db.flush()
    return group, zone_a, zone_b


@pytest.mark.asyncio
async def test_zone_token_cannot_read_foreign_zone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A read grant bound to zone A reads A (200) but 403s on zone B —
    across get_zone / export_zone / server-state / dnssec / delegation-preview /
    drift (all of which route through _require_zone)."""
    owner, _ = await _make_user(db_session, superadmin=True)
    group, zone_a, zone_b = await _group_with_two_zones(db_session)
    raw = await _make_token(
        db_session,
        owner,
        [{"action": "read", "resource_type": "dns_zone", "resource_id": str(zone_a.id)}],
    )
    await db_session.commit()
    hdr = {"Authorization": f"Bearer {raw}"}
    base = f"/api/v1/dns/groups/{group.id}/zones"

    # In-scope: every single-zone read on the bound zone works.
    assert (await client.get(f"{base}/{zone_a.id}", headers=hdr)).status_code == 200
    assert (await client.get(f"{base}/{zone_a.id}/export", headers=hdr)).status_code == 200

    # Out-of-scope: same token, foreign zone → 403 on every read surface.
    for suffix in (
        "",
        "/export",
        "/server-state",
        "/dnssec/info",
        "/delegation-preview",
        "/drift",
    ):
        r = await client.get(f"{base}/{zone_b.id}{suffix}", headers=hdr)
        assert r.status_code == 403, f"{suffix or '<get_zone>'} leaked: {r.status_code} {r.text}"


@pytest.mark.asyncio
async def test_list_zones_filtered_to_bound_zone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """list_zones returns only the token's bound zone — not the whole group."""
    owner, _ = await _make_user(db_session, superadmin=True)
    group, zone_a, zone_b = await _group_with_two_zones(db_session)
    raw = await _make_token(
        db_session,
        owner,
        [{"action": "read", "resource_type": "dns_zone", "resource_id": str(zone_a.id)}],
    )
    await db_session.commit()
    hdr = {"Authorization": f"Bearer {raw}"}

    r = await client.get(f"/api/v1/dns/groups/{group.id}/zones", headers=hdr)
    assert r.status_code == 200, r.text
    returned_ids = {row["id"] for row in r.json()}
    assert returned_ids == {str(zone_a.id)}
    assert str(zone_b.id) not in returned_ids


@pytest.mark.asyncio
async def test_export_all_zones_filtered_to_bound_zone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """export_all_zones zips only the bound zone, never the foreign one."""
    owner, _ = await _make_user(db_session, superadmin=True)
    group, zone_a, zone_b = await _group_with_two_zones(db_session)
    raw = await _make_token(
        db_session,
        owner,
        [{"action": "read", "resource_type": "dns_zone", "resource_id": str(zone_a.id)}],
    )
    await db_session.commit()
    hdr = {"Authorization": f"Bearer {raw}"}

    r = await client.get(f"/api/v1/dns/groups/{group.id}/zones/export", headers=hdr)
    assert r.status_code == 200, r.text
    names = set(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert zone_a.name.rstrip(".") + ".zone" in names
    assert zone_b.name.rstrip(".") + ".zone" not in names


@pytest.mark.asyncio
async def test_unscoped_session_still_sees_all_zones(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Legitimate flow guard: a normal (non-token) superadmin session is
    unaffected — it lists both zones and reads either one."""
    _, token = await _make_user(db_session, superadmin=True)
    group, zone_a, zone_b = await _group_with_two_zones(db_session)
    await db_session.commit()
    hdr = {"Authorization": f"Bearer {token}"}
    base = f"/api/v1/dns/groups/{group.id}/zones"

    r_list = await client.get(base, headers=hdr)
    assert r_list.status_code == 200, r_list.text
    ids = {row["id"] for row in r_list.json()}
    assert {str(zone_a.id), str(zone_b.id)} <= ids

    assert (await client.get(f"{base}/{zone_a.id}", headers=hdr)).status_code == 200
    assert (await client.get(f"{base}/{zone_b.id}", headers=hdr)).status_code == 200


@pytest.mark.asyncio
async def test_write_scoped_token_can_still_read_own_zone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A write grant implies read on the bound zone (#374 write-implies-read),
    so a dns:write-scoped token bound to zone A can still GET zone A — the fix
    must not regress that legitimate path."""
    owner, _ = await _make_user(db_session, superadmin=True)
    group, zone_a, zone_b = await _group_with_two_zones(db_session)
    raw = await _make_token(
        db_session,
        owner,
        [{"action": "write", "resource_type": "dns_zone", "resource_id": str(zone_a.id)}],
    )
    await db_session.commit()
    hdr = {"Authorization": f"Bearer {raw}"}
    base = f"/api/v1/dns/groups/{group.id}/zones"

    assert (await client.get(f"{base}/{zone_a.id}", headers=hdr)).status_code == 200
    # ...but a foreign zone is still 403.
    assert (await client.get(f"{base}/{zone_b.id}", headers=hdr)).status_code == 403
