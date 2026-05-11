"""Self-signed cert bootstrap — Phase 4b.5 (issue #134).

Runs on appliance api startup. If the ``appliance_certificate`` table
has no active row, generate a self-signed certificate valid 5 years
with the appliance's hostname + every detected non-loopback IP as
SANs, insert it as ``source=self-signed`` + ``is_active=true``, and
deploy it to the cert volume so nginx has something to serve from
the very first request.

If an active row already exists (operator-uploaded cert, CSR-signed,
or a previous self-signed bootstrap), we still re-deploy it to the
cert volume — handles the case where the volume was wiped (fresh
upgrade, factory reset, manual ``docker volume rm``) but the DB
row survived. nginx then reloads via deployer.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import socket
import uuid

import structlog
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from app.config import settings
from app.core.crypto import decrypt_str, encrypt_str
from app.db import AsyncSessionLocal
from app.models.appliance import CERT_SOURCE_SELF_SIGNED, ApplianceCertificate
from app.services.appliance.deployment import deploy_and_reload
from app.services.appliance.tls import _format_fingerprint

logger = structlog.get_logger(__name__)

# Self-signed certs default to 5 years — long enough that the operator
# won't trip "browser refuses connection" mid-deploy, short enough that
# leaving the appliance on its self-signed default forever is mildly
# uncomfortable (and motivates the operator to upload a real cert).
_SELF_SIGNED_DAYS = 5 * 365


async def ensure_self_signed_cert() -> None:
    """Idempotent: ensure an active cert exists + is materialised on disk.

    Path A — active row already exists:
        Re-deploy the cert to the cert volume (handles wipe-of-volume
        edge cases) and reload nginx. Cheap and idempotent: the file
        write is atomic + the SIGHUP costs ~50ms.

    Path B — no active row:
        Generate a fresh RSA-2048 self-signed cert. CN = appliance
        hostname; SANs = hostname + every non-loopback IPv4/IPv6
        bound to a local interface. Insert as is_active=true,
        source=self-signed. Deploy + reload.

    Failure-tolerant: any error logs + returns. The lifespan caller
    has a try/except wrapping this whole function — appliance boot
    should never fail because of TLS bootstrap.
    """
    if not settings.appliance_mode:
        return

    async with AsyncSessionLocal() as db:
        active_row = (
            await db.execute(
                select(ApplianceCertificate).where(
                    ApplianceCertificate.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()

        if active_row is not None and active_row.cert_pem:
            # Path A — replay the active cert to disk in case the
            # volume was reset. Decrypts the key inside this function
            # only; the plaintext never leaves the deployer's scope.
            try:
                key_pem = decrypt_str(active_row.key_encrypted)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "appliance_active_cert_decrypt_failed",
                    cert_id=str(active_row.id),
                    error=str(exc),
                )
                return
            deploy_and_reload(
                active_row.cert_pem, key_pem, name=active_row.name
            )
            logger.info(
                "appliance_active_cert_redeployed",
                cert_id=str(active_row.id),
                name=active_row.name,
                source=active_row.source,
            )
            return

        # Path B — generate a self-signed default.
        hostname = settings.appliance_hostname or socket.gethostname()
        ips = _detect_local_ips()
        cert_pem, key_pem, info = _generate_self_signed_cert(hostname, ips)

        row = ApplianceCertificate(
            name=_unique_name("self-signed-default"),
            source=CERT_SOURCE_SELF_SIGNED,
            cert_pem=cert_pem,
            key_encrypted=encrypt_str(key_pem),
            is_active=True,
            activated_at=_dt.datetime.now(_dt.timezone.utc),
            subject_cn=info["subject_cn"],
            sans_json=info["sans"],
            issuer_cn=info["issuer_cn"],
            fingerprint_sha256=info["fingerprint_sha256"],
            valid_from=info["valid_from"],
            valid_to=info["valid_to"],
            notes=(
                "Auto-generated on first boot. Replace with an uploaded "
                "cert, a CSR-signed cert, or a Let's Encrypt cert via the "
                "Appliance → Web UI Certificate tab."
            ),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        logger.info(
            "appliance_self_signed_cert_generated",
            cert_id=str(row.id),
            hostname=hostname,
            sans=info["sans"],
            fingerprint=info["fingerprint_sha256"][:23] + "…",
        )

        deploy_and_reload(cert_pem, key_pem, name=row.name)


# ── Helpers ─────────────────────────────────────────────────────────


async def _unique_name_exists(name: str) -> bool:
    async with AsyncSessionLocal() as db:
        existing = await db.execute(
            select(ApplianceCertificate).where(ApplianceCertificate.name == name)
        )
        return existing.scalar_one_or_none() is not None


def _unique_name(base: str) -> str:
    """Make a unique name, falling back to ``base-<uuid8>`` on collision.

    Collisions are vanishingly rare — they'd only happen if an operator
    manually pre-created a row named ``self-signed-default`` before
    the appliance came up. Belt-and-braces.
    """
    return f"{base}-{uuid.uuid4().hex[:8]}"


def _detect_local_ips() -> list[str]:
    """Return every non-loopback, non-link-local IP bound to this host.

    Uses ``socket.getaddrinfo`` against the hostname for portability
    (no ``ip``-binary required, no /sys/class/net spelunking that
    breaks in containers without ``--net=host``). Falls back to an
    empty list on any failure — a self-signed cert with only the
    hostname SAN is still useful.
    """
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for family, _, _, _, sockaddr in socket.getaddrinfo(
            hostname, None
        ):
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
                continue
            if ip_str not in ips:
                ips.append(ip_str)
    except (socket.gaierror, OSError):
        return []
    return ips


def _generate_self_signed_cert(
    hostname: str, ips: list[str]
) -> tuple[str, str, dict[str, object]]:
    """Generate a fresh RSA-2048 self-signed cert + key.

    Subject + Issuer are both ``CN=<hostname>`` so OpenSSL renders
    "self-signed" consistently. SANs include every detected IP plus
    the hostname (browsers only check SANs since 2017, ignore CN).
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, hostname)]
    )

    san_entries: list[x509.GeneralName] = [x509.DNSName(hostname)]
    for ip in ips:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass

    now = _dt.datetime.now(_dt.timezone.utc)
    not_after = now + _dt.timedelta(days=_SELF_SIGNED_DAYS)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    info: dict[str, object] = {
        "subject_cn": hostname,
        "issuer_cn": hostname,
        "sans": [hostname, *ips],
        "fingerprint_sha256": _format_fingerprint(
            cert.fingerprint(hashes.SHA256())
        ),
        "valid_from": now,
        "valid_to": not_after,
    }
    return cert_pem, key_pem, info
