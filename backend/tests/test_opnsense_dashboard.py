"""Non-negotiable #15 (surface 2 of 2): the OPNsense integration must
appear on the Integrations dashboard tab.

Confirms ``GET /dashboards/integrations/summary`` emits a panel with
``kind="opnsense"`` and that a registered OPNsense firewall shows up as
a target row in that panel.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.ipam import IPSpace
from app.models.opnsense import OPNsenseRouter

SUMMARY = "/api/v1/dashboards/integrations/summary"


async def _admin(db: AsyncSession) -> dict:
    u = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@x.com",
        display_name="T",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(u)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(str(u.id))}"}


@pytest.mark.asyncio
async def test_opnsense_panel_present_in_summary(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _admin(db_session)
    await db_session.commit()

    r = await client.get(SUMMARY, headers=headers)
    assert r.status_code == 200, r.text
    panels = {p["kind"] for p in r.json()["panels"]}
    assert "opnsense" in panels


@pytest.mark.asyncio
async def test_registered_firewall_shows_as_target(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _admin(db_session)
    space = IPSpace(name=f"opn-dash-{uuid.uuid4().hex[:6]}", description="")
    db_session.add(space)
    await db_session.flush()
    fw = OPNsenseRouter(
        name=f"fw-{uuid.uuid4().hex[:6]}",
        host="fw.test",
        api_key="KEY",
        ipam_space_id=space.id,
    )
    db_session.add(fw)
    await db_session.commit()

    r = await client.get(SUMMARY, headers=headers)
    assert r.status_code == 200, r.text
    opn_panel = next(p for p in r.json()["panels"] if p["kind"] == "opnsense")
    assert opn_panel["label"] == "OPNsense"
    names = {t["display"] for t in opn_panel["targets"]}
    assert fw.name in names
