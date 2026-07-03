"""Wake-on-LAN magic packet (issue #533).

Pure-Python magic-packet build + UDP broadcast send. The control-plane
(server vantage) uses this directly; the appliance vantage runs an
equivalent ``_run_wol`` on the supervisor agent so the packet originates
on the target's L2 segment (WoL only wakes a NIC that receives the frame
on its own broadcast domain).

WoL is IPv4-only here: the magic packet rides a UDP broadcast and IPv6 has
no broadcast address. The 102-byte AMD Magic Packet is 6×0xFF followed by
16 repetitions of the target MAC.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket

from pydantic import BaseModel, Field, field_validator

_HEX12_RE = re.compile(r"^[0-9A-Fa-f]{12}$")


def normalize_mac(mac: str) -> str:
    """Return the canonical lowercase colon form (``aa:bb:cc:dd:ee:ff``).

    Accepts colon / hyphen / dot separators or a bare 12-hex string.
    Raises ``ValueError`` on anything else so callers can surface a 422.
    """
    stripped = re.sub(r"[:.\-]", "", mac.strip())
    if not _HEX12_RE.match(stripped):
        raise ValueError(f"not a valid MAC address: {mac!r}")
    low = stripped.lower()
    return ":".join(low[i : i + 2] for i in range(0, 12, 2))


def build_magic_packet(mac: str) -> bytes:
    """Build the 102-byte AMD Magic Packet for ``mac``."""
    mac_bytes = bytes.fromhex(normalize_mac(mac).replace(":", ""))
    return b"\xff" * 6 + mac_bytes * 16


def _send_sync(packet: bytes, broadcast: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port))


async def send_magic_packet(mac: str, broadcast: str, port: int = 9) -> None:
    """Build + broadcast the magic packet, off the event loop.

    ``broadcast`` is the IPv4 broadcast address to send to (the subnet's
    directed broadcast, or ``255.255.255.255`` for the local segment);
    ``port`` is conventionally 9 (discard) or 7 (echo).
    """
    packet = build_magic_packet(mac)
    await asyncio.to_thread(_send_sync, packet, broadcast, port)


class WolWireRequest(BaseModel):
    """Params shipped to (and re-validated on) an appliance agent, and the
    validated shape the server-vantage send uses. Validators double as the
    SSRF/arg guard: a bad MAC or non-IPv4 broadcast is rejected before any
    packet is built."""

    mac: str
    broadcast: str
    port: int = Field(default=9, ge=1, le=65535)

    @field_validator("mac")
    @classmethod
    def _v_mac(cls, v: str) -> str:
        return normalize_mac(v)

    @field_validator("broadcast")
    @classmethod
    def _v_broadcast(cls, v: str) -> str:
        try:
            return str(ipaddress.IPv4Address(v.strip()))
        except ipaddress.AddressValueError as exc:
            raise ValueError(f"broadcast must be an IPv4 address: {v!r}") from exc


class WolResult(BaseModel):
    """Uniform WoL send result. ``ran_from`` mirrors the network-tools
    convention: ``server`` for the api-container send, ``appliance:<name>``
    when a Fleet appliance sent it."""

    mac: str
    broadcast: str
    port: int
    sent: bool
    ran_from: str = "server"
    error: str | None = None
