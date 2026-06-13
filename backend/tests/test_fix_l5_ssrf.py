"""Regression tests for the L5 defense-in-depth SSRF guard.

SECURITY (#400 / GHSA-mj4g-hw3m-62rm, finding L5):
    The integration "Test Connection" endpoints have the control plane
    dial an operator-supplied host. ``app.core.ssrf.assert_safe_target``
    resolves the host, logs the resolved IP, and flags the classic SSRF
    pivot targets (loopback / link-local / cloud-metadata) while
    deliberately treating RFC1918 LAN hosts as legitimate (proxmox /
    unifi / opnsense / docker / k8s on the LAN must keep working).

These tests pin that behaviour with no DB and a mocked resolver so they
stay hermetic (no real DNS / network).
"""

from __future__ import annotations

import pytest

from app.core.ssrf import (
    SSRFBlockedError,
    assert_safe_target,
    extract_host,
)


@pytest.mark.parametrize(
    "target,expected",
    [
        ("192.168.1.10", "192.168.1.10"),
        ("https://192.168.1.10:8006/", "192.168.1.10"),
        ("https://pve.lan:8006/api2/json", "pve.lan"),
        ("tcp://docker.example.com:2376", "docker.example.com"),
        ("controller.lan:8443", "controller.lan"),
        ("unix:///var/run/docker.sock", ""),  # no network host
        ("npipe:////./pipe/docker_engine", ""),
        ("", ""),
        ("[2001:db8::1]:443", "2001:db8::1"),
    ],
)
def test_extract_host(target: str, expected: str) -> None:
    assert extract_host(target) == expected


def test_rfc1918_target_is_allowed_not_flagged(monkeypatch) -> None:
    """A LAN (RFC1918) target is the legitimate common case — it must
    resolve cleanly, be logged, and NOT be flagged or blocked."""

    warned: list[tuple] = []
    monkeypatch.setattr(
        "app.core.ssrf.logger.warning",
        lambda *a, **k: warned.append((a, k)),
    )

    # Bare IP literal — no DNS needed.
    resolved = assert_safe_target("10.0.5.20", label="proxmox", block=True)
    assert resolved == ["10.0.5.20"]
    assert warned == []  # RFC1918 is never flagged


def test_loopback_is_flagged_but_not_blocked_by_default(monkeypatch) -> None:
    """Loopback is flagged (WARNING) but the advisory default never
    raises — co-located on-box services must keep working."""

    warned: list[tuple] = []
    monkeypatch.setattr(
        "app.core.ssrf.logger.warning",
        lambda *a, **k: warned.append((a, k)),
    )

    resolved = assert_safe_target("127.0.0.1", label="docker")
    assert resolved == ["127.0.0.1"]
    assert len(warned) == 1
    kwargs = warned[0][1]
    assert kwargs["blocked"] is False
    assert any("loopback" in f for f in kwargs["flagged"])


def test_cloud_metadata_is_flagged(monkeypatch) -> None:
    """169.254.169.254 — the canonical cloud-creds SSRF pivot — is
    flagged with the dedicated reason string."""

    warned: list[tuple] = []
    monkeypatch.setattr(
        "app.core.ssrf.logger.warning",
        lambda *a, **k: warned.append((a, k)),
    )

    assert_safe_target("http://169.254.169.254/latest/meta-data/", label="k8s")
    assert len(warned) == 1
    assert any("cloud_metadata" in f for f in warned[0][1]["flagged"])


def test_link_local_is_flagged(monkeypatch) -> None:
    warned: list[tuple] = []
    monkeypatch.setattr(
        "app.core.ssrf.logger.warning",
        lambda *a, **k: warned.append((a, k)),
    )

    assert_safe_target("169.254.10.10", label="opnsense")
    assert len(warned) == 1
    assert any("link_local" in f for f in warned[0][1]["flagged"])


def test_block_true_raises_on_pivot() -> None:
    """When a caller opts into hard blocking, a pivot target raises."""

    with pytest.raises(SSRFBlockedError):
        assert_safe_target("127.0.0.1", label="test", block=True)
    with pytest.raises(SSRFBlockedError):
        assert_safe_target("169.254.169.254", label="test", block=True)


def test_hostname_resolving_to_loopback_is_flagged(monkeypatch) -> None:
    """A hostname (not a literal) that resolves to loopback is flagged —
    this is the DNS-rebind / localhost-alias SSRF case."""

    def fake_getaddrinfo(host, *a, **k):
        return [(2, 1, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr("app.core.ssrf.socket.getaddrinfo", fake_getaddrinfo)
    warned: list[tuple] = []
    monkeypatch.setattr(
        "app.core.ssrf.logger.warning",
        lambda *a, **k: warned.append((a, k)),
    )

    resolved = assert_safe_target("evil.lanalias.example", label="proxmox")
    assert resolved == ["127.0.0.1"]
    assert len(warned) == 1
    assert any("loopback" in f for f in warned[0][1]["flagged"])


def test_resolution_failure_is_non_fatal(monkeypatch) -> None:
    """If DNS resolution fails, the guard returns [] and does NOT raise —
    the downstream connect surfaces the real error."""

    def boom(host, *a, **k):
        raise OSError("name resolution failed")

    monkeypatch.setattr("app.core.ssrf.socket.getaddrinfo", boom)

    resolved = assert_safe_target("nonexistent.invalid", label="proxmox", block=True)
    assert resolved == []  # non-fatal


def test_unix_socket_endpoint_short_circuits(monkeypatch) -> None:
    """A unix-socket docker endpoint has no network host — the guard
    returns [] without attempting resolution or flagging."""

    def should_not_run(*a, **k):  # pragma: no cover
        raise AssertionError("getaddrinfo must not be called for unix://")

    monkeypatch.setattr("app.core.ssrf.socket.getaddrinfo", should_not_run)

    assert assert_safe_target("unix:///var/run/docker.sock", label="docker") == []
