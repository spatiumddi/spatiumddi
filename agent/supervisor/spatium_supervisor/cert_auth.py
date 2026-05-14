"""Supervisor-side cert auth (#170 Wave D follow-up).

The supervisor's heartbeat picks up the control-plane-signed X.509
cert + CA chain on the first heartbeat after admin approval (see
``SupervisorHeartbeatResponse.cert_pem``). This module:

* Persists the cert + CA chain under ``{state_dir}/tls/`` so a
  supervisor restart finds them again.
* Loads them back on subsequent boots.
* Builds the auth headers the backend's ``cert_auth.py`` middleware
  validates: ``X-Appliance-Cert`` (base64 PEM), ``X-Appliance-
  Timestamp`` (ISO 8601 UTC), ``X-Appliance-Signature`` (base64
  Ed25519 over ``method + path + timestamp + appliance_id``).

The private key never leaves disk — we only sign payloads with it.
The signed payload binds the request method + path + timestamp +
appliance_id so a captured signature can't be replayed against a
different endpoint or after the 5-min skew window.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

CERT_FILENAME = "cert.pem"
CA_CHAIN_FILENAME = "ca-chain.pem"


def _tls_dir(state_dir: Path) -> Path:
    return state_dir / "tls"


def save_cert(state_dir: Path, cert_pem: str, ca_chain_pem: str) -> None:
    """Persist the cert + CA chain. Idempotent — re-saving identical
    bytes is a no-op (the supervisor heartbeat-loop calls this every
    tick once approved; we only write when content changes to avoid
    pointless disk churn)."""
    tls_dir = _tls_dir(state_dir)
    tls_dir.mkdir(parents=True, exist_ok=True)

    cert_path = tls_dir / CERT_FILENAME
    if not _content_matches(cert_path, cert_pem):
        _atomic_write(cert_path, cert_pem)
    ca_path = tls_dir / CA_CHAIN_FILENAME
    if not _content_matches(ca_path, ca_chain_pem):
        _atomic_write(ca_path, ca_chain_pem)


def load_cert(state_dir: Path) -> str | None:
    """Return the persisted cert PEM, or ``None`` when not yet approved."""
    path = _tls_dir(state_dir) / CERT_FILENAME
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="ascii")
    except OSError:
        return None


def _content_matches(path: Path, body: str) -> bool:
    if not path.exists():
        return False
    try:
        return path.read_text(encoding="ascii") == body
    except OSError:
        return False


def _atomic_write(path: Path, body: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="ascii")
    tmp.chmod(0o644)
    tmp.replace(path)


def build_auth_headers(
    method: str,
    path: str,
    cert_pem: str,
    private_key: Ed25519PrivateKey,
    appliance_id: uuid.UUID,
) -> dict[str, str]:
    """Build the three cert-auth headers. Caller passes the request
    method (``"POST"``) + path (``/api/v1/appliance/supervisor/heartbeat``)
    so the signature binds to the specific request, defeating replays."""
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    payload = f"{method.upper()} {path}\n{timestamp}\n{appliance_id}".encode("utf-8")
    signature = private_key.sign(payload)
    return {
        "X-Appliance-Cert": base64.b64encode(cert_pem.encode("ascii")).decode("ascii"),
        "X-Appliance-Timestamp": timestamp,
        "X-Appliance-Signature": base64.b64encode(signature).decode("ascii"),
    }


__all__ = ["save_cert", "load_cert", "build_auth_headers"]
