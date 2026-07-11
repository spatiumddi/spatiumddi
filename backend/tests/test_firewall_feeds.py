"""Tests for the firewall block-list feed rendering + token auth (#606)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_str
from app.models.block_sync import NetworkBlock
from app.models.firewall_feed import FirewallFeed
from app.services.firewall_feeds.service import (
    generate_feed_token,
    render_blocklist,
    verify_feed_token,
)


def test_generate_feed_token_unique() -> None:
    a = generate_feed_token()
    b = generate_feed_token()
    assert a and b and a != b
    assert len(a) >= 20


def test_verify_feed_token() -> None:
    feed = FirewallFeed(name="f", kind="ip", token_encrypted=encrypt_str("s3cret"))
    assert verify_feed_token(feed, "s3cret") is True
    assert verify_feed_token(feed, "wrong") is False
    assert verify_feed_token(feed, "") is False
    assert verify_feed_token(feed, None) is False
    # A feed with no token never authorises.
    assert verify_feed_token(FirewallFeed(name="g", kind="ip", token_encrypted=b""), "x") is False


@pytest.mark.asyncio
async def test_render_blocklist_only_active_ip_blocks(db_session: AsyncSession) -> None:
    feed = FirewallFeed(name="feed", kind="ip", token_encrypted=encrypt_str("t"))
    db_session.add(feed)
    now = datetime.now(UTC)
    db_session.add_all(
        [
            NetworkBlock(kind="ip", value="10.0.0.9", enabled=True),
            NetworkBlock(kind="ip", value="10.0.0.8", enabled=True),
            NetworkBlock(kind="ip", value="10.0.0.7", enabled=False),  # disabled → excluded
            NetworkBlock(
                kind="ip",
                value="10.0.0.6",
                enabled=True,
                expires_at=now - timedelta(hours=1),  # expired → excluded
            ),
            NetworkBlock(kind="mac", value="aa:bb:cc:dd:ee:ff", enabled=True),  # wrong kind
        ]
    )
    await db_session.commit()

    body = await render_blocklist(db_session, feed)
    lines = body.splitlines()
    assert lines == ["10.0.0.8", "10.0.0.9"]  # sorted, active-ip only
    assert body.endswith("\n")


@pytest.mark.asyncio
async def test_render_blocklist_empty(db_session: AsyncSession) -> None:
    feed = FirewallFeed(name="empty", kind="ip", token_encrypted=encrypt_str("t"))
    db_session.add(feed)
    await db_session.commit()
    assert await render_blocklist(db_session, feed) == ""
