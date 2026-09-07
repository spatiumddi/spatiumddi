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
#
# These drive the REAL ``SocketDropCounter.sample()`` by pointing it at
# fixture files on disk. An earlier version of this file subclassed it and
# reimplemented ``sample()``, which meant the five tests below asserted on a
# copy of the logic and would have passed with the production method
# arbitrarily broken.


def _write_proc(tmp_path, name, rows):
    """Write a /proc/net/udp-shaped file and return its path."""
    path = tmp_path / name
    path.write_text(_proc(*rows))
    return str(path)


def _counter_over(tmp_path, snapshots):
    """A real SocketDropCounter reading a file this helper rewrites per tick.

    ``read_socket_drops`` opens the path on every call, so rewriting the file
    between ``sample()`` calls is exactly what a changing kernel table looks
    like to it.
    """
    path = tmp_path / "udp"
    counter = SocketDropCounter()

    def tick(rows):
        # rows=None models procfs becoming unreadable: point the counter at a
        # path that does not exist.
        if rows is None:
            return counter.sample_from(("/nonexistent/net/udp",))
        path.write_text(_proc(*rows))
        return counter.sample_from((str(path),))

    return [tick(r) for r in snapshots]


def test_first_sample_has_no_baseline(tmp_path):
    (out,) = _counter_over(tmp_path, [[_row("0043", "1", 40)]])
    assert out is None


def test_delta_is_per_socket(tmp_path):
    out = _counter_over(
        tmp_path, [[_row("0043", "1", 40)], [_row("0043", "1", 47)]]
    )
    assert out == [None, 7]


def test_a_socket_disappearing_does_not_read_as_negative(tmp_path):
    """Kea closes and reopens its sockets on reconfiguration. Summing the
    totals would show the fleet-wide count falling, which looks exactly like
    a counter reset; summing per inode gives the right answer of 0."""
    out = _counter_over(
        tmp_path,
        [
            [_row("0043", "1", 500), _row("0223", "2", 500)],
            [_row("0223", "2", 500)],
        ],
    )
    assert out == [None, 0]


def test_a_new_socket_contributes_from_zero(tmp_path):
    out = _counter_over(
        tmp_path,
        [
            [_row("0043", "1", 500)],
            [_row("0043", "1", 500), _row("0223", "2", 12)],
        ],
    )
    assert out == [None, 12]


def test_unreadable_procfs_clears_the_baseline(tmp_path):
    """If a sample cannot be taken, the next one must not diff against a
    stale baseline and report a bucket's worth of drops as one spike."""
    out = _counter_over(
        tmp_path, [[_row("0043", "1", 10)], None, [_row("0043", "1", 900)]]
    )
    assert out == [None, None, None]


def test_a_partial_read_is_not_a_sample(tmp_path):
    """One of the two procfs files unreadable must fail the WHOLE sample.

    Returning the half that read as a valid snapshot drops the missing
    inodes out of the baseline; when the next read succeeds they come back
    as brand-new sockets and their entire lifetime ``sk_drops`` is charged
    to that one bucket — a fabricated spike, on a rule whose floor is one
    packet.
    """
    v4 = _write_proc(tmp_path, "udp", [_row("0043", "1", 5)])
    v6 = _write_proc(tmp_path, "udp6", [_row("0223", "2", 5000)])

    both = read_socket_drops(paths=(v4, v6))
    assert both == {"1": 5, "2": 5000}

    # v6 present but unreadable (a directory stands in for EISDIR — any
    # OSError that is not FileNotFoundError takes the same branch).
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    assert read_socket_drops(paths=(v4, str(unreadable))) is None

    # A genuinely absent file is different and stays a valid partial answer:
    # no IPv6 stack means no v6 sockets to miss.
    assert read_socket_drops(paths=(v4, str(tmp_path / "gone"))) == {"1": 5}
