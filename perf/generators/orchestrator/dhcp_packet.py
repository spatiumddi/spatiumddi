"""Raw DHCPv4 packet library — pre-built byte templates for the hot send loop (§3.2).

The orchestrator models 250-300k devices; at the surge peak it sends thousands of
DHCPv4 packets per second. Per the doc, the hot path uses **raw byte templates built
with ``struct``** — each device pre-encodes its DISCOVER/REQUEST once, and we mutate
only the volatile fields (xid / secs / flags / ciaddr / giaddr / requested-IP /
server-id) per send. ``scapy`` is used ONLY by the optional one-shot validation
harness (:func:`validate_layout`) — once the option layout is confirmed we trust the
byte-packer and never import scapy on the hot path.

DHCPv4 wire format (RFC 2131 §2, RFC 2132 options):

    op(1) htype(1) hlen(1) hops(1) xid(4) secs(2) flags(2)
    ciaddr(4) yiaddr(4) siaddr(4) giaddr(4) chaddr(16)
    sname(64) file(128) magic-cookie(4=0x63825363) options... 0xFF

Options we set:
  53 DHCP-Message-Type (1=DISCOVER 3=REQUEST 7=RELEASE)
  61 Client-Identifier  (01 + 6-byte MAC, RFC 2132 §9.14)
  12 Host-Name          (option-12, only for the hostname-bearing fraction → DDNS)
  50 Requested-IP       (REQUEST only — the offered/leased address)
  54 Server-Identifier  (REQUEST in SELECTING; RELEASE)
  55 Parameter-Request-List (router/dns/domain/etc — realistic option pull)
  255 END

Grounded against the SpatiumDDI Kea driver:
  * relay topology — Kea selects the subnet by ``giaddr`` matching a scope's
    ``relay.ip-addresses`` (`backend/app/drivers/dhcp/kea.py:237-241`); so a relay
    packet MUST stamp the per-subnet giaddr and unicast to the server.
  * broadcast topology — giaddr=0, Kea selects by receiving interface
    (`kea.py:322-323` ``interfaces-config.interfaces=["*"]``); packet is broadcast.
"""

from __future__ import annotations

import struct

# --- message types (option 53) ---
DHCPDISCOVER = 1
DHCPOFFER = 2
DHCPREQUEST = 3
DHCPDECLINE = 4
DHCPACK = 5
DHCPNAK = 6
DHCPRELEASE = 7

MAGIC_COOKIE = b"\x63\x82\x53\x63"
BOOTREQUEST = 1
BOOTREPLY = 2
HTYPE_ETHER = 1
HLEN_ETHER = 6
FLAG_BROADCAST = 0x8000

# Full BOOTP header through sname(64)+file(128) = 236 bytes. The 4-byte magic cookie
# and the DHCP options follow at offset 236.
_HEADER_FMT = "!BBBBIHH4s4s4s4s16s64s128s"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)  # 236

# Parameter-Request-List: subnet-mask, router, dns, domain-name, broadcast,
# ntp, mtu, domain-search — a realistic enterprise client pull.
_PRL = bytes([1, 3, 6, 15, 28, 42, 26, 119])


def mac_to_bytes(mac: str) -> bytes:
    """``02:00:00:00:00:01`` -> 6 raw bytes."""
    return bytes(int(b, 16) for b in mac.split(":"))


def ip_to_bytes(ip: str) -> bytes:
    return bytes(int(o) for o in ip.split("."))


def _chaddr(mac6: bytes) -> bytes:
    return mac6 + b"\x00" * (16 - len(mac6))


def _opt(code: int, payload: bytes) -> bytes:
    return bytes([code, len(payload)]) + payload


def _pack_header(
    *,
    op: int,
    xid: int,
    secs: int,
    flags: int,
    ciaddr: bytes,
    giaddr: bytes,
    chaddr16: bytes,
) -> bytes:
    return struct.pack(
        _HEADER_FMT,
        op,            # op
        HTYPE_ETHER,   # htype
        HLEN_ETHER,    # hlen
        0,             # hops (0 from a client; Kea relay match keys on giaddr)
        xid & 0xFFFFFFFF,
        secs & 0xFFFF,
        flags & 0xFFFF,
        ciaddr,        # ciaddr (set on renew/release: the held IP)
        b"\x00\x00\x00\x00",  # yiaddr (server fills)
        b"\x00\x00\x00\x00",  # siaddr
        giaddr,        # giaddr (relay IP in relay topology, else 0)
        chaddr16,
        b"\x00" * 64,  # sname
        b"\x00" * 128,  # file
    )


