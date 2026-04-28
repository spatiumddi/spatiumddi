"""Unit tests for the SNMP poller wrappers.

We don't talk to a real SNMP agent here. The pysnmp HLAPI shape is
mocked through ``app.services.snmp.poller._import_hlapi`` so we can
verify:

  * v1 / v2c / v3 auth construction picks up the right
    ``CommunityData`` / ``UsmUserData`` shape.
  * OID resolution lands the right bases for each table.
  * Fallback paths fire — ``walk_arp`` switches to
    ipNetToMediaTable on ``SNMPProtocolError`` from the
    modern table; ``walk_fdb`` falls back to dot1dTpFdbTable.
  * Errors map to the right exception type
    (``SNMPTimeoutError`` / ``SNMPAuthError`` / …).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.core.crypto import encrypt_str
from app.services.snmp import poller
from app.services.snmp.errors import (
    SNMPAuthError,
    SNMPProtocolError,
    SNMPTimeoutError,
    SNMPTransportError,
)
from app.services.snmp.oids import (
    OID_DOT1D_TP_FDB_PORT,
    OID_DOT1D_TP_FDB_STATUS,
    OID_DOT1Q_TP_FDB_PORT,
    OID_DOT1Q_TP_FDB_STATUS,
    OID_IF_DESCR,
    OID_IF_NAME,
    OID_IP_NTM_PHYS_ADDRESS,
    OID_IP_NTM_TYPE,
    OID_IP_NTP_PHYS_ADDRESS,
    OID_IP_NTP_STATE,
    OID_IP_NTP_TYPE,
    OID_SYS_DESCR,
    OID_SYS_NAME,
    OID_SYS_OBJECT_ID,
    OID_SYS_UP_TIME,
)

# ── Test doubles ────────────────────────────────────────────────────


class _StubVarbind:
    """Mimics the (oid, value) pair pysnmp yields. Indexable like a tuple."""

    def __init__(self, oid: str, value: Any) -> None:
        self._oid = oid
        self._value = value

    def __getitem__(self, idx: int) -> Any:
        return [self._oid, self._value][idx]


def _make_walk(by_base: dict[str, dict[str, Any]]):
    """Return an async iterator factory that yields the given OID → value
    fragments, regardless of which OIDs the caller actually asked for.

    Layout: ``by_base[base_oid] = {suffix: value}`` — the produced
    iterator emits one ``(error, status, idx, [varbind])`` tuple per
    leaf so the poller's varbind loop sees them.
    """

    async def _iter():
        for base, leaves in by_base.items():
            for suffix, value in leaves.items():
                full = f"{base}.{suffix}"
                yield (None, 0, 0, [_StubVarbind(full, value)])

    return _iter


def _hlapi_stub(*, walk_factory=None, get_result=None):
    """Build a stand-in for the pysnmp HLAPI module."""

    class _ObjectIdentity:
        def __init__(self, oid: str) -> None:
            self.oid = oid

    class _ObjectType:
        def __init__(self, ident: _ObjectIdentity) -> None:
            self.ident = ident

    class _SnmpEngine:
        pass

    class _ContextData:
        pass

    class _CommunityData:
        def __init__(self, community: str, mpModel: int = 1) -> None:  # noqa: N803
            self.community = community
            self.mpModel = mpModel

    class _UsmUserData:
        def __init__(self, security_name: str, **kwargs: Any) -> None:
            self.security_name = security_name
            self.kwargs = kwargs

    class _UdpTransportTarget:
        def __init__(self, address: tuple[str, int], **kwargs: Any) -> None:
            self.address = address
            self.kwargs = kwargs

        @classmethod
        async def create(
            cls, address: tuple[str, int], **kwargs: Any
        ) -> _UdpTransportTarget:  # noqa: D401
            return cls(address, **kwargs)

    def _bulk_walk(*_args: Any, **_kwargs: Any):
        if walk_factory is None:
            return _make_walk({})()
        return walk_factory()

    async def _get_cmd(*_args: Any, **_kwargs: Any):
        if get_result is None:
            return (None, 0, 0, [])
        return get_result

    return MagicMock(
        ObjectIdentity=_ObjectIdentity,
        ObjectType=_ObjectType,
        SnmpEngine=_SnmpEngine,
        ContextData=_ContextData,
        CommunityData=_CommunityData,
        UsmUserData=_UsmUserData,
        UdpTransportTarget=_UdpTransportTarget,
        bulkWalkCmd=_bulk_walk,
        getCmd=_get_cmd,
        # Auth/priv protocol sentinels — the poller looks them up by
        # name on the hlapi module via ``getattr``.
        usmHMACMD5AuthProtocol="md5",
        usmHMACSHAAuthProtocol="sha1",
        usmHMAC192SHA256AuthProtocol="sha256",
        usmHMAC256SHA384AuthProtocol="sha384",
        usmHMAC384SHA512AuthProtocol="sha512",
        usmDESPrivProtocol="des",
        usmAesCfb128Protocol="aes128",
        usmAesCfb256Protocol="aes256",
    )


def _make_device(
    *,
    snmp_version: str = "v2c",
    community: str | None = "public",
    v3_security_name: str | None = None,
    v3_security_level: str | None = None,
    v3_auth_protocol: str | None = None,
    v3_auth_key: str | None = None,
    v3_priv_protocol: str | None = None,
    v3_priv_key: str | None = None,
):
    """Construct a minimal NetworkDevice with the columns the poller reads."""
    from app.models.network import NetworkDevice

    dev = NetworkDevice(
        name="test",
        hostname="10.0.0.1",
        ip_address="10.0.0.1",
        snmp_version=snmp_version,
        snmp_port=161,
        snmp_timeout_seconds=2,
        snmp_retries=1,
        ip_space_id=uuid.uuid4(),
        community_encrypted=encrypt_str(community) if community else None,
        v3_security_name=v3_security_name,
        v3_security_level=v3_security_level,
        v3_auth_protocol=v3_auth_protocol,
        v3_auth_key_encrypted=encrypt_str(v3_auth_key) if v3_auth_key else None,
        v3_priv_protocol=v3_priv_protocol,
        v3_priv_key_encrypted=encrypt_str(v3_priv_key) if v3_priv_key else None,
    )
    dev.id = uuid.uuid4()
    return dev


# ── Auth construction ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_auth_v2c_uses_community_data() -> None:
    hlapi = _hlapi_stub()
    dev = _make_device(snmp_version="v2c", community="topsecret")
    auth = poller._build_auth(dev, hlapi)
    assert auth.community == "topsecret"
    assert auth.mpModel == 1


@pytest.mark.asyncio
async def test_build_auth_v1_uses_mp_model_zero() -> None:
    hlapi = _hlapi_stub()
    dev = _make_device(snmp_version="v1", community="public")
    auth = poller._build_auth(dev, hlapi)
    assert auth.mpModel == 0


@pytest.mark.asyncio
async def test_build_auth_v3_authpriv_resolves_protocols() -> None:
    hlapi = _hlapi_stub()
    dev = _make_device(
        snmp_version="v3",
        community=None,
        v3_security_name="snmpadmin",
        v3_security_level="authPriv",
        v3_auth_protocol="SHA256",
        v3_auth_key="auth-passphrase",
        v3_priv_protocol="AES128",
        v3_priv_key="priv-passphrase",
    )
    auth = poller._build_auth(dev, hlapi)
    assert auth.security_name == "snmpadmin"
    assert auth.kwargs["authProtocol"] == "sha256"
    assert auth.kwargs["privProtocol"] == "aes128"
    assert auth.kwargs["authKey"] == "auth-passphrase"
    assert auth.kwargs["privKey"] == "priv-passphrase"


@pytest.mark.asyncio
async def test_build_auth_v3_authnopriv_omits_priv_kwargs() -> None:
    hlapi = _hlapi_stub()
    dev = _make_device(
        snmp_version="v3",
        community=None,
        v3_security_name="snmpro",
        v3_security_level="authNoPriv",
        v3_auth_protocol="SHA",
        v3_auth_key="auth-passphrase",
    )
    auth = poller._build_auth(dev, hlapi)
    assert "privProtocol" not in auth.kwargs
    assert "privKey" not in auth.kwargs


@pytest.mark.asyncio
async def test_build_auth_missing_community_raises() -> None:
    hlapi = _hlapi_stub()
    dev = _make_device(snmp_version="v2c", community=None)
    with pytest.raises(SNMPAuthError):
        poller._build_auth(dev, hlapi)


# ── Error mapping ───────────────────────────────────────────────────


def test_classify_pysnmp_error_timeout() -> None:
    err = poller._classify_pysnmp_error("Request timed out", 0, 0)
    assert isinstance(err, SNMPTimeoutError)


def test_classify_pysnmp_error_auth() -> None:
    err = poller._classify_pysnmp_error("AuthenticationFailure", 0, 0)
    assert isinstance(err, SNMPAuthError)


def test_classify_pysnmp_error_unknown_user() -> None:
    err = poller._classify_pysnmp_error("UnknownUserName", 0, 0)
    assert isinstance(err, SNMPAuthError)


def test_classify_pysnmp_error_protocol() -> None:
    err = poller._classify_pysnmp_error(None, 5, 1)
    assert isinstance(err, SNMPProtocolError)


def test_classify_pysnmp_error_transport_fallback() -> None:
    err = poller._classify_pysnmp_error("Some other error", 0, 0)
    assert isinstance(err, SNMPTransportError)


def test_classify_pysnmp_error_success_is_none() -> None:
    assert poller._classify_pysnmp_error(None, 0, 0) is None


# ── test_connection ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_connection_decodes_sys_group() -> None:
    dev = _make_device()
    get_result = (
        None,
        0,
        0,
        [
            _StubVarbind(OID_SYS_DESCR, "Cisco IOS Software, C3560 Version 12.2"),
            _StubVarbind(OID_SYS_OBJECT_ID, "1.3.6.1.4.1.9.1.516"),
            _StubVarbind(OID_SYS_NAME, "switch1"),
            _StubVarbind(OID_SYS_UP_TIME, 1234567),
        ],
    )
    hlapi = _hlapi_stub(get_result=get_result)
    with patch.object(poller, "_import_hlapi", return_value=hlapi):
        info = await poller.test_connection(dev)
    assert info.sys_descr.startswith("Cisco IOS")
    assert info.sys_name == "switch1"
    # 1 234 567 / 100 == 12 345 seconds (~3.4 hours)
    assert info.sys_uptime_seconds == 12345
    assert info.vendor == "Cisco"


@pytest.mark.asyncio
async def test_test_connection_propagates_timeout() -> None:
    dev = _make_device()
    get_result = ("Request timed out (no response)", 0, 0, [])
    hlapi = _hlapi_stub(get_result=get_result)
    with patch.object(poller, "_import_hlapi", return_value=hlapi), pytest.raises(SNMPTimeoutError):
        await poller.test_connection(dev)


# ── walk_arp fallback ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_walk_arp_falls_back_to_legacy_when_modern_protocol_errors() -> None:
    dev = _make_device()

    call_count = {"n": 0}

    def factory():
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call: simulate noSuchObject by raising SNMPProtocolError
            async def bad():
                raise SNMPProtocolError("noSuchObject")
                yield  # pragma: no cover

            return bad()
        # Second call: legacy ipNetToMediaTable yields one v4 entry
        return _make_walk(
            {
                OID_IP_NTM_PHYS_ADDRESS: {"3.10.0.0.7": _StubMac("aa:bb:cc:dd:ee:ff")},
                OID_IP_NTM_TYPE: {"3.10.0.0.7": 3},
            }
        )()

    hlapi = _hlapi_stub(walk_factory=factory)
    with patch.object(poller, "_import_hlapi", return_value=hlapi):
        rows = await poller.walk_arp(dev)
    assert len(rows) == 1
    assert rows[0].ip_address == "10.0.0.7"
    assert rows[0].mac_address == "aa:bb:cc:dd:ee:ff"
    assert rows[0].address_type == "ipv4"
    # Legacy table has no state column.
    assert rows[0].state == "unknown"


@pytest.mark.asyncio
async def test_walk_arp_reads_modern_table_v4() -> None:
    dev = _make_device()

    # ipNetToPhysicalTable index: ifIndex=3, addrType=1 (ipv4), addrLen=4, addr=10.0.0.42
    suffix = "3.1.4.10.0.0.42"

    def factory():
        return _make_walk(
            {
                OID_IP_NTP_PHYS_ADDRESS: {suffix: _StubMac("11:22:33:44:55:66")},
                OID_IP_NTP_TYPE: {suffix: 3},  # dynamic
                OID_IP_NTP_STATE: {suffix: 1},  # reachable
            }
        )()

    hlapi = _hlapi_stub(walk_factory=factory)
    with patch.object(poller, "_import_hlapi", return_value=hlapi):
        rows = await poller.walk_arp(dev)
    assert len(rows) == 1
    assert rows[0].ip_address == "10.0.0.42"
    assert rows[0].address_type == "ipv4"
    assert rows[0].state == "reachable"
    assert rows[0].mac_address == "11:22:33:44:55:66"


# ── walk_fdb fallback ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_walk_fdb_uses_qbridge_when_available() -> None:
    dev = _make_device()
    # vlan=10, mac=aabbccddeeff → suffix "10.170.187.204.221.238.255"
    q_suffix = "10.170.187.204.221.238.255"

    def factory():
        # First call: dot1dBasePortIfIndex (port 5 → ifIndex 105)
        # Second call: Q-BRIDGE
        if not hasattr(factory, "phase"):
            factory.phase = 0
        factory.phase += 1
        if factory.phase == 1:
            return _make_walk({"1.3.6.1.2.1.17.1.4.1.2": {"5": 105}})()
        if factory.phase == 2:
            return _make_walk(
                {
                    OID_DOT1Q_TP_FDB_PORT: {q_suffix: 5},
                    OID_DOT1Q_TP_FDB_STATUS: {q_suffix: 3},  # learned
                }
            )()
        return _make_walk({})()

    hlapi = _hlapi_stub(walk_factory=factory)
    with patch.object(poller, "_import_hlapi", return_value=hlapi):
        rows = await poller.walk_fdb(dev)
    assert len(rows) == 1
    assert rows[0].vlan_id == 10
    assert rows[0].mac_address == "aa:bb:cc:dd:ee:ff"
    assert rows[0].fdb_type == "learned"
    assert rows[0].if_index == 105


@pytest.mark.asyncio
async def test_walk_fdb_falls_back_to_bridge_mib() -> None:
    dev = _make_device()
    # Legacy index = mac bytes only
    legacy_suffix = "1.2.3.4.5.6"

    def factory():
        if not hasattr(factory, "phase"):
            factory.phase = 0
        factory.phase += 1
        if factory.phase == 1:
            # bridge port → ifIndex map
            return _make_walk({"1.3.6.1.2.1.17.1.4.1.2": {"7": 207}})()
        if factory.phase == 2:
            # Q-BRIDGE walk fails (noSuchObject)
            async def bad():
                raise SNMPProtocolError("noSuchObject")
                yield  # pragma: no cover

            return bad()
        if factory.phase == 3:
            return _make_walk(
                {
                    OID_DOT1D_TP_FDB_PORT: {legacy_suffix: 7},
                    OID_DOT1D_TP_FDB_STATUS: {legacy_suffix: 3},
                }
            )()
        return _make_walk({})()

    hlapi = _hlapi_stub(walk_factory=factory)
    with patch.object(poller, "_import_hlapi", return_value=hlapi):
        rows = await poller.walk_fdb(dev)
    assert len(rows) == 1
    assert rows[0].vlan_id is None
    assert rows[0].mac_address == "01:02:03:04:05:06"
    assert rows[0].if_index == 207


# ── walk_interfaces ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_walk_interfaces_merges_iftable_and_ifxtable() -> None:
    dev = _make_device()

    def factory():
        return _make_walk(
            {
                OID_IF_DESCR: {"3": "GigabitEthernet0/3"},
                OID_IF_NAME: {"3": "Gi0/3"},
                "1.3.6.1.2.1.31.1.1.1.18": {"3": "uplink"},  # ifAlias
                "1.3.6.1.2.1.2.2.1.7": {"3": 1},  # admin up
                "1.3.6.1.2.1.2.2.1.8": {"3": 1},  # oper up
                "1.3.6.1.2.1.2.2.1.5": {"3": 1_000_000_000},  # ifSpeed 1 Gb/s
                "1.3.6.1.2.1.31.1.1.1.15": {"3": 0},  # ifHighSpeed (force fall-back)
            }
        )()

    hlapi = _hlapi_stub(walk_factory=factory)
    with patch.object(poller, "_import_hlapi", return_value=hlapi):
        rows = await poller.walk_interfaces(dev)
    assert len(rows) == 1
    assert rows[0].if_index == 3
    assert rows[0].name == "Gi0/3"
    assert rows[0].alias == "uplink"
    assert rows[0].admin_status == "up"
    assert rows[0].oper_status == "up"
    assert rows[0].speed_bps == 1_000_000_000


# ── helper ──────────────────────────────────────────────────────────


class _StubMac:
    """Mimics a pysnmp OctetString carrying a 6-byte MAC."""

    def __init__(self, formatted: str) -> None:
        self._raw = bytes(int(b, 16) for b in formatted.split(":"))

    def asOctets(self) -> bytes:  # noqa: N802 — pysnmp API
        return self._raw

    def __str__(self) -> str:
        return ":".join(f"{b:02x}" for b in self._raw)
