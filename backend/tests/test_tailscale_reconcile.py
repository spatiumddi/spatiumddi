"""Tests for the Tailscale reconciler.

Stub ``TailscaleClient`` so we don't need a real tailnet. Validates
auto-creation of CGNAT + IPv6 ULA blocks/subnets, device-address
mirroring, claim-on-existing + lock semantics, expired-device
filtering, and unclaim-on-disappear preservation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.tailscale import TailscaleTenant
from app.services.tailscale.client import _TailscaleDevice
from app.services.tailscale.reconcile import reconcile_tenant

# ── Fixtures ─────────────────────────────────────────────────────────


async def _make_space(db: AsyncSession) -> IPSpace:
    space = IPSpace(name=f"tailscale-test-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    return space


async def _make_tenant(
    db: AsyncSession,
    space: IPSpace,
    *,
    skip_expired: bool = True,
) -> TailscaleTenant:
    tenant = TailscaleTenant(
        name=f"ts-{uuid.uuid4().hex[:6]}",
        tailnet="-",
        api_key_encrypted=b"",  # reconciler guards on empty
        ipam_space_id=space.id,
        skip_expired=skip_expired,
    )
    db.add(tenant)
    await db.flush()
    return tenant


def _device(
    *,
    id_: str = "1",
    name: str = "host.example.ts.net",
    hostname: str = "host",
    addresses: list[str] | None = None,
    os: str = "linux",
    client_version: str = "1.62.0",
    user: str = "alice@example.com",
    tags: list[str] | None = None,
    expires: str | None = None,
    key_expiry_disabled: bool = False,
    last_seen: str | None = None,
    advertised_routes: list[str] | None = None,
    enabled_routes: list[str] | None = None,
) -> _TailscaleDevice:
    return _TailscaleDevice(
        id=id_,
        node_id=f"n{id_}",
        name=name,
        hostname=hostname,
        addresses=addresses or [],
        os=os,
        client_version=client_version,
        user=user,
        tags=tags or [],
        last_seen=last_seen,
        expires=expires,
        key_expiry_disabled=key_expiry_disabled,
        advertised_routes=advertised_routes or [],
        enabled_routes=enabled_routes or [],
    )


class _FakeClient:
    def __init__(self, devices: list[_TailscaleDevice]) -> None:
        self.devices = devices

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def list_devices(self):
        return self.devices


def _patch_client(fake: _FakeClient):
    def _ctor(**_kwargs):
        return fake

    return patch(
        "app.services.tailscale.reconcile.TailscaleClient",
        side_effect=_ctor,
    )


def _patch_decrypt(value: str = "tskey-api-fake"):
    return patch("app.services.tailscale.reconcile.decrypt_str", return_value=value)


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_creates_cgnat_block_and_subnet(
    db_session: AsyncSession,
) -> None:
    """First reconcile auto-creates the IPv4 CGNAT + IPv6 ULA blocks
    and one subnet each. Idempotent — second reconcile creates
    nothing new."""
    space = await _make_space(db_session)
    tenant = await _make_tenant(db_session, space)
    tenant.api_key_encrypted = b"x"  # non-empty so decrypt path runs
    await db_session.commit()

    fake = _FakeClient([])
    with _patch_client(fake), _patch_decrypt():
        summary = await reconcile_tenant(db_session, tenant)
    assert summary.ok, summary.error
    assert summary.blocks_created == 2
    assert summary.subnets_created == 2

    blocks = (
        (await db_session.execute(select(IPBlock).where(IPBlock.tailscale_tenant_id == tenant.id)))
        .scalars()
        .all()
    )
    assert {str(b.network) for b in blocks} == {
        "100.64.0.0/10",
        "fd7a:115c:a1e0::/48",
    }

    subs = (
        (await db_session.execute(select(Subnet).where(Subnet.tailscale_tenant_id == tenant.id)))
        .scalars()
        .all()
    )
    assert {str(s.network) for s in subs} == {"100.64.0.0/10", "fd7a:115c:a1e0::/48"}

    # Idempotent.
    with _patch_client(fake), _patch_decrypt():
        summary2 = await reconcile_tenant(db_session, tenant)
    assert summary2.ok
    assert summary2.blocks_created == 0
    assert summary2.subnets_created == 0


@pytest.mark.asyncio
async def test_reconcile_mirrors_device_addresses(
    db_session: AsyncSession,
) -> None:
    """Device with both IPv4 and IPv6 lands as two IPAddress rows
    (one per address) under the right subnet."""
    space = await _make_space(db_session)
    tenant = await _make_tenant(db_session, space)
    tenant.api_key_encrypted = b"x"
    await db_session.commit()

    devices = [
        _device(
            id_="dev1",
            name="laptop.example.ts.net",
            hostname="laptop",
            addresses=["100.64.1.5", "fd7a:115c:a1e0::5"],
            tags=["tag:dev"],
            advertised_routes=["192.168.7.0/24"],
            enabled_routes=["192.168.7.0/24"],
        ),
    ]
    with _patch_client(_FakeClient(devices)), _patch_decrypt():
        summary = await reconcile_tenant(db_session, tenant)

    assert summary.ok, summary.error
    assert summary.addresses_created == 2
    assert summary.device_count == 1
    assert tenant.tailnet_domain == "example.ts.net"

    rows = (
        (
            await db_session.execute(
                select(IPAddress).where(IPAddress.tailscale_tenant_id == tenant.id)
            )
        )
        .scalars()
        .all()
    )
    assert {str(r.address) for r in rows} == {"100.64.1.5", "fd7a:115c:a1e0::5"}
    for r in rows:
        assert r.status == "tailscale-node"
        assert r.hostname == "laptop.example.ts.net"
        assert "linux" in (r.description or "")
        cf = r.custom_fields or {}
        assert cf.get("user") == "alice@example.com"
        assert cf.get("tags") == ["tag:dev"]
        assert cf.get("enabled_routes") == ["192.168.7.0/24"]


@pytest.mark.asyncio
async def test_reconcile_skips_expired_devices_when_flag_on(
    db_session: AsyncSession,
) -> None:
    space = await _make_space(db_session)
    tenant = await _make_tenant(db_session, space, skip_expired=True)
    tenant.api_key_encrypted = b"x"
    await db_session.commit()

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    devices = [
        _device(
            id_="alive",
            name="a.example.ts.net",
            hostname="a",
            addresses=["100.64.1.10"],
        ),
        _device(
            id_="expired",
            name="b.example.ts.net",
            hostname="b",
            addresses=["100.64.1.11"],
            expires=past,
        ),
    ]
    with _patch_client(_FakeClient(devices)), _patch_decrypt():
        summary = await reconcile_tenant(db_session, tenant)

    assert summary.ok
    assert summary.skipped_expired == 1
    addrs = {
        str(a.address)
        for a in (
            await db_session.execute(
                select(IPAddress).where(IPAddress.tailscale_tenant_id == tenant.id)
            )
        )
        .scalars()
        .all()
    }
    assert addrs == {"100.64.1.10"}


@pytest.mark.asyncio
async def test_reconcile_includes_expired_when_flag_off(
    db_session: AsyncSession,
) -> None:
    space = await _make_space(db_session)
    tenant = await _make_tenant(db_session, space, skip_expired=False)
    tenant.api_key_encrypted = b"x"
    await db_session.commit()

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    devices = [
        _device(
            id_="expired",
            name="b.example.ts.net",
            hostname="b",
            addresses=["100.64.1.11"],
            expires=past,
        ),
    ]
    with _patch_client(_FakeClient(devices)), _patch_decrypt():
        summary = await reconcile_tenant(db_session, tenant)

    assert summary.ok
    assert summary.skipped_expired == 0
    assert summary.addresses_created == 1


@pytest.mark.asyncio
async def test_reconcile_keeps_expired_when_key_expiry_disabled(
    db_session: AsyncSession,
) -> None:
    """When the operator has turned key-expiry off on a device,
    Tailscale still stamps an ``expires`` timestamp (sometimes in
    the past, frozen at the time the toggle was flipped) but the
    device is operationally fine. The reconciler must not skip
    those even with ``skip_expired=True``."""
    space = await _make_space(db_session)
    tenant = await _make_tenant(db_session, space, skip_expired=True)
    tenant.api_key_encrypted = b"x"
    await db_session.commit()

    past = (datetime.now(UTC) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    devices = [
        _device(
            id_="long_lived_server",
            name="server.example.ts.net",
            hostname="server",
            addresses=["100.64.5.10"],
            expires=past,
            key_expiry_disabled=True,
        ),
        _device(
            id_="actually_expired",
            name="old.example.ts.net",
            hostname="old",
            addresses=["100.64.5.11"],
            expires=past,
            key_expiry_disabled=False,
        ),
    ]
    with _patch_client(_FakeClient(devices)), _patch_decrypt():
        summary = await reconcile_tenant(db_session, tenant)

    assert summary.ok
    # The key-expiry-disabled device is mirrored; the actually-
    # expired one is skipped.
    assert summary.skipped_expired == 1
    assert summary.addresses_created == 1
    addrs = {
        str(a.address)
        for a in (
            await db_session.execute(
                select(IPAddress).where(IPAddress.tailscale_tenant_id == tenant.id)
            )
        )
        .scalars()
        .all()
    }
    assert addrs == {"100.64.5.10"}


@pytest.mark.asyncio
async def test_reconcile_treats_zero_year_expires_as_never(
    db_session: AsyncSession,
) -> None:
    """Tailscale uses ``0001-01-01T00:00:00Z`` for "no expiry" — it
    looks like the deep past but means "never". We must NOT skip
    those devices when ``skip_expired=True``."""
    space = await _make_space(db_session)
    tenant = await _make_tenant(db_session, space, skip_expired=True)
    tenant.api_key_encrypted = b"x"
    await db_session.commit()

    devices = [
        _device(
            id_="never_expires",
            name="forever.example.ts.net",
            hostname="forever",
            addresses=["100.64.1.20"],
            expires="0001-01-01T00:00:00Z",
        ),
    ]
    with _patch_client(_FakeClient(devices)), _patch_decrypt():
        summary = await reconcile_tenant(db_session, tenant)

    assert summary.ok
    assert summary.skipped_expired == 0
    assert summary.addresses_created == 1


@pytest.mark.asyncio
async def test_reconcile_claims_pre_existing_operator_row_with_lock(
    db_session: AsyncSession,
) -> None:
    """If an operator created an IP entry in the CGNAT block before
    enabling the integration, the reconciler should claim the row
    (set tailscale_tenant_id) AND stamp user_modified_at so the
    operator's hostname + description are preserved."""
    space = await _make_space(db_session)
    tenant = await _make_tenant(db_session, space)
    tenant.api_key_encrypted = b"x"

    # Pre-existing block + subnet that the reconciler will adopt.
    block = IPBlock(space_id=space.id, network="100.64.0.0/10", name="op-block")
    db_session.add(block)
    await db_session.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network="100.64.0.0/10",
        name="op-subnet",
        total_ips=2**22,
    )
    db_session.add(subnet)
    await db_session.flush()

    op_row = IPAddress(
        subnet_id=subnet.id,
        address="100.64.1.99",
        status="allocated",
        hostname="my-handpicked-name",
        description="operator wrote this",
    )
    db_session.add(op_row)
    await db_session.commit()

    devices = [
        _device(
            id_="claim",
            name="claim.example.ts.net",
            hostname="claim",
            addresses=["100.64.1.99"],
        ),
    ]
    with _patch_client(_FakeClient(devices)), _patch_decrypt():
        summary = await reconcile_tenant(db_session, tenant)

    assert summary.ok, summary.error
    await db_session.refresh(op_row)
    assert op_row.tailscale_tenant_id == tenant.id
    assert op_row.user_modified_at is not None
    # Lock prevents the reconciler from overwriting the operator's
    # hostname / description / status.
    assert op_row.hostname == "my-handpicked-name"
    assert op_row.description == "operator wrote this"
    assert op_row.status == "allocated"


