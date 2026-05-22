"""#278 follow-up — creating an ASN dispatches a one-shot whois/RPKI refresh.

So a freshly-added public AS is populated within seconds instead of
sitting at whois_state "n/a" until the next refresh_due_asns beat tick.
Fire-and-forget: a broker error must not fail the 201.
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
    import app.tasks.asn_whois_refresh as task_mod

    calls: list[str] = []
    monkeypatch.setattr(
        task_mod.refresh_one_asn_by_id, "delay", lambda asn_id: calls.append(asn_id)
    )

    h = await _admin(db_session)
    r = await client.post("/api/v1/asns", json={"number": 64512}, headers=h)
    assert r.status_code == 201, r.text
    assert calls == [r.json()["id"]]


@pytest.mark.asyncio
async def test_create_succeeds_when_dispatch_raises(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.tasks.asn_whois_refresh as task_mod

    def _boom(asn_id: str) -> None:
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(task_mod.refresh_one_asn_by_id, "delay", _boom)

    h = await _admin(db_session)
    r = await client.post("/api/v1/asns", json={"number": 64513}, headers=h)
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_refresh_one_by_id_missing_asn_is_noop() -> None:
    import app.tasks.asn_whois_refresh as task_mod

    out = await task_mod._refresh_one_by_id_async(str(uuid.uuid4()))
    assert out["status"] == "missing"
