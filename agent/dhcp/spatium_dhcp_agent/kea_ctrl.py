"""Tiny client for the Kea control unix socket.

Kea speaks line-delimited JSON on the control socket; each request is a single
JSON object with ``command`` + optional ``arguments``, the response is a single
JSON object.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class KeaCtrlError(RuntimeError):
    pass


def send_command(
    socket_path: Path,
    command: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Send a single command over the Kea control unix socket and return the
    decoded JSON response."""
    payload: dict[str, Any] = {"command": command}
    if arguments is not None:
        payload["arguments"] = arguments
    data = json.dumps(payload).encode("utf-8")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(socket_path))
        s.sendall(data)
        # Kea closes after the single response; read until EOF.
        chunks: list[bytes] = []
        while True:
            buf = s.recv(65536)
            if not buf:
                break
            chunks.append(buf)
    raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not raw:
        raise KeaCtrlError(f"empty response for command {command!r}")
    try:
        resp = json.loads(raw)
    except json.JSONDecodeError as e:
        raise KeaCtrlError(f"non-JSON response from kea: {raw[:200]}") from e
    result = resp.get("result")
    if result not in (0, None):
        raise KeaCtrlError(
            f"kea command {command!r} failed: result={result} text={resp.get('text')!r}"
        )
    return resp


def config_reload(socket_path: Path) -> None:
    """Ask kea-dhcp4 to reload its config file."""
    log.info("kea_config_reload_send", socket=str(socket_path))
    send_command(socket_path, "config-reload")
    log.info("kea_config_reload_ok")
