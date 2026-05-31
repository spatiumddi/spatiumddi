"""Offline unit tests for the Hetzner DNS driver (issue #37).

Hetzner is a tier-3 provider with no test account, so every test
monkeypatches :meth:`HetznerDNSDriver._client` to return a fake
async-context-manager client that serves canned envelopes and records the
calls made against it. Nothing here touches the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.drivers.dns._cloud_base import CloudDNSError
from app.drivers.dns.base import RecordChange, RecordData
from app.drivers.dns.hetzner import HetznerDNSDriver


class _FakeResponse:
    """Minimal stand-in for an ``httpx.Response`` (status + json())."""

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Async-context-manager fake of ``httpx.AsyncClient``.

    Each verb pops the next queued response off the matching list and
    records ``(method, path, params, json)`` for assertion. A queued
    response can be a ``_FakeResponse`` or a zero-arg callable returning
    one (so a test can vary the reply by call order).
    """

    def __init__(self, queues: dict[str, list[Any]]) -> None:
        self._queues = queues
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def _next(self, method: str, path: str, params: Any, body: Any) -> _FakeResponse:
        self.calls.append({"method": method, "path": path, "params": params, "json": body})
        queue = self._queues.get(method)
        if not queue:
            raise AssertionError(f"unexpected {method} {path} (no queued response)")
        item = queue.pop(0)
        return item() if callable(item) else item

    async def get(self, path: str, params: Any = None) -> _FakeResponse:
        return self._next("get", path, params, None)

    async def post(self, path: str, json: Any = None) -> _FakeResponse:
        return self._next("post", path, None, json)

    async def put(self, path: str, json: Any = None) -> _FakeResponse:
        return self._next("put", path, None, json)

    async def delete(self, path: str, params: Any = None) -> _FakeResponse:
        return self._next("delete", path, params, None)


def _zones_env(zones: list[Any], *, last_page: int = 1, page: int = 1) -> dict[str, Any]:
    """Build a Hetzner-shaped ``GET /zones`` envelope."""
    return {
        "zones": zones,
        "meta": {
            "pagination": {
                "page": page,
                "per_page": 50,
                "last_page": last_page,
                "total_entries": len(zones),
            }
        },
    }


def _records_env(records: list[Any], *, last_page: int = 1, page: int = 1) -> dict[str, Any]:
    """Build a Hetzner-shaped ``GET /records`` envelope."""
    return {
        "records": records,
        "meta": {
            "pagination": {
                "page": page,
                "per_page": 50,
                "last_page": last_page,
                "total_entries": len(records),
            }
        },
    }


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> HetznerDNSDriver:
    driver = HetznerDNSDriver()
    monkeypatch.setattr(driver, "_client", lambda token: fake)
    return driver


class _Server:
    """Stub DNSServer row — only the attrs the driver reads."""

    id = "srv-1"
    name = "hz-test"
    credentials_encrypted = b"x"


_CREDS = {"api_token": "tok"}


# ── _list_zones pagination ──────────────────────────────────────────────
async def test_list_zones_paginates_and_flags_reverse(monkeypatch: pytest.MonkeyPatch) -> None:
    page1 = _zones_env(
        [
            {"id": "z1", "name": "example.com"},
            {"id": "z2", "name": "10.in-addr.arpa"},
        ],
        last_page=2,
        page=1,
    )
    page2 = _zones_env([{"id": "z3", "name": "example.net"}], last_page=2, page=2)
    fake = _FakeClient({"get": [_FakeResponse(200, page1), _FakeResponse(200, page2)]})
    driver = _patch_client(monkeypatch, fake)

    zones = await driver._list_zones(_Server(), _CREDS)

    assert [z.name for z in zones] == ["example.com.", "10.in-addr.arpa.", "example.net."]
    assert [z.zone_id for z in zones] == ["z1", "z2", "z3"]
    # Reverse-zone detection.
    assert zones[1].is_reverse is True
    assert zones[0].is_reverse is False
    # Two pages → two GETs.
    assert sum(1 for c in fake.calls if c["method"] == "get") == 2
    assert fake.calls[0]["params"] == {"per_page": 50, "page": 1}
    assert fake.calls[1]["params"] == {"per_page": 50, "page": 2}


