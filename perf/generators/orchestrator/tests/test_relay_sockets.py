"""open_relay_sockets: one socket per giaddr, wildcard fallback, broadcast unchanged."""

from __future__ import annotations

import errno
import logging
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay_sockets import open_relay_sockets  # noqa: E402


class FakeSocket:
    def __init__(self, family, kind, *, refuse: set[str]):
        self.family, self.kind = family, kind
        self.opts: list[tuple[int, int, object]] = []
        self.bound: tuple[str, int] | None = None
        self.blocking: bool | None = None
        self.closed = False
        self._refuse = refuse

    def setsockopt(self, level, name, value):
        self.opts.append((level, name, value))

    def bind(self, addr):
        if addr[0] in self._refuse:
            raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
        self.bound = addr

    def setblocking(self, flag):
        self.blocking = flag

    def close(self):
        self.closed = True


def factory(refuse: set[str] = frozenset()):
    made: list[FakeSocket] = []

    def make(family, kind):
        s = FakeSocket(family, kind, refuse=set(refuse))
        made.append(s)
        return s

    make.made = made  # type: ignore[attr-defined]
    return make


@pytest.fixture
def log():
    return logging.getLogger("test.relay_sockets")


def test_relay_binds_one_socket_per_giaddr_plus_wildcard(log):
    make = factory()
    wildcard, socks = open_relay_sockets(
        "virbr0", ["10.9.0.1", "10.9.1.1", "10.9.2.1"], 67, log, factory=make
    )
    assert wildcard.bound == ("0.0.0.0", 67)
    assert {g: s.bound for g, s in socks.items()} == {
        "10.9.0.1": ("10.9.0.1", 67),
        "10.9.1.1": ("10.9.1.1", 67),
        "10.9.2.1": ("10.9.2.1", 67),
    }
    assert all(s is not wildcard for s in socks.values())
    assert all(s.blocking is False for s in [wildcard, *socks.values()])
    # every socket carries the device pin and the reuse/broadcast options
    for s in make.made:
        names = {name for _lvl, name, _val in s.opts}
        assert {
            socket.SO_REUSEADDR,
            socket.SO_BROADCAST,
            socket.SO_BINDTODEVICE,
        } <= names
        assert (socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"virbr0") in s.opts


def test_unbindable_giaddr_falls_back_to_the_wildcard_socket(log):
    make = factory(refuse={"10.9.1.1"})
    wildcard, socks = open_relay_sockets(
        "", ["10.9.0.1", "10.9.1.1"], 67, log, factory=make
    )
    assert socks["10.9.0.1"].bound == ("10.9.0.1", 67)
    assert socks["10.9.1.1"] is wildcard
    refused = [s for s in make.made if s.bound is None and s is not wildcard]
    assert len(refused) == 1 and refused[0].closed
    # no device pin was requested, so none was set
    assert all(
        name != socket.SO_BINDTODEVICE for s in make.made for _l, name, _v in s.opts
    )


def test_duplicate_and_empty_giaddrs_collapse(log):
    make = factory()
    wildcard, socks = open_relay_sockets(
        "", ["10.9.0.1", "", "10.9.0.1"], 67, log, factory=make
    )
    assert list(socks) == ["10.9.0.1"]
    assert len(make.made) == 2  # wildcard + one giaddr socket


def test_broadcast_topology_opens_only_the_wildcard_client_socket(log):
    make = factory()
    wildcard, socks = open_relay_sockets("eth0", [], 68, log, factory=make)
    assert wildcard.bound == ("0.0.0.0", 68)
    assert socks == {}
    assert len(make.made) == 1


def test_wildcard_permission_error_propagates(log):
    class Denied(FakeSocket):
        def bind(self, addr):
            raise PermissionError(errno.EACCES, "Permission denied")

    def make(family, kind):
        return Denied(family, kind, refuse=set())

    with pytest.raises(PermissionError):
        open_relay_sockets("", ["10.9.0.1"], 67, log, factory=make)
