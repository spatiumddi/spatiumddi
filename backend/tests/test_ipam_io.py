"""Tests for the IPAM import / export service layer."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.ipam import IPBlock, IPSpace, Subnet
from app.core.security import hash_password
from app.services.ipam_io import (
    commit_import,
    export_subtree,
    parse_payload,
    preview_import,
)
from app.services.ipam_io.parser import ParsedPayload


async def _seed_user(db: AsyncSession) -> User:
    user = User(
        username="ioadmin",
        email="io@example.com",
        display_name="IO Admin",
        hashed_password=hash_password("x" * 10),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user


async def _seed_space(db: AsyncSession, name: str = "Test") -> IPSpace:
    space = IPSpace(name=name, description="")
    db.add(space)
    await db.flush()
    return space


# ── Parser tests ───────────────────────────────────────────────────────────────


def test_parse_csv_basic() -> None:
    data = b"network,name,vlan_id,gateway,owner\n10.0.1.0/24,Servers,10,10.0.1.1,NetOps\n"
    parsed = parse_payload(data, "import.csv", "text/csv")
    assert len(parsed.subnets) == 1
    row = parsed.subnets[0]
    assert row["network"] == "10.0.1.0/24"
    assert row["name"] == "Servers"
    assert row["vlan_id"] == 10
    assert row["gateway"] == "10.0.1.1"
    # 'owner' is unknown → custom field
    assert row["custom_fields"] == {"owner": "NetOps"}


def test_parse_json_list() -> None:
    body = json.dumps(
        [{"network": "10.0.2.0/24", "name": "App"}, {"network": "10.0.3.0/24"}]
    ).encode()
    parsed = parse_payload(body, "x.json", "application/json")
    assert [s["network"] for s in parsed.subnets] == ["10.0.2.0/24", "10.0.3.0/24"]


def test_parse_xlsx_roundtrip() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "subnets"
    ws.append(["network", "name", "vlan_id"])
    ws.append(["10.5.0.0/24", "X", 42])
    buf = io.BytesIO()
    wb.save(buf)
    parsed = parse_payload(buf.getvalue(), "x.xlsx", None)
    assert len(parsed.subnets) == 1
    assert parsed.subnets[0]["network"] == "10.5.0.0/24"
    assert parsed.subnets[0]["vlan_id"] == 42


# ── Preview ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_creates_and_conflicts(db_session: AsyncSession) -> None:
    space = await _seed_space(db_session, "PreviewSpace")
    block = IPBlock(space_id=space.id, network="10.0.0.0/8", name="agg")
    db_session.add(block)
    await db_session.flush()
    existing = Subnet(
        space_id=space.id,
        block_id=block.id,
        network="10.0.1.0/24",
        name="already-here",
        total_ips=254,
    )
    db_session.add(existing)
    await db_session.flush()

    payload = ParsedPayload(
        subnets=[
            {"network": "10.0.1.0/24", "name": "dup"},
            {"network": "10.0.2.0/24", "name": "new"},
            {"network": "not-a-cidr"},
        ]
    )

    preview = await preview_import(db_session, payload, space_id=space.id, strategy="fail")
    assert preview.space_name == "PreviewSpace"
    created_networks = [r.network for r in preview.creates if r.kind == "subnet"]
    assert "10.0.2.0/24" in created_networks
    assert any(r.network == "10.0.1.0/24" for r in preview.conflicts)
    assert any("Invalid CIDR" in (r.reason or "") for r in preview.errors)


# ── Commit ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_commit_creates_with_auto_parent(db_session: AsyncSession) -> None:
    space = await _seed_space(db_session, "CommitSpace")
    user = await _seed_user(db_session)

    payload = ParsedPayload(
        subnets=[
            {"network": "192.168.10.0/24", "name": "lan"},
            {"network": "192.168.20.0/24", "name": "wifi", "vlan_id": 20},
        ]
    )
    result = await commit_import(
        db_session, payload, current_user=user, space_id=space.id, strategy="skip"
    )
    assert result.created_subnets == 2
    # No existing block → auto-created parents per subnet
    assert result.auto_created_blocks >= 1


@pytest.mark.asyncio
async def test_commit_overwrite(db_session: AsyncSession) -> None:
    space = await _seed_space(db_session, "OverwriteSpace")
    user = await _seed_user(db_session)
    block = IPBlock(space_id=space.id, network="10.0.0.0/8", name="agg")
    db_session.add(block)
    await db_session.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network="10.0.5.0/24",
        name="old",
        description="old-desc",
        total_ips=254,
    )
    db_session.add(subnet)
    await db_session.flush()

    payload = ParsedPayload(
        subnets=[{"network": "10.0.5.0/24", "name": "new", "description": "new-desc"}]
    )
    result = await commit_import(
        db_session, payload, current_user=user, space_id=space.id, strategy="overwrite"
    )
    assert result.updated_subnets == 1
    await db_session.refresh(subnet)
    assert subnet.name == "new"
    assert subnet.description == "new-desc"


# ── Export round-trip ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_csv_contains_subnets(db_session: AsyncSession) -> None:
    space = await _seed_space(db_session, "ExportSpace")
    block = IPBlock(space_id=space.id, network="172.16.0.0/12", name="b")
    db_session.add(block)
    await db_session.flush()
    db_session.add(
        Subnet(
            space_id=space.id,
            block_id=block.id,
            network="172.16.5.0/24",
            name="prod",
            total_ips=254,
        )
    )
    await db_session.flush()

    data, ctype, filename = await export_subtree(
        db_session, space_id=space.id, format="csv"
    )
    assert ctype == "text/csv"
    assert filename.endswith(".csv")
    text = data.decode()
    assert "172.16.5.0/24" in text
    assert "prod" in text


@pytest.mark.asyncio
async def test_csv_roundtrip(db_session: AsyncSession) -> None:
    """Export → re-parse yields the same subnet networks."""
    space = await _seed_space(db_session, "RoundTrip")
    block = IPBlock(space_id=space.id, network="10.0.0.0/8", name="b")
    db_session.add(block)
    await db_session.flush()
    nets = ["10.0.1.0/24", "10.0.2.0/24"]
    for n in nets:
        db_session.add(
            Subnet(space_id=space.id, block_id=block.id, network=n, name=n, total_ips=254)
        )
    await db_session.flush()

    data, _, _ = await export_subtree(db_session, space_id=space.id, format="csv")
    parsed = parse_payload(data, "rt.csv", "text/csv")
    parsed_nets = sorted(str(s["network"]) for s in parsed.subnets)
    assert parsed_nets == sorted(nets)
