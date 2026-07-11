"""Firewall block-list feed rendering + token auth (#606).

Projects the ``NetworkBlock`` desired-state set (the same intent the #601 push
reconcilers converge) into the plain-text block-list a feed-polling firewall
consumes — one IP / CIDR per line. Deliberately does NOT import
``app.services.block_sync.reconcile`` (which pulls in every vendor client); the
"is this block active?" check is inlined so the feed path stays lightweight and
the unauthenticated poll endpoint has a tiny dependency surface.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_str
from app.models.block_sync import NetworkBlock
from app.models.firewall_feed import FirewallFeed


def generate_feed_token() -> str:
    """A fresh unguessable feed token (URL-safe, ~43 chars)."""
    return secrets.token_urlsafe(32)


def feed_token(feed: FirewallFeed) -> str:
    """Decrypt the feed's token for the operator reveal / URL-build flow."""
    if not feed.token_encrypted:
        return ""
    try:
        return decrypt_str(feed.token_encrypted)
    except ValueError:
        return ""


def verify_feed_token(feed: FirewallFeed, presented: str | None) -> bool:
    """Constant-time compare the token a polling firewall presented against the
    feed's stored token."""
    if not presented or not feed.token_encrypted:
        return False
    try:
        actual = decrypt_str(feed.token_encrypted)
    except ValueError:
        return False
    return bool(actual) and hmac.compare_digest(actual, presented)


async def render_blocklist(db: AsyncSession, feed: FirewallFeed) -> str:
    """Render the feed body: the active ``NetworkBlock`` values of the feed's
    kind, deduped + sorted, one per line, trailing newline."""
    now = datetime.now(UTC)
    rows = (
        (await db.execute(select(NetworkBlock).where(NetworkBlock.kind == feed.kind)))
        .scalars()
        .all()
    )
    values: set[str] = set()
    for b in rows:
        if not b.enabled:
            continue
        if b.expires_at is not None and b.expires_at <= now:
            continue
        values.add(b.value)
    if not values:
        return ""
    return "\n".join(sorted(values)) + "\n"


async def record_poll(db: AsyncSession, feed: FirewallFeed, source_ip: str | None) -> None:
    """Stamp poll telemetry so operators can confirm a firewall is consuming the
    feed. The count is bumped with an atomic ``poll_count = poll_count + 1`` so
    concurrent polls (a firewall behind NAT, or several) don't lose increments to
    a read-modify-write race. Best-effort — never blocks serving the feed."""
    await db.execute(
        update(FirewallFeed)
        .where(FirewallFeed.id == feed.id)
        .values(
            last_polled_at=datetime.now(UTC),
            last_polled_ip=source_ip,
            poll_count=FirewallFeed.poll_count + 1,
        )
    )


__all__ = [
    "feed_token",
    "generate_feed_token",
    "record_poll",
    "render_blocklist",
    "verify_feed_token",
]
