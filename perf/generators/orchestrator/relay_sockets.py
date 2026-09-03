"""DHCP sockets for the device fleet — one per relay address in relay topology.

Why per-giaddr sockets: in relay topology Kea unicasts every OFFER/ACK to
``giaddr:67``. A load-gen host that owns those giaddrs is rarely the only
process listening on UDP/67 — libvirt's dnsmasq (the appliance VM's own DHCP
server), a leftover ``dhcrelay``, a lab DHCP server all bind the wildcard
address on the same device. Two wildcard sockets with the same score share the
unicast replies according to the kernel's UDP lookup tie-break (in practice a
CPU-affinity split), so the fleet silently loses a large fraction of the OFFERs
it was sent and logs them as timeouts (measured 2026-09-03: 58 % seen of 74.8k
OFFERs on the wire, ~45 % handshake success on a healthy Kea).

A socket bound to the giaddr itself is looked up before any wildcard socket,
so it receives every reply to that address, deterministically. The wildcard
socket stays open as the fallback for a giaddr the host cannot bind (the address
is not local, or the platform refuses), and for the broadcast topology.
"""

from __future__ import annotations

import errno
import logging
import socket
from collections.abc import Callable

SockFactory = Callable[..., socket.socket]


def _new_socket(iface: str, log: logging.Logger, factory: SockFactory) -> socket.socket:
    s = factory(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # Multi-homed egress (perf #454). In broadcast topology the DISCOVER goes
    # to 255.255.255.255; on a box with several NICs (docker bridges, tailscale,
    # …) the kernel won't necessarily send it out the one facing the appliance.
    # Bind the socket to the configured device so it does. SO_BINDTODEVICE needs
    # CAP_NET_RAW (the load-gen already runs as root for the :67/:68 bind).
    if iface:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
        except OSError as exc:
            log.error(
                "SO_BINDTODEVICE(%s) failed: %s — broadcast may not "
                "reach the appliance on a multi-homed box",
                iface,
                exc,
                extra={"fields": {"event": "bindtodevice_failed", "iface": iface}},
            )
    return s


def open_relay_sockets(
    iface: str,
    giaddrs: list[str],
    port: int,
    log: logging.Logger,
    factory: SockFactory = socket.socket,
) -> tuple[socket.socket, dict[str, socket.socket]]:
    """Open the wildcard socket on ``port`` plus, for each distinct giaddr, a
    socket bound to ``(giaddr, port)``. Returns ``(wildcard, {giaddr: socket})``;
    a giaddr that could not be bound maps to the wildcard socket, so callers can
    always send and receive through ``socks[giaddr]``.

    ``PermissionError`` from the wildcard bind propagates (the caller explains
    the capability); a per-giaddr bind failure is logged and falls back."""
    wildcard = _new_socket(iface, log, factory)
    # 0.0.0.0 is required for the broadcast topology: a simulated client has no
    # lease yet, so it must listen on the wildcard address to receive the
    # broadcast OFFER/ACK. SO_BINDTODEVICE above already pins the socket to the
    # appliance-facing NIC on multi-homed hosts, so this is not "all interfaces".
    wildcard.bind(("0.0.0.0", port))
    wildcard.setblocking(False)
    socks: dict[str, socket.socket] = {}
    for giaddr in dict.fromkeys(g for g in giaddrs if g):
        s = _new_socket(iface, log, factory)
        try:
            s.bind((giaddr, port))
        except OSError as exc:
            s.close()
            why = (
                errno.errorcode.get(exc.errno, str(exc.errno))
                if exc.errno
                else str(exc)
            )
            log.warning(
                "relay socket for giaddr %s not bound (%s): falling back to the "
                "wildcard socket — replies to it can be shared with other UDP/%d "
                "listeners on the device",
                giaddr,
                why,
                port,
                extra={
                    "fields": {
                        "event": "giaddr_bind_failed",
                        "giaddr": giaddr,
                        "errno": exc.errno,
                    }
                },
            )
            socks[giaddr] = wildcard
            continue
        s.setblocking(False)
        socks[giaddr] = s
    bound = sum(1 for s in socks.values() if s is not wildcard)
    if giaddrs:
        log.info(
            "relay sockets bound",
            extra={
                "fields": {
                    "event": "giaddr_sockets",
                    "bound": bound,
                    "giaddrs": len(socks),
                    "port": port,
                }
            },
        )
    return wildcard, socks
