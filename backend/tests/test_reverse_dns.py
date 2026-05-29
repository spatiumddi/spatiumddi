"""Reverse-DNS (PTR) auto-population (issue #41)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.services.ipam import reverse_dns
from app.services.ipam.reverse_dns import populate_reverse_dns, short_label

RESOLVERS = ["192.0.2.53"]  # pinned → _build_resolver skips /etc/resolv.conf


async def _make_user(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_subnet(db: AsyncSession, cidr: str = "192.0.2.0/24") -> Subnet:
    space = IPSpace(name=f"rdns-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network=cidr, name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=cidr, name="s")
    db.add(subnet)
    await db.flush()
    return subnet


# ── Pure helper ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fqdn,expected",
    [
        ("server01.corp.example.com.", "server01"),
        ("server01.corp.example.com", "server01"),
        ("host", "host"),
        ("a.b", "a"),
    ],
)
def test_short_label(fqdn: str, expected: str) -> None:
    assert short_label(fqdn) == expected


# ── populate_reverse_dns ────────────────────────────────────────────────


def _fake_ptr(mapping: dict[str, str]):
    async def _resolve(ip: str, resolver: object) -> str | None:  # noqa: ARG001
        return mapping.get(ip)

    return _resolve


async def test_populate_fills_hostname_and_description(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    subnet = await _make_subnet(db_session)
    row = IPAddress(subnet_id=subnet.id, address="192.0.2.10", status="allocated")
    db_session.add(row)
    await db_session.flush()

    monkeypatch.setattr(
        reverse_dns, "resolve_ptr", _fake_ptr({"192.0.2.10": "server01.corp.example.com."})
    )
    counts = await populate_reverse_dns(db_session, resolvers=RESOLVERS)
    await db_session.flush()

    assert counts == {"scanned": 1, "resolved": 1, "updated": 1, "no_ptr": 0}
    await db_session.refresh(row)
    assert row.hostname == "server01"
    assert row.description == "server01.corp.example.com"


async def test_populate_preserves_operator_description(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    subnet = await _make_subnet(db_session)
    row = IPAddress(
        subnet_id=subnet.id, address="192.0.2.11", status="allocated", description="my note"
    )
    db_session.add(row)
    await db_session.flush()

    monkeypatch.setattr(reverse_dns, "resolve_ptr", _fake_ptr({"192.0.2.11": "host.example.com"}))
    await populate_reverse_dns(db_session, resolvers=RESOLVERS)
    await db_session.flush()
    await db_session.refresh(row)
    assert row.hostname == "host"
    assert row.description == "my note"  # not clobbered


async def test_populate_skips_integration_and_lease_and_noncandidate(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    subnet = await _make_subnet(db_session)
    cluster_owned = IPAddress(
        subnet_id=subnet.id,
        address="192.0.2.20",
        status="kubernetes-node",
        kubernetes_cluster_id=None,  # set below
    )
    lease = IPAddress(
        subnet_id=subnet.id, address="192.0.2.21", status="dhcp", auto_from_lease=True
    )
    available = IPAddress(subnet_id=subnet.id, address="192.0.2.22", status="available")
    good = IPAddress(subnet_id=subnet.id, address="192.0.2.23", status="discovered")
    db_session.add_all([cluster_owned, lease, available, good])
    await db_session.flush()

    monkeypatch.setattr(
        reverse_dns,
        "resolve_ptr",
        _fake_ptr(
            {
                "192.0.2.20": "k8s.example.com",
                "192.0.2.21": "lease.example.com",
                "192.0.2.22": "free.example.com",
                "192.0.2.23": "disco.example.com",
            }
        ),
    )
    counts = await populate_reverse_dns(db_session, resolvers=RESOLVERS)
    await db_session.flush()

    # Only the `discovered` row is a candidate (no provenance FK, not a
    # lease, real status). The k8s/lease/available rows are all skipped.
    assert counts["scanned"] == 1
    assert counts["updated"] == 1
    await db_session.refresh(good)
    assert good.hostname == "disco"


async def test_populate_no_ptr_leaves_row_untouched(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    subnet = await _make_subnet(db_session)
    row = IPAddress(subnet_id=subnet.id, address="192.0.2.30", status="allocated")
    db_session.add(row)
    await db_session.flush()

    monkeypatch.setattr(reverse_dns, "resolve_ptr", _fake_ptr({}))  # NXDOMAIN everywhere
    counts = await populate_reverse_dns(db_session, resolvers=RESOLVERS)
    await db_session.flush()
    assert counts == {"scanned": 1, "resolved": 0, "updated": 0, "no_ptr": 1}
    await db_session.refresh(row)
    assert row.hostname is None


# ── On-demand run endpoint ──────────────────────────────────────────────


async def test_run_endpoint_queues_and_audits(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.audit import AuditLog
    from app.tasks import reverse_dns as task_mod

    _, token = await _make_user(db_session)
    await db_session.commit()

    class _Result:
        id = "fake-task-id"

    monkeypatch.setattr(task_mod.sweep_reverse_dns, "delay", lambda *a, **k: _Result())

    r = await client.post(
        "/api/v1/settings/reverse-dns/run", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    assert r.json()["task_id"] == "fake-task-id"

    audit = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.resource_display == "reverse-dns-run")
            )
        )
        .scalars()
        .all()
    )
    assert len(audit) == 1
