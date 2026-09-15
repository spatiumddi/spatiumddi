"""spatiumddi#1065 — a subnet-level DDNS opt-in takes effect.

``ddns_enabled`` on the subnet row is only read when ``ddns_inherit_settings``
is false; the subnet form sends the flag and never the toggle, so a subnet
switched on through the form (or through a PUT of its GET body with the flag
flipped) was stored with inheritance still on and published nothing — live on
nightly-2026.09.13: lease-events 200, no ``ddns_applied``, PTR NXDOMAIN.

Now an opt-in on the request clears the family's inheritance, on create and
on update, and the response shows it; a ``false`` is not an opt-in, and
``{"ddns_inherit_settings": true}`` on its own still re-inherits.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.ipam import IPBlock, IPSpace, Subnet
from app.services.dns.ddns import resolve_effective_ddns


async def _admin_headers(db: AsyncSession) -> dict[str, str]:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="admin",
        hashed_password=hash_password("password123"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _space_block(db: AsyncSession) -> tuple[str, str]:
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:8]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.0.0.0/8", name="b")
    db.add(block)
    await db.commit()
    return str(space.id), str(block.id)


async def _create(
    client: AsyncClient, headers: dict[str, str], network: str, **extra: object
) -> dict:
    space_id, block_id = extra.pop("_ids")  # type: ignore[misc]
    resp = await client.post(
        "/api/v1/ipam/subnets",
        headers=headers,
        json={"space_id": space_id, "block_id": block_id, "network": network, **extra},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_create_with_ddns_enabled_turns_inheritance_off(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _admin_headers(db_session)
    ids = await _space_block(db_session)

    body = await _create(client, headers, "10.65.1.0/24", _ids=ids, ddns_enabled=True)
    assert body["ddns_enabled"] is True
    assert body["ddns_inherit_settings"] is False

    got = await client.get(f"/api/v1/ipam/subnets/{body['id']}", headers=headers)
    assert got.status_code == 200
    assert got.json()["ddns_inherit_settings"] is False

    # The knob is the effective one now: resolve_effective_ddns stops at the subnet.
    subnet = await db_session.get(Subnet, uuid.UUID(body["id"]))
    assert subnet is not None
    eff = await resolve_effective_ddns(db_session, subnet)
    assert eff.enabled is True and eff.source == "subnet"


@pytest.mark.asyncio
async def test_create_without_the_optin_keeps_inheriting(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _admin_headers(db_session)
    ids = await _space_block(db_session)
    body = await _create(client, headers, "10.65.2.0/24", _ids=ids)
    assert body["ddns_enabled"] is False
    assert body["ddns_inherit_settings"] is True
    off = await _create(client, headers, "10.65.3.0/24", _ids=ids, ddns_enabled=False)
    assert off["ddns_inherit_settings"] is True


@pytest.mark.asyncio
async def test_put_of_the_forms_body_turns_inheritance_off(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The subnet form sends the flag and the policy and nothing about
    inheriting — the body that was stored and ignored."""
    headers = await _admin_headers(db_session)
    ids = await _space_block(db_session)
    body = await _create(client, headers, "10.65.4.0/24", _ids=ids)
    assert body["ddns_inherit_settings"] is True

    resp = await client.put(
        f"/api/v1/ipam/subnets/{body['id']}",
        headers=headers,
        json={"ddns_enabled": True, "ddns_hostname_policy": "client_or_generated"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ddns_enabled"] is True
    assert resp.json()["ddns_inherit_settings"] is False

    got = await client.get(f"/api/v1/ipam/subnets/{body['id']}", headers=headers)
    assert got.json()["ddns_inherit_settings"] is False


@pytest.mark.asyncio
async def test_put_of_a_get_body_with_the_flag_flipped_turns_inheritance_off(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The reporter's drill A: GET, flip ddns_enabled, PUT the whole body —
    which carries ``ddns_inherit_settings: true``. The opt-in wins and the
    response says so; nothing is stored inert."""
    headers = await _admin_headers(db_session)
    ids = await _space_block(db_session)
    body = await _create(client, headers, "10.65.5.0/24", _ids=ids)
    got = (await client.get(f"/api/v1/ipam/subnets/{body['id']}", headers=headers)).json()
    assert got["ddns_inherit_settings"] is True

    resp = await client.put(
        f"/api/v1/ipam/subnets/{body['id']}",
        headers=headers,
        json={**got, "ddns_enabled": True, "ddns_hostname_policy": "client_or_generated"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ddns_enabled"] is True
    assert resp.json()["ddns_inherit_settings"] is False


@pytest.mark.asyncio
async def test_a_false_is_not_an_optin_and_the_toggle_alone_reinherits(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _admin_headers(db_session)
    ids = await _space_block(db_session)
    body = await _create(client, headers, "10.65.6.0/24", _ids=ids)

    # A subnet that inherits and is saved with the flag off keeps inheriting —
    # the form sends ``ddns_enabled: false`` on every unrelated save.
    resp = await client.put(
        f"/api/v1/ipam/subnets/{body['id']}",
        headers=headers,
        json={"ddns_enabled": False, "description": "unrelated edit"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ddns_inherit_settings"] is True

    # Opt in, then hand control back to the block/space explicitly.
    resp = await client.put(
        f"/api/v1/ipam/subnets/{body['id']}", headers=headers, json={"ddns_enabled": True}
    )
    assert resp.json()["ddns_inherit_settings"] is False
    resp = await client.put(
        f"/api/v1/ipam/subnets/{body['id']}",
        headers=headers,
        json={"ddns_inherit_settings": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ddns_inherit_settings"] is True