def build_discover(
    *, mac: str, client_id: bytes, hostname: str | None, broadcast: bool
) -> bytes:
    """Pre-built DISCOVER template; mutate xid/secs/giaddr per send via patch helpers."""
    mac6 = mac_to_bytes(mac)
    flags = FLAG_BROADCAST if broadcast else 0
    hdr = _pack_header(
        op=BOOTREQUEST,
        xid=0,  # patched per send
        secs=0,
        flags=flags,
        ciaddr=b"\x00\x00\x00\x00",
        giaddr=b"\x00\x00\x00\x00",  # patched per send in relay topology
        chaddr16=_chaddr(mac6),
    )
    opts = MAGIC_COOKIE
    opts += _opt(53, bytes([DHCPDISCOVER]))
    opts += _opt(61, client_id)
    if hostname:
        opts += _opt(12, hostname.encode("ascii", "ignore")[:63])
    opts += _opt(55, _PRL)
    opts += b"\xff"
    return hdr + opts


def build_request_selecting(
    *,
    mac: str,
    client_id: bytes,
    hostname: str | None,
    requested_ip: str,
    server_id: str,
    broadcast: bool,
) -> bytes:
    """REQUEST in SELECTING state (after OFFER): carries opt-50 + opt-54, ciaddr=0."""
    mac6 = mac_to_bytes(mac)
    flags = FLAG_BROADCAST if broadcast else 0
    hdr = _pack_header(
        op=BOOTREQUEST,
        xid=0,
        secs=0,
        flags=flags,
        ciaddr=b"\x00\x00\x00\x00",  # SELECTING: ciaddr MUST be 0 (RFC 2131 §4.3.2)
        giaddr=b"\x00\x00\x00\x00",
        chaddr16=_chaddr(mac6),
    )
    opts = MAGIC_COOKIE
    opts += _opt(53, bytes([DHCPREQUEST]))
    opts += _opt(61, client_id)
    opts += _opt(54, ip_to_bytes(server_id))      # which OFFER we accept
    opts += _opt(50, ip_to_bytes(requested_ip))   # the offered address
    if hostname:
        opts += _opt(12, hostname.encode("ascii", "ignore")[:63])
    opts += _opt(55, _PRL)
    opts += b"\xff"
    return hdr + opts


def build_request_renew(
    *,
    mac: str,
    client_id: bytes,
    hostname: str | None,
    leased_ip: str,
) -> bytes:
    """RENEWING REQUEST (T1): unicast, ciaddr=leased_ip, NO opt-50/opt-54.

    HARD FSM CONTRACT (§3.2/H3): a renewal re-requests the CURRENT lease — ciaddr is
    the held IP and there is no Requested-IP option. The server must hand back the
    SAME yiaddr. The orchestrator asserts yiaddr==leased_ip on the ACK; a different IP
    is a named correctness FAIL.
    """
    mac6 = mac_to_bytes(mac)
    hdr = _pack_header(
        op=BOOTREQUEST,
        xid=0,
        secs=0,
        flags=0,  # RENEWING is unicast; do NOT set broadcast (RFC 2131 §4.3.6)
        ciaddr=ip_to_bytes(leased_ip),  # the held IP
        giaddr=b"\x00\x00\x00\x00",
        chaddr16=_chaddr(mac6),
    )
    opts = MAGIC_COOKIE
    opts += _opt(53, bytes([DHCPREQUEST]))
    opts += _opt(61, client_id)
    if hostname:
        opts += _opt(12, hostname.encode("ascii", "ignore")[:63])
    opts += _opt(55, _PRL)
    opts += b"\xff"
    return hdr + opts


def build_request_rebind(
    *,
    mac: str,
    client_id: bytes,
    hostname: str | None,
    leased_ip: str,
) -> bytes:
    """REBINDING REQUEST (T2): broadcast, ciaddr=leased_ip, no opt-50/opt-54."""
    mac6 = mac_to_bytes(mac)
    hdr = _pack_header(
        op=BOOTREQUEST,
        xid=0,
        secs=0,
        flags=FLAG_BROADCAST,  # REBINDING is broadcast (any server may answer)
        ciaddr=ip_to_bytes(leased_ip),
        giaddr=b"\x00\x00\x00\x00",
        chaddr16=_chaddr(mac6),
    )
    opts = MAGIC_COOKIE
    opts += _opt(53, bytes([DHCPREQUEST]))
    opts += _opt(61, client_id)
    if hostname:
        opts += _opt(12, hostname.encode("ascii", "ignore")[:63])
    opts += _opt(55, _PRL)
    opts += b"\xff"
    return hdr + opts


