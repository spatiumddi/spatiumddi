"""Unit tests for the DNS agent's pairing-code → bootstrap-key
resolver (#169 Phase 3).

These don't hit the network — they exercise the precedence rules
plus the response-shape parsing, using a stub ``httpx.Client``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from spatium_dns_agent.pairing import (
    PairingError,
    _BOOTSTRAP_KEY_FILE,
    _load_bootstrap_key,
    _save_bootstrap_key,
    resolve_bootstrap_key,
)


class _StubResponse:
    def __init__(self, status_code: int, json_body: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_body
        self.text = text

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _StubClient:
    """Minimal httpx-shaped stub. Records every ``.post`` call's args
    so tests can assert on the wire shape."""

    def __init__(self, response: _StubResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, *, json: dict) -> _StubResponse:
        self.calls.append((url, json))
        return self.response

    def __enter__(self) -> "_StubClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_explicit_key_wins_over_cached(tmp_path: Path) -> None:
    """Operator-supplied env key beats anything on disk — gives them a
    way to force re-bootstrap with the long form."""
    _save_bootstrap_key(tmp_path, "cached-key")
    out = resolve_bootstrap_key(
        explicit_key="env-key",
        pairing_code="",
        state_dir=tmp_path,
        hostname="dns-1",
        client_factory=lambda: _StubClient(_StubResponse(500)),
    )
    assert out == "env-key"


def test_cached_key_wins_over_pairing_code(tmp_path: Path) -> None:
    """A code already consumed once on a prior boot leaves a key on
    disk; we use that rather than try to re-consume (which would 403
    because codes are single-use)."""
    _save_bootstrap_key(tmp_path, "previously-resolved")

    # Stub would raise if hit — we shouldn't hit it.
    def factory() -> _StubClient:  # pragma: no cover
        raise AssertionError("should not call /pair when cache is present")

    out = resolve_bootstrap_key(
        explicit_key="",
        pairing_code="11111111",
        state_dir=tmp_path,
        hostname="dns-1",
        client_factory=factory,
    )
    assert out == "previously-resolved"


def test_pairing_code_resolves_and_persists(tmp_path: Path) -> None:
    """Happy path: code exchanged for the DNS key from a 'dns' code."""
    stub = _StubClient(
        _StubResponse(
            200,
            json_body={
                "bootstrap_keys": {"dns": "real-dns-key"},
                "deployment_kind": "dns",
                "server_group_id": None,
            },
        )
    )
    out = resolve_bootstrap_key(
        explicit_key="",
        pairing_code="12345678",
        state_dir=tmp_path,
        hostname="dns-1",
        client_factory=lambda: stub,
    )
    assert out == "real-dns-key"
    # Was persisted for re-bootstrap.
    assert _load_bootstrap_key(tmp_path) == "real-dns-key"
    # Wire shape — we send code + hostname only, no bootstrap key.
    assert stub.calls == [
        ("/api/v1/appliance/pair", {"code": "12345678", "hostname": "dns-1"})
    ]
    # File mode is 0600.
    mode = (tmp_path / _BOOTSTRAP_KEY_FILE).stat().st_mode & 0o777
    assert mode == 0o600


def test_pairing_code_kind_both_picks_dns_half(tmp_path: Path) -> None:
    """A 'both' code carries both keys; the DNS agent reads its own."""
    stub = _StubClient(
        _StubResponse(
            200,
            json_body={
                "bootstrap_keys": {"dns": "the-dns-key", "dhcp": "ignored"},
                "deployment_kind": "both",
                "server_group_id": None,
            },
        )
    )
    out = resolve_bootstrap_key(
        explicit_key="",
        pairing_code="22222222",
        state_dir=tmp_path,
        hostname="dns-combined",
        client_factory=lambda: stub,
    )
    assert out == "the-dns-key"


def test_pairing_code_dhcp_only_raises(tmp_path: Path) -> None:
    """A 'dhcp' code can't satisfy a DNS agent — the response has no
    DNS key. Fatal: code was for the wrong kind, operator needs a
    fresh 'dns' or 'both' code."""
    stub = _StubClient(
        _StubResponse(
            200,
            json_body={
                "bootstrap_keys": {"dhcp": "dhcp-only"},
                "deployment_kind": "dhcp",
                "server_group_id": None,
            },
        )
    )
    with pytest.raises(PairingError, match="doesn't carry a DNS"):
        resolve_bootstrap_key(
            explicit_key="",
            pairing_code="33333333",
            state_dir=tmp_path,
            hostname="dns-1",
            client_factory=lambda: stub,
        )


def test_pairing_code_403_is_fatal(tmp_path: Path) -> None:
    """A 403 means the code is dead — no point retrying. Surface as
    PairingError so the supervisor exits cleanly."""
    stub = _StubClient(_StubResponse(403, text="invalid or expired"))
    with pytest.raises(PairingError, match="rejected"):
        resolve_bootstrap_key(
            explicit_key="",
            pairing_code="44444444",
            state_dir=tmp_path,
            hostname="dns-1",
            client_factory=lambda: stub,
        )
    # Nothing should have been cached on failure.
    assert _load_bootstrap_key(tmp_path) is None


def test_no_inputs_raises_clearly(tmp_path: Path) -> None:
    """No env key, no pairing code, no cached key = clear error."""
    with pytest.raises(RuntimeError, match="must be set"):
        resolve_bootstrap_key(
            explicit_key="",
            pairing_code="",
            state_dir=tmp_path,
            hostname="dns-1",
            client_factory=lambda: _StubClient(_StubResponse(500)),
        )
