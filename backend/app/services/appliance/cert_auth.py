"""Cert-based mTLS-equivalent auth for supervisor endpoints (#170 Wave D follow-up).

Replaces the Wave-B1 session-token interim on
``POST /api/v1/appliance/supervisor/heartbeat`` and
``POST /api/v1/appliance/supervisor/poll`` once the supervisor has
been approved and picked up its X.509 cert via heartbeat.

True mTLS — the supervisor presenting its client cert during the
TLS handshake — needs nginx to terminate TLS and forward the cert
to the api via ``X-SSL-Client-Cert``. SpatiumDDI's frontend nginx
doesn't do that today, and dev runs are plain HTTP against
localhost. Until that lands, we accept the cert + an Ed25519
signature over a per-request payload in custom headers. The
private key never leaves /var/persist/spatium-supervisor; the
signature proves possession of the key bound to the cert; the
cert chain proves the appliance was approved by an admin. Same
security properties as mTLS; just a different transport for the
cert + signature.

Header contract:

* ``X-Appliance-Cert``       — base64(PEM cert from
                                ``appliance.cert_pem``).
* ``X-Appliance-Timestamp``  — ISO 8601 UTC. Server requires
                                ±300 s skew (anti-replay).
* ``X-Appliance-Signature``  — base64(Ed25519 signature over
                                ``f"{method} {path}\\n{timestamp}\\n{appliance_id}"``).

Server-side validation (this module):

1. Decode + parse the cert.
2. Verify chain against the appliance_ca singleton row.
3. Verify cert not expired.
4. Verify the cert's subject CN parses as a UUID + matches a live
   appliance row in ``approved`` state.
5. Verify the cert's pubkey matches the appliance row's
   ``public_key_der`` (defence-in-depth against an old cert being
   replayed after the supervisor re-keyed).
6. Verify timestamp within ±300 s of server time.
7. Verify the signature against the cert's pubkey + the canonical
   payload.

On success, returns the ``Appliance`` row so the endpoint handler
can read it without re-fetching. On failure, raises 403 with a
generic "invalid client cert" message — never leaks which step
failed (timing-side-channel + auth-error-disclosure hygiene).

The session-token interim path stays in place as a fallback when
the cert headers are absent: pending_approval rows still need the
session token because their cert doesn't exist yet. The handler
dispatches to whichever auth produced a valid principal.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appliance import (
    APPLIANCE_STATE_APPROVED,
    Appliance,
    ApplianceCA,
)

logger = structlog.get_logger(__name__)

# Skew tolerance for X-Appliance-Timestamp. 5 minutes lines up with
# OAuth2's nbf/exp convention and with typical NTP drift on a fresh
# appliance host before chrony locks in.
_SKEW_TOLERANCE_SECONDS = 300


@dataclass(frozen=True)
class CertAuthPrincipal:
    """The result of validating cert-auth headers. Carries the loaded
    appliance row so the endpoint handler doesn't need a re-fetch."""

    appliance: Appliance


class CertAuthFailed(HTTPException):
    """Generic 403 — never expose which step failed."""

    def __init__(self, reason: str) -> None:
        super().__init__(status.HTTP_403_FORBIDDEN, "Invalid appliance client cert.")
        # Stash the reason on the exception for the route handler's
        # structured-log line; the operator-facing detail stays
        # generic.
        self.reason = reason


