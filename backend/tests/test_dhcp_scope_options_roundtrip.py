"""#583 — DHCP scope edit form dropped DNS Servers + PXE profile.

Two independent round-trip bugs surfaced when editing an existing scope:

* Option 6 (DNS Servers) was sent by the frontend under the IANA name
  ``domain-name-servers`` while the canonical stored vocabulary is
  ``dns-servers``. The response serializer's name→code table only knew
  ``dns-servers``, so a legacy row read back as code 0 and fell into the
  hidden custom-options bucket — the DNS Servers field rendered empty and
  a save wiped it.
* ``pxe_profile_id`` was missing from ``ScopeResponse`` entirely, so the
  PXE picker always reset to "(none)" and a save silently detached the
  bound profile.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dhcp import DHCPPXEProfile, DHCPScope, DHCPServerGroup
from app.models.ipam import IPBlock, IPSpace, Subnet

CIDR = "192.0.2.0/24"


async def _make_token(db: AsyncSession) -> str:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="T",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


async def _subnet_and_group(db: AsyncSession) -> tuple[Subnet, DHCPServerGroup]:
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network=CIDR, name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=CIDR, name="s")
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add_all([subnet, grp])
    await db.flush()
    return subnet, grp


async def _create(
    client: AsyncClient, h: dict, subnet: Subnet, grp: DHCPServerGroup, **extra
) -> dict:
    r = await client.post(
        f"/api/v1/dhcp/subnets/{subnet.id}/dhcp-scopes",
        headers=h,
        json={"group_id": str(grp.id), "name": "s", **extra},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _dns_option(body: dict) -> dict | None:
    return next((o for o in body["options"] if o["code"] == 6), None)


@pytest.mark.asyncio
async def test_dns_servers_canonical_name_round_trips(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _make_token(db_session)
    subnet, grp = await _subnet_and_group(db_session)
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    body = await _create(
        client,
        h,
        subnet,
        grp,
        options=[{"code": 6, "name": "dns-servers", "value": ["10.0.0.1", "10.0.0.2"]}],
    )
    # Comes back as code 6 (not 0) so the DNS Servers field renders on edit.
    opt = _dns_option(body)
    assert opt is not None, body["options"]
    assert opt["value"] == ["10.0.0.1", "10.0.0.2"]


@pytest.mark.asyncio
async def test_dns_servers_legacy_iana_name_still_maps_to_code_6(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # Older clients (and already-persisted rows) used the IANA name; it must
    # still resolve to code 6 rather than dropping into custom options (#583).
    token = await _make_token(db_session)
    subnet, grp = await _subnet_and_group(db_session)
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    body = await _create(
        client,
        h,
        subnet,
        grp,
        options=[{"code": 6, "name": "domain-name-servers", "value": ["10.0.0.9"]}],
    )
    opt = _dns_option(body)
    assert opt is not None, body["options"]
    assert opt["value"] == ["10.0.0.9"]
    # Write-side normalisation collapses onto the canonical stored name.
    scope = await db_session.get(DHCPScope, uuid.UUID(body["id"]))
    await db_session.refresh(scope)
    assert "dns-servers" in scope.options
    assert "domain-name-servers" not in scope.options


@pytest.mark.asyncio
async def test_pxe_profile_id_round_trips(client: AsyncClient, db_session: AsyncSession) -> None:
    token = await _make_token(db_session)
    subnet, grp = await _subnet_and_group(db_session)
    profile = DHCPPXEProfile(group_id=grp.id, name="p", next_server="10.0.0.5")
    db_session.add(profile)
    await db_session.flush()
    profile_id = str(profile.id)
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    body = await _create(client, h, subnet, grp)
    # ScopeCreate ignores pxe_profile_id — bind it via update, like the UI.
    r = await client.put(
        f"/api/v1/dhcp/scopes/{body['id']}",
        headers=h,
        json={"pxe_profile_id": profile_id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["pxe_profile_id"] == profile_id

    # And a fresh GET echoes it so the edit form pre-selects the profile.
    r = await client.get(f"/api/v1/dhcp/scopes/{body['id']}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["pxe_profile_id"] == profile_id


@pytest.mark.asyncio
async def test_pxe_profile_id_null_when_unbound(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _make_token(db_session)
    subnet, grp = await _subnet_and_group(db_session)
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    body = await _create(client, h, subnet, grp)
    assert body["pxe_profile_id"] is None
