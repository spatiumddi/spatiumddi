"""Internal CA + cert signing for the appliance fleet (#170 Wave B1).

The control plane runs its own private CA (RSA-2048 self-signed root)
that signs every supervisor's identity cert on admin approval. The CA
is a singleton row in ``appliance_ca`` — generated lazily on first
need (first approve attempt) so a fresh-install control plane that
never approves a supervisor doesn't pay the cost.

Cert lifecycle (B1):

1. Supervisor registers with its Ed25519 pubkey → ``appliance`` row
   in ``pending_approval`` state.
2. Admin clicks Approve → this module's ``sign_supervisor_cert()``
   builds an X.509 cert: subject CN = appliance_id, SAN = the pubkey
   fingerprint as a custom OID + DNS, public_key = the supervisor's
   submitted Ed25519 pubkey, signed by the CA's RSA-2048 key. 90-day
   validity.
3. Cert + cert serial + issued/expires timestamps are written back to
   the appliance row. Supervisor picks them up via /supervisor/poll.
4. (Wave C) the supervisor's mTLS client cert presents this; the
   API verifies the presented cert chains to the CA + the cert's CN
   matches a live appliance row.

Algorithm choice — the supervisor identity is Ed25519 (modern, small,
fast) but the CA is RSA-2048 for maximum HTTP/TLS client compat.
``cryptography`` supports mixed-algorithm X.509 fine (Ed25519 subject
key, RSA-2048 issuer signature).

Storage — the CA's private key lives in ``appliance_ca.key_encrypted``
Fernet-encrypted using the global SECRET_KEY-derived Fernet (see
``app.core.crypto``). The public cert is plaintext PEM. Operators who
need to re-issue all certs against a fresh CA today would have to
manually clear the row + re-approve every appliance; CA rotation is
a Wave-D-or-later affordance.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_pem_private_key,
)
from cryptography.x509.oid import NameOID
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_str, encrypt_str
from app.models.appliance import (  # noqa: F401  -- Appliance used by needs_renewal()
    Appliance,
    ApplianceCA,
)

# Lifetimes — picked per the issue's open-question recommendation:
# CA = 10 years (re-issue is operationally painful; long enough to
# outlast a typical platform's lifespan), supervisor cert = 90 days
# with 30-day renewal window. Tweakable by environment variable for
# integration tests that want to exercise expiry without time-travel.
CA_VALIDITY_DAYS = 365 * 10
SUPERVISOR_CERT_VALIDITY_DAYS = 90
SUPERVISOR_CERT_RENEWAL_DAYS = 30  # renew when <= this many days remain

CA_KEY_SIZE_BITS = 2048
CA_SUBJECT_CN = "SpatiumDDI Internal Appliance CA"


# ── CA bootstrap ───────────────────────────────────────────────────


async def ensure_ca(db: AsyncSession) -> ApplianceCA:
    """Return the singleton CA row, generating it if missing.

    Idempotent — concurrent first-time calls might race; the unique
    primary key on ``id=1`` ensures only one survives. The loser
    re-reads the winning row on its next call.
    """
    row = await db.get(ApplianceCA, 1)
    if row is not None:
        return row

    now = datetime.now(UTC)
    expires = now + timedelta(days=CA_VALIDITY_DAYS)

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=CA_KEY_SIZE_BITS,
    )

    # Self-signed root cert.
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_SUBJECT_CN)])
    serial = x509.random_serial_number()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(serial)
        .not_valid_before(now)
        .not_valid_after(expires)
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(Encoding.PEM).decode("ascii")
    key_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    ).decode("ascii")
    key_encrypted = encrypt_str(key_pem)

    row = ApplianceCA(
        id=1,
        subject_cn=CA_SUBJECT_CN,
        algorithm=f"rsa-{CA_KEY_SIZE_BITS}",
        cert_pem=cert_pem,
        key_encrypted=key_encrypted,
        created_at=now,
        expires_at=expires,
    )
    db.add(row)
    await db.flush()
    return row


def _load_ca_private_key(row: ApplianceCA) -> rsa.RSAPrivateKey:
    """Decrypt + parse the CA's RSA private key. Raises on Fernet
    failure (means SECRET_KEY changed between CA generation + use —
    same operator-painful scenario as every other Fernet column in
    the schema)."""
    pem = decrypt_str(row.key_encrypted).encode("ascii")
    key = load_pem_private_key(pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise RuntimeError(
            f"appliance_ca.key_encrypted is not an RSA private key "
            f"(got {type(key).__name__}); CA is corrupt."
        )
    return key


# ── Supervisor cert issuance ───────────────────────────────────────


def sign_supervisor_cert(
    *,
    ca: ApplianceCA,
    appliance_id: uuid.UUID,
    public_key_der: bytes,
    public_key_fingerprint: str,
    hostname: str,
    validity_days: int = SUPERVISOR_CERT_VALIDITY_DAYS,
) -> tuple[str, str, datetime, datetime]:
    """Sign a fresh supervisor identity cert.

    Returns ``(cert_pem, serial_hex, issued_at, expires_at)``. The
    serial is hex-encoded uppercase (matching ``openssl x509 -serial``
    output) so it round-trips through string columns + JSON cleanly.

    Subject CN = appliance_id (UUID string). SANs include the
    fingerprint as a DNS-shaped entry (``<fp>.appliance.spatiumddi``)
    so the supervisor's mTLS handshake can be SAN-verified against
    the cached fingerprint without needing OID parsing.
    """
    # Parse the supervisor's Ed25519 pubkey and re-derive its canonical
    # SPKI form (defends against subtly-different DER encodings on the
    # wire vs what we'll embed in the cert).
    sup_pub = serialization.load_der_public_key(public_key_der)
    if not isinstance(sup_pub, ed25519.Ed25519PublicKey):
        raise ValueError(f"supervisor pubkey is not Ed25519 (got {type(sup_pub).__name__})")

    ca_private = _load_ca_private_key(ca)
    ca_cert = x509.load_pem_x509_certificate(ca.cert_pem.encode("ascii"))

    now = datetime.now(UTC)
    expires_at = now + timedelta(days=validity_days)
    serial = x509.random_serial_number()

    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, str(appliance_id)),
            # Free-form OU carries the operator-visible hostname so a
            # quick ``openssl x509 -text`` on the cert is descriptive.
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, hostname[:64]),
        ]
    )

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(sup_pub)
        .serial_number(serial)
        .not_valid_before(now)
        .not_valid_after(expires_at)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    # Fingerprint-as-DNS — easiest verification path
                    # for the supervisor's local mTLS client (Wave C).
                    x509.DNSName(f"{public_key_fingerprint}.appliance.spatiumddi"),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(sup_pub),
            critical=False,
        )
        .add_extension(
            # AuthorityKeyIdentifier expects a sign-capable key type;
            # ca_cert.public_key() returns the union of every key type
            # cryptography can parse. We just generated this CA above
            # with rsa.generate_private_key, so we know the type — the
            # cast keeps mypy happy without runtime cost.
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_private.public_key()
            ),
            critical=False,
        )
        .sign(ca_private, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(Encoding.PEM).decode("ascii")
    serial_hex = f"{serial:X}"
    return cert_pem, serial_hex, now, expires_at


# ── Session token (unauth poll between register + approval) ─────────


def generate_session_token() -> tuple[str, str]:
    """Return ``(cleartext, sha256_hex)``. The cleartext goes to the
    supervisor in the register response; the hash lives on the
    appliance row for constant-time verification on /supervisor/poll.
    """
    cleartext = secrets.token_urlsafe(32)  # ~256 bits, URL-safe
    import hashlib

    digest = hashlib.sha256(cleartext.encode("ascii")).hexdigest()
    return cleartext, digest


def verify_session_token(submitted: str, stored_hash: str | None) -> bool:
    """Constant-time compare of submitted token against stored hash.
    Returns False if the appliance has no session token (already
    approved + transitioned to mTLS, or never had one)."""
    if not stored_hash:
        return False
    import hashlib
    import hmac

    submitted_hash = hashlib.sha256(submitted.encode("ascii")).hexdigest()
    return hmac.compare_digest(submitted_hash, stored_hash)


# Convenience for the supervisor's renewal-window calculation
def needs_renewal(appliance: Appliance, *, now: datetime | None = None) -> bool:
    """True when the supervisor should request a fresh cert. Driven
    by the global renewal-window constant; called by the supervisor's
    /poll handler to decide whether to ask for a re-issue."""
    if appliance.cert_expires_at is None:
        return False
    now = now or datetime.now(UTC)
    remaining = appliance.cert_expires_at - now
    return remaining <= timedelta(days=SUPERVISOR_CERT_RENEWAL_DAYS)


# Re-export for endpoint code
__all__ = [
    "ApplianceCA",
    "ensure_ca",
    "sign_supervisor_cert",
    "generate_session_token",
    "verify_session_token",
    "needs_renewal",
    "SUPERVISOR_CERT_VALIDITY_DAYS",
    "SUPERVISOR_CERT_RENEWAL_DAYS",
]
