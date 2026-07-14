"""Kea lease cache — cache-threshold / cache-max-age plumbing (#637).

Kea 3.0 (Alpine 3.23) turns lease caching ON by default (``cache-threshold``
0.25): a client re-requesting a lease with more than 75% of its lifetime left
gets the same lease back, with an unchanged expiry and NO lease-database write.

That silently starves SpatiumDDI's lease pipeline, which is driven by memfile
CSV writes (the agent tails the lease file → lease-events → DDNS + the IPAM
lease mirror). So the control plane renders the value explicitly rather than
inheriting Kea's default, and defaults it to **0.0 (disabled)** — the pre-3.0
write-through behaviour — with an opt-in per group and a per-scope override.

The subtle invariant these tests exist to protect: **0.0 is a real value**, not
an absent one. Every layer must distinguish "0.0 → caching explicitly off" from
"None → inherit". A truthiness check anywhere in the chain silently converts an
explicit disable into an inherit.
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

V4_CIDR = "10.37.0.0/24"


def _bundle(
    *,
    threshold: float = 0.0,
    max_age: int | None = None,
    scope_threshold: float | None = None,
    scope_max_age: int | None = None,
) -> ConfigBundle:
    return ConfigBundle(
        server_id="00000000-0000-0000-0000-000000000000",
        server_name="kea-lease-cache-test",
        driver="kea",
        roles=(),
        options=ServerOptionsDef(options={}, lease_time=3600),
        scopes=(
            ScopeDef(
                subnet_cidr=V4_CIDR,
                pools=(PoolDef(start_ip="10.37.0.100", end_ip="10.37.0.200"),),
                lease_cache_threshold=scope_threshold,
                lease_cache_max_age=scope_max_age,
            ),
        ),
        client_classes=(),
        generated_at=datetime.now(UTC),
        lease_cache_threshold=threshold,
        lease_cache_max_age=max_age,
    )


# ── Defaults ────────────────────────────────────────────────────────────────


def test_bundle_default_is_caching_disabled() -> None:
    """The whole point of #637: upgrading to Kea 3.0 must NOT silently enable
    caching. A bundle built without an explicit setting renders 0.0."""
    assert _bundle().lease_cache_threshold == 0.0


def test_default_renders_explicit_zero_not_absent() -> None:
    """``cache-threshold`` must be PRESENT and 0.0 — omitting the key would let
    Kea 3.0 fall back to its own 0.25 default, which is the bug."""
    cfg = json.loads(KeaDriver().render_config(_bundle()))
    assert cfg["Dhcp4"]["cache-threshold"] == 0.0


# ── Kea driver render ───────────────────────────────────────────────────────


def test_group_threshold_renders_on_dhcp4_root() -> None:
    cfg = json.loads(KeaDriver().render_config(_bundle(threshold=0.25)))
    assert cfg["Dhcp4"]["cache-threshold"] == 0.25


def test_group_max_age_renders_when_set() -> None:
    cfg = json.loads(KeaDriver().render_config(_bundle(threshold=0.25, max_age=900)))
    assert cfg["Dhcp4"]["cache-max-age"] == 900


def test_group_max_age_absent_when_unset() -> None:
    """None = uncapped, which is Kea's own default — emit nothing."""
    cfg = json.loads(KeaDriver().render_config(_bundle(threshold=0.25)))
    assert "cache-max-age" not in cfg["Dhcp4"]


def test_scope_without_override_inherits_group() -> None:
    """No per-subnet key → Kea applies the Dhcp4-root value."""
    cfg = json.loads(KeaDriver().render_config(_bundle(threshold=0.25)))
    assert "cache-threshold" not in cfg["Dhcp4"]["subnet4"][0]


def test_scope_override_renders_on_subnet() -> None:
    cfg = json.loads(KeaDriver().render_config(_bundle(threshold=0.25, scope_threshold=0.5)))
    assert cfg["Dhcp4"]["subnet4"][0]["cache-threshold"] == 0.5


def test_scope_override_of_zero_survives() -> None:
    """The regression this suite exists for: a scope that explicitly disables
    caching (0.0) while the group enables it (0.25). A truthiness guard would
    drop the 0.0 and the scope would silently inherit 0.25."""
    cfg = json.loads(KeaDriver().render_config(_bundle(threshold=0.25, scope_threshold=0.0)))
    assert cfg["Dhcp4"]["subnet4"][0]["cache-threshold"] == 0.0


