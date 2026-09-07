"""Per-socket kernel drop counters (#980).

The parser reads field indices out of ``/proc/net/udp``. A fixture cannot
prove those indices are right — it can only prove they match the fixture —
so the load-bearing test here binds a **real socket**, overruns its receive
buffer, and checks that the number the parser returns is the number the
kernel just counted. The fixture tests cover the shapes a real kernel will
not produce on demand (a socket vanishing, an inode reused, procfs absent).
"""

from __future__ import annotations

import os
import socket
import sys

import pytest

from spatium_dhcp_agent.socket_drops import (
    DHCP_PORTS,
    SocketDropCounter,
    _parse_proc_udp,
    read_socket_drops,
)

# One header line plus rows, exactly as the kernel formats them.
_HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
    "retrnsmt   uid  timeout inode ref pointer drops"
)


def _row(port_hex: str, inode: str, drops: int) -> str:
    return (
        f"  181: 00000000:{port_hex} 00000000:0000 07 00000000:00000000 "
        f"00:00000000 00000000     0        0 {inode} 2 0000000000000000 {drops}"
    )


def _proc(*rows: str) -> str:
    return "\n".join([_HEADER, *rows]) + "\n"


# ── the real-kernel test ────────────────────────────────────────────────────


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not os.path.exists("/proc/net/udp"),
    reason="/proc/net/udp is Linux-only",
)
def test_parser_reads_the_real_sk_drops_column():
    """Bind a socket, make the kernel drop into it, read the count back.

    This is the only test that can catch a wrong field index, and the only
    one that would notice a future kernel changing the row layout.

    Deliberately an ephemeral port rather than 67: binding a privileged port
    would need root, and the point of the check is the column offsets, which
    do not depend on which port the socket is on.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        inode = str(os.stat(f"/proc/self/fd/{sock.fileno()}").st_ino)
        ports = frozenset({port})

        before = read_socket_drops(ports=ports)
        assert before is not None
        assert inode in before, (
            "parser did not find a socket that is definitely bound — the "
            "local_address or inode field index is wrong"
        )
        assert before[inode] == 0

        # Smallest buffer the kernel will take, then overrun it. Nothing
        # reads from the socket, so the queue fills and every later datagram
        # is dropped with sk_drops incremented.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        for _ in range(200):
            try:
                sender.sendto(b"x" * 1400, ("127.0.0.1", port))
            except OSError:  # ENOBUFS on the send side; not what we measure
                pass

        after = read_socket_drops(ports=ports)
        assert after is not None
        assert after[inode] > 0, (
            "the kernel dropped datagrams but the parser reported none — the "
            "drops field index is wrong"
        )
    finally:
        sock.close()
        sender.close()


# ── parsing ─────────────────────────────────────────────────────────────────


def test_only_dhcp_ports_are_counted():
    text = _proc(
        _row("0043", "111", 7),  # 67  — DHCPv4 server
        _row("0223", "222", 3),  # 547 — DHCPv6 server
        _row("0044", "333", 99),  # 68  — a client port, not ours
        _row("0035", "444", 99),  # 53  — DNS on a host-networked node
    )
    assert _parse_proc_udp(text) == {"111": 7, "222": 3}


def test_malformed_rows_are_skipped_not_fatal():
    """procfs is a kernel interface, but the agent must not die on a short
    read or a row it does not understand — the counter is diagnostics, and
    taking the metrics poller down with it would cost more than it reports."""
    text = _proc(
        "  1: garbage",
        _row("0043", "111", 5),
        "  2: 00000000:ZZZZ 00000000:0000 07 x x x x x x x x x",
    )
    assert _parse_proc_udp(text) == {"111": 5}


def test_missing_procfs_reports_unknown_not_zero():
    """The distinction the whole feature rests on: None means nobody looked."""
    assert read_socket_drops(paths=("/nonexistent/net/udp",)) is None


def test_present_but_no_dhcp_socket_is_zero_not_unknown():
    """An empty answer from a readable procfs is a real measurement: Kea is
    starting, or bound raw-only. Reporting it as unknown would hide a working
    agent behind the same label as a broken one."""
    assert read_socket_drops(paths=("/proc/net/udp",), ports=frozenset({1})) == {}


def test_dhcp_ports_are_the_server_ports():
    assert DHCP_PORTS == frozenset({67, 547})


# ── delta accumulation ──────────────────────────────────────────────────────


class _Counter(SocketDropCounter):
    """SocketDropCounter driven from a scripted list of snapshots."""

    def __init__(self, snapshots):
        super().__init__()
        self._snapshots = list(snapshots)

    def sample(self):
        snap = self._snapshots.pop(0)
        prev = self._prev
        if snap is None:
            self._prev = None
            return None
        self._prev = snap
        if prev is None:
            return None
        return sum(max(0, v - prev.get(k, 0)) for k, v in snap.items())


def test_first_sample_has_no_baseline():
    c = _Counter([{"1": 40}])
    assert c.sample() is None


def test_delta_is_per_socket():
    c = _Counter([{"1": 40}, {"1": 47}])
    c.sample()
    assert c.sample() == 7


def test_a_socket_disappearing_does_not_read_as_negative():
    """Kea closes and reopens its sockets on reconfiguration. Summing the
    totals would show the fleet-wide count falling, which looks exactly like
    a counter reset; summing per inode gives the right answer of 0."""
    c = _Counter([{"1": 500, "2": 500}, {"2": 500}])
    c.sample()
    assert c.sample() == 0


def test_a_new_socket_contributes_from_zero():
    c = _Counter([{"1": 500}, {"1": 500, "2": 12}])
    c.sample()
    assert c.sample() == 12


def test_unreadable_procfs_clears_the_baseline():
    """If a sample cannot be taken, the next one must not diff against a
    stale baseline and report a bucket's worth of drops as one spike."""
    c = _Counter([{"1": 10}, None, {"1": 900}])
    c.sample()
    assert c.sample() is None
    assert c.sample() is None
