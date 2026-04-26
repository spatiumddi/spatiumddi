"""Tests for the reservation TTL sweep task.

The Celery wrapper just calls ``asyncio.run(_sweep())``, so the tests
exercise the inner async function directly with a real DB session
fixture (mirrors the pattern in ``test_dhcp_mac_blocks`` etc).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.settings import PlatformSettings
from app.tasks.ipam_reservation_sweep import _sweep


async def _seed_subnet(db: AsyncSession) -> Subnet:
    space = IPSpace(name=f"rsv-sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.60.0.0/16", name="rsv-blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network="10.60.1.0/24",
        name="rsv-sn",
    )
    db.add(subnet)
    await db.flush()
    return subnet


@pytest.mark.asyncio
async def test_expired_reservation_released(db_session: AsyncSession) -> None:
    subnet = await _seed_subnet(db_session)
    expired_at = datetime.now(UTC) - timedelta(minutes=30)
    ip = IPAddress(
        subnet_id=subnet.id,
        address="10.60.1.10",
        status="reserved",
        reserved_until=expired_at,
        hostname="expiring-rsv",
    )
    db_session.add(ip)
    await db_session.commit()

    result = await _sweep()
    assert result["released"] == 1

    # Re-load: row should be available, TTL cleared.
    refreshed = (
        await db_session.execute(select(IPAddress).where(IPAddress.id == ip.id))
    ).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.status == "available"
    assert refreshed.reserved_until is None

    # Audit row landed for this resource.
    audits = list(
        (await db_session.execute(select(AuditLog).where(AuditLog.resource_id == str(ip.id))))
        .scalars()
        .all()
    )
    assert any(
        a.new_value and a.new_value.get("reason") == "reservation_ttl_expired" for a in audits
    )


@pytest.mark.asyncio
async def test_unexpired_reservation_left_alone(db_session: AsyncSession) -> None:
    subnet = await _seed_subnet(db_session)
    future = datetime.now(UTC) + timedelta(hours=1)
    ip = IPAddress(
        subnet_id=subnet.id,
        address="10.60.1.11",
        status="reserved",
        reserved_until=future,
        hostname="future-rsv",
    )
    db_session.add(ip)
    await db_session.commit()

    result = await _sweep()
    assert result["released"] == 0

    refreshed = (
        await db_session.execute(select(IPAddress).where(IPAddress.id == ip.id))
    ).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.status == "reserved"
    assert refreshed.reserved_until == future


@pytest.mark.asyncio
async def test_null_ttl_left_alone(db_session: AsyncSession) -> None:
    """Reserved rows without a TTL (legacy / indefinite) are not swept."""
    subnet = await _seed_subnet(db_session)
    ip = IPAddress(
        subnet_id=subnet.id,
        address="10.60.1.12",
        status="reserved",
        hostname="indefinite-rsv",
    )
    db_session.add(ip)
    await db_session.commit()

    result = await _sweep()
    assert result["released"] == 0

    refreshed = (
        await db_session.execute(select(IPAddress).where(IPAddress.id == ip.id))
    ).scalar_one()
    assert refreshed.status == "reserved"


@pytest.mark.asyncio
async def test_idempotent(db_session: AsyncSession) -> None:
    """Sweep is safe to retry — second run releases nothing."""
    subnet = await _seed_subnet(db_session)
    expired = datetime.now(UTC) - timedelta(minutes=10)
    ip = IPAddress(
        subnet_id=subnet.id,
        address="10.60.1.13",
        status="reserved",
        reserved_until=expired,
    )
    db_session.add(ip)
    await db_session.commit()

    first = await _sweep()
    second = await _sweep()
    assert first["released"] == 1
    assert second["released"] == 0


@pytest.mark.asyncio
async def test_disabled_setting_skips_sweep(db_session: AsyncSession) -> None:
    """When ``reservation_sweep_enabled=False`` the task no-ops even
    if matching rows are present."""
    settings_row = PlatformSettings(id=1, reservation_sweep_enabled=False)
    db_session.add(settings_row)
    subnet = await _seed_subnet(db_session)
    expired = datetime.now(UTC) - timedelta(minutes=30)
    ip = IPAddress(
        subnet_id=subnet.id,
        address="10.60.1.14",
        status="reserved",
        reserved_until=expired,
    )
    db_session.add(ip)
    await db_session.commit()

    result = await _sweep()
    assert result["released"] == 0
    assert result["skipped_disabled"] == 1

    # Row is still reserved.
    refreshed = (
        await db_session.execute(select(IPAddress).where(IPAddress.id == ip.id))
    ).scalar_one()
    assert refreshed.status == "reserved"


@pytest.mark.asyncio
async def test_status_change_clears_reserved_until(db_session: AsyncSession) -> None:
    """Switching status away from ``reserved`` should null the TTL.

    Exercised through the API to make sure the handler-side guard
    actually fires (otherwise a stale TTL would haunt rows whose
    operator manually marked them ``allocated`` before the sweep
    fired).
    """
    from httpx import AsyncClient  # local: avoid leaking into other tests

    from app.core.security import create_access_token, hash_password
    from app.main import app
    from app.models.auth import User

    user = User(
        username=f"rsv-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Rsv Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db_session.add(user)
    await db_session.flush()
    token = create_access_token(str(user.id))

    subnet = await _seed_subnet(db_session)
    future = datetime.now(UTC) + timedelta(hours=1)
    ip = IPAddress(
        subnet_id=subnet.id,
        address="10.60.1.15",
        status="reserved",
        reserved_until=future,
        hostname="claim-me",
    )
    db_session.add(ip)
    await db_session.commit()

    from httpx import ASGITransport

    from app.db import get_db

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.put(
                f"/api/v1/ipam/addresses/{ip.id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"status": "allocated"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["reserved_until"] is None
    finally:
        app.dependency_overrides.clear()
