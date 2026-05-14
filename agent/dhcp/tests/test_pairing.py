"""Unit tests for the DHCP agent's pairing-code → bootstrap-key
resolver (#169 Phase 3). Mirror of ``agent/dns/tests/test_pairing.py``
— same matrix of cases but reading the ``dhcp`` half of the
response.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from spatium_dhcp_agent.pairing import (
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
    _save_bootstrap_key(tmp_path, "cached-key")
    out = resolve_bootstrap_key(
        explicit_key="env-key",
        pairing_code="",
        state_dir=tmp_path,
        hostname="dhcp-1",
        client_factory=lambda: _StubClient(_StubResponse(500)),
    )
    assert out == "env-key"


def test_cached_key_wins_over_pairing_code(tmp_path: Path) -> None:
    _save_bootstrap_key(tmp_path, "previously-resolved")

    def factory() -> _StubClient:  # pragma: no cover
        raise AssertionError("should not call /pair when cache is present")

    out = resolve_bootstrap_key(
        explicit_key="",
        pairing_code="11111111",
        state_dir=tmp_path,
        hostname="dhcp-1",
        client_factory=factory,
    )
    assert out == "previously-resolved"


def test_pairing_code_resolves_and_persists(tmp_path: Path) -> None:
    stub = _StubClient(
        _StubResponse(
            200,
            json_body={
                "bootstrap_keys": {"dhcp": "real-dhcp-key"},
                "deployment_kind": "dhcp",
                "server_group_id": None,
            },
        )
    )
    out = resolve_bootstrap_key(
        explicit_key="",
        pairing_code="12345678",
        state_dir=tmp_path,
        hostname="dhcp-1",
        client_factory=lambda: stub,
    )
    assert out == "real-dhcp-key"
    assert _load_bootstrap_key(tmp_path) == "real-dhcp-key"
    assert stub.calls == [
        ("/api/v1/appliance/pair", {"code": "12345678", "hostname": "dhcp-1"})
    ]
    mode = (tmp_path / _BOOTSTRAP_KEY_FILE).stat().st_mode & 0o777
    assert mode == 0o600


def test_pairing_code_kind_both_picks_dhcp_half(tmp_path: Path) -> None:
    stub = _StubClient(
        _StubResponse(
            200,
            json_body={
                "bootstrap_keys": {"dns": "ignored", "dhcp": "the-dhcp-key"},
                "deployment_kind": "both",
                "server_group_id": None,
            },
        )
    )
    out = resolve_bootstrap_key(
        explicit_key="",
        pairing_code="22222222",
        state_dir=tmp_path,
        hostname="dhcp-combined",
        client_factory=lambda: stub,
    )
    assert out == "the-dhcp-key"


def test_pairing_code_dns_only_raises(tmp_path: Path) -> None:
    stub = _StubClient(
        _StubResponse(
            200,
            json_body={
                "bootstrap_keys": {"dns": "dns-only"},
                "deployment_kind": "dns",
                "server_group_id": None,
            },
        )
    )
    with pytest.raises(PairingError, match="doesn't carry a DHCP"):
        resolve_bootstrap_key(
            explicit_key="",
            pairing_code="33333333",
            state_dir=tmp_path,
            hostname="dhcp-1",
            client_factory=lambda: stub,
        )


def test_pairing_code_403_is_fatal(tmp_path: Path) -> None:
    stub = _StubClient(_StubResponse(403, text="invalid or expired"))
    with pytest.raises(PairingError, match="rejected"):
        resolve_bootstrap_key(
            explicit_key="",
            pairing_code="44444444",
            state_dir=tmp_path,
            hostname="dhcp-1",
            client_factory=lambda: stub,
        )
    assert _load_bootstrap_key(tmp_path) is None


def test_no_inputs_raises_clearly(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="must be set"):
        resolve_bootstrap_key(
            explicit_key="",
            pairing_code="",
            state_dir=tmp_path,
            hostname="dhcp-1",
            client_factory=lambda: _StubClient(_StubResponse(500)),
        )
