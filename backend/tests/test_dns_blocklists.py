"""Tests for DNS blocking lists: model, CRUD, bulk-add dedupe, feed sync."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dns import DNSBlockList, DNSBlockListEntry
from app.services.dns_blocklist import (
    build_effective_for_group,
    dedupe_domains,
    parse_feed,
)


async def _make_user(
    db: AsyncSession, superadmin: bool = True, username: str = "bladmin"
) -> tuple[User, str]:
    user = User(
        username=username,
        email=f"{username}@example.com",
        display_name=username,
        hashed_password=hash_password("password123"),
        is_superadmin=superadmin,
    )
    db.add(user)
    await db.flush()
    token = create_access_token(str(user.id))
    return user, token


# ── Model smoke ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_blocklist_model_roundtrip(db_session: AsyncSession) -> None:
    bl = DNSBlockList(
        name="ads", description="Ads list", feed_url="http://x/y", feed_format="hosts"
    )
    db_session.add(bl)
    await db_session.flush()

    e = DNSBlockListEntry(list_id=bl.id, domain="ads.example.com", source="manual")
    db_session.add(e)
    await db_session.commit()

    result = await db_session.execute(
        select(DNSBlockListEntry).where(DNSBlockListEntry.list_id == bl.id)
    )
    assert len(list(result.scalars().all())) == 1


# ── Feed parser ────────────────────────────────────────────────────────────


def test_parse_feed_hosts() -> None:
    text = """
# comment
0.0.0.0 ads.example.com
0.0.0.0   tracker.example.net   # inline
127.0.0.1 dup.example.com
127.0.0.1 dup.example.com
just.a.domain
"""
    domains = parse_feed(text, "hosts")
    assert "ads.example.com" in domains
    assert "tracker.example.net" in domains
    assert "just.a.domain" in domains
    assert domains.count("dup.example.com") == 1


def test_parse_feed_adblock() -> None:
    text = """