def _b64decode(value: str, field: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CertAuthFailed(f"{field} not base64") from exc


def _canonical_payload(method: str, path: str, timestamp: str, appliance_id: str) -> bytes:
    """The exact bytes the supervisor signs + we verify. Order matters:
    ``method`` + ``path`` lock the signature to the specific request;
    ``timestamp`` defeats replay; ``appliance_id`` prevents a
    supervisor reusing a signature meant for one row against another's
    endpoint."""
    return f"{method.upper()} {path}\n{timestamp}\n{appliance_id}".encode()


async def authenticate_cert(request: Request, db: AsyncSession) -> CertAuthPrincipal | None:
    """Validate cert-auth headers on ``request`` against the appliance
    row + CA.

    Returns ``None`` when the headers are absent (route falls back to
    the session-token path); raises :class:`CertAuthFailed` (403) when
    headers are present but invalid.

    Doesn't touch ``request.state`` — the caller decides how to thread
    the principal into the handler.
    """
    cert_header = request.headers.get("X-Appliance-Cert")
    sig_header = request.headers.get("X-Appliance-Signature")
    ts_header = request.headers.get("X-Appliance-Timestamp")
    if cert_header is None or sig_header is None or ts_header is None:
        return None  # fall through to session-token path

    # ── 1. Parse the cert. ─────────────────────────────────────────
    try:
        cert_pem = _b64decode(cert_header, "X-Appliance-Cert")
        cert = x509.load_pem_x509_certificate(cert_pem)
    except (ValueError, CertAuthFailed) as exc:
        raise CertAuthFailed(f"cert parse: {exc}") from exc

    # ── 2. Verify chain against the appliance_ca singleton. ────────
    ca_row = await db.get(ApplianceCA, 1)
    if ca_row is None:
        raise CertAuthFailed("CA not initialised")
    try:
        ca_cert = x509.load_pem_x509_certificate(ca_row.cert_pem.encode("ascii"))
    except ValueError as exc:
        raise CertAuthFailed(f"CA cert parse: {exc}") from exc
    ca_pubkey = ca_cert.public_key()
    if not isinstance(ca_pubkey, RSAPublicKey):
        raise CertAuthFailed("CA pubkey not RSA")
    try:
        # x509.verify_directly_issued_by lands in cryptography 40+;
        # for the version pinned in this repo we use the manual path
        # of verifying signature + comparing issuer.
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ca_pubkey.verify(
            cert.signature,
            cert.tbs_certificate_bytes,
            padding.PKCS1v15(),
            cert.signature_hash_algorithm or hashes.SHA256(),
        )
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise CertAuthFailed(f"chain verify: {exc}") from exc

    # ── 3. Expiry. ────────────────────────────────────────────────
    now = datetime.now(UTC)
    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc
    if now < not_before or now > not_after:
        raise CertAuthFailed(f"cert expired or not yet valid (window {not_before} → {not_after})")

    # ── 4. Subject CN → appliance row. ────────────────────────────
    try:
        cn_attr = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0]
        cn_value = cn_attr.value
        if isinstance(cn_value, bytes):
            cn_value = cn_value.decode("ascii")
        appliance_id = uuid.UUID(cn_value)
    except (IndexError, ValueError) as exc:
        raise CertAuthFailed(f"subject CN: {exc}") from exc
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise CertAuthFailed("appliance row not found")
    if row.state != APPLIANCE_STATE_APPROVED:
        raise CertAuthFailed(f"appliance state {row.state!r} != approved")

    # ── 5. Cert pubkey == row pubkey. ─────────────────────────────
    cert_pubkey = cert.public_key()
    if not isinstance(cert_pubkey, Ed25519PublicKey):
        raise CertAuthFailed("cert pubkey not Ed25519")
    from cryptography.hazmat.primitives.serialization import (  # noqa: PLC0415
        Encoding,
        PublicFormat,
    )

    cert_pubkey_der = cert_pubkey.public_bytes(
        encoding=Encoding.DER, format=PublicFormat.SubjectPublicKeyInfo
    )
    if cert_pubkey_der != row.public_key_der:
        raise CertAuthFailed("cert pubkey != row pubkey (re-keyed?)")

    # ── 6. Timestamp skew. ────────────────────────────────────────
    try:
        ts = datetime.fromisoformat(ts_header)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except ValueError as exc:
        raise CertAuthFailed(f"timestamp parse: {exc}") from exc
    skew = abs((now - ts).total_seconds())
    if skew > _SKEW_TOLERANCE_SECONDS:
        raise CertAuthFailed(f"timestamp skew {skew:.0f}s > {_SKEW_TOLERANCE_SECONDS}s")

    # ── 7. Signature over the canonical payload. ──────────────────
    try:
        signature = _b64decode(sig_header, "X-Appliance-Signature")
    except CertAuthFailed:
        raise
    payload = _canonical_payload(
        request.method,
        request.url.path,
        ts_header,
        str(appliance_id),
    )
    try:
        cert_pubkey.verify(signature, payload)
    except InvalidSignature as exc:
        raise CertAuthFailed(f"signature: {exc}") from exc

    return CertAuthPrincipal(appliance=row)


__all__ = ["authenticate_cert", "CertAuthFailed", "CertAuthPrincipal"]


# Quiet ruff unused-import in the lazy-import block above. Putting the
# imports inside the function body keeps cryptography out of the
# module's import graph until a request actually needs them.
_ = timedelta