# ── _list_zone_records relativization + TTL handling ────────────────────
async def test_list_zone_records_relativizes_and_normalizes_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    zone_lookup = _zones_env([{"id": "zid", "name": "example.com"}])
    records = _records_env(
        [
            # apex record — Hetzner returns "@"; ttl absent → zone default.
            {"id": "r1", "name": "@", "type": "A", "value": "1.2.3.4"},
            {"id": "r2", "name": "www", "type": "A", "value": "5.6.7.8", "ttl": 300},
            {
                "id": "r3",
                "name": "@",
                "type": "MX",
                "value": "mail.example.com",
                "ttl": 3600,
                "priority": 10,
            },
        ]
    )
    fake = _FakeClient({"get": [_FakeResponse(200, zone_lookup), _FakeResponse(200, records)]})
    driver = _patch_client(monkeypatch, fake)

    recs = await driver._list_zone_records(_Server(), _CREDS, "example.com.")

    # Apex stays "@"; sub-label verbatim; absent ttl → None (zone default).
    assert recs[0] == RecordData(name="@", record_type="A", value="1.2.3.4", ttl=None)
    assert recs[1] == RecordData(name="www", record_type="A", value="5.6.7.8", ttl=300)
    assert recs[2].name == "@"
    assert recs[2].priority == 10
    assert recs[2].ttl == 3600
    # Zone-id was resolved by name (de-dotted).
    assert fake.calls[0]["params"] == {"name": "example.com"}
    # Records were fetched scoped by the resolved zone_id.
    assert fake.calls[1]["params"]["zone_id"] == "zid"


# ── _apply_record create ────────────────────────────────────────────────
async def test_apply_record_create_posts_relative_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        {
            "get": [_FakeResponse(200, _zones_env([{"id": "zid", "name": "example.com"}]))],
            "post": [_FakeResponse(201, {"record": {"id": "new"}})],
        }
    )
    driver = _patch_client(monkeypatch, fake)
    change = RecordChange(
        op="create",
        zone_name="example.com.",
        record=RecordData(name="www", record_type="A", value="1.1.1.1", ttl=None),
        target_serial=1,
    )

    await driver._apply_record(_Server(), _CREDS, change)

    post = next(c for c in fake.calls if c["method"] == "post")
    assert post["path"] == "/records"
    # ttl omitted entirely when None (inherit zone default).
    assert post["json"] == {
        "zone_id": "zid",
        "type": "A",
        "name": "www",
        "value": "1.1.1.1",
    }


# ── _apply_record create at apex renders "@" ────────────────────────────
async def test_apply_record_create_apex_uses_at(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        {
            "get": [_FakeResponse(200, _zones_env([{"id": "zid", "name": "example.com"}]))],
            "post": [_FakeResponse(201, {"record": {"id": "new"}})],
        }
    )
    driver = _patch_client(monkeypatch, fake)
    change = RecordChange(
        op="create",
        zone_name="example.com.",
        record=RecordData(name="@", record_type="A", value="3.3.3.3", ttl=120),
        target_serial=1,
    )

    await driver._apply_record(_Server(), _CREDS, change)

    post = next(c for c in fake.calls if c["method"] == "post")
    assert post["json"] == {
        "zone_id": "zid",
        "type": "A",
        "name": "@",
        "value": "3.3.3.3",
        "ttl": 120,
    }


# ── _apply_record update (existing record found → PUT) ──────────────────
async def test_apply_record_update_puts_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        {
            "get": [
                _FakeResponse(200, _zones_env([{"id": "zid", "name": "example.com"}])),
                # find existing record (client-side value match over the zone)
                _FakeResponse(
                    200,
                    _records_env([{"id": "rid", "name": "www", "type": "A", "value": "2.2.2.2"}]),
                ),
            ],
            "put": [_FakeResponse(200, {"record": {"id": "rid"}})],
        }
    )
    driver = _patch_client(monkeypatch, fake)
    change = RecordChange(
        op="update",
        zone_name="example.com.",
        record=RecordData(name="www", record_type="A", value="2.2.2.2", ttl=120),
        target_serial=1,
    )

    await driver._apply_record(_Server(), _CREDS, change)

    put = next(c for c in fake.calls if c["method"] == "put")
    assert put["path"] == "/records/rid"
    assert put["json"]["value"] == "2.2.2.2"
    assert put["json"]["ttl"] == 120
    assert put["json"]["zone_id"] == "zid"


