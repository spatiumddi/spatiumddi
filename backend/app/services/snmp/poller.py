"""Async SNMP wrappers built on top of pysnmp 6.x's HLAPI.

Public surface:

* :func:`test_connection` — fetches the system group; lets the API's
  ``Test Connection`` button succeed/fail in seconds without touching
  the table walks.
* :func:`walk_interfaces` — IF-MIB ifTable + ifXTable.
* :func:`walk_arp` — IP-MIB ipNetToPhysicalTable, with RFC1213-MIB
  ipNetToMediaTable fallback for v4-only legacy boxes.
* :func:`walk_fdb` — Q-BRIDGE-MIB dot1qTpFdbTable, with BRIDGE-MIB
  dot1dTpFdbTable fallback for VLAN-unaware switches.

Each function inspects ``device.snmp_version`` and constructs the
right ``CommunityData`` (v1 / v2c) or ``UsmUserData`` (v3) before
running. Plaintext secrets are decrypted from the device row via
``app.core.crypto`` for the duration of the call only.

Errors are normalised to the ``SNMPTimeoutError`` /
``SNMPAuthError`` / ``SNMPTransportError`` / ``SNMPProtocolError``
hierarchy in ``errors.py`` so the calling Celery task can map them
to a clean ``last_poll_status`` without parsing pysnmp tracebacks.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from app.core.crypto import decrypt_str

from .errors import (
    SNMPAuthError,
    SNMPProtocolError,
    SNMPTimeoutError,
    SNMPTransportError,
)
from .oids import (  # noqa: F401  (some imports are surface API)
    OID_DOT1D_BASE_PORT_IF_INDEX,
    OID_DOT1D_TP_FDB_PORT,
    OID_DOT1D_TP_FDB_STATUS,
    OID_DOT1Q_TP_FDB_PORT,
    OID_DOT1Q_TP_FDB_STATUS,
    OID_IF_ADMIN_STATUS,
    OID_IF_ALIAS,
    OID_IF_DESCR,
    OID_IF_HIGH_SPEED,
    OID_IF_LAST_CHANGE,
    OID_IF_NAME,
    OID_IF_OPER_STATUS,
    OID_IF_PHYS_ADDRESS,
    OID_IF_SPEED,
    OID_IP_NTM_PHYS_ADDRESS,
    OID_IP_NTM_TYPE,
    OID_IP_NTP_PHYS_ADDRESS,
    OID_IP_NTP_STATE,
    OID_IP_NTP_TYPE,
    OID_LLDP_REM_CHASSIS_ID,
    OID_LLDP_REM_CHASSIS_ID_SUBTYPE,
    OID_LLDP_REM_PORT_DESC,
    OID_LLDP_REM_PORT_ID,
    OID_LLDP_REM_PORT_ID_SUBTYPE,
    OID_LLDP_REM_SYS_CAP_ENABLED,
    OID_LLDP_REM_SYS_DESC,
    OID_LLDP_REM_SYS_NAME,
    OID_SYS_DESCR,
    OID_SYS_NAME,
    OID_SYS_OBJECT_ID,
    OID_SYS_UP_TIME,
)

if TYPE_CHECKING:
    from app.models.network import NetworkDevice

logger = structlog.get_logger(__name__)


# ── Public dataclasses ──────────────────────────────────────────────


@dataclass(frozen=True)
class SysInfo:
    sys_descr: str | None
    sys_object_id: str | None
    sys_name: str | None
    sys_uptime_seconds: int | None
    vendor: str | None  # derived heuristically from sysDescr / sysObjectID


@dataclass(frozen=True)
class InterfaceData:
    if_index: int
    name: str
    alias: str | None
    description: str | None
    speed_bps: int | None
    mac_address: str | None
    admin_status: str | None
    oper_status: str | None
    last_change_seconds: int | None


@dataclass(frozen=True)
class ArpData:
    if_index: int | None
    ip_address: str
    mac_address: str
    address_type: str  # "ipv4" | "ipv6"
    state: str  # reachable | stale | delay | probe | invalid | unknown


@dataclass(frozen=True)
class FdbData:
    if_index: int  # bridge port already resolved → ifIndex
    mac_address: str
    vlan_id: int | None
    fdb_type: str  # learned | static | mgmt | other


@dataclass(frozen=True)
class NeighbourData:
    """One LLDP neighbour seen on a local interface.

    Both ``chassis_id`` and ``port_id`` carry the same opaque-binary
    semantics as the wire — formatting depends on the corresponding
    subtype enum (``chassis_id_subtype`` / ``port_id_subtype``) which
    is preserved for the persistence layer + UI to render correctly.
    """

    if_index: int  # local-port-num from the lldpRem index, treated as ifIndex
    chassis_id_subtype: int  # 1..7 per LLDP-MIB LldpChassisIdSubtype
    chassis_id: str  # decoded for the common subtypes (4=mac, 7=local), hex otherwise
    port_id_subtype: int  # 1..7 per LLDP-MIB LldpPortIdSubtype
    port_id: str
    port_desc: str | None
    sys_name: str | None
    sys_desc: str | None
    sys_cap_enabled: int | None  # bitmask per LLDP-MIB LldpSystemCapabilitiesMap


# ── IF-MIB enum mapping ─────────────────────────────────────────────

_IF_STATUS_MAP: dict[int, str] = {
    1: "up",
    2: "down",
    3: "testing",
    4: "unknown",
    5: "dormant",
    6: "notPresent",
    7: "lowerLayerDown",
}

_IP_NTP_STATE_MAP: dict[int, str] = {
    1: "reachable",
    2: "stale",
    3: "delay",
    4: "probe",
    5: "invalid",
    6: "unknown",
    7: "incomplete",
}

# IP-MIB InetAddressType — present in the column index of
# ipNetToPhysicalTable so we can tell IPv4 (1) from IPv6 (2).
_IP_NTP_ADDR_TYPE = {1: "ipv4", 2: "ipv6"}

_FDB_STATUS_MAP: dict[int, str] = {
    1: "other",
    2: "invalid",
    3: "learned",
    4: "self",
    5: "mgmt",
}

# LLDP-MIB LldpChassisIdSubtype + LldpPortIdSubtype enums. We don't
# translate to strings inside the poller — the persistence layer
# stores the raw int so the formatter has full fidelity. The
# constant lives here for readability of the format helpers below.
LLDP_CHASSIS_ID_SUBTYPES: dict[int, str] = {
    1: "chassisComponent",
    2: "interfaceAlias",
    3: "portComponent",
    4: "macAddress",
    5: "networkAddress",
    6: "interfaceName",
    7: "local",
}
LLDP_PORT_ID_SUBTYPES: dict[int, str] = {
    1: "interfaceAlias",
    2: "portComponent",
    3: "macAddress",
    4: "networkAddress",
    5: "interfaceName",
    6: "agentCircuitId",
    7: "local",
}

# Vendor heuristics from sysDescr substrings. Order matters — first
# substring match wins, so put more specific names higher (e.g.
# "MikroTik" before "Linux" because MikroTik routers identify their
# kernel as Linux in some firmware variants).
_VENDOR_HINTS: tuple[tuple[str, str], ...] = (
    ("Cisco", "Cisco"),
    ("Juniper", "Juniper"),
    ("Arista", "Arista"),
    ("Aruba", "Aruba"),
    ("HP ProCurve", "HP"),
    ("HPE Comware", "HPE"),
    ("MikroTik", "MikroTik"),
    ("RouterOS", "MikroTik"),
    ("FortiGate", "Fortinet"),
    ("FortiOS", "Fortinet"),
    ("OPNsense", "OPNsense"),
    ("pfSense", "pfSense"),
    ("Cumulus", "Cumulus"),
    ("SONiC", "SONiC"),
    ("Ubiquiti", "Ubiquiti"),
    ("EdgeOS", "Ubiquiti"),
    ("UniFi", "Ubiquiti"),
    ("Extreme", "Extreme"),
    ("Brocade", "Brocade"),
    ("Dell", "Dell"),
    ("Huawei", "Huawei"),
    ("Linux", "Linux/net-snmp"),
    ("Net-SNMP", "Linux/net-snmp"),
)


def _try_int(value: Any) -> int | None:
    """Best-effort ``int(value)`` — returns None if the cast fails or
    the value is None / unset. Keeps the table-walk loops short on
    error handling without a sea of try/except blocks."""
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _guess_vendor(sys_descr: str | None, sys_object_id: str | None) -> str | None:
    """Best-effort vendor name from sysDescr substring matching.

    sysObjectID isn't currently mapped — proper vendor identification
    via PEN (Private Enterprise Number) would require a static table
    we can grow later. The substring approach gets ~95 % of operator
    devices right with no maintenance burden.
    """
    if sys_descr:
        for needle, vendor in _VENDOR_HINTS:
            if needle.lower() in sys_descr.lower():
                return vendor
    return None


# ── pysnmp engine resolution ────────────────────────────────────────
#
# pysnmp 6.x exposes the async HLAPI under
# ``pysnmp.hlapi.v3arch.asyncio``. Older 4.x ships the same symbols in
# ``pysnmp.hlapi.asyncio``. We resolve at call time so test stubs can
# patch the resulting symbols cleanly without faking an entire module.


def _import_hlapi() -> Any:
    """Return the pysnmp asyncio HLAPI module — picks the right path
    for whichever pysnmp the operator has installed.

    Kept inside a function instead of at module top-level so importing
    this module never fails on a fresh checkout where ``pysnmp`` isn't
    yet installed (the tests patch the public functions; the import
    chain for ``app.services.snmp`` should not require the dependency
    just to read constants).
    """
    try:
        from pysnmp.hlapi.v3arch import asyncio as hlapi  # type: ignore[import-not-found]

        return hlapi
    except ImportError:
        try:
            from pysnmp.hlapi import asyncio as hlapi  # type: ignore[import-not-found]

            return hlapi
        except ImportError as exc:
            raise SNMPTransportError(
                "pysnmp is not installed in this environment; SNMP polling is unavailable"
            ) from exc


_AUTH_PROTO_NAMES = {
    "MD5": "usmHMACMD5AuthProtocol",
    "SHA": "usmHMACSHAAuthProtocol",
    "SHA224": "usmHMAC128SHA224AuthProtocol",
    "SHA256": "usmHMAC192SHA256AuthProtocol",
    "SHA384": "usmHMAC256SHA384AuthProtocol",
    "SHA512": "usmHMAC384SHA512AuthProtocol",
}

_PRIV_PROTO_NAMES = {
    "DES": "usmDESPrivProtocol",
    "3DES": "usm3DESEDEPrivProtocol",
    "AES128": "usmAesCfb128Protocol",
    "AES192": "usmAesCfb192Protocol",
    "AES256": "usmAesCfb256Protocol",
}

_LEVEL_NAMES = {
    "noAuthNoPriv": "noAuthNoPriv",
    "authNoPriv": "authNoPriv",
    "authPriv": "authPriv",
}


def _build_auth(device: NetworkDevice, hlapi: Any) -> Any:
    """Construct ``CommunityData`` / ``UsmUserData`` from a device.

    Decrypts the Fernet-encrypted secrets in-process; the plaintext
    never lives outside this scope.
    """
    if device.snmp_version in ("v1", "v2c"):
        if not device.community_encrypted:
            raise SNMPAuthError("v1 / v2c device has no stored community string")
        community = decrypt_str(device.community_encrypted)
        mp_model = 0 if device.snmp_version == "v1" else 1
        return hlapi.CommunityData(community, mpModel=mp_model)

    if device.snmp_version == "v3":
        if not device.v3_security_name:
            raise SNMPAuthError("v3 device has no security_name")

        level = device.v3_security_level or "noAuthNoPriv"
        kwargs: dict[str, Any] = {}

        if level in ("authNoPriv", "authPriv"):
            if not device.v3_auth_key_encrypted or not device.v3_auth_protocol:
                raise SNMPAuthError("v3 authNoPriv/authPriv requires auth key + protocol")
            proto_name = _AUTH_PROTO_NAMES.get(device.v3_auth_protocol)
            if proto_name is None:
                raise SNMPAuthError(f"unknown v3 auth protocol: {device.v3_auth_protocol}")
            kwargs["authProtocol"] = getattr(hlapi, proto_name)
            kwargs["authKey"] = decrypt_str(device.v3_auth_key_encrypted)

        if level == "authPriv":
            if not device.v3_priv_key_encrypted or not device.v3_priv_protocol:
                raise SNMPAuthError("v3 authPriv requires priv key + protocol")
            proto_name = _PRIV_PROTO_NAMES.get(device.v3_priv_protocol)
            if proto_name is None:
                raise SNMPAuthError(f"unknown v3 priv protocol: {device.v3_priv_protocol}")
            kwargs["privProtocol"] = getattr(hlapi, proto_name)
            kwargs["privKey"] = decrypt_str(device.v3_priv_key_encrypted)

        return hlapi.UsmUserData(device.v3_security_name, **kwargs)

    raise SNMPAuthError(f"unsupported snmp_version: {device.snmp_version}")


async def _build_target(device: NetworkDevice, hlapi: Any) -> Any:
    """``UdpTransportTarget.create`` builds an asyncio-capable target
    in pysnmp 6.x. 4.x just uses the ``UdpTransportTarget(...)``
    constructor synchronously. We probe for ``create`` first.

    Address selection: prefer ``ip_address`` (always present, always
    a parseable INET), fall back to ``hostname`` only when the IP
    column is empty. The form treats hostname as optional / display
    only — operators shouldn't have to enter the same address twice.
    """
    host = str(device.ip_address) if device.ip_address else device.hostname
    addr = (host, device.snmp_port)
    timeout = device.snmp_timeout_seconds
    retries = device.snmp_retries
    if hasattr(hlapi.UdpTransportTarget, "create"):
        return await hlapi.UdpTransportTarget.create(addr, timeout=timeout, retries=retries)
    return hlapi.UdpTransportTarget(addr, timeout=timeout, retries=retries)


def _format_mac(value: Any) -> str | None:
    """Coerce an OctetString / bytes / str MAC into ``aa:bb:cc:dd:ee:ff``.

    pysnmp returns MACs as 6-byte ``OctetString``; ``str(value)`` on
    those is binary garbage. We pull ``asOctets`` when available and
    fall back to a hex-pair regex when the agent already pretty-prints.
    """
    if value is None:
        return None
    raw: bytes
    if hasattr(value, "asOctets"):
        raw = bytes(value.asOctets())
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, str):
        # Already-formatted? Strip separators and re-grow.
        hex_only = re.sub(r"[^0-9a-fA-F]", "", value)
        if len(hex_only) != 12:
            return None
        return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2)).lower()
    else:
        return None
    if len(raw) != 6:
        return None
    return ":".join(f"{b:02x}" for b in raw)


def _format_ip(value: Any, address_type: str) -> str | None:
    """Coerce a packed-bytes INET address into dotted-quad / colon-hex.

    ipNetToPhysicalTable's address column is encoded as a 4- or 16-
    byte ``OctetString`` carrying the raw network-order address. We
    split on length so a malformed agent value can't crash the walk.
    """
    if value is None:
        return None
    raw: bytes
    if hasattr(value, "asOctets"):
        raw = bytes(value.asOctets())
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, str):
        return value.strip() or None
    else:
        return None

    import ipaddress

    try:
        if address_type == "ipv4" and len(raw) == 4:
            return str(ipaddress.IPv4Address(raw))
        if address_type == "ipv6" and len(raw) == 16:
            return str(ipaddress.IPv6Address(raw))
        # Some agents send v4 bytes with the v6 type tag — try both.
        if len(raw) == 4:
            return str(ipaddress.IPv4Address(raw))
        if len(raw) == 16:
            return str(ipaddress.IPv6Address(raw))
    except (ValueError, ipaddress.AddressValueError):
        return None
    return None


def _classify_pysnmp_error(
    error_indication: Any, error_status: Any, error_index: Any
) -> Exception | None:
    """Map a pysnmp varbind result tuple to one of our error types.

    Returns ``None`` when the result is success.
    """
    if error_indication:
        msg = str(error_indication)
        lowered = msg.lower()
        if "timeout" in lowered or "timed out" in lowered or "no response" in lowered:
            return SNMPTimeoutError(msg)
        if (
            "authentication" in lowered
            or "decryption" in lowered
            or "wrongdigest" in lowered
            or "unknownusername" in lowered
            or "unsupportedsec" in lowered
        ):
            return SNMPAuthError(msg)
        return SNMPTransportError(msg)
    if error_status:
        # status > 0 is a PDU-level error; surface verbatim.
        return SNMPProtocolError(
            f"agent returned error_status={error_status} at index={error_index}"
        )
    return None


# ── Walks ───────────────────────────────────────────────────────────


async def _walk_oids(
    device: NetworkDevice, oids: Iterable[str], hlapi: Any | None = None
) -> AsyncIterator[tuple[str, Any]]:
    """Yield ``(oid_str, value)`` pairs from a bulk-walk over ``oids``.

    Walks each OID column **independently** (one bulkWalkCmd per OID)
    rather than batching them into a single GETBULK PDU. Why: when
    pysnmp packs N column OIDs into a single GETBULK with
    ``max-repetitions=R``, the device must respond with ``N*R``
    varbinds in one PDU. Devices with conservative SNMP engines
    (UniFi switches, busy gateways, anything running a stripped
    net-snmp) silently drop those large PDUs — the request never
    gets a reply and we hit the per-request timeout. Per-column
    walks keep each PDU small (R varbinds) and match what
    ``snmpbulkwalk -c X HOST OID`` does, which is what works
    reliably against every device we've tested.

    ``max-repetitions`` reduced to 10 (from 25) for the same
    defence-in-depth reason: smaller PDUs round-trip more reliably
    over flaky links and through middleboxes that fragment large
    UDP responses. Cost is ~2× more round-trips per column; on
    a 10ms-RTT LAN that's still sub-second per device.

    Falls back to repeated GETNEXT internally for SNMPv1 (pysnmp
    handles that automatically inside ``bulkWalkCmd``).
    """
    hlapi = hlapi or _import_hlapi()
    auth = _build_auth(device, hlapi)
    target = await _build_target(device, hlapi)

    for oid in oids:
        walker = hlapi.bulkWalkCmd(
            hlapi.SnmpEngine(),
            auth,
            target,
            hlapi.ContextData(),
            0,  # non-repeaters
            10,  # max-repetitions per PDU (was 25 — see above)
            hlapi.ObjectType(hlapi.ObjectIdentity(oid)),
            lexicographicMode=False,
        )

        async for ei, es, eidx, varbinds in walker:
            err = _classify_pysnmp_error(ei, es, eidx)
            if err is not None:
                raise err
            for vb in varbinds:
                yield str(vb[0]), vb[1]


async def test_connection(device: NetworkDevice) -> SysInfo:
    """One-shot scalar probe — returns the system group.

    Uses a single ``getCmd`` over the four sys* scalars. Any error
    (transport / auth / timeout / protocol) is normalised; on success
    we attach a vendor heuristic so the caller can prefill the
    ``vendor`` column without a follow-up query.
    """
    hlapi = _import_hlapi()
    auth = _build_auth(device, hlapi)
    target = await _build_target(device, hlapi)

    object_types = [
        hlapi.ObjectType(hlapi.ObjectIdentity(OID_SYS_DESCR)),
        hlapi.ObjectType(hlapi.ObjectIdentity(OID_SYS_OBJECT_ID)),
        hlapi.ObjectType(hlapi.ObjectIdentity(OID_SYS_NAME)),
        hlapi.ObjectType(hlapi.ObjectIdentity(OID_SYS_UP_TIME)),
    ]

    ei, es, eidx, varbinds = await hlapi.getCmd(
        hlapi.SnmpEngine(),
        auth,
        target,
        hlapi.ContextData(),
        *object_types,
    )
    err = _classify_pysnmp_error(ei, es, eidx)
    if err is not None:
        raise err

    sys_descr = str(varbinds[0][1]) if varbinds[0][1] is not None else None
    sys_object_id = str(varbinds[1][1]) if varbinds[1][1] is not None else None
    sys_name = str(varbinds[2][1]) if varbinds[2][1] is not None else None
    raw_uptime = varbinds[3][1]
    # sysUpTime is in TimeTicks (1/100 s); convert to whole seconds.
    sys_uptime_seconds: int | None = None
    if raw_uptime is not None:
        try:
            sys_uptime_seconds = int(int(raw_uptime) / 100)
        except (TypeError, ValueError):
            sys_uptime_seconds = None

    return SysInfo(
        sys_descr=sys_descr,
        sys_object_id=sys_object_id,
        sys_name=sys_name,
        sys_uptime_seconds=sys_uptime_seconds,
        vendor=_guess_vendor(sys_descr, sys_object_id),
    )


def _suffix_after(oid_str: str, base: str) -> str | None:
    """Return the suffix of ``oid_str`` that follows ``base.`` (with
    the trailing dot included by the caller)."""
    if oid_str.startswith(base + "."):
        return oid_str[len(base) + 1 :]
    return None


async def walk_interfaces(device: NetworkDevice) -> list[InterfaceData]:
    """Return one row per ifTable entry merged with ifXTable extensions."""
    hlapi = _import_hlapi()

    by_index: dict[int, dict[str, Any]] = {}

    async for oid_str, value in _walk_oids(
        device,
        [
            OID_IF_DESCR,
            OID_IF_PHYS_ADDRESS,
            OID_IF_SPEED,
            OID_IF_ADMIN_STATUS,
            OID_IF_OPER_STATUS,
            OID_IF_LAST_CHANGE,
            OID_IF_NAME,
            OID_IF_HIGH_SPEED,
            OID_IF_ALIAS,
        ],
        hlapi=hlapi,
    ):
        for base, key in (
            (OID_IF_DESCR, "descr"),
            (OID_IF_PHYS_ADDRESS, "mac"),
            (OID_IF_SPEED, "speed_low"),
            (OID_IF_ADMIN_STATUS, "admin"),
            (OID_IF_OPER_STATUS, "oper"),
            (OID_IF_LAST_CHANGE, "last_change"),
            (OID_IF_NAME, "name"),
            (OID_IF_HIGH_SPEED, "speed_high"),
            (OID_IF_ALIAS, "alias"),
        ):
            suffix = _suffix_after(oid_str, base)
            if suffix is None:
                continue
            try:
                if_index = int(suffix)
            except ValueError:
                continue
            by_index.setdefault(if_index, {})[key] = value
            break

    out: list[InterfaceData] = []
    for if_index, fields in sorted(by_index.items()):
        # ifHighSpeed is in megabits/s (32-bit) and is the source of
        # truth on links faster than 4.29 Gb/s. Multiply up to bps so
        # the schema column is consistent across speeds.
        speed_bps: int | None = None
        if "speed_high" in fields:
            try:
                hi = int(fields["speed_high"])
                if hi:
                    speed_bps = hi * 1_000_000
            except (TypeError, ValueError):
                pass
        if speed_bps is None and "speed_low" in fields:
            try:
                speed_bps = int(fields["speed_low"])
            except (TypeError, ValueError):
                pass

        admin = _try_int(fields.get("admin"))
        oper = _try_int(fields.get("oper"))
        last_change = _try_int(fields.get("last_change"))

        name = (
            str(fields.get("name") or "").strip()
            or str(fields.get("descr") or "").strip()
            or f"if{if_index}"
        )
        alias = str(fields.get("alias")).strip() if fields.get("alias") is not None else None
        if alias == "":
            alias = None

        out.append(
            InterfaceData(
                if_index=if_index,
                name=name,
                alias=alias,
                description=str(fields["descr"]).strip() if "descr" in fields else None,
                speed_bps=speed_bps,
                mac_address=_format_mac(fields.get("mac")),
                admin_status=_IF_STATUS_MAP.get(admin) if admin is not None else None,
                oper_status=_IF_STATUS_MAP.get(oper) if oper is not None else None,
                last_change_seconds=(last_change // 100) if last_change is not None else None,
            )
        )
    return out


def _parse_ip_ntp_index(suffix: str) -> tuple[int, str, list[int]] | None:
    """Decode the ipNetToPhysicalTable index suffix.

    Layout is ``ifIndex.addrType.addrLen.<addrLen bytes of address>``.
    Returns ``(ifIndex, address_type, addr_bytes)`` or ``None`` when
    the suffix is malformed.
    """
    parts = suffix.split(".")
    if len(parts) < 4:
        return None
    try:
        if_index = int(parts[0])
        addr_type = int(parts[1])
        addr_len = int(parts[2])
    except ValueError:
        return None
    if addr_type not in _IP_NTP_ADDR_TYPE:
        return None
    if len(parts) < 3 + addr_len:
        return None
    try:
        addr_bytes = [int(p) for p in parts[3 : 3 + addr_len]]
    except ValueError:
        return None
    return if_index, _IP_NTP_ADDR_TYPE[addr_type], addr_bytes


def _bytes_to_ip(addr_bytes: list[int], address_type: str) -> str | None:
    """Format a packed-byte IP from the index segment."""
    import ipaddress

    try:
        raw = bytes(addr_bytes)
        if address_type == "ipv4" and len(raw) == 4:
            return str(ipaddress.IPv4Address(raw))
        if address_type == "ipv6" and len(raw) == 16:
            return str(ipaddress.IPv6Address(raw))
    except (ValueError, ipaddress.AddressValueError):
        return None
    return None


async def walk_arp(device: NetworkDevice) -> list[ArpData]:
    """Walk ipNetToPhysicalTable. Falls back to ipNetToMediaTable
    on noSuchObject (legacy v4-only agents).
    """
    hlapi = _import_hlapi()
    rows: dict[tuple[int, str, str], dict[str, Any]] = {}

    try:
        async for oid_str, value in _walk_oids(
            device,
            [OID_IP_NTP_PHYS_ADDRESS, OID_IP_NTP_TYPE, OID_IP_NTP_STATE],
            hlapi=hlapi,
        ):
            for base, key in (
                (OID_IP_NTP_PHYS_ADDRESS, "mac"),
                (OID_IP_NTP_TYPE, "type"),
                (OID_IP_NTP_STATE, "state"),
            ):
                suffix = _suffix_after(oid_str, base)
                if suffix is None:
                    continue
                parsed = _parse_ip_ntp_index(suffix)
                if parsed is None:
                    break
                if_index, address_type, addr_bytes = parsed
                ip_str = _bytes_to_ip(addr_bytes, address_type)
                if ip_str is None:
                    break
                rows.setdefault(
                    (if_index, address_type, ip_str),
                    {"if_index": if_index, "address_type": address_type, "ip": ip_str},
                )[key] = value
                break
    except SNMPProtocolError:
        # noSuchObject on the modern table → fall back below.
        rows = {}

    if not rows:
        return await _walk_arp_legacy(device, hlapi=hlapi)

    out: list[ArpData] = []
    for fields in rows.values():
        mac = _format_mac(fields.get("mac"))
        if mac is None:
            continue
        # Filter agent-internal "invalid" rows (state 5 / type 2).
        state_int = _try_int(fields.get("state"))
        type_int = _try_int(fields.get("type"))
        if type_int == 2:  # invalid
            continue

        state_str = _IP_NTP_STATE_MAP.get(state_int, "unknown") if state_int else "unknown"
        out.append(
            ArpData(
                if_index=fields["if_index"],
                ip_address=fields["ip"],
                mac_address=mac,
                address_type=fields["address_type"],
                state=state_str,
            )
        )
    return out


async def _walk_arp_legacy(device: NetworkDevice, hlapi: Any) -> list[ArpData]:
    """ipNetToMediaTable fallback. v4-only; indexed by (ifIndex, IPv4)."""
    rows: dict[tuple[int, str], dict[str, Any]] = {}
    async for oid_str, value in _walk_oids(
        device, [OID_IP_NTM_PHYS_ADDRESS, OID_IP_NTM_TYPE], hlapi=hlapi
    ):
        for base, key in (
            (OID_IP_NTM_PHYS_ADDRESS, "mac"),
            (OID_IP_NTM_TYPE, "type"),
        ):
            suffix = _suffix_after(oid_str, base)
            if suffix is None:
                continue
            parts = suffix.split(".")
            if len(parts) < 5:
                break
            try:
                if_index = int(parts[0])
                addr_bytes = [int(parts[i]) for i in range(1, 5)]
            except ValueError:
                break
            ip_str = _bytes_to_ip(addr_bytes, "ipv4")
            if ip_str is None:
                break
            rows.setdefault(
                (if_index, ip_str),
                {"if_index": if_index, "ip": ip_str},
            )[key] = value
            break

    out: list[ArpData] = []
    for fields in rows.values():
        mac = _format_mac(fields.get("mac"))
        if mac is None:
            continue
        type_int = _try_int(fields.get("type"))
        if type_int == 2:  # invalid
            continue
        out.append(
            ArpData(
                if_index=fields["if_index"],
                ip_address=fields["ip"],
                mac_address=mac,
                address_type="ipv4",
                # Legacy table has no state — operators get "unknown" here.
                state="unknown",
            )
        )
    return out


async def walk_fdb(device: NetworkDevice) -> list[FdbData]:
    """Walk dot1qTpFdbTable; fall back to dot1dTpFdbTable when the
    Q-BRIDGE table isn't supported.

    Both tables index by bridge-port number, not ifIndex; we walk
    dot1dBasePortIfIndex once and apply the mapping afterwards. Rows
    whose port has no ifIndex mapping are dropped (rare; usually
    indicates a phantom learn entry).
    """
    hlapi = _import_hlapi()

    # Bridge-port → ifIndex mapping (shared across both fdb tables).
    port_to_ifindex: dict[int, int] = {}
    try:
        async for oid_str, value in _walk_oids(device, [OID_DOT1D_BASE_PORT_IF_INDEX], hlapi=hlapi):
            suffix = _suffix_after(oid_str, OID_DOT1D_BASE_PORT_IF_INDEX)
            if suffix is None:
                continue
            try:
                port = int(suffix)
                ifidx = int(value)
            except (TypeError, ValueError):
                continue
            port_to_ifindex[port] = ifidx
    except SNMPProtocolError:
        port_to_ifindex = {}

    # Try Q-BRIDGE first.
    rows: list[FdbData] = []
    q_rows: dict[tuple[int, str], dict[str, Any]] = {}
    used_legacy = False
    try:
        async for oid_str, value in _walk_oids(
            device, [OID_DOT1Q_TP_FDB_PORT, OID_DOT1Q_TP_FDB_STATUS], hlapi=hlapi
        ):
            for base, key in (
                (OID_DOT1Q_TP_FDB_PORT, "port"),
                (OID_DOT1Q_TP_FDB_STATUS, "status"),
            ):
                suffix = _suffix_after(oid_str, base)
                if suffix is None:
                    continue
                parts = suffix.split(".")
                if len(parts) < 7:  # vlan + 6 mac bytes
                    break
                try:
                    vlan_id = int(parts[0])
                    mac_bytes = bytes(int(p) for p in parts[1:7])
                except ValueError:
                    break
                mac = ":".join(f"{b:02x}" for b in mac_bytes) if len(mac_bytes) == 6 else None
                if mac is None:
                    break
                q_rows.setdefault((vlan_id, mac), {"vlan": vlan_id, "mac": mac})[key] = value
                break
    except SNMPProtocolError:
        used_legacy = True

    if q_rows and not used_legacy:
        for fields in q_rows.values():
            port = _try_int(fields.get("port"))
            status = _try_int(fields.get("status"))
            # status 2 is "invalid" — agent told us to discard it.
            if status == 2 or port is None or port == 0:
                continue
            ifidx = port_to_ifindex.get(port, port)
            rows.append(
                FdbData(
                    if_index=ifidx,
                    mac_address=fields["mac"],
                    vlan_id=fields["vlan"],
                    fdb_type=_FDB_STATUS_MAP.get(status or 0, "other"),
                )
            )
        return rows

    # Legacy BRIDGE-MIB.
    legacy_rows: dict[str, dict[str, Any]] = {}
    async for oid_str, value in _walk_oids(
        device, [OID_DOT1D_TP_FDB_PORT, OID_DOT1D_TP_FDB_STATUS], hlapi=hlapi
    ):
        for base, key in (
            (OID_DOT1D_TP_FDB_PORT, "port"),
            (OID_DOT1D_TP_FDB_STATUS, "status"),
        ):
            suffix = _suffix_after(oid_str, base)
            if suffix is None:
                continue
            parts = suffix.split(".")
            if len(parts) < 6:
                break
            try:
                mac_bytes = bytes(int(p) for p in parts[:6])
            except ValueError:
                break
            mac = ":".join(f"{b:02x}" for b in mac_bytes) if len(mac_bytes) == 6 else None
            if mac is None:
                break
            legacy_rows.setdefault(mac, {"mac": mac})[key] = value
            break

    for fields in legacy_rows.values():
        port = _try_int(fields.get("port"))
        status = _try_int(fields.get("status"))
        if status == 2 or port is None or port == 0:
            continue
        ifidx = port_to_ifindex.get(port, port)
        rows.append(
            FdbData(
                if_index=ifidx,
                mac_address=fields["mac"],
                vlan_id=None,
                fdb_type=_FDB_STATUS_MAP.get(status or 0, "other"),
            )
        )
    return rows


# ── LLDP-MIB lldpRemTable ────────────────────────────────────────────


def _format_lldp_id(value: Any, subtype: int) -> str:
    """Decode an LLDP chassis-id / port-id according to its subtype.

    LLDP IDs are opaque binary on the wire; the subtype enum tells us
    how to render. We fall back to a hex string for subtypes whose
    contents are vendor-defined or carry binary chassisComponent
    identifiers — surfacing raw hex is honest, and the operator can
    cross-reference against vendor docs if needed.
    """
    if value is None:
        return ""
    # MAC address (subtype 4 for chassis, 3 for port) — most common.
    if subtype in (3, 4):
        try:
            raw = bytes(value) if hasattr(value, "__iter__") else None
            if raw is None and isinstance(value, str | bytes):
                raw = value.encode("latin-1") if isinstance(value, str) else value
            if raw is not None and len(raw) == 6:
                return ":".join(f"{b:02x}" for b in raw)
        except (TypeError, ValueError):
            pass
    # Interface name (chassis subtype 6, port subtype 5),
    # interfaceAlias (chassis 2, port 1), local (chassis/port 7) —
    # these are operator-readable text.
    if subtype in (1, 2, 5, 6, 7):
        try:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace").strip()
            return str(value).strip()
        except Exception:  # noqa: BLE001 — defensive
            pass
    # Fall back to hex.
    try:
        raw = bytes(value) if hasattr(value, "__iter__") else value.encode("latin-1")  # type: ignore[union-attr]
        return raw.hex()
    except (TypeError, ValueError, AttributeError):
        return str(value)


async def walk_lldp_neighbours(device: NetworkDevice) -> list[NeighbourData]:
    """Walk LLDP-MIB lldpRemTable and return one row per neighbour.

    Returns an empty list if the device doesn't speak LLDP-MIB
    (``noSuchObject`` / ``endOfMibView``). That's a real outcome on
    consumer firmware (UDM-SE / older RouterOS) and on switches
    where LLDP is admin-disabled — the caller surfaces the empty
    result as informational, not an error.

    The lldpRemEntry index is ``timeMark.localPortNum.lldpRemIndex``;
    we keep ``localPortNum`` as the local interface's ifIndex (the
    default mapping on every tier-1 vendor we target). When that
    assumption fails on a specific deployment we can enrich via
    ``lldpLocPortIfIndex`` later without changing the schema.
    """
    hlapi = _import_hlapi()

    rows: dict[tuple[int, int, int], dict[str, Any]] = {}
    oids = [
        OID_LLDP_REM_CHASSIS_ID_SUBTYPE,
        OID_LLDP_REM_CHASSIS_ID,
        OID_LLDP_REM_PORT_ID_SUBTYPE,
        OID_LLDP_REM_PORT_ID,
        OID_LLDP_REM_PORT_DESC,
        OID_LLDP_REM_SYS_NAME,
        OID_LLDP_REM_SYS_DESC,
        OID_LLDP_REM_SYS_CAP_ENABLED,
    ]
    try:
        async for oid_str, value in _walk_oids(device, oids, hlapi=hlapi):
            for base, key in [
                (OID_LLDP_REM_CHASSIS_ID_SUBTYPE, "chassis_subtype"),
                (OID_LLDP_REM_CHASSIS_ID, "chassis_id"),
                (OID_LLDP_REM_PORT_ID_SUBTYPE, "port_subtype"),
                (OID_LLDP_REM_PORT_ID, "port_id"),
                (OID_LLDP_REM_PORT_DESC, "port_desc"),
                (OID_LLDP_REM_SYS_NAME, "sys_name"),
                (OID_LLDP_REM_SYS_DESC, "sys_desc"),
                (OID_LLDP_REM_SYS_CAP_ENABLED, "sys_cap_enabled"),
            ]:
                suffix = _suffix_after(oid_str, base)
                if suffix is None:
                    continue
                parts = suffix.split(".")
                if len(parts) < 3:
                    break
                try:
                    time_mark = int(parts[0])
                    local_port = int(parts[1])
                    remote_index = int(parts[2])
                except ValueError:
                    break
                rows.setdefault(
                    (time_mark, local_port, remote_index),
                    {"local_port": local_port},
                )[key] = value
                break
    except SNMPProtocolError:
        # Device doesn't expose LLDP-MIB. Empty result, not an error.
        return []

    out: list[NeighbourData] = []
    for fields in rows.values():
        chassis_subtype = _try_int(fields.get("chassis_subtype")) or 0
        port_subtype = _try_int(fields.get("port_subtype")) or 0
        chassis_id = _format_lldp_id(fields.get("chassis_id"), chassis_subtype)
        port_id = _format_lldp_id(fields.get("port_id"), port_subtype)
        # Drop rows whose mandatory IDs decoded to empty strings —
        # those are usually mid-update wire reads we caught at the
        # wrong tick.
        if not chassis_id or not port_id:
            continue
        sys_name = fields.get("sys_name")
        sys_desc = fields.get("sys_desc")
        port_desc = fields.get("port_desc")

        def _to_str(v: Any) -> str | None:
            if v is None:
                return None
            try:
                if isinstance(v, bytes):
                    s = v.decode("utf-8", errors="replace")
                else:
                    s = str(v)
                s = s.strip()
                return s or None
            except Exception:  # noqa: BLE001
                return None

        out.append(
            NeighbourData(
                if_index=fields["local_port"],
                chassis_id_subtype=chassis_subtype,
                chassis_id=chassis_id,
                port_id_subtype=port_subtype,
                port_id=port_id,
                port_desc=_to_str(port_desc),
                sys_name=_to_str(sys_name),
                sys_desc=_to_str(sys_desc),
                sys_cap_enabled=_try_int(fields.get("sys_cap_enabled")),
            )
        )
    return out


__all__ = [
    "SysInfo",
    "InterfaceData",
    "ArpData",
    "FdbData",
    "NeighbourData",
    "LLDP_CHASSIS_ID_SUBTYPES",
    "LLDP_PORT_ID_SUBTYPES",
    "test_connection",
    "walk_interfaces",
    "walk_arp",
    "walk_fdb",
    "walk_lldp_neighbours",
]