def test_scope_max_age_override_renders() -> None:
    cfg = json.loads(
        KeaDriver().render_config(_bundle(threshold=0.25, scope_threshold=0.5, scope_max_age=120))
    )
    assert cfg["Dhcp4"]["subnet4"][0]["cache-max-age"] == 120


# ── ETag (CLAUDE.md cross-cutting pattern #2) ───────────────────────────────


def test_group_threshold_shifts_etag() -> None:
    assert _bundle().compute_etag() != _bundle(threshold=0.25).compute_etag()


def test_group_max_age_shifts_etag() -> None:
    a = _bundle(threshold=0.25)
    b = _bundle(threshold=0.25, max_age=600)
    assert a.compute_etag() != b.compute_etag()


def test_scope_override_shifts_etag() -> None:
    a = _bundle(threshold=0.25)
    b = _bundle(threshold=0.25, scope_threshold=0.5)
    assert a.compute_etag() != b.compute_etag()


# ── Group → bundle plumbing ─────────────────────────────────────────────────


async def _group_with_server(
    db: AsyncSession,
    *,
    threshold: float = 0.0,
    max_age: int | None = None,
) -> DHCPServer:
    grp = DHCPServerGroup(
        name=f"grp-{uuid.uuid4().hex[:6]}",
        lease_cache_threshold=threshold,
        lease_cache_max_age=max_age,
    )
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


async def test_group_default_bundles_as_disabled(db_session: AsyncSession) -> None:
    srv = await _group_with_server(db_session)
    bundle = await build_config_bundle(db_session, srv)
    assert bundle.lease_cache_threshold == 0.0
    assert bundle.lease_cache_max_age is None


async def test_group_value_reaches_bundle(db_session: AsyncSession) -> None:
    srv = await _group_with_server(db_session, threshold=0.25, max_age=900)
    bundle = await build_config_bundle(db_session, srv)
    assert bundle.lease_cache_threshold == 0.25
    assert bundle.lease_cache_max_age == 900


# ── Group API round-trip ────────────────────────────────────────────────────


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


async def test_group_api_defaults_and_round_trips(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _superadmin(db_session)
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        "/api/v1/dhcp/server-groups",
        headers=h,
        json={"name": f"g-{uuid.uuid4().hex[:6]}"},
    )
    assert r.status_code == 201, r.text
    gid = r.json()["id"]
    # Default must be disabled — an upgrading install keeps 2.6 behaviour.
    assert r.json()["lease_cache_threshold"] == 0.0
    assert r.json()["lease_cache_max_age"] is None

    r = await client.put(
        f"/api/v1/dhcp/server-groups/{gid}",
        headers=h,
        json={"lease_cache_threshold": 0.25, "lease_cache_max_age": 900},
    )
    assert r.status_code == 200, r.text
    assert r.json()["lease_cache_threshold"] == 0.25
    assert r.json()["lease_cache_max_age"] == 900


async def test_group_api_can_clear_max_age(client: AsyncClient, db_session: AsyncSession) -> None:
    """An explicit null must CLEAR the cap back to uncapped. A naive
    ``exclude_none`` on the update body would swallow it and the operator could
    never undo a max-age once set."""
    token = await _superadmin(db_session)
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        "/api/v1/dhcp/server-groups",
        headers=h,
        json={
            "name": f"g-{uuid.uuid4().hex[:6]}",
            "lease_cache_threshold": 0.25,
            "lease_cache_max_age": 900,
        },
    )
    assert r.status_code == 201, r.text
    gid = r.json()["id"]

    r = await client.put(
        f"/api/v1/dhcp/server-groups/{gid}",
        headers=h,
        json={"lease_cache_max_age": None},
    )
    assert r.status_code == 200, r.text
    assert r.json()["lease_cache_max_age"] is None


async def test_group_api_rejects_out_of_range_threshold(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _superadmin(db_session)
    await db_session.commit()
    r = await client.post(
        "/api/v1/dhcp/server-groups",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": f"g-{uuid.uuid4().hex[:6]}", "lease_cache_threshold": 1.5},
    )
    assert r.status_code == 422, r.text
