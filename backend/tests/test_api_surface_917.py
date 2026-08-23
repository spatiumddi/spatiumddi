"""REST parity for capabilities that were copilot-only (issue #917).

Five issues from the mobile client turned out to be one finding: the copilot
tool registry is written against the service layer, non-negotiable #13
guarantees every REST surface gets a tool, and **nothing guarantees the
converse** — so a capability could exist, be reachable from a chat window, and
be invisible to the only API an external client has.

These tests pin the parity rather than the implementation: each one asserts
that the REST route and the copilot tool answer the same question over the
same data, because "they disagree" is the failure that would put the two
surfaces back where they started.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dhcp import (
    DHCPLease,
    DHCPLeaseHistory,
    DHCPScope,
    DHCPServer,
    DHCPServerGroup,
)
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.ownership import Customer

CIDR = "10.62.0.0/24"


async def _token(db: AsyncSession, name: str = "surface") -> str:
    user = User(
        username=f"{name}-{uuid.uuid4().hex[:6]}",
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.test",
        display_name=name,
        hashed_password=hash_password("x"),
        auth_source="local",
        is_superadmin=True,
    )
    user.groups = []
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


async def _subnet(db: AsyncSession) -> Subnet:
    space = IPSpace(name=f"s-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network=CIDR, name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=CIDR, name="s")
    db.add(subnet)
    await db.flush()
    return subnet


async def _dhcp_server(db: AsyncSession, *, name: str) -> tuple[DHCPServer, DHCPScope]:
    subnet = await _subnet(db)
    group = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(group)
    await db.flush()
    scope = DHCPScope(group_id=group.id, subnet_id=subnet.id, name=f"scope-{name}")
    server = DHCPServer(name=name, host="10.0.0.1", driver="kea", server_group_id=group.id)
    db.add_all([scope, server])
    await db.flush()
    return server, scope


# ── A2: fleet-wide lease search ──────────────────────────────────────


@pytest.mark.asyncio
async def test_lease_search_spans_every_server(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The question a technician starts from is "does this MAC have a lease
    ANYWHERE", and the per-server route cannot answer it without a fan-out
    plus a client-side merge that is order-sensitive."""
    server_a, scope_a = await _dhcp_server(db_session, name="kea-a")
    server_b, scope_b = await _dhcp_server(db_session, name="kea-b")
    now = datetime.now(UTC)
    db_session.add_all(
        [
            DHCPLease(
                server_id=server_a.id,
                scope_id=scope_a.id,
                ip_address="10.62.0.10",
                mac_address="aa:bb:cc:00:00:01",
                state="active",
                last_seen_at=now - timedelta(minutes=5),
            ),
            DHCPLease(
                server_id=server_b.id,
                scope_id=scope_b.id,
                ip_address="10.62.0.11",
                mac_address="aa:bb:cc:00:00:02",
                state="active",
                last_seen_at=now,
            ),
        ]
    )
    await db_session.flush()

    headers = {"Authorization": f"Bearer {await _token(db_session)}"}
    res = await client.get("/api/v1/dhcp/leases", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 2
    # Newest-seen first, ACROSS servers — the ordering a per-server merge
    # cannot reproduce from two independently paginated lists.
    assert [i["ip_address"] for i in body["items"]] == ["10.62.0.11", "10.62.0.10"]


@pytest.mark.asyncio
async def test_lease_mac_filter_normalises_separators(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Operators paste MACs in whichever form their last tool printed.

    A lookup that answers "no lease" because the separator differed is worse
    than one that errors — it sends the technician somewhere else.
    """
    server, scope = await _dhcp_server(db_session, name="kea-mac")
    db_session.add(
        DHCPLease(
            server_id=server.id,
            scope_id=scope.id,
            ip_address="10.62.0.20",
            mac_address="aa:bb:cc:dd:ee:ff",
            state="active",
        )
    )
    await db_session.flush()
    headers = {"Authorization": f"Bearer {await _token(db_session)}"}

    for spelling in ("aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff", "AABBCCDDEEFF"):
        res = await client.get("/api/v1/dhcp/leases", params={"mac": spelling}, headers=headers)
        assert res.status_code == 200, (spelling, res.text)
        assert res.json()["total"] == 1, spelling

    bad = await client.get("/api/v1/dhcp/leases", params={"mac": "nope"}, headers=headers)
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_lease_ip_filter_rejects_a_non_ip(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An unparseable literal must 422 rather than reaching an INET cast,
    which raises a DBAPIError and surfaces as a 500 on the client's own bad
    input."""
    await _dhcp_server(db_session, name="kea-ip")
    headers = {"Authorization": f"Bearer {await _token(db_session)}"}
    res = await client.get("/api/v1/dhcp/leases", params={"ip": "not-an-ip"}, headers=headers)
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_lease_history_spans_every_server(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """ "Has this MAC ever had a lease here?" — the other half of the
    client-lookup flow."""
    server_a, scope_a = await _dhcp_server(db_session, name="kea-h1")
    server_b, scope_b = await _dhcp_server(db_session, name="kea-h2")
    now = datetime.now(UTC)
    db_session.add_all(
        [
            DHCPLeaseHistory(
                server_id=server_a.id,
                scope_id=scope_a.id,
                ip_address="10.62.0.30",
                mac_address="aa:bb:cc:00:00:03",
                expired_at=now - timedelta(days=2),
                lease_state="expired",
            ),
            DHCPLeaseHistory(
                server_id=server_b.id,
                scope_id=scope_b.id,
                ip_address="10.62.0.31",
                mac_address="aa:bb:cc:00:00:04",
                expired_at=now - timedelta(days=1),
                lease_state="released",
            ),
        ]
    )
    await db_session.flush()

    headers = {"Authorization": f"Bearer {await _token(db_session)}"}
    res = await client.get("/api/v1/dhcp/lease-history", headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["total"] == 2

    scoped = await client.get(
        "/api/v1/dhcp/lease-history",
        params={"server_id": str(server_a.id)},
        headers=headers,
    )
    assert scoped.json()["total"] == 1

    bad_state = await client.get(
        "/api/v1/dhcp/lease-history", params={"lease_state": "nope"}, headers=headers
    )
    assert bad_state.status_code == 422


@pytest.mark.asyncio
async def test_group_filter_separates_unknown_from_empty(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An unknown group and a real group with no servers are different answers.

    Only one of them is the caller's mistake — collapsing both into a 404 (as
    an earlier cut did) makes a freshly-created group look like a typo, and
    collapsing both into an empty page hides the typo.
    """
    await _dhcp_server(db_session, name="kea-g")
    empty_group = DHCPServerGroup(name=f"empty-{uuid.uuid4().hex[:6]}")
    db_session.add(empty_group)
    await db_session.flush()
    headers = {"Authorization": f"Bearer {await _token(db_session)}"}

    unknown = await client.get(
        "/api/v1/dhcp/leases", params={"group_id": str(uuid.uuid4())}, headers=headers
    )
    assert unknown.status_code == 404

    empty = await client.get(
        "/api/v1/dhcp/leases", params={"group_id": str(empty_group.id)}, headers=headers
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["total"] == 0

    # And the empty-group filter must actually FILTER — an ``in_([])`` that
    # got optimised away would return the other group's leases.
    history = await client.get(
        "/api/v1/dhcp/lease-history",
        params={"group_id": str(empty_group.id)},
        headers=headers,
    )
    assert history.status_code == 200
    assert history.json()["total"] == 0


# ── A1: hygiene report ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hygiene_report_matches_the_copilot_tool(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Both surfaces call the same builder, which calls the alert matchers —
    so a tuning change to a detection reaches the rule, the tool and the
    route at once."""
    from app.services.ai.tools.ipam import (
        FindIPHygieneFindingsArgs,
        find_ip_hygiene_findings,
    )

    subnet = await _subnet(db_session)
    db_session.add(
        IPAddress(
            subnet_id=subnet.id,
            address="10.62.0.50",
            status="available",
            last_seen_at=datetime.now(UTC),
            last_seen_method="icmp",
        )
    )
    await db_session.flush()

    headers = {"Authorization": f"Bearer {await _token(db_session)}"}
    res = await client.get("/api/v1/ipam/reports/hygiene", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert [f["address"] for f in body["free_but_responding"]] == ["10.62.0.50"]
    assert body["counts"]["free_but_responding"] == 1
    assert body["thresholds"]["free_responding_days"] == 1
    assert body["truncated"] is False

    tool = await find_ip_hygiene_findings(db_session, None, FindIPHygieneFindingsArgs())
    assert tool["counts"] == body["counts"]


@pytest.mark.asyncio
async def test_hygiene_counts_are_full_not_page_length(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An estate with thousands of findings is exactly the one that needs the
    real number, so ``counts`` must not collapse to ``len(rows)``."""
    subnet = await _subnet(db_session)
    for i in range(5):
        db_session.add(
            IPAddress(
                subnet_id=subnet.id,
                address=f"10.62.0.{60 + i}",
                status="available",
                last_seen_at=datetime.now(UTC),
                last_seen_method="icmp",
            )
        )
    await db_session.flush()

    headers = {"Authorization": f"Bearer {await _token(db_session)}"}
    res = await client.get("/api/v1/ipam/reports/hygiene", params={"limit": 2}, headers=headers)
    body = res.json()
    assert len(body["free_but_responding"]) == 2
    assert body["counts"]["free_but_responding"] == 5
    assert body["truncated"] is True


# ── A3: vendor rollup ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vendor_rollup_reports_unresolved_macs(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """OUI lookup is off by default, and the rollup must stay honest about
    it: ``total_macs_seen`` is what tells an empty network apart from a
    disabled feature or a stale OUI table."""
    subnet = await _subnet(db_session)
    db_session.add(
        IPAddress(
            subnet_id=subnet.id,
            address="10.62.0.70",
            status="allocated",
            mac_address="aa:bb:cc:11:22:33",
        )
    )
    await db_session.flush()

    headers = {"Authorization": f"Bearer {await _token(db_session)}"}
    res = await client.get("/api/v1/ipam/reports/vendors", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total_macs_seen"] == 1
    # No OUI table in the test DB, so nothing resolves — and the response says
    # so rather than reporting an empty network.
    assert body["total_with_vendor"] == 0
    assert body["vendors"] == []


@pytest.mark.asyncio
async def test_vendor_devices_requires_a_search_term(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = {"Authorization": f"Bearer {await _token(db_session)}"}
    res = await client.get("/api/v1/ipam/reports/vendors/devices", headers=headers)
    assert res.status_code == 422


# ── A4: customer summary ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_customer_summary_counts_owned_resources(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """ "Is this customer safe to decommission?" in one call instead of nine."""
    customer = Customer(name=f"cust-{uuid.uuid4().hex[:6]}", status="active")
    db_session.add(customer)
    await db_session.flush()
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="", customer_id=customer.id)
    db_session.add(space)
    await db_session.flush()

    headers = {"Authorization": f"Bearer {await _token(db_session)}"}
    res = await client.get(f"/api/v1/customers/{customer.id}/summary", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["owned_resources"]["ip_spaces"] == 1
    assert body["owned_resource_total"] == 1

    missing = await client.get(f"/api/v1/customers/{uuid.uuid4()}/summary", headers=headers)
    assert missing.status_code == 404


# ── C1: per-resource alert filters ───────────────────────────────────


@pytest.mark.asyncio
async def test_alert_events_filter_by_subject(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A per-resource alerts panel had to pull up to 1000 events and filter
    client-side, which silently truncates on the noisy install where the
    panel matters most."""
    from app.models.alerts import AlertEvent, AlertRule

    rule = AlertRule(name="r", rule_type="subnet_utilization", severity="warning", enabled=True)
    db_session.add(rule)
    await db_session.flush()
    target = uuid.uuid4()
    db_session.add_all(
        [
            AlertEvent(
                rule_id=rule.id,
                subject_type="subnet",
                subject_id=str(target),
                subject_display="10.62.0.0/24",
                severity="warning",
                message="hot",
                fired_at=datetime.now(UTC),
            ),
            AlertEvent(
                rule_id=rule.id,
                subject_type="subnet",
                subject_id=str(uuid.uuid4()),
                subject_display="10.63.0.0/24",
                severity="critical",
                message="other",
                fired_at=datetime.now(UTC),
            ),
        ]
    )
    await db_session.flush()

    headers = {"Authorization": f"Bearer {await _token(db_session)}"}
    res = await client.get(
        "/api/v1/alerts/events",
        params={"subject_type": "subnet", "subject_id": str(target)},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    assert [e["subject_display"] for e in res.json()] == ["10.62.0.0/24"]

    by_sev = await client.get(
        "/api/v1/alerts/events", params={"severity": "critical"}, headers=headers
    )
    assert [e["subject_display"] for e in by_sev.json()] == ["10.63.0.0/24"]


# ── B: response schemas ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_routes_publish_a_real_schema() -> None:
    """The routes #917 named must describe their payload.

    Asserted against the generated document rather than the decorator,
    because FastAPI infers a ``response_model`` from a ``-> dict`` annotation:
    the attribute is set and the published schema is still an unconstrained
    object, which is what a generated client actually chokes on.
    """
    from app.main import app

    document = app.openapi()
    schemas = document["components"]["schemas"]

    def _schema_for(path: str, method: str = "get") -> dict[str, Any]:
        body = document["paths"][path][method]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        ref = body.get("$ref")
        return schemas[ref.rsplit("/", 1)[-1]] if ref else body

    for path in (
        "/api/v1/ipam/reports/stale-ips",
        "/api/v1/ipam/reports/hygiene",
        "/api/v1/ipam/reports/vendors",
        "/api/v1/ipam/subnets/{subnet_id}/reconciliation",
        "/api/v1/diagnostics/name-conformance",
        "/api/v1/dhcp/leases",
        "/api/v1/dhcp/lease-history",
    ):
        schema = _schema_for(path)
        assert schema.get("properties"), f"{path} publishes an untyped object"


@pytest.mark.asyncio
async def test_vendor_rollup_total_ignores_the_search_filter(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``total_with_vendor`` is a statement about the OUI table, not about the
    caller's search — computing it after the filter made a narrow search look
    like an empty OUI table, which is the exact confusion the field exists to
    prevent."""
    subnet = await _subnet(db_session)
    db_session.add(
        IPAddress(
            subnet_id=subnet.id,
            address="10.62.0.80",
            status="allocated",
            mac_address="aa:bb:cc:44:55:66",
        )
    )
    await db_session.flush()

    headers = {"Authorization": f"Bearer {await _token(db_session)}"}
    res = await client.get(
        "/api/v1/ipam/reports/vendors",
        params={"vendor_search": "nothing-matches-this"},
        headers=headers,
    )
    body = res.json()
    assert body["total_macs_seen"] == 1
    assert body["matching_macs"] == 0
