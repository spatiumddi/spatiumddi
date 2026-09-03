"""`GET /api/v1/dns/agents/config` hands the bundle back as bytes serialised
once, and the fast path / 304 contract is unchanged.

Returning the dict sent it through FastAPI's jsonable_encoder, which walks and
copies the whole structure before the JSON is written — for a 250k-record
group that is 500k record dicts duplicated on the request loop, the difference
between a bundle that fits the api's memory limit and one that is memcg-killed
(appliance sizing campaign, 2026-09-03). The route now returns a JSON Response
built from one json.dumps; these tests pin what the agent sees.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dns import agents as agents_api
from app.models.dns import DNSRecord, DNSRecordOp, DNSServer, DNSServerGroup, DNSZone
from app.services.dns.agent_token import mint_agent_token

CONFIG_URL = "/api/v1/dns/agents/config"


async def _agent(db: AsyncSession, records: int) -> tuple[DNSServer, DNSZone, dict[str, str]]:
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    server = DNSServer(
        group_id=grp.id,
        driver="bind9",
        host="10.0.0.1",
        name=f"srv-{uuid.uuid4().hex[:6]}",
        is_primary=True,
        is_enabled=True,
        agent_id=uuid.uuid4(),
    )
    db.add(server)
    await db.flush()
    zone = DNSZone(
        group_id=grp.id,
        name=f"z{uuid.uuid4().hex[:6]}.example.",
        zone_type="primary",
        kind="forward",
        primary_ns="ns1.example.",
        admin_email="admin.example.",
    )
    db.add(zone)
    await db.flush()
    db.add_all(
        DNSRecord(
            zone_id=zone.id,
            name=f"h{i:04d}",
            fqdn=f"h{i:04d}.{zone.name}",
            record_type="A",
            value=f"10.0.{i // 250}.{i % 250}",
        )
        for i in range(records)
    )
    await db.flush()
    token, _exp = mint_agent_token(str(server.id), str(server.agent_id), "fp")
    return server, zone, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_the_bundle_comes_back_as_json_bytes_with_its_etag(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agents_api, "LONGPOLL_TIMEOUT_SECONDS", 1)
    _server, zone, headers = await _agent(db_session, records=300)
    await db_session.commit()

    resp = await client.get(CONFIG_URL, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["etag"].startswith("sha256:")
    assert resp.headers["etag"]  # the ETag the agent will send back
    zones = {z["name"]: z for z in body["zones"]}
    assert len(zones[zone.name]["records"]) == 300
    assert body["pending_record_ops"] == []
    assert body["pending_ops_remaining"] == 0

    # Nothing changed and nothing is pending: the held poll answers 304.
    again = await client.get(CONFIG_URL, headers={**headers, "If-None-Match": resp.headers["etag"]})
    assert again.status_code == 304
    assert again.headers["etag"] == resp.headers["etag"]


@pytest.mark.asyncio
async def test_pending_ops_take_the_fast_path_a_page_at_a_time(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(agents_api, "LONGPOLL_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(settings, "dns_agent_ops_batch", 4)
    server, zone, headers = await _agent(db_session, records=10)
    for i in range(6):
        db_session.add(
            DNSRecordOp(
                server_id=server.id,
                zone_name=zone.name,
                op="create",
                record={"name": f"n{i}", "type": "A", "value": f"10.9.9.{i}"},
                state="pending",
                created_at=datetime(2026, 9, 3, 12, 0, i, tzinfo=UTC),
            )
        )
    await db_session.commit()

    first = await client.get(CONFIG_URL, headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    # Returned immediately (no 5 s hold) with the first page only.
    assert [op["record"]["name"] for op in body["pending_record_ops"]] == ["n0", "n1", "n2", "n3"]
    assert body["pending_ops_remaining"] == 2

    # The next poll — even with the same ETag — ships the next page.
    second = await client.get(
        CONFIG_URL, headers={**headers, "If-None-Match": first.headers["etag"]}
    )
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert [op["record"]["name"] for op in body2["pending_record_ops"]] == ["n4", "n5"]
    assert body2["pending_ops_remaining"] == 0
