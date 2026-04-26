"""Tests for DHCP lease history.

Covers the three write paths (pull-leases absence-delete,
time-based cleanup expiry, MAC supersede) plus the list endpoint
filters and the daily prune task. All tests run against a real
Postgres so the INET / MACADDR column types behave authentically.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dhcp import DHCPLease, DHCPLeaseHistory, DHCPServer, DHCPServerGroup
from app.models.settings import PlatformSettings
from app.services.dhcp.lease_history import record_lease_history


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


async def _make_lease(
    db: AsyncSession,
    server: DHCPServer,
    *,
    ip: str = "10.0.0.5",
    mac: str = "aa:bb:cc:dd:ee:ff",
) -> DHCPLease:
    lease = DHCPLease(
        server_id=server.id,
        ip_address=ip,
        mac_address=mac,
        hostname="host01",
        state="active",
    )
    db.add(lease)
    await db.flush()
    return lease


# ── Helper roundtrip ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_lease_history_basic(db_session: AsyncSession) -> None:
    _, srv = await _make_group_with_server(db_session)
    lease = await _make_lease(db_session, srv)

    row = record_lease_history(db_session, lease, lease_state="expired")
    await db_session.commit()
    await db_session.refresh(row)

    assert row.lease_state == "expired"
    assert str(row.ip_address) == "10.0.0.5"
    assert str(row.mac_address) == "aa:bb:cc:dd:ee:ff"
    assert row.expired_at is not None


@pytest.mark.asyncio
async def test_record_lease_history_mac_override(db_session: AsyncSession) -> None:
    """Supersede branch should preserve the OLD mac on the history row."""
    _, srv = await _make_group_with_server(db_session)
    lease = await _make_lease(db_session, srv, mac="aa:bb:cc:dd:ee:01")
    # Simulate the in-place update: caller has already rewritten the
    # active row, but history should record the old MAC.
    row = record_lease_history(
        db_session,
        lease,
        lease_state="superseded",
        mac_override="aa:bb:cc:dd:ee:99",
    )
    await db_session.commit()
    await db_session.refresh(row)
    assert str(row.mac_address) == "aa:bb:cc:dd:ee:99"
    assert row.lease_state == "superseded"


# ── Time-based cleanup writes history ────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_writes_expired_history(db_session: AsyncSession) -> None:
    from app.tasks.dhcp_lease_cleanup import _sweep

    _, srv = await _make_group_with_server(db_session)
    past = datetime.now(UTC) - timedelta(hours=1)
    lease = DHCPLease(
        server_id=srv.id,
        ip_address="10.0.0.6",
        mac_address="aa:bb:cc:dd:ee:02",
        hostname="cleanup-test",
        state="active",
        expires_at=past,
    )
    db_session.add(lease)
    await db_session.commit()

    # Sweep opens its own session; commit and let it run.
    await _sweep()

    rows = (
        (
            await db_session.execute(
                select(DHCPLeaseHistory).where(DHCPLeaseHistory.server_id == srv.id)
            )
        )
        .scalars()
        .all()
    )
    states = [r.lease_state for r in rows]
    assert "expired" in states


# ── pull_leases supersede + remove ───────────────────────────────────


class _StubDriver:
    """Minimal driver stub for pull_leases — only get_leases is used."""

    def __init__(self, leases: list[dict]) -> None:
        self._leases = leases

    async def get_leases(self, _server: DHCPServer) -> list[dict]:
        return self._leases


@pytest.mark.asyncio
async def test_pull_leases_records_supersede(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.dhcp import pull_leases as pl

    _, srv = await _make_group_with_server(db_session)
    # windows_dhcp is the only registered agentless driver today; flip
    # the row so pull_leases doesn't bail on the agentless guard.
    srv.driver = "windows_dhcp"
    await _make_lease(db_session, srv, ip="10.0.0.7", mac="aa:bb:cc:dd:ee:10")
    await db_session.commit()

    new_leases = [
        {
            "ip_address": "10.0.0.7",
            "mac_address": "aa:bb:cc:dd:ee:99",  # different MAC
            "hostname": "host01",
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        }
    ]
    monkeypatch.setattr(pl, "get_driver", lambda _drv: _StubDriver(new_leases))
    monkeypatch.setattr(pl, "is_agentless", lambda _drv: True)
    # Skip DDNS — service has its own dependencies we don't want here.
    import app.services.dns.ddns as ddns

    async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(ddns, "apply_ddns_for_lease", _noop)

    await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(DHCPLeaseHistory).where(DHCPLeaseHistory.server_id == srv.id)
            )
        )
        .scalars()
        .all()
    )
    states = [r.lease_state for r in rows]
    assert "superseded" in states
    superseded = next(r for r in rows if r.lease_state == "superseded")
    assert str(superseded.mac_address) == "aa:bb:cc:dd:ee:10"


@pytest.mark.asyncio
async def test_pull_leases_records_remove(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.dhcp import pull_leases as pl

    _, srv = await _make_group_with_server(db_session)
    srv.driver = "windows_dhcp"
    await _make_lease(db_session, srv, ip="10.0.0.8", mac="aa:bb:cc:dd:ee:20")
    await db_session.commit()

    monkeypatch.setattr(pl, "get_driver", lambda _drv: _StubDriver([]))
    monkeypatch.setattr(pl, "is_agentless", lambda _drv: True)
    import app.services.dns.ddns as ddns

    async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(ddns, "apply_ddns_for_lease", _noop)

    await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(DHCPLeaseHistory).where(DHCPLeaseHistory.server_id == srv.id)
            )
        )
        .scalars()
        .all()
    )
    states = [r.lease_state for r in rows]
    assert "removed" in states


# ── HTTP list endpoint ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_endpoint_paginates_and_filters(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session)
    _, srv = await _make_group_with_server(db_session)
    now = datetime.now(UTC)
    db_session.add_all(
        [
            DHCPLeaseHistory(
                server_id=srv.id,
                ip_address="10.0.1.5",
                mac_address="aa:bb:cc:00:00:01",
                hostname="alpha",
                expired_at=now - timedelta(minutes=5),
                lease_state="expired",
            ),
            DHCPLeaseHistory(
                server_id=srv.id,
                ip_address="10.0.1.6",
                mac_address="aa:bb:cc:00:00:02",
                hostname="beta",
                expired_at=now - timedelta(minutes=10),
                lease_state="removed",
            ),
            DHCPLeaseHistory(
                server_id=srv.id,
                ip_address="10.0.1.7",
                mac_address="aa:bb:cc:00:00:03",
                hostname="alpha-2",
                expired_at=now - timedelta(minutes=15),
                lease_state="superseded",
            ),
        ]
    )
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    # Unfiltered — returns 3, ordered by expired_at DESC.
    r = await client.get(f"/api/v1/dhcp/servers/{srv.id}/lease-history", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert [row["lease_state"] for row in body["items"]] == ["expired", "removed", "superseded"]

    # Filter by state.
    r = await client.get(
        f"/api/v1/dhcp/servers/{srv.id}/lease-history?lease_state=removed",
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["total"] == 1

    # Hostname substring.
    r = await client.get(
        f"/api/v1/dhcp/servers/{srv.id}/lease-history?hostname=alpha",
        headers=h,
    )
    assert r.json()["total"] == 2

    # Bad state → 422.
    r = await client.get(
        f"/api/v1/dhcp/servers/{srv.id}/lease-history?lease_state=bogus",
        headers=h,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_list_endpoint_404_unknown_server(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session)
    await db_session.commit()
    bogus = uuid.uuid4()
    r = await client.get(
        f"/api/v1/dhcp/servers/{bogus}/lease-history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


# ── Prune task ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_drops_old_rows(db_session: AsyncSession) -> None:
    from app.tasks.dhcp_lease_history_prune import _prune

    _, srv = await _make_group_with_server(db_session)
    settings = PlatformSettings(id=1, dhcp_lease_history_retention_days=7)
    db_session.add(settings)
    now = datetime.now(UTC)
    db_session.add_all(
        [
            DHCPLeaseHistory(
                server_id=srv.id,
                ip_address="10.0.2.1",
                mac_address="aa:bb:cc:00:00:11",
                expired_at=now - timedelta(days=10),  # past retention
                lease_state="expired",
            ),
            DHCPLeaseHistory(
                server_id=srv.id,
                ip_address="10.0.2.2",
                mac_address="aa:bb:cc:00:00:12",
                expired_at=now - timedelta(days=3),  # within retention
                lease_state="expired",
            ),
        ]
    )
    await db_session.commit()

    removed = await _prune()
    assert removed == 1
    remaining = (
        (
            await db_session.execute(
                select(DHCPLeaseHistory).where(DHCPLeaseHistory.server_id == srv.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(remaining) == 1
    assert str(remaining[0].ip_address) == "10.0.2.2"


@pytest.mark.asyncio
async def test_prune_disabled_when_zero(db_session: AsyncSession) -> None:
    from app.tasks.dhcp_lease_history_prune import _prune

    _, srv = await _make_group_with_server(db_session)
    settings = PlatformSettings(id=1, dhcp_lease_history_retention_days=0)
    db_session.add(settings)
    db_session.add(
        DHCPLeaseHistory(
            server_id=srv.id,
            ip_address="10.0.2.3",
            mac_address="aa:bb:cc:00:00:13",
            expired_at=datetime.now(UTC) - timedelta(days=365),
            lease_state="expired",
        )
    )
    await db_session.commit()

    removed = await _prune()
    assert removed == 0
    remaining = (
        (
            await db_session.execute(
                select(DHCPLeaseHistory).where(DHCPLeaseHistory.server_id == srv.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(remaining) == 1