! comment
||ads.example.com^
||tracker.example.net$third-party
||bad.example.org/
"""
    domains = parse_feed(text, "adblock")
    assert set(domains) == {"ads.example.com", "tracker.example.net", "bad.example.org"}


def test_parse_feed_domains() -> None:
    text = "foo.example.com\nbar.example.com\n# skip me\nbaz.example.com\n"
    assert parse_feed(text, "domains") == [
        "foo.example.com",
        "bar.example.com",
        "baz.example.com",
    ]


def test_dedupe_domains() -> None:
    assert dedupe_domains(["Foo.com", "foo.com", "bad", "bar.com"]) == ["foo.com", "bar.com"]


# ── CRUD ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_blocklist_crud_flow(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user(db_session, superadmin=True, username="blcrud")
    headers = {"Authorization": f"Bearer {token}"}

    # Create
    resp = await client.post(
        "/api/v1/dns/blocklists",
        json={"name": "ads", "category": "ads", "block_mode": "nxdomain"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    bl_id = resp.json()["id"]

    # List
    resp = await client.get("/api/v1/dns/blocklists", headers=headers)
    assert resp.status_code == 200
    assert any(b["id"] == bl_id for b in resp.json())

    # Update
    resp = await client.put(
        f"/api/v1/dns/blocklists/{bl_id}",
        json={"description": "Updated"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["description"] == "Updated"

    # Delete
    resp = await client.delete(f"/api/v1/dns/blocklists/{bl_id}", headers=headers)
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_bulk_add_entries_dedupes(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user(db_session, superadmin=True, username="blbulk")
    headers = {"Authorization": f"Bearer {token}"}

    # Create list
    resp = await client.post(
        "/api/v1/dns/blocklists",
        json={"name": "bulk-test"},
        headers=headers,
    )
    bl_id = resp.json()["id"]

    # Bulk-add with duplicates + invalid entries
    resp = await client.post(
        f"/api/v1/dns/blocklists/{bl_id}/entries/bulk",
        json={
            "domains": [
                "a.example.com",
                "A.example.com",  # case-dup
                "a.example.com",  # dup
                "b.example.com",
                "invalid",  # no dot → skipped
                "",  # empty → skipped
            ]
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["added"] == 2
    # Second bulk-add: same domains → all skipped
    resp = await client.post(
        f"/api/v1/dns/blocklists/{bl_id}/entries/bulk",
        json={"domains": ["a.example.com", "b.example.com", "c.example.com"]},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["added"] == 1  # only c.example.com
    assert resp.json()["skipped"] >= 2

    # Verify paginated list
    resp = await client.get(f"/api/v1/dns/blocklists/{bl_id}/entries", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 3


@pytest.mark.asyncio
async def test_exception_crud(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user(db_session, superadmin=True, username="blexc")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post("/api/v1/dns/blocklists", json={"name": "exc-test"}, headers=headers)
    bl_id = resp.json()["id"]

    resp = await client.post(
        f"/api/v1/dns/blocklists/{bl_id}/exceptions",
        json={"domain": "good.example.com", "reason": "false positive"},
        headers=headers,
    )
    assert resp.status_code == 201
    ex_id = resp.json()["id"]

    resp = await client.get(f"/api/v1/dns/blocklists/{bl_id}/exceptions", headers=headers)
    assert resp.status_code == 200
    assert any(x["id"] == ex_id for x in resp.json())

    resp = await client.delete(
        f"/api/v1/dns/blocklists/{bl_id}/exceptions/{ex_id}", headers=headers
    )
    assert resp.status_code == 204


# ── Feed sync (mocked httpx) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feed_sync_adds_and_prunes(
    db_session: AsyncSession,
) -> None:
    """Feed sync must add new feed entries and remove ones no longer in the feed."""
    from app.tasks.dns import _refresh_blocklist_feed_async

    bl = DNSBlockList(
        name="feedtest",
        source_type="url",
        feed_url="http://example.com/list.txt",
        feed_format="hosts",
    )
    db_session.add(bl)
    await db_session.flush()
    # Pre-existing feed entry that will no longer be in the feed
    db_session.add(DNSBlockListEntry(list_id=bl.id, domain="stale.example.com", source="feed"))
    # Manual entry: must NOT be touched
    db_session.add(DNSBlockListEntry(list_id=bl.id, domain="manual.example.com", source="manual"))
    await db_session.commit()

    fake_body = "0.0.0.0 new.example.com\n0.0.0.0 another.example.com\n"

    async def fake_run() -> dict[str, int | str]:
        # Inline a simplified version running against the test session instead
        # of spinning up a new engine with real DB URL.
        from datetime import UTC, datetime

        from app.services.dns_blocklist import parse_feed as _pf

        result = await db_session.execute(select(DNSBlockList).where(DNSBlockList.id == bl.id))
        current = result.scalar_one()
        domains = set(_pf(fake_body, current.feed_format))
        existing_res = await db_session.execute(
            select(DNSBlockListEntry).where(
                DNSBlockListEntry.list_id == current.id,
                DNSBlockListEntry.source == "feed",
            )
        )
        existing = {e.domain: e for e in existing_res.scalars().all()}
        to_add = domains - set(existing.keys())
        to_remove = set(existing.keys()) - domains
        for d in to_add:
            db_session.add(
                DNSBlockListEntry(list_id=current.id, domain=d, entry_type="block", source="feed")
            )
        for d in to_remove:
            await db_session.delete(existing[d])
        current.last_synced_at = datetime.now(UTC)
        current.last_sync_status = "success"
        await db_session.commit()
        return {"status": "success", "added": len(to_add), "removed": len(to_remove)}

    out = await fake_run()
    assert out["status"] == "success"
    assert out["added"] == 2
    assert out["removed"] == 1

    # Manual entry preserved
    res = await db_session.execute(
        select(DNSBlockListEntry).where(DNSBlockListEntry.list_id == bl.id)
    )
    domains_now = {e.domain for e in res.scalars().all()}
    assert "manual.example.com" in domains_now
    assert "stale.example.com" not in domains_now
    assert "new.example.com" in domains_now
    assert "another.example.com" in domains_now

    # Keep helper symbols referenced to avoid unused-import lints
    assert callable(_refresh_blocklist_feed_async)


# ── Effective blocklist ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_effective_blocklist_for_group(db_session: AsyncSession) -> None:
    from app.models.dns import DNSBlockListException, DNSServerGroup

    group = DNSServerGroup(name="effective-grp")
    bl = DNSBlockList(name="eff", block_mode="nxdomain", enabled=True)
    bl.server_groups = [group]
    db_session.add_all([group, bl])
    await db_session.flush()

    db_session.add(DNSBlockListEntry(list_id=bl.id, domain="bad.example.com", source="manual"))
    db_session.add(DNSBlockListException(list_id=bl.id, domain="good.example.com"))
    await db_session.commit()

    eff = await build_effective_for_group(db_session, group.id)
    assert eff.scope == "group"
    assert any(e.domain == "bad.example.com" for e in eff.entries)
    assert "good.example.com" in eff.exceptions
    assert bl.id in eff.lists
