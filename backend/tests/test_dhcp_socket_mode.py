"""DHCP socket mode — Kea dhcp-socket-type render + group plumbing (#365).

``DHCPServerGroup.dhcp_socket_mode`` ("direct"/"relay") drives Kea's
``Dhcp4.interfaces-config.dhcp-socket-type`` ("raw"/"udp"). "direct"
(raw) is the default so Kea hears broadcast DISCOVERs from directly-
attached clients; "relay" (udp) is for relay-only deployments.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.drivers.dhcp.base import ConfigBundle, PoolDef, ScopeDef, ServerOptionsDef
from app.drivers.dhcp.kea import KeaDriver
from app.models.auth import User
from app.models.dhcp import DHCPServer, DHCPServerGroup
from app.services.dhcp.config_bundle import build_config_bundle

V4_CIDR = "10.30.0.0/24"
V6_CIDR = "2001:db8:365::/64"


def _bundle(socket_type: str = "raw", *, v6: bool = False) -> ConfigBundle:
    scope = (
        ScopeDef(
            subnet_cidr=V6_CIDR,
            address_family="ipv6",
            pools=(PoolDef(start_ip="2001:db8:365::100", end_ip="2001:db8:365::1ff"),),
        )
        if v6
        else ScopeDef(
            subnet_cidr=V4_CIDR,
            pools=(PoolDef(start_ip="10.30.0.100", end_ip="10.30.0.200"),),
        )
    )
    return ConfigBundle(
        server_id="00000000-0000-0000-0000-000000000000",
        server_name="kea-socket-test",
        driver="kea",
        roles=(),
        options=ServerOptionsDef(options={}, lease_time=3600),
        scopes=(scope,),
        client_classes=(),
        generated_at=datetime.now(UTC),
        dhcp_socket_type=socket_type,
    )


# ── Kea driver render ────────────────────────────────────────────────────


def test_render_raw_socket() -> None:
    cfg = json.loads(KeaDriver().render_config(_bundle("raw")))
    assert cfg["Dhcp4"]["interfaces-config"]["dhcp-socket-type"] == "raw"


def test_render_udp_socket() -> None:
    cfg = json.loads(KeaDriver().render_config(_bundle("udp")))
    assert cfg["Dhcp4"]["interfaces-config"]["dhcp-socket-type"] == "udp"


def test_bundle_default_socket_type_is_raw() -> None:
    # A bundle built without an explicit socket type (older control plane,
    # groupless server) defaults to raw so directly-attached clients work.
    bundle = ConfigBundle(
        server_id="0" * 8,
        server_name="x",
        driver="kea",
        roles=(),
        options=ServerOptionsDef(options={}, lease_time=3600),
        scopes=(ScopeDef(subnet_cidr=V4_CIDR),),
        client_classes=(),
        generated_at=datetime.now(UTC),
    )
    assert bundle.dhcp_socket_type == "raw"
    cfg = json.loads(KeaDriver().render_config(bundle))
    assert cfg["Dhcp4"]["interfaces-config"]["dhcp-socket-type"] == "raw"


def test_v6_has_no_socket_type() -> None:
    # dhcp-socket-type is a Dhcp4-only concept; the Dhcp6 daemon must not
    # carry it (Kea rejects the config otherwise).
    cfg = json.loads(KeaDriver().render_config(_bundle("raw", v6=True)))
    assert "Dhcp4" not in cfg
    assert "dhcp-socket-type" not in cfg["Dhcp6"]["interfaces-config"]


def test_socket_type_shifts_etag() -> None:
    assert _bundle("raw").compute_etag() != _bundle("udp").compute_etag()


# ── build_config_bundle: group mode → socket type ───────────────────────


async def _group_with_server(db: AsyncSession, socket_mode: str) -> DHCPServer:
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:8]}", dhcp_socket_mode=socket_mode)
    db.add(grp)
    await db.flush()
    srv = DHCPServer(
        name=f"kea-{uuid.uuid4().hex[:6]}",
        driver="kea",
        host="127.0.0.1",
        port=67,
        server_group_id=grp.id,
    )
    db.add(srv)
    await db.flush()
    return srv


async def test_direct_group_renders_raw(db_session: AsyncSession) -> None:
    srv = await _group_with_server(db_session, "direct")
    bundle = await build_config_bundle(db_session, srv)
    assert bundle.dhcp_socket_type == "raw"


async def test_relay_group_renders_udp(db_session: AsyncSession) -> None:
    srv = await _group_with_server(db_session, "relay")
    bundle = await build_config_bundle(db_session, srv)
    assert bundle.dhcp_socket_type == "udp"


# ── Group API round-trip ────────────────────────────────────────────────


async def _superadmin(db: AsyncSession) -> str:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


async def test_group_api_persists_socket_mode(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _superadmin(db_session)
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    # Default on create is "direct".
    r = await client.post(
        "/api/v1/dhcp/server-groups",
        headers=h,
        json={"name": f"g-{uuid.uuid4().hex[:6]}"},
    )
    assert r.status_code == 201, r.text
    gid = r.json()["id"]
    assert r.json()["dhcp_socket_mode"] == "direct"

    # PUT to relay round-trips.
    r = await client.put(
        f"/api/v1/dhcp/server-groups/{gid}",
        headers=h,
        json={"dhcp_socket_mode": "relay"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["dhcp_socket_mode"] == "relay"


async def test_group_api_rejects_bad_socket_mode(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _superadmin(db_session)
    await db_session.commit()
    r = await client.post(
        "/api/v1/dhcp/server-groups",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": f"g-{uuid.uuid4().hex[:6]}", "dhcp_socket_mode": "bogus"},
    )
    assert r.status_code == 422, r.text