# ── _apply_record update with no match → falls back to create (POST) ────
async def test_apply_record_update_missing_creates(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        {
            "get": [
                _FakeResponse(200, _zones_env([{"id": "zid", "name": "example.com"}])),
                _FakeResponse(200, _records_env([])),  # no existing record
            ],
            "post": [_FakeResponse(201, {"record": {"id": "new"}})],
        }
    )
    driver = _patch_client(monkeypatch, fake)
    change = RecordChange(
        op="update",
        zone_name="example.com.",
        record=RecordData(name="@", record_type="A", value="3.3.3.3", ttl=None),
        target_serial=1,
    )

    await driver._apply_record(_Server(), _CREDS, change)

    assert [c["method"] for c in fake.calls] == ["get", "get", "post"]
    post = next(c for c in fake.calls if c["method"] == "post")
    # Apex name renders as "@".
    assert post["json"]["name"] == "@"


# ── _apply_record delete (found → DELETE) ───────────────────────────────
async def test_apply_record_delete_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        {
            "get": [
                _FakeResponse(200, _zones_env([{"id": "zid", "name": "example.com"}])),
                _FakeResponse(
                    200,
                    _records_env([{"id": "rid", "name": "www", "type": "A", "value": "1.1.1.1"}]),
                ),
            ],
            "delete": [_FakeResponse(200, {})],
        }
    )
    driver = _patch_client(monkeypatch, fake)
    change = RecordChange(
        op="delete",
        zone_name="example.com.",
        record=RecordData(name="www", record_type="A", value="1.1.1.1"),
        target_serial=1,
    )

    await driver._apply_record(_Server(), _CREDS, change)

    delete = next(c for c in fake.calls if c["method"] == "delete")
    assert delete["path"] == "/records/rid"


# ── _apply_record delete with no match → no-op (no DELETE issued) ───────
async def test_apply_record_delete_missing_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        {
            "get": [
                _FakeResponse(200, _zones_env([{"id": "zid", "name": "example.com"}])),
                _FakeResponse(200, _records_env([])),  # nothing to delete
            ]
        }
    )
    driver = _patch_client(monkeypatch, fake)
    change = RecordChange(
        op="delete",
        zone_name="example.com.",
        record=RecordData(name="gone", record_type="A", value="1.1.1.1"),
        target_serial=1,
    )

    await driver._apply_record(_Server(), _CREDS, change)

    # Only the two lookups happened — no DELETE.
    assert [c["method"] for c in fake.calls] == ["get", "get"]


