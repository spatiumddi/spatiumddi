"""Offline unit tests for the FortiGate DHCP driver.

FortiGate is a REST provider with no CI test account, so every test that
exercises I/O monkeypatches :meth:`FortiGateDHCPDriver._client` to return a
fake async-context-manager client that serves canned FortiOS envelopes and
records the calls made against it. Pure body-building / option-mapping is
tested directly. Nothing here touches the network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.drivers.dhcp._cloud_base import CloudDHCPError
from app.drivers.dhcp.base import PoolDef, ScopeDef, StaticAssignmentDef
from app.drivers.dhcp.fortigate import FortiGateDHCPDriver


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Async-context-manager fake of ``httpx.AsyncClient``.

    Each verb pops the next queued response off the matching list and
    records ``(method, path, json)`` for assertion.
    """

    def __init__(self, queues: dict[str, list[Any]]) -> None:
        self._queues = queues
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def _next(self, method: str, path: str, body: Any) -> _FakeResponse:
        self.calls.append({"method": method, "path": path, "json": body})
        queue = self._queues.get(method)
        if not queue:
            raise AssertionError(f"unexpected {method} {path} (no queued response)")
        item = queue.pop(0)
        return item() if callable(item) else item

    async def get(self, path: str, params: Any = None) -> _FakeResponse:
        return self._next("get", path, None)

    async def post(self, path: str, json: Any = None) -> _FakeResponse:
        return self._next("post", path, json)

    async def put(self, path: str, json: Any = None) -> _FakeResponse:
        return self._next("put", path, json)

    async def delete(self, path: str, params: Any = None) -> _FakeResponse:
        return self._next("delete", path, None)


def _ok(results: Any) -> _FakeResponse:
    return _FakeResponse(200, {"status": "success", "results": results})


def _write_ok(mkey: int = 5) -> _FakeResponse:
    return _FakeResponse(200, {"status": "success", "http_status": 200, "mkey": mkey})


def _server() -> SimpleNamespace:
    return SimpleNamespace(id="srv-1", name="fw-lab", host="10.0.0.1", port=443)


_CREDS = {"api_token": "tok", "vdom": "root", "verify_tls": False}


