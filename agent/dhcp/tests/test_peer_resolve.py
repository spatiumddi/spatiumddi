"""Tests for the HA peer-IP re-resolve watcher.

The watcher must:
  • seed the initial hostname→IP map without firing a reload;
  • call apply_fn when a peer hostname resolves to a new IP;
  • tolerate transient DNS failures without thrashing;
  • skip peers whose URL is already an IP literal.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from spatium_dhcp_agent.peer_resolve import PeerResolveWatcher


def _bundle(peer_urls: list[str]) -> dict:
    """Build a minimal bundle with just enough structure for the watcher."""
    return {
        "bundle": {
            "failover": {
                "peers": [{"name": f"peer-{i}", "url": u} for i, u in enumerate(peer_urls)]
            }
        }
    }


def test_initial_set_bundle_does_not_reload():
    """Seeding the watcher must never trigger apply_fn — no "change" yet."""
    calls: list = []
    w = PeerResolveWatcher(apply_fn=lambda *a, **kw: calls.append((a, kw)))
    with patch("socket.gethostbyname", return_value="10.0.0.1"):
        w.set_bundle(_bundle(["http://dhcp-kea:8000/"]))
    assert calls == []


def test_tick_fires_reload_when_ip_changes():
    """A hostname that resolves to a new IP on the next tick triggers apply."""
    calls: list = []
    w = PeerResolveWatcher(apply_fn=lambda b, reload_kea=True: calls.append(reload_kea))
    with patch("socket.gethostbyname", return_value="10.0.0.1"):
        w.set_bundle(_bundle(["http://dhcp-kea:8000/"]))
    with patch("socket.gethostbyname", return_value="10.0.0.2"):
        w._tick_once()
    assert calls == [True]


def test_tick_no_reload_when_ip_unchanged():
    calls: list = []
    w = PeerResolveWatcher(apply_fn=lambda *a, **kw: calls.append(1))
    with patch("socket.gethostbyname", return_value="10.0.0.1"):
        w.set_bundle(_bundle(["http://dhcp-kea:8000/"]))
        w._tick_once()
    assert calls == []


def test_transient_resolution_failure_does_not_thrash():
    """OSError on resolution means "try again later", not "reload now"."""
    calls: list = []
    w = PeerResolveWatcher(apply_fn=lambda *a, **kw: calls.append(1))
    with patch("socket.gethostbyname", return_value="10.0.0.1"):
        w.set_bundle(_bundle(["http://dhcp-kea:8000/"]))
    with patch("socket.gethostbyname", side_effect=OSError("temp fail")):
        w._tick_once()
    assert calls == []


def test_ip_literal_peer_skipped():
    """If a peer URL is already an IP literal, no resolution attempt is made."""
    resolve_attempts = []

    def _fake_resolve(host):
        resolve_attempts.append(host)
        return "10.0.0.9"

    calls: list = []
    w = PeerResolveWatcher(apply_fn=lambda *a, **kw: calls.append(1))
    with patch("socket.gethostbyname", side_effect=_fake_resolve):
        w.set_bundle(_bundle(["http://192.0.2.5:8000/", "http://dhcp-kea:8000/"]))
        w._tick_once()
    # Only the hostname was resolved; the IP-literal peer was skipped
    assert resolve_attempts == ["dhcp-kea", "dhcp-kea"]
    assert calls == []


def test_empty_failover_no_op():
    """A bundle without a failover block must not fire reload or crash."""
    calls: list = []
    w = PeerResolveWatcher(apply_fn=lambda *a, **kw: calls.append(1))
    w.set_bundle({"bundle": {}})
    w._tick_once()
    assert calls == []


def test_apply_fn_exception_does_not_kill_watcher():
    """If apply_fn raises, the watcher logs and continues (no propagation)."""

    def _boom(bundle, reload_kea=True):
        raise RuntimeError("kea ate it")

    w = PeerResolveWatcher(apply_fn=_boom)
    with patch("socket.gethostbyname", return_value="10.0.0.1"):
        w.set_bundle(_bundle(["http://dhcp-kea:8000/"]))
    with patch("socket.gethostbyname", return_value="10.0.0.2"):
        # Must not propagate
        w._tick_once()


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