# ── _apply_record update targets the right value of a multi-value RRset ──
async def test_apply_record_update_matches_value_in_multivalue_rrset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round-robin A name has two values on Hetzner; updating the TTL of
    the ``5.6.7.8`` record must PUT against *its* id, not the first row's
    (issue #331)."""
    multi = _records_env(
        [
            {"id": "rid-a", "type": "A", "name": "www", "value": "1.2.3.4"},
            {"id": "rid-b", "type": "A", "name": "www", "value": "5.6.7.8"},
        ]
    )
    fake = _FakeClient(
        {
            "get": [
                _FakeResponse(200, _zones_env([{"id": "zid", "name": "example.com"}])),
                _FakeResponse(200, multi),  # find existing record
            ],
            "put": [_FakeResponse(200, {"record": {"id": "rid-b"}})],
        }
    )
    driver = _patch_client(monkeypatch, fake)
    change = RecordChange(
        op="update",
        zone_name="example.com.",
        record=RecordData(name="www", record_type="A", value="5.6.7.8", ttl=600),
        target_serial=1,
    )

    await driver._apply_record(_Server(), _CREDS, change)

    # We PUT against the id whose value actually matched the op value.
    put = next(c for c in fake.calls if c["method"] == "put")
    assert put["path"] == "/records/rid-b"
    assert put["json"]["value"] == "5.6.7.8"
    assert put["json"]["ttl"] == 600


# ── _apply_record delete targets the right value of a multi-value RRset ──
async def test_apply_record_delete_matches_value_in_multivalue_rrset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``delete A 1.2.3.4`` against a 2-value RRset must DELETE the row holding
    ``1.2.3.4`` even if Hetzner lists ``5.6.7.8`` first (issue #331)."""
    multi = _records_env(
        [
            {"id": "rid-keep", "type": "A", "name": "www", "value": "5.6.7.8"},
            {"id": "rid-drop", "type": "A", "name": "www", "value": "1.2.3.4"},
        ]
    )
    fake = _FakeClient(
        {
            "get": [
                _FakeResponse(200, _zones_env([{"id": "zid", "name": "example.com"}])),
                _FakeResponse(200, multi),
            ],
            "delete": [_FakeResponse(200, {})],
        }
    )
    driver = _patch_client(monkeypatch, fake)
    change = RecordChange(
        op="delete",
        zone_name="example.com.",
        record=RecordData(name="www", record_type="A", value="1.2.3.4"),
        target_serial=1,
    )

    await driver._apply_record(_Server(), _CREDS, change)

    delete = next(c for c in fake.calls if c["method"] == "delete")
    # The value-keyed row, not the first-listed sibling.
    assert delete["path"] == "/records/rid-drop"


# ── _apply_record delete is a no-op when no value matches ───────────────
async def test_apply_record_delete_no_value_match_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Hetzner returns sibling values but none equal the op value, the
    delete must be a safe no-op (no DELETE issued) rather than removing a
    wrong-value row (issue #331)."""
    multi = _records_env(
        [
            {"id": "rid-a", "type": "A", "name": "www", "value": "5.6.7.8"},
            {"id": "rid-b", "type": "A", "name": "www", "value": "9.9.9.9"},
        ]
    )
    fake = _FakeClient(
        {
            "get": [
                _FakeResponse(200, _zones_env([{"id": "zid", "name": "example.com"}])),
                _FakeResponse(200, multi),
            ]
        }
    )
    driver = _patch_client(monkeypatch, fake)
    change = RecordChange(
        op="delete",
        zone_name="example.com.",
        record=RecordData(name="www", record_type="A", value="1.2.3.4"),
        target_serial=1,
    )

    await driver._apply_record(_Server(), _CREDS, change)

    # Only the two lookups happened — no DELETE against a non-matching value.
    assert [c["method"] for c in fake.calls] == ["get", "get"]


# ── _find_record_id disambiguates MX rows by priority too ───────────────
async def test_apply_record_delete_mx_matches_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two MX records at the apex share a content host but differ by priority;
    deleting one must match value *and* priority (issue #331)."""
    multi = _records_env(
        [
            {
                "id": "mx-10",
                "type": "MX",
                "name": "@",
                "value": "mail.example.com",
                "priority": 10,
            },
            {
                "id": "mx-20",
                "type": "MX",
                "name": "@",
                "value": "mail.example.com",
                "priority": 20,
            },
        ]
    )
    fake = _FakeClient(
        {
            "get": [
                _FakeResponse(200, _zones_env([{"id": "zid", "name": "example.com"}])),
                _FakeResponse(200, multi),
            ],
            "delete": [_FakeResponse(200, {})],
        }
    )
    driver = _patch_client(monkeypatch, fake)
    change = RecordChange(
        op="delete",
        zone_name="example.com.",
        record=RecordData(name="@", record_type="MX", value="mail.example.com", priority=20),
        target_serial=1,
    )

    await driver._apply_record(_Server(), _CREDS, change)

    delete = next(c for c in fake.calls if c["method"] == "delete")
    assert delete["path"] == "/records/mx-20"


# ── _apply_zone create ──────────────────────────────────────────────────
async def test_apply_zone_create(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"post": [_FakeResponse(201, {"zone": {"id": "z9"}})]})
    driver = _patch_client(monkeypatch, fake)
    zone = type("Z", (), {"name": "example.org."})()

    await driver._apply_zone(_Server(), _CREDS, zone, "create")

    post = fake.calls[0]
    assert post["path"] == "/zones"
    # Bare name (no trailing dot) on create.
    assert post["json"] == {"name": "example.org"}


async def test_apply_zone_delete_resolves_then_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        {
            "get": [_FakeResponse(200, _zones_env([{"id": "zid", "name": "example.org"}]))],
            "delete": [_FakeResponse(200, {})],
        }
    )
    driver = _patch_client(monkeypatch, fake)
    zone = type("Z", (), {"name": "example.org."})()

    await driver._apply_zone(_Server(), _CREDS, zone, "delete")

    assert [c["method"] for c in fake.calls] == ["get", "delete"]
    assert fake.calls[0]["params"] == {"name": "example.org"}
    assert fake.calls[1]["path"] == "/zones/zid"


# ── Error surfacing ─────────────────────────────────────────────────────
async def test_error_envelope_raises_clouddnserror(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"error": {"message": "Invalid zone id.", "code": 422}}
    fake = _FakeClient({"get": [_FakeResponse(422, payload)]})
    driver = _patch_client(monkeypatch, fake)

    with pytest.raises(CloudDNSError) as exc:
        await driver._list_zones(_Server(), _CREDS)
    assert "Invalid zone id." in str(exc.value)


async def test_flat_message_error_raises_clouddnserror(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"message": "Unauthorized"}
    fake = _FakeClient({"get": [_FakeResponse(401, payload)]})
    driver = _patch_client(monkeypatch, fake)

    with pytest.raises(CloudDNSError) as exc:
        await driver._list_zones(_Server(), _CREDS)
    assert "Unauthorized" in str(exc.value)


async def test_non_2xx_no_body_raises_clouddnserror(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"get": [_FakeResponse(500, {})]})
    driver = _patch_client(monkeypatch, fake)

    with pytest.raises(CloudDNSError) as exc:
        await driver._list_zones(_Server(), _CREDS)
    assert "HTTP 500" in str(exc.value)


async def test_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = HetznerDNSDriver()
    change = RecordChange(
        op="create",
        zone_name="example.com.",
        record=RecordData(name="@", record_type="A", value="1.1.1.1"),
        target_serial=1,
    )
    with pytest.raises(CloudDNSError):
        await driver._apply_record(_Server(), {}, change)


# ── Static helpers ──────────────────────────────────────────────────────
def test_relativize_helper() -> None:
    driver = HetznerDNSDriver()
    # Hetzner returns relative names already; apex collapses to "@".
    assert driver._relativize("@", "example.com.") == "@"
    assert driver._relativize("www", "example.com.") == "www"
    # Robust against an absolute FQDN if Hetzner ever returns one.
    assert driver._relativize("example.com.", "example.com.") == "@"
    assert driver._relativize("a.b.example.com.", "example.com") == "a.b"


def test_absolute_name_helper() -> None:
    driver = HetznerDNSDriver()
    assert driver._absolute_name("@", "example.com.") == "@"
    assert driver._absolute_name("", "example.com.") == "@"
    assert driver._absolute_name("www", "example.com.") == "www"
    # Absolute FQDN → relative label.
    assert driver._absolute_name("www.example.com.", "example.com.") == "www"
    assert driver._absolute_name("example.com.", "example.com.") == "@"


def test_capabilities_shape() -> None:
    caps = HetznerDNSDriver().capabilities()
    assert caps["name"] == "hetzner"
    assert caps["agentless"] is True
    assert caps["manages_zones"] is True
    assert caps["views"] is False
    assert caps["rpz"] is False
    # Hetzner has no online DNSSEC signing via the API.
    assert caps["dnssec_online"] is False
    assert "CAA" in caps["record_types"]
    # Must NOT advertise types Hetzner's API doesn't serve.
    assert "SVCB" not in caps["record_types"]
    assert "HTTPS" not in caps["record_types"]