def _patch(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> FortiGateDHCPDriver:
    driver = FortiGateDHCPDriver()
    monkeypatch.setattr(driver, "_client", lambda server, creds: fake)
    return driver


def _iface(name: str, addr: str, mask: str, status: str = "up") -> dict[str, Any]:
    return {"name": name, "ip": [addr, mask], "status": status}


# ── Pure body building / option mapping ──────────────────────────────────


def _full_scope() -> ScopeDef:
    return ScopeDef(
        subnet_cidr="192.168.20.0/24",
        lease_time=3600,
        is_active=True,
        options={
            "routers": "192.168.20.1",
            "dns-servers": ["8.8.8.8", "1.1.1.1"],
            "domain-name": "lab.local",
            "ntp-servers": "192.168.20.1",
            "broadcast-address": "192.168.20.255",  # must be dropped
            "mtu": "1500",  # generic option, non-ip → string
            "code:252": "http://wpad.lab/wpad.dat",  # custom code → string
            "tftp-server-address": "192.168.20.5",  # generic option, ip
        },
        pools=(
            PoolDef("192.168.20.100", "192.168.20.200", "dynamic"),
            PoolDef("192.168.20.150", "192.168.20.160", "excluded"),
            PoolDef("192.168.20.240", "192.168.20.250", "reserved"),
        ),
        statics=(StaticAssignmentDef("192.168.20.50", "00:11:22:33:44:55", "printer"),),
    )


def test_build_body_first_class_fields() -> None:
    body = FortiGateDHCPDriver()._build_server_body("port2", _full_scope())
    assert body["interface"] == "port2"
    assert body["netmask"] == "255.255.255.0"
    assert body["status"] == "enable"
    assert body["lease-time"] == 3600
    assert body["default-gateway"] == "192.168.20.1"
    assert body["dns-service"] == "specify"
    assert body["dns-server1"] == "8.8.8.8"
    assert body["dns-server2"] == "1.1.1.1"
    assert body["domain"] == "lab.local"
    assert body["ntp-service"] == "specify"
    assert body["ntp-server1"] == "192.168.20.1"


def test_build_body_ranges_and_reservation() -> None:
    body = FortiGateDHCPDriver()._build_server_body("port2", _full_scope())
    # One dynamic pool → one ip-range.
    assert body["ip-range"] == [{"id": 1, "start-ip": "192.168.20.100", "end-ip": "192.168.20.200"}]
    # excluded .150-.160 is within the dynamic range → kept; reserved
    # .240-.250 is OUTSIDE the dynamic .100-.200 → clipped out (FortiGate
    # rejects an exclude-range outside the ip-range).
    assert body["exclude-range"] == [
        {"id": 1, "start-ip": "192.168.20.150", "end-ip": "192.168.20.160"},
    ]
    # Static → reserved-address (MAC → IP). No ``action`` field — on FortiOS
    # 7.4.x sending ``action: "assign"`` makes the API zero the reserved IP.
    assert body["reserved-address"] == [
        {
            "id": 1,
            "type": "mac",
            "ip": "192.168.20.50",
            "mac": "00:11:22:33:44:55",
            "description": "printer",
        }
    ]
    assert "action" not in body["reserved-address"][0]


def test_build_body_generic_options() -> None:
    body = FortiGateDHCPDriver()._build_server_body("port2", _full_scope())
    opts = {(o["code"], o["type"]): o for o in body["options"]}
    # broadcast-address (28) is derived by FortiGate → never emitted.
    assert not any(o["code"] == 28 for o in body["options"])
    # mtu (26) is not an IP → string.
    assert opts[(26, "string")]["value"] == "1500"
    # custom code:252 → string.
    assert opts[(252, "string")]["value"] == "http://wpad.lab/wpad.dat"
    # tftp-server-address (150) is an IP → type ip.
    assert opts[(150, "ip")]["ip"] == "192.168.20.5"


def test_build_body_clips_exclude_to_pool() -> None:
    # An excluded pool that partially overlaps the dynamic range is clipped
    # to the intersection (FortiGate rejects an exclude-range outside the
    # ip-range).
    scope = ScopeDef(
        subnet_cidr="10.0.0.0/24",
        pools=(
            PoolDef("10.0.0.100", "10.0.0.200", "dynamic"),
            PoolDef("10.0.0.180", "10.0.0.240", "excluded"),  # overhangs the pool end
        ),
    )
    body = FortiGateDHCPDriver()._build_server_body("port1", scope)
    assert body["exclude-range"] == [
        {"id": 1, "start-ip": "10.0.0.180", "end-ip": "10.0.0.200"},
    ]


def test_build_body_inactive_scope_disables() -> None:
    scope = ScopeDef(subnet_cidr="10.0.0.0/24", is_active=False)
    body = FortiGateDHCPDriver()._build_server_body("port3", scope)
    assert body["status"] == "disable"


# ── Interface matching ───────────────────────────────────────────────────


async def test_match_interface_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        {
            "get": [
                _ok(
                    [
                        _iface("port1", "10.0.0.1", "255.255.255.0"),
                        _iface("port2", "192.168.20.1", "255.255.255.0"),
                    ]
                ),
                _ok([]),  # existing servers list (none)
            ],
            "post": [_write_ok()],
        }
    )
    driver = _patch(monkeypatch, fake)
    await driver._apply_scope(_server(), _CREDS, _full_scope())
    # POST landed with interface port2 (the CIDR match).
    post = next(c for c in fake.calls if c["method"] == "post")
    assert post["json"]["interface"] == "port2"


async def test_match_interface_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"get": [_ok([_iface("port1", "10.0.0.1", "255.255.255.0")])]})
    driver = _patch(monkeypatch, fake)
    with pytest.raises(CloudDHCPError, match="No FortiGate interface"):
        await driver._apply_scope(_server(), _CREDS, _full_scope())


async def test_match_interface_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        {
            "get": [
                _ok(
                    [
                        _iface("port2", "192.168.20.1", "255.255.255.0"),
                        _iface("port3", "192.168.20.2", "255.255.255.0"),
                    ]
                ),
            ]
        }
    )
    driver = _patch(monkeypatch, fake)
    with pytest.raises(CloudDHCPError, match="Multiple FortiGate interfaces"):
        await driver._apply_scope(_server(), _CREDS, _full_scope())