def build_release(
    *,
    mac: str,
    client_id: bytes,
    leased_ip: str,
    server_id: str,
) -> bytes:
    """DHCPRELEASE: unicast to the server, ciaddr=leased_ip, opt-54=server-id."""
    mac6 = mac_to_bytes(mac)
    hdr = _pack_header(
        op=BOOTREQUEST,
        xid=0,
        secs=0,
        flags=0,
        ciaddr=ip_to_bytes(leased_ip),
        giaddr=b"\x00\x00\x00\x00",
        chaddr16=_chaddr(mac6),
    )
    opts = MAGIC_COOKIE
    opts += _opt(53, bytes([DHCPRELEASE]))
    opts += _opt(61, client_id)
    opts += _opt(54, ip_to_bytes(server_id))
    opts += b"\xff"
    return hdr + opts


# --- per-send patch helpers (mutate the volatile header fields in place) ----------
# xid is at byte offset 4 (after op/htype/hlen/hops); secs at 8; flags at 10; giaddr
# at the 4-byte field after siaddr. Compute giaddr offset from the format prefix.
_XID_OFF = struct.calcsize("!BBBB")            # 4
_SECS_OFF = struct.calcsize("!BBBBI")          # 8
_GIADDR_OFF = struct.calcsize("!BBBBIHH4s4s4s")  # giaddr starts right after siaddr


def patch_send_fields(pkt: bytearray, *, xid: int, secs: int = 0, giaddr: str | None = None) -> None:
    """Mutate xid (+optional secs/giaddr) on a pre-built packet for this send."""
    struct.pack_into("!I", pkt, _XID_OFF, xid & 0xFFFFFFFF)
    if secs:
        struct.pack_into("!H", pkt, _SECS_OFF, secs & 0xFFFF)
    if giaddr is not None:
        pkt[_GIADDR_OFF : _GIADDR_OFF + 4] = ip_to_bytes(giaddr)


# --- receive-side parse: just enough to drive the FSM + timestamp latency ---------

def parse_reply(data: bytes) -> dict | None:
    """Parse OFFER/ACK/NAK: extract xid, yiaddr, msg-type (53), server-id (54), T1/T2.

    Returns ``None`` if the packet is not a BOOTREPLY with a valid magic cookie.
    """
    if len(data) < _HEADER_SIZE + 4:
        return None
    op = data[0]
    if op != BOOTREPLY:
        return None
    xid = struct.unpack_from("!I", data, _XID_OFF)[0]
    yiaddr = ".".join(str(b) for b in data[16:20])
    # options begin after the 236-byte BOOTP header + 64 sname + 128 file = 240,
    # then the 4-byte magic cookie.
    cookie_off = _HEADER_SIZE
    if data[cookie_off : cookie_off + 4] != MAGIC_COOKIE:
        return None
    i = cookie_off + 4
    out: dict = {"xid": xid, "yiaddr": yiaddr}
    n = len(data)
    while i < n:
        code = data[i]
        if code == 255:  # END
            break
        if code == 0:  # PAD
            i += 1
            continue
        if i + 1 >= n:
            break
        length = data[i + 1]
        val = data[i + 2 : i + 2 + length]
        if code == 53 and length >= 1:
            out["msg_type"] = val[0]
        elif code == 54 and length == 4:
            out["server_id"] = ".".join(str(b) for b in val)
        elif code == 51 and length == 4:  # lease time
            out["lease_time"] = struct.unpack("!I", val)[0]
        elif code == 58 and length == 4:  # renewal (T1)
            out["t1"] = struct.unpack("!I", val)[0]
        elif code == 59 and length == 4:  # rebind (T2)
            out["t2"] = struct.unpack("!I", val)[0]
        i += 2 + length
    return out


def validate_layout(pkt: bytes) -> dict:
    """Optional one-shot scapy validation harness (NOT on the hot path).

    Decodes a byte-packed packet with scapy and returns the parsed option dict so a
    dry-run can confirm the struct layout matches before trusting the byte-packer.
    Raises ImportError if scapy isn't installed (it is optional, only for validation).
    """
    from scapy.all import BOOTP, DHCP  # type: ignore  # noqa: F401  (optional dep)
    from scapy.layers.dhcp import DHCP as _DHCP  # type: ignore

    bootp = BOOTP(pkt)
    opts = {}
    if bootp.haslayer(_DHCP):
        for entry in bootp[_DHCP].options:
            if isinstance(entry, tuple):
                opts[entry[0]] = entry[1:] if len(entry) > 2 else entry[1]
    return {"xid": bootp.xid, "chaddr": bootp.chaddr, "options": opts}
