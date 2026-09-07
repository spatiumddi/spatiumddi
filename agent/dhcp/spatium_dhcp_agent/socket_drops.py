"""Per-socket kernel receive-drop counters for the DHCP listen ports (#980).

Kea answers every packet it manages to read. When a burst arrives faster
than its receive thread is scheduled, the loss happens one layer down — the
kernel finds the socket's receive buffer full and drops the datagram before
``recvmsg()`` ever sees it. Nothing inside Kea observes that, so every
product surface reports a healthy server while clients retransmit.

**``pkt4-receive-drop`` is not this number.** Measured against kea-dhcp4
3.0.3 on 2026-09-06: a run that lost 9,700 datagrams to socket-buffer
overflow left ``pkt4-receive-drop`` at exactly 0, because that counter
covers packets Kea *read* and then discarded (unparseable, DROP class, no
subnet). Both are worth reporting and they name different failures — this
module supplies the one that moves when a node is out of CPU.

The kernel exposes it per socket as the last column of ``/proc/net/udp``
(``sk_drops``), which is the same event that increments the node-wide
``Udp: RcvbufErrors`` in ``/proc/net/snmp``. We read the per-socket form
deliberately: the node-wide counter is shared with every other UDP consumer
in the network namespace — and the DHCP pod runs with host networking — so
it cannot say the loss was ours.

Only sockets bound to a DHCP service port are counted (67 for DHCPv4, 547
for DHCPv6). The AF_PACKET socket Kea opens for ``dhcp-socket-type: raw``
is *not* visible here — ``/proc/net/packet`` carries no drop column, and
reading its statistics needs ``PACKET_STATISTICS`` on the socket itself,
which only the owning process can do. That is a real blind spot for
directly-attached broadcast traffic; relayed traffic, which is unicast and
therefore arrives on the UDP fallback socket, is fully covered.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

# DHCPv4 server port and DHCPv6 server port. ``/proc/net/udp`` prints the
# local port in hex, so these are compared post-parse rather than as text.
DHCP_PORTS = frozenset({67, 547})

_PROC_UDP = ("/proc/net/udp", "/proc/net/udp6")


def _parse_proc_udp(text: str, ports: frozenset[int] = DHCP_PORTS) -> dict[str, int]:
    """One ``/proc/net/udp`` body -> ``{inode: sk_drops}`` for DHCP ports.

    Row layout (both the v4 and v6 files), whitespace-separated::

        sl local_address rem_address st tx:rx tr:when retrnsmt uid
        timeout inode ref pointer drops

    ``local_address`` is ``<hex-addr>:<hex-port>``; ``drops`` is last.
    Keying by inode rather than by address means a socket that Kea closes
    and reopens on reconfiguration reads as a *new* counter starting at
    zero instead of as a counter that went backwards.

    ``ports`` is a parameter only so the tests can point it at an ephemeral
    port: binding 67 needs privilege, and a fixture cannot prove the field
    indices below are the ones this kernel actually uses. Production always
    passes the default.
    """
    out: dict[str, int] = {}
    for line in text.splitlines()[1:]:  # [0] is the column header
        fields = line.split()
        if len(fields) < 13:
            continue
        local = fields[1]
        if ":" not in local:
            continue
        try:
            port = int(local.rsplit(":", 1)[1], 16)
            drops = int(fields[-1])
        except ValueError:
            continue
        if port not in ports:
            continue
        out[fields[9]] = drops
    return out


def read_socket_drops(
    paths: tuple[str, ...] = _PROC_UDP, ports: frozenset[int] = DHCP_PORTS
) -> dict[str, int] | None:
    """Snapshot ``{inode: sk_drops}`` for every DHCP-port UDP socket.

    Returns ``None`` — never ``{}`` — when the counter cannot be read at
    all, so a caller can report UNKNOWN rather than a zero that reads as
    "no loss". ``{}`` is a real answer: procfs was readable and no DHCP
    socket is currently open (Kea starting, or bound raw-only).
    """
    merged: dict[str, int] = {}
    readable = False
    for path in paths:
        try:
            with open(path, encoding="ascii", errors="replace") as fh:
                text = fh.read()
        except FileNotFoundError:
            # No IPv6 stack, or not Linux. Not an error on its own.
            continue
        except OSError as e:
            log.debug("socket_drops_unreadable", path=path, error=str(e))
            continue
        readable = True
        merged.update(_parse_proc_udp(text, ports))
    return merged if readable else None


class SocketDropCounter:
    """Turns per-socket monotonic ``sk_drops`` into a per-bucket delta.

    Each socket's counter is monotonic for the life of *that* socket and
    disappears with it, so the aggregate is summed per inode rather than in
    total: a socket that goes away must not make the fleet-wide sum fall
    (which would read as a counter reset), and a socket that appears
    contributes its own count from zero.
    """

    def __init__(self) -> None:
        self._prev: dict[str, int] | None = None

    def sample(self) -> int | None:
        """Drops since the previous call, or ``None`` if not measurable.

        The first call after start returns ``None``: with no baseline, the
        counters standing on the sockets are however much was lost before
        the agent came up, which belongs to no bucket in particular.
        """
        current = read_socket_drops()
        if current is None:
            self._prev = None
            return None
        prev = self._prev
        self._prev = current
        if prev is None:
            return None
        total = 0
        for inode, value in current.items():
            baseline = prev.get(inode, 0)
            # max() guards a same-inode counter that appears to fall. It
            # cannot happen for sk_drops, but an inode number reused by a
            # different socket inside one interval would look exactly like
            # that, and a negative contribution is never the right answer.
            total += max(0, value - baseline)
        return total


__all__ = ["DHCP_PORTS", "SocketDropCounter", "read_socket_drops"]
