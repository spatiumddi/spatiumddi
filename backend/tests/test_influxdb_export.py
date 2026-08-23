"""InfluxDB push export (issue #889).

Three things are worth pinning here, because each fails *silently* rather
than loudly:

* **Line-protocol escaping.** A comma in a subnet description or a space
  in a server name is not an error at the server — it re-parses as a
  different tag, or as a field, and the series quietly forks. The rules
  differ per position, which is why every position gets a case.
* **Endpoint + auth selection per version.** v3 is not a third client;
  it is the v2 endpoint with bearer auth and bucket=database naming. A
  regression that collapsed the two token schemes would work against
  InfluxDB 3 and break every v2 install.
* **Watermark advance.** Advancing on a *failed* push is how you get a
  permanent hole in a Grafana dashboard that nothing reports.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_str
from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dns import DNSServer, DNSServerGroup
from app.models.influxdb import InfluxDBTarget
from app.models.metrics import DNSMetricSample
from app.services.influxdb.client import (
    InfluxDBWriteError,
    InfluxTargetConfig,
    build_write_request,
)
from app.services.influxdb.collect import REPUSH_OVERLAP_SECONDS, collect_dns_points
from app.services.influxdb.line_protocol import Point, render_batch, render_point
from app.services.influxdb.push import is_due, push_target

# ── line protocol ──────────────────────────────────────────────────


def test_render_point_escapes_every_position() -> None:
    line = render_point(
        Point(
            measurement="dns queries,agg",
            tags={"server": "ns1 dc=1", "zone": "a,b"},
            fields={"queries total": 5},
            timestamp=1700000000,
        )
    )
    # Measurement escapes comma + space, but NOT '=' (legal unescaped there).
    assert line.startswith(r"dns\ queries\,agg,")
    assert r"server=ns1\ dc\=1" in line
    assert r"zone=a\,b" in line
    assert r"queries\ total=5i" in line
    assert line.endswith(" 1700000000")


def test_integer_and_float_and_bool_field_types() -> None:
    line = render_point(
        Point(
            measurement="m",
            tags={},
            fields={"i": 3, "f": 1.5, "b": True},
            timestamp=1,
        )
    )
    # bool must not render as an int — a `1i` where the series holds
    # booleans is a field-type conflict InfluxDB rejects for the whole
    # batch, not just the point.
    assert "b=true" in line
    assert "i=3i" in line
    assert "f=1.5" in line


def test_empty_tag_values_are_dropped() -> None:
    line = render_point(
        Point(measurement="m", tags={"a": "", "b": "x"}, fields={"v": 1}, timestamp=1)
    )
    assert "a=" not in line
    assert "b=x" in line


def test_newlines_cannot_terminate_a_point_early() -> None:
    line = render_point(
        Point(
            measurement="m",
            tags={"desc": "line1\nline2"},
            fields={"v": 1},
            timestamp=1,
        )
    )
    assert "\n" not in line


def test_string_field_is_quoted_and_escaped() -> None:
    line = render_point(Point(measurement="m", tags={}, fields={"s": 'a"b\\c'}, timestamp=1))
    assert 's="a\\"b\\\\c"' in line


def test_field_less_point_is_refused_not_shipped() -> None:
    # A field-less line is a parse error that rejects the whole batch it
    # arrives in, so it must never reach the wire.
    with pytest.raises(ValueError):
        render_point(Point(measurement="m", tags={"a": "b"}, fields={}, timestamp=1))


def test_render_batch_joins_with_newlines() -> None:
    body = render_batch(
        [
            Point(measurement="a", tags={}, fields={"v": 1}, timestamp=1),
            Point(measurement="b", tags={}, fields={"v": 2}, timestamp=2),
        ]
    )
    assert body.split("\n") == ["a v=1i 1", "b v=2i 2"]


# ── endpoint + auth per version ────────────────────────────────────


def test_v1_uses_write_endpoint_and_basic_auth() -> None:
    req = build_write_request(
        InfluxTargetConfig(
            version="v1",
            url="http://influx:8086/",
            database="spatium",
            username="admin",
            password="s3cret",
        )
    )
    assert req.url == "http://influx:8086/write"
    assert req.params["db"] == "spatium"
    assert req.params["precision"] == "s"
    assert req.auth == ("admin", "s3cret")
    assert "Authorization" not in req.headers


def test_v1_without_credentials_sends_no_auth() -> None:
    req = build_write_request(
        InfluxTargetConfig(version="v1", url="http://influx:8086", database="spatium")
    )
    assert req.auth is None


def test_v2_uses_token_scheme() -> None:
    req = build_write_request(
        InfluxTargetConfig(
            version="v2", url="http://influx:8086", org="acme", bucket="ddi", token="tok"
        )
    )
    assert req.url == "http://influx:8086/api/v2/write"
    assert req.params["org"] == "acme"
    assert req.params["bucket"] == "ddi"
    assert req.headers["Authorization"] == "Token tok"


def test_v3_reuses_v2_endpoint_with_bearer_and_optional_org() -> None:
    req = build_write_request(
        InfluxTargetConfig(version="v3", url="http://influx:8181", bucket="ddi", token="tok")
    )
    assert req.url == "http://influx:8181/api/v2/write"
    # v3 names a *database* in the bucket parameter, and org is optional
    # (Core / Enterprise accept-and-ignore it).
    assert req.params["bucket"] == "ddi"
    assert "org" not in req.params
    assert req.headers["Authorization"] == "Bearer tok"


def test_v2_requires_org_but_v3_does_not() -> None:
    with pytest.raises(ValueError, match="org is required"):
        build_write_request(InfluxTargetConfig(version="v2", url="http://x", bucket="b", token="t"))


@pytest.mark.parametrize(
    ("cfg", "match"),
    [
        (InfluxTargetConfig(version="v1", url="", database="d"), "url is required"),
        (InfluxTargetConfig(version="v1", url="http://x"), "database is required"),
        (
            InfluxTargetConfig(version="v2", url="http://x", org="o", bucket="b"),
            "token is required",
        ),
        (
            InfluxTargetConfig(version="v3", url="http://x", token="t"),
            "database is required",
        ),
        (InfluxTargetConfig(version="v9", url="http://x"), "unsupported InfluxDB version"),
    ],
)
def test_incomplete_targets_are_refused(cfg: InfluxTargetConfig, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        build_write_request(cfg)


# ── per-target interval gating ─────────────────────────────────────


def test_never_pushed_target_is_always_due() -> None:
    now = datetime.now(UTC)
    assert is_due(InfluxDBTarget(push_interval_seconds=3600, last_push_at=None), now)


def test_interval_gate_holds_until_elapsed() -> None:
    now = datetime.now(UTC)
    row = InfluxDBTarget(push_interval_seconds=300, last_push_at=now - timedelta(seconds=299))
    assert not is_due(row, now)
    row.last_push_at = now - timedelta(seconds=300)
    assert is_due(row, now)


# ── CRUD API ───────────────────────────────────────────────────────


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


def _v2_body(**over: object) -> dict:
    body = {
        "name": "grafana",
        "enabled": True,
        "version": "v2",
        "url": "http://influx:8086",
        "verify_tls": True,
        "timeout_seconds": 10,
        "database": "",
        "username": "",
        "org": "acme",
        "bucket": "ddi",
        "token": "tok",
        "measurement_prefix": "spatiumddi_",
        "push_interval_seconds": 60,
        "push_dns_metrics": True,
        "push_dhcp_metrics": True,
        "push_subnet_utilization": True,
        "push_dhcp_scope_leases": True,
    }
    body.update(over)
    return body


@pytest.mark.asyncio
async def test_crud_roundtrip_never_returns_the_token(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = {"Authorization": f"Bearer {await _superadmin(db_session)}"}

    r = await client.get("/api/v1/settings/influxdb-targets", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == []

    secret = "s3cret-" + uuid.uuid4().hex
    r = await client.post(
        "/api/v1/settings/influxdb-targets", headers=h, json=_v2_body(token=secret)
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["token_set"] is True
    # The secret must never appear in the response, under any key. (The
    # needle is randomised so it can't collide with a field *name* — an
    # earlier version of this assertion matched "token_set".)
    assert secret not in r.text

    target_id = created["id"]
    # Omitting ``token`` on update means "keep what's stored" — the
    # operator editing an interval must not silently clear the credential.
    body = _v2_body(push_interval_seconds=120)
    body.pop("token")
    r = await client.put(f"/api/v1/settings/influxdb-targets/{target_id}", headers=h, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["push_interval_seconds"] == 120
    assert r.json()["token_set"] is True

    r = await client.delete(f"/api/v1/settings/influxdb-targets/{target_id}", headers=h)
    assert r.status_code == 204
    r = await client.get("/api/v1/settings/influxdb-targets", headers=h)
    assert r.json() == []


@pytest.mark.asyncio
async def test_unwritable_target_is_422_at_save_not_a_push_error_later(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = {"Authorization": f"Bearer {await _superadmin(db_session)}"}
    r = await client.post("/api/v1/settings/influxdb-targets", headers=h, json=_v2_body(bucket=""))
    assert r.status_code == 422, r.text
    assert "bucket is required" in r.text


@pytest.mark.asyncio
async def test_non_superadmin_is_forbidden(client: AsyncClient, db_session: AsyncSession) -> None:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Plain",
        hashed_password=hash_password("x"),
        is_superadmin=False,
    )
    db_session.add(user)
    await db_session.flush()
    h = {"Authorization": f"Bearer {create_access_token(str(user.id))}"}
    assert (await client.get("/api/v1/settings/influxdb-targets", headers=h)).status_code == 403


# ── collect + watermark ────────────────────────────────────────────


async def _dns_sample(db: AsyncSession, bucket_at: datetime, queries: int) -> uuid.UUID:
    group = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(group)
    await db.flush()
    server = DNSServer(
        group_id=group.id,
        name=f"ns {uuid.uuid4().hex[:4]}",
        driver="bind9",
        host="10.0.0.53",
        port=53,
    )
    db.add(server)
    await db.flush()
    db.add(
        DNSMetricSample(
            server_id=server.id,
            bucket_at=bucket_at,
            queries_total=queries,
            noerror=queries,
            nxdomain=0,
            servfail=0,
            recursion=0,
            rate_dropped=0,
            rate_slipped=0,
        )
    )
    await db.flush()
    return server.id


@pytest.mark.asyncio
async def test_collect_dns_points_reports_the_newest_bucket(db_session: AsyncSession) -> None:
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    await _dns_sample(db_session, base, 10)
    await _dns_sample(db_session, base + timedelta(minutes=1), 20)

    points, newest = await collect_dns_points(db_session, None)
    assert len(points) == 2
    assert newest == base + timedelta(minutes=1)
    # The bucket's own timestamp travels with the point — not "now" —
    # so a backfill lands on the hour the traffic actually happened.
    assert {p.timestamp for p in points} == {
        int(base.timestamp()),
        int((base + timedelta(minutes=1)).timestamp()),
    }


@pytest.mark.asyncio
async def test_collect_dns_points_resends_an_overlap_window(db_session: AsyncSession) -> None:
    """A late-arriving sample must not be skipped forever.

    The cursor is deliberately not a strict ``>`` on the watermark: an
    agent that reports a bucket after the watermark passed it would
    otherwise never be exported. Re-sending is free — line protocol
    overwrites a point with the same measurement, tags and timestamp.
    """
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    await _dns_sample(db_session, base, 10)

    inside_overlap = base + timedelta(seconds=REPUSH_OVERLAP_SECONDS - 60)
    points, _ = await collect_dns_points(db_session, inside_overlap)
    assert len(points) == 1

    beyond_overlap = base + timedelta(seconds=REPUSH_OVERLAP_SECONDS + 60)
    points, newest = await collect_dns_points(db_session, beyond_overlap)
    assert points == []
    assert newest is None


@pytest.mark.asyncio
async def test_successful_push_advances_the_watermark_and_prefixes(
    db_session: AsyncSession,
) -> None:
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    await _dns_sample(db_session, base, 42)

    row = InfluxDBTarget(
        name=f"t-{uuid.uuid4().hex[:6]}",
        version="v2",
        url="http://influx:8086",
        org="acme",
        bucket="ddi",
        token_encrypted=encrypt_str("tok"),
        measurement_prefix="pfx_",
        push_dhcp_metrics=False,
        push_subnet_utilization=False,
        push_dhcp_scope_leases=False,
    )
    db_session.add(row)
    await db_session.flush()

    sent: list[str] = []

    async def _capture(cfg: object, body: str) -> None:
        sent.append(body)

    now = datetime.now(UTC)
    with patch("app.services.influxdb.push.write_lines", new=_capture):
        result = await push_target(db_session, row, now=now)

    assert result.ok and result.points == 1
    assert sent and sent[0].startswith("pfx_dns_queries,")
    assert row.last_dns_bucket_at == base
    assert row.last_push_error is None


@pytest.mark.asyncio
async def test_failed_push_leaves_the_watermark_alone(db_session: AsyncSession) -> None:
    """Advancing on failure is how a dashboard gets a permanent hole.

    The samples stay in Postgres until ``prune_metrics`` retires them, so
    a target that recovers inside the retention window backfills — but
    only if the cursor never moved past what was actually delivered.
    """
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    await _dns_sample(db_session, base, 42)

    row = InfluxDBTarget(
        name=f"t-{uuid.uuid4().hex[:6]}",
        version="v2",
        url="http://influx:8086",
        org="acme",
        bucket="ddi",
        token_encrypted=encrypt_str("tok"),
        push_dhcp_metrics=False,
        push_subnet_utilization=False,
        push_dhcp_scope_leases=False,
    )
    db_session.add(row)
    await db_session.flush()

    async def _boom(cfg: object, body: str) -> None:
        raise InfluxDBWriteError("HTTP 401: unauthorized")

    now = datetime.now(UTC)
    with patch("app.services.influxdb.push.write_lines", new=_boom):
        result = await push_target(db_session, row, now=now)

    assert not result.ok
    assert row.last_dns_bucket_at is None
    assert row.last_push_error == "HTTP 401: unauthorized"
    # ``last_push_at`` still moves, or a fast-failing target would be
    # retried on every 30 s beat tick instead of on its own interval.
    assert row.last_push_at == now
