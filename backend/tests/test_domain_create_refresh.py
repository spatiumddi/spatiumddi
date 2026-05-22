"""#278 — creating a domain dispatches a one-shot WHOIS refresh.

So a freshly-added domain is populated within seconds instead of sitting
empty until the next ``refresh_due_domains`` beat tick. The dispatch is
fire-and-forget: a broker error must not fail the 201.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User


async def _admin(db: AsyncSession) -> dict[str, str]:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


@pytest.mark.asyncio
async def test_create_dispatches_refresh(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.tasks.domain_whois_refresh as task_mod

    calls: list[str] = []
    monkeypatch.setattr(
        task_mod.refresh_one_domain_by_id, "delay", lambda domain_id: calls.append(domain_id)
    )

    h = await _admin(db_session)
    r = await client.post("/api/v1/domains", json={"name": "example.com"}, headers=h)
    assert r.status_code == 201, r.text
    assert calls == [r.json()["id"]]


@pytest.mark.asyncio
async def test_create_succeeds_when_dispatch_raises(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broker outage on .delay() must not fail the create — the
    scheduled sweep still picks the row up (next_check_at IS NULL first)."""
    import app.tasks.domain_whois_refresh as task_mod

    def _boom(domain_id: str) -> None:
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(task_mod.refresh_one_domain_by_id, "delay", _boom)

    h = await _admin(db_session)
    r = await client.post("/api/v1/domains", json={"name": "resilient.com"}, headers=h)
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_refresh_one_by_id_missing_domain_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one-shot task tolerates a domain deleted before it ran."""
    from app.tasks.domain_whois_refresh import _refresh_one_by_id_async

    out = await _refresh_one_by_id_async(str(uuid.uuid4()))
    assert out["status"] == "missing"