@pytest.mark.asyncio
async def test_reconcile_unclaim_locked_row_when_device_disappears(
    db_session: AsyncSession,
) -> None:
    """A locked row (operator edits) should NOT be deleted when its
    device disappears upstream — instead the reconciler releases
    the FK so the operator's "manually managed" row stays put."""
    space = await _make_space(db_session)
    tenant = await _make_tenant(db_session, space)
    tenant.api_key_encrypted = b"x"
    await db_session.commit()

    # First pass — populate one device.
    devices = [
        _device(
            id_="alive",
            name="alive.example.ts.net",
            hostname="alive",
            addresses=["100.64.2.50"],
        ),
    ]
    with _patch_client(_FakeClient(devices)), _patch_decrypt():
        await reconcile_tenant(db_session, tenant)

    # Operator edits the row.
    row = (
        await db_session.execute(select(IPAddress).where(IPAddress.address == "100.64.2.50"))
    ).scalar_one()
    row.hostname = "operator-renamed"
    row.user_modified_at = datetime.now(UTC)
    await db_session.commit()

    # Second pass — device gone upstream.
    with _patch_client(_FakeClient([])), _patch_decrypt():
        summary = await reconcile_tenant(db_session, tenant)
    assert summary.ok
    assert summary.addresses_deleted == 0

    await db_session.refresh(row)
    assert row.tailscale_tenant_id is None  # un-claimed
    assert row.hostname == "operator-renamed"  # operator edits preserved


@pytest.mark.asyncio
async def test_reconcile_deletes_unlocked_row_when_device_disappears(
    db_session: AsyncSession,
) -> None:
    """An unlocked tenant-owned row should be deleted when its
    device disappears upstream — that's how the integration stays
    tidy as the tailnet churns."""
    space = await _make_space(db_session)
    tenant = await _make_tenant(db_session, space)
    tenant.api_key_encrypted = b"x"
    await db_session.commit()

    devices = [
        _device(
            id_="ephemeral",
            name="ephemeral.example.ts.net",
            hostname="ephemeral",
            addresses=["100.64.3.7"],
        ),
    ]
    with _patch_client(_FakeClient(devices)), _patch_decrypt():
        await reconcile_tenant(db_session, tenant)

    with _patch_client(_FakeClient([])), _patch_decrypt():
        summary = await reconcile_tenant(db_session, tenant)
    assert summary.ok
    assert summary.addresses_deleted == 1

    rows = (
        (
            await db_session.execute(
                select(IPAddress).where(IPAddress.tailscale_tenant_id == tenant.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []
