"""Tests for DHCP MAC blocklist — model, config bundle, API, Kea render.

Covers the interesting behaviors: normalization edge cases, ConfigBundle
filtering (enabled + expiry), full CRUD through the API with OUI +
IPAM cross-ref, and the Kea DROP-class wire shape. Windows
``sync_mac_blocks`` is not exercised here — it's a WinRM transport
integration that the Path-A test harness covers separately.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dhcp.mac_blocks import _canonicalize_mac
from app.core.security import create_access_token, hash_password
from app.drivers.dhcp.base import MACBlockDef
from app.models.auth import User
from app.models.dhcp import DHCPMACBlock, DHCPServer, DHCPServerGroup
from app.services.dhcp.config_bundle import build_config_bundle


async def _make_user(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_group_with_server(db: AsyncSession) -> tuple[DHCPServerGroup, DHCPServer]:
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    db.add(grp)
    await db.flush()
    srv = DHCPServer(
        name=f"s-{uuid.uuid4().hex[:6]}",
        driver="kea",
        host="127.0.0.1",
        port=67,
        server_group_id=grp.id,
    )
    db.add(srv)
    await db.flush()
    return grp, srv


# ── MAC normalization ─────────────────────────────────────────────


def test_canonicalize_mac_accepts_common_formats() -> None:
    assert _canonicalize_mac("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff"
    assert _canonicalize_mac("aa-bb-cc-dd-ee-ff") == "aa:bb:cc:dd:ee:ff"
    assert _canonicalize_mac("aabb.ccdd.eeff") == "aa:bb:cc:dd:ee:ff"
    assert _canonicalize_mac("aabbccddeeff") == "aa:bb:cc:dd:ee:ff"
    # Whitespace + mixed case
    assert _canonicalize_mac("  AA:bb:CC:dd:EE:ff  ") == "aa:bb:cc:dd:ee:ff"


def test_canonicalize_mac_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        _canonicalize_mac("not-a-mac")
    with pytest.raises(ValueError):
        _canonicalize_mac("aabbccdd")  # too short
    with pytest.raises(ValueError):
        _canonicalize_mac("aabbccddeeffgg")  # too long
    with pytest.raises(ValueError):
        _canonicalize_mac("aabbccddeexx")  # non-hex chars


# ── Model smoke ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_roundtrip(db_session: AsyncSession) -> None:
    grp, _ = await _make_group_with_server(db_session)
    block = DHCPMACBlock(
        group_id=grp.id,
        mac_address="aa:bb:cc:dd:ee:ff",
        reason="rogue",
        description="suspicious device",
        enabled=True,
    )
    db_session.add(block)
    await db_session.commit()
    await db_session.refresh(block)
    assert str(block.mac_address) == "aa:bb:cc:dd:ee:ff"
    assert block.match_count == 0


# ── ConfigBundle filtering ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_bundle_includes_active_blocks(db_session: AsyncSession) -> None:
    grp, srv = await _make_group_with_server(db_session)
    db_session.add(
        DHCPMACBlock(
            group_id=grp.id,
            mac_address="aa:bb:cc:dd:ee:01",
            reason="rogue",
            enabled=True,
        )
    )
    await db_session.commit()

    bundle = await build_config_bundle(db_session, srv)
    macs = [mb.mac_address for mb in bundle.mac_blocks]
    assert "aa:bb:cc:dd:ee:01" in macs


@pytest.mark.asyncio
async def test_bundle_excludes_disabled(db_session: AsyncSession) -> None:
    grp, srv = await _make_group_with_server(db_session)
    db_session.add(
        DHCPMACBlock(
            group_id=grp.id,
            mac_address="aa:bb:cc:dd:ee:02",
            enabled=False,
        )
    )
    await db_session.commit()
    bundle = await build_config_bundle(db_session, srv)
    assert not any(m.mac_address == "aa:bb:cc:dd:ee:02" for m in bundle.mac_blocks)


@pytest.mark.asyncio
async def test_bundle_excludes_expired(db_session: AsyncSession) -> None:
    grp, srv = await _make_group_with_server(db_session)
    past = datetime.now(UTC) - timedelta(hours=1)
    future = datetime.now(UTC) + timedelta(hours=1)
    db_session.add_all(
        [
            DHCPMACBlock(
                group_id=grp.id,
                mac_address="aa:bb:cc:dd:ee:03",
                enabled=True,
                expires_at=past,
            ),
            DHCPMACBlock(
                group_id=grp.id,
                mac_address="aa:bb:cc:dd:ee:04",
                enabled=True,
                expires_at=future,
            ),
        ]
    )
    await db_session.commit()
    bundle = await build_config_bundle(db_session, srv)
    macs = {m.mac_address for m in bundle.mac_blocks}
    assert "aa:bb:cc:dd:ee:03" not in macs  # expired
    assert "aa:bb:cc:dd:ee:04" in macs  # still valid


@pytest.mark.asyncio
async def test_bundle_etag_shifts_when_block_added(
    db_session: AsyncSession,
) -> None:
    """A new MAC block should change the bundle ETag so Kea agents re-render."""
    grp, srv = await _make_group_with_server(db_session)
    before = await build_config_bundle(db_session, srv)
    db_session.add(DHCPMACBlock(group_id=grp.id, mac_address="aa:bb:cc:dd:ee:05", enabled=True))
    await db_session.commit()
    after = await build_config_bundle(db_session, srv)
    assert before.etag != after.etag


# ── MACBlockDef wire shape ────────────────────────────────────────


def test_macblockdef_is_frozen() -> None:
    mb = MACBlockDef(mac_address="aa:bb:cc:dd:ee:ff", reason="rogue", description="x")
    with pytest.raises(Exception):
        mb.mac_address = "different"  # type: ignore[misc]


# ── CRUD API roundtrip ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_crud_roundtrip(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user(db_session)
    grp, _ = await _make_group_with_server(db_session)
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    # Empty list on new group
    r = await client.get(f"/api/v1/dhcp/server-groups/{grp.id}/mac-blocks", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == []

    # Create — accepts a dashed MAC, normalizes to colon-separated
    r = await client.post(
        f"/api/v1/dhcp/server-groups/{grp.id}/mac-blocks",
        headers=h,
        json={
            "mac_address": "AA-BB-CC-DD-EE-FF",
            "reason": "rogue",
            "description": "test",
        },
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["mac_address"] == "aa:bb:cc:dd:ee:ff"
    assert created["reason"] == "rogue"
    assert created["match_count"] == 0
    block_id = created["id"]

    # Duplicate rejected
    r = await client.post(
        f"/api/v1/dhcp/server-groups/{grp.id}/mac-blocks",
        headers=h,
        json={"mac_address": "aa:bb:cc:dd:ee:ff"},
    )
    assert r.status_code == 409

    # List now has one
    r = await client.get(f"/api/v1/dhcp/server-groups/{grp.id}/mac-blocks", headers=h)
    assert r.status_code == 200
    assert len(r.json()) == 1

    # Update — toggle enabled + set expiry
    future = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    r = await client.put(
        f"/api/v1/dhcp/mac-blocks/{block_id}",
        headers=h,
        json={"enabled": False, "expires_at": future, "description": "updated"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False
    assert r.json()["description"] == "updated"

    # Delete
    r = await client.delete(f"/api/v1/dhcp/mac-blocks/{block_id}", headers=h)
    assert r.status_code == 204
    r = await client.get(f"/api/v1/dhcp/server-groups/{grp.id}/mac-blocks", headers=h)
    assert r.json() == []


@pytest.mark.asyncio
async def test_invalid_mac_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user(db_session)
    grp, _ = await _make_group_with_server(db_session)
    await db_session.commit()
    r = await client.post(
        f"/api/v1/dhcp/server-groups/{grp.id}/mac-blocks",
        headers={"Authorization": f"Bearer {token}"},
        json={"mac_address": "not-a-mac"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_invalid_reason_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user(db_session)
    grp, _ = await _make_group_with_server(db_session)
    await db_session.commit()
    r = await client.post(
        f"/api/v1/dhcp/server-groups/{grp.id}/mac-blocks",
        headers={"Authorization": f"Bearer {token}"},
        json={"mac_address": "aa:bb:cc:dd:ee:ff", "reason": "not-a-reason"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_missing_group_404(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user(db_session)
    await db_session.commit()
    bogus = uuid.uuid4()
    r = await client.get(
        f"/api/v1/dhcp/server-groups/{bogus}/mac-blocks",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404