async def test_iface_ip_string_form(monkeypatch: pytest.MonkeyPatch) -> None:
    # FortiOS may return `ip` as a space-separated string instead of a list.
    fake = _FakeClient(
        {
            "get": [
                _ok([{"name": "port2", "ip": "192.168.20.1 255.255.255.0", "status": "up"}]),
                _ok([]),
            ],
            "post": [_write_ok()],
        }
    )
    driver = _patch(monkeypatch, fake)
    await driver._apply_scope(_server(), _CREDS, _full_scope())
    assert any(c["method"] == "post" for c in fake.calls)


# ── Create vs update (adopt) ─────────────────────────────────────────────


async def test_apply_scope_updates_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        {
            "get": [
                _ok([_iface("port2", "192.168.20.1", "255.255.255.0")]),
                _ok([{"id": 7, "interface": "port2"}]),  # existing server on port2
            ],
            "put": [_write_ok(7)],
        }
    )
    driver = _patch(monkeypatch, fake)
    await driver._apply_scope(_server(), _CREDS, _full_scope())
    put = next(c for c in fake.calls if c["method"] == "put")
    assert put["path"] == "/cmdb/system.dhcp/server/7"


async def test_remove_scope_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        {
            "get": [
                _ok([_iface("port2", "192.168.20.1", "255.255.255.0")]),
                _ok([{"id": 7, "interface": "port2"}]),
            ],
            "delete": [_write_ok(7)],
        }
    )
    driver = _patch(monkeypatch, fake)
    await driver._remove_scope(_server(), _CREDS, "192.168.20.0/24")
    delete = next(c for c in fake.calls if c["method"] == "delete")
    assert delete["path"] == "/cmdb/system.dhcp/server/7"


async def test_remove_scope_no_interface_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"get": [_ok([_iface("port1", "10.0.0.1", "255.255.255.0")])]})
    driver = _patch(monkeypatch, fake)
    # No matching interface → idempotent no-op (no delete issued, no raise).
    await driver._remove_scope(_server(), _CREDS, "192.168.20.0/24")
    assert not any(c["method"] == "delete" for c in fake.calls)


# ── Leases + probe ───────────────────────────────────────────────────────


async def test_get_leases(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        {
            "get": [
                _ok(
                    [
                        {
                            "ip": "192.168.20.101",
                            "mac": "aa:bb:cc:dd:ee:ff",
                            "hostname": "host1",
                            "expire_time": 1893456000,
                            "status": "leased",
                            "type": "ipv4",
                        },
                        {  # non-leased → filtered out
                            "ip": "192.168.20.102",
                            "mac": "aa:bb:cc:dd:ee:00",
                            "status": "expired",
                            "type": "ipv4",
                        },
                    ]
                )
            ]
        }
    )
    driver = _patch(monkeypatch, fake)
    leases = await driver._get_leases(_server(), _CREDS)
    assert len(leases) == 1
    assert leases[0]["ip_address"] == "192.168.20.101"
    assert leases[0]["mac_address"] == "aa:bb:cc:dd:ee:ff"
    assert leases[0]["state"] == "active"
    assert leases[0]["expires_at"] is not None


async def test_probe_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"get": [_ok([_iface("port2", "192.168.20.1", "255.255.255.0")])]})
    driver = _patch(monkeypatch, fake)
    result = await driver._probe(_server(), _CREDS)
    assert result.ok
    assert result.interface_count == 1


async def test_probe_via_public_method_handles_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Server with no credentials → probe() returns ok=False, never raises.
    driver = FortiGateDHCPDriver()
    result = await driver.probe(
        SimpleNamespace(id="x", name="fw", host="h", port=443, credentials_encrypted=None)
    )
    assert not result.ok


# ── Envelope error handling ──────────────────────────────────────────────


async def test_error_envelope_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"get": [_FakeResponse(403, {"status": "error", "error": -37})]})
    driver = _patch(monkeypatch, fake)
    with pytest.raises(CloudDHCPError, match="HTTP 403"):
        await driver._probe(_server(), _CREDS)
