"""Scoping test for the #623 repair migration ``d7b3f2a9c15e``.

The migration clears the auto-generated DNS records orphaned when it removes a
stranded DHCP mirror row. An earlier draft did this with a blanket
``dns_record.ip_address_id IS NULL`` delete, which also matched every
integration/ACME ``auto_generated`` record (those legitimately carry a null
``ip_address_id``) — wiping the Tailscale/NetBird/Kubernetes/ACME zones on
upgrade. This test proves the migration now deletes ONLY the records tied to the
mirror rows it actually removes, and leaves unrelated auto-generated records
alone.

The test schema is built from the models via ``create_all`` (not by running
Alembic), so we drive the migration's exact ``_UPGRADE_STATEMENTS`` — imported
from the revision module — against seeded rows.
"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import (
    DHCPLease,
    DHCPScope,
    DHCPServer,
    DHCPServerGroup,
    DHCPStaticAssignment,
)
from app.models.dns import DNSRecord, DNSServerGroup, DNSZone
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "d7b3f2a9c15e_repair_stranded_dhcp_lease_static_mirrors.py"
)


def _load_upgrade_statements() -> tuple[str, ...]:
    spec = importlib.util.spec_from_file_location("_mig_d7b3f2a9c15e", _MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._UPGRADE_STATEMENTS  # type: ignore[attr-defined]


async def _run_migration(db: AsyncSession) -> None:
    for statement in _load_upgrade_statements():
        await db.execute(text(statement))


@pytest.mark.asyncio
async def test_repair_migration_scopes_dns_delete_to_dhcp_orphans(
    db_session: AsyncSession,
) -> None:
    db = db_session
    now = datetime.now(UTC)

    # ── IPAM + zone scaffolding ─────────────────────────────────────────
    space = IPSpace(name=f"mig-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.0.0.0/8", name="root")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network="10.60.0.0/24", name="lan")
    db.add(subnet)
    await db.flush()

    dns_grp = DNSServerGroup(name=f"mig-{uuid.uuid4().hex[:6]}")
    db.add(dns_grp)
    await db.flush()
    zone = DNSZone(
        group_id=dns_grp.id,
        name="mig.example.",
        zone_type="primary",
        kind="forward",
        primary_ns="ns1.mig.example.",
        admin_email="admin.mig.example.",
    )
    db.add(zone)
    await db.flush()

    # ── A SOFT-DELETED scope, so its DHCP-derived rows are "stranded" ───
    group = DHCPServerGroup(name=f"mig-{uuid.uuid4().hex[:6]}")
    db.add(group)
    await db.flush()
    server = DHCPServer(
        name=f"mig-{uuid.uuid4().hex[:6]}",
        driver="kea",
        host="127.0.0.1",
        server_group_id=group.id,
    )
    db.add(server)
    scope = DHCPScope(
        group_id=group.id,
        subnet_id=subnet.id,
        name="stranded-scope",
        address_family="ipv4",
        deleted_at=now,
    )
    db.add(scope)
    await db.flush()

    # (a) Stranded STATIC reservation mirror (soft-deleted reservation) + its A record.
    static = DHCPStaticAssignment(
        scope_id=scope.id,
        ip_address="10.60.0.10",
        mac_address="aa:bb:cc:dd:ee:10",
        deleted_at=now,
    )
    db.add(static)
    await db.flush()
    static_mirror = IPAddress(
        subnet_id=subnet.id,
        address="10.60.0.10",
        status="static_dhcp",
        static_assignment_id=str(static.id),
    )
    db.add(static_mirror)
    await db.flush()
    static_a = DNSRecord(
        zone_id=zone.id,
        name="res",
        record_type="A",
        value="10.60.0.10",
        auto_generated=True,
        ip_address_id=static_mirror.id,
    )
    db.add(static_a)

    # (b) Stranded dynamic LEASE mirror + lease + its A record.
    lease_mirror = IPAddress(
        subnet_id=subnet.id,
        address="10.60.0.150",
        status="dhcp",
        mac_address="aa:bb:cc:dd:ee:99",
        auto_from_lease=True,
    )
    db.add(lease_mirror)
    await db.flush()
    db.add(
        DHCPLease(
            server_id=server.id,
            scope_id=scope.id,
            ip_address="10.60.0.150",
            mac_address="aa:bb:cc:dd:ee:99",
            state="active",
        )
    )
    lease_a = DNSRecord(
        zone_id=zone.id,
        name="lease-host",
        record_type="A",
        value="10.60.0.150",
        auto_generated=True,
        ip_address_id=lease_mirror.id,
    )
    db.add(lease_a)

    # ── UNRELATED auto-generated records that MUST survive ──────────────
    # ACME DNS-01 challenge TXT — auto_generated, no IP link.
    acme_txt = DNSRecord(
        zone_id=zone.id,
        name="_acme-challenge",
        record_type="TXT",
        value="challenge-token",
        auto_generated=True,
    )
    db.add(acme_txt)
    # Tailscale/NetBird-style mesh record — auto_generated, no IP link.
    mesh_a = DNSRecord(
        zone_id=zone.id,
        name="node1",
        record_type="A",
        value="100.64.0.1",
        auto_generated=True,
        tailscale_tenant_id=None,
    )
    db.add(mesh_a)
    await db.flush()

    acme_id, mesh_id = acme_txt.id, mesh_a.id
    static_a_id, lease_a_id = static_a.id, lease_a.id

    # ── Run the migration's exact upgrade SQL ───────────────────────────
    await _run_migration(db)
    db.expire_all()

    surviving = set((await db.execute(select(DNSRecord.id))).scalars().all())

    # The two DHCP-orphaned records are gone…
    assert static_a_id not in surviving
    assert lease_a_id not in surviving
    # …and the mirror rows they hung off are gone too.
    assert (
        await db.execute(select(IPAddress).where(IPAddress.address == "10.60.0.10"))
    ).scalar_one_or_none() is None
    assert (
        await db.execute(select(IPAddress).where(IPAddress.address == "10.60.0.150"))
    ).scalar_one_or_none() is None

    # But the unrelated auto_generated records with a null ip_address_id SURVIVE.
    assert acme_id in surviving
    assert mesh_id in surviving
