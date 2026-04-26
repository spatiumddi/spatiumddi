"""Tests for the log retention sweep."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import DHCPServer
from app.models.dns import DNSServer, DNSServerGroup
from app.models.logs import DHCPLogEntry, DNSQueryLogEntry
from app.tasks.prune_logs import DEFAULT_RETENTION_HOURS, _sweep_with_session


async def _make_dns_server(db: AsyncSession) -> DNSServer:
    grp = DNSServerGroup(name=f"grp-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    s = DNSServer(
        group_id=grp.id,
        name=f"dns-{uuid.uuid4().hex[:6]}",
        host="127.0.0.1",
        driver="bind9",
    )
    db.add(s)
    await db.flush()
    return s


async def _make_dhcp_server(db: AsyncSession) -> DHCPServer:
    s = DHCPServer(
        name=f"dhcp-{uuid.uuid4().hex[:6]}",
        host="127.0.0.1",
        driver="kea",
    )
    db.add(s)
    await db.flush()
    return s


@pytest.mark.asyncio
async def test_prune_drops_old_dns_keeps_recent(db_session: AsyncSession) -> None:
    server = await _make_dns_server(db_session)
    now = datetime.now(UTC)
    # One row well past the cutoff, one inside the window.
    db_session.add(
        DNSQueryLogEntry(
            server_id=server.id,
            ts=now - timedelta(hours=DEFAULT_RETENTION_HOURS + 5),
            qname="old.example",
            raw="old",
        )
    )
    db_session.add(
        DNSQueryLogEntry(
            server_id=server.id,
            ts=now - timedelta(minutes=10),
            qname="new.example",
            raw="new",
        )
    )
    await db_session.commit()

    result = await _sweep_with_session(db_session)
    assert result["dns_query_log_removed"] >= 1
    assert result["retention_hours"] == DEFAULT_RETENTION_HOURS

    remaining = (
        await db_session.scalar(
            select(func.count())
            .select_from(DNSQueryLogEntry)
            .where(DNSQueryLogEntry.server_id == server.id)
        )
        or 0
    )
    assert remaining == 1


@pytest.mark.asyncio
async def test_prune_drops_old_dhcp_keeps_recent(db_session: AsyncSession) -> None:
    server = await _make_dhcp_server(db_session)
    now = datetime.now(UTC)
    db_session.add(
        DHCPLogEntry(
            server_id=server.id,
            ts=now - timedelta(hours=DEFAULT_RETENTION_HOURS + 1),
            severity="INFO",
            code="DHCP4_LEASE_ALLOC",
            raw="old",
        )
    )
    db_session.add(
        DHCPLogEntry(
            server_id=server.id,
            ts=now - timedelta(minutes=5),
            severity="INFO",
            code="DHCP4_LEASE_ALLOC",
            raw="new",
        )
    )
    await db_session.commit()

    result = await _sweep_with_session(db_session)
    assert result["dhcp_log_removed"] >= 1

    remaining = (
        await db_session.scalar(
            select(func.count())
            .select_from(DHCPLogEntry)
            .where(DHCPLogEntry.server_id == server.id)
        )
        or 0
    )
    assert remaining == 1
