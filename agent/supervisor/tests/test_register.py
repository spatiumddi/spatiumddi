"""Tests for the supervisor → control-plane register helper."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from spatium_supervisor.identity import load_or_generate
from spatium_supervisor.register import (
    RegisterDisabled,
    RegisterFatal,
    register,
)


def _client_for(handler) -> httpx.Client:
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def test_register_happy_path(tmp_path: Path) -> None:
    identity, _ = load_or_generate(tmp_path)

    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = req.read()
        return httpx.Response(
            200,
            json={
                "appliance_id": "11111111-2222-3333-4444-555555555555",
                "state": "pending_approval",
                "public_key_fingerprint": identity.fingerprint,
            },
        )

    result = register(
        control_plane_url="https://ddi.example.com/",
        pairing_code="12345678",
        identity=identity,
        hostname="dns-east-1",
        supervisor_version="2026.05.14-1",
        client=_client_for(handler),
        backoff_seconds=0,
    )
    assert result.appliance_id == "11111111-2222-3333-4444-555555555555"
    assert result.state == "pending_approval"
    assert (
        captured["url"]
        == "https://ddi.example.com/api/v1/appliance/supervisor/register"
    )
    assert b'"pairing_code":"12345678"' in captured["body"]
    assert b'"hostname":"dns-east-1"' in captured["body"]


def test_register_404_raises_disabled(tmp_path: Path) -> None:
    identity, _ = load_or_generate(tmp_path)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not found."})

    with pytest.raises(RegisterDisabled):
        register(
            control_plane_url="https://ddi.example.com",
            pairing_code="12345678",
            identity=identity,
            hostname="x",
            supervisor_version="dev",
            client=_client_for(handler),
            backoff_seconds=0,
        )


def test_register_403_raises_fatal(tmp_path: Path) -> None:
    identity, _ = load_or_generate(tmp_path)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "Pairing code is invalid."})

    with pytest.raises(RegisterFatal, match="Pairing code"):
        register(
            control_plane_url="https://ddi.example.com",
            pairing_code="00000000",
            identity=identity,
            hostname="x",
            supervisor_version="dev",
            client=_client_for(handler),
            backoff_seconds=0,
        )


def test_register_retries_on_5xx_then_succeeds(tmp_path: Path) -> None:
    identity, _ = load_or_generate(tmp_path)

    state = {"calls": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] < 3:
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(
            200,
            json={
                "appliance_id": "11111111-2222-3333-4444-555555555555",
                "state": "pending_approval",
                "public_key_fingerprint": identity.fingerprint,
            },
        )

    result = register(
        control_plane_url="https://ddi.example.com",
        pairing_code="12345678",
        identity=identity,
        hostname="x",
        supervisor_version="dev",
        client=_client_for(handler),
        backoff_seconds=0,
    )
    assert state["calls"] == 3
    assert result.appliance_id


def test_register_exhausts_attempts_and_raises_fatal(tmp_path: Path) -> None:
    identity, _ = load_or_generate(tmp_path)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(RegisterFatal, match="after"):
        register(
            control_plane_url="https://ddi.example.com",
            pairing_code="12345678",
            identity=identity,
            hostname="x",
            supervisor_version="dev",
            client=_client_for(handler),
            max_attempts=3,
            backoff_seconds=0,
        )


def test_register_422_raises_fatal(tmp_path: Path) -> None:
    identity, _ = load_or_generate(tmp_path)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "bad pubkey"})

    with pytest.raises(RegisterFatal, match="malformed"):
        register(
            control_plane_url="https://ddi.example.com",
            pairing_code="12345678",
            identity=identity,
            hostname="x",
            supervisor_version="dev",
            client=_client_for(handler),
            backoff_seconds=0,
        )
