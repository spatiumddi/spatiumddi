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
import uuid

import structlog
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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

    # #272 HA — the cert identity MUST be stable across api replicas.
    # In a multi-node control plane the api Deployment runs N pods (one
    # per control-plane node), all sharing one cluster-wide
    # spatium-appliance-tls Secret + one appliance_certificate row. If
    # we derived the CN/SANs from the running POD (socket.gethostname()
    # = the pod name, or the pod's own IP), every replica would see "the
    # active cert doesn't cover MY name/IP" and regenerate — three pods
    # → three certs, each overwriting the Secret (exactly the churn an
    # operator reported). So use ONLY the host-stable identity that
    # firstboot threads into the Deployment env (identical for every
    # replica): APPLIANCE_HOSTNAME + APPLIANCE_HOST_IPS + the
    # control-plane VIP. No per-pod fallbacks. The result: the first
    # node's cert is generated once and every replica just re-deploys
    # it; the operator adds more SANs (other node IPs) via a CSR.
    hostname = settings.appliance_hostname or "spatiumddi-appliance"
    ips = _parse_host_ips(settings.appliance_host_ips)
    # #272 Phase 6 — extra SANs the cert must also cover (the
    # control-plane VIP etc), threaded in from the chart on promote.
    extra_sans = _parse_extra_sans(settings.appliance_extra_cert_sans)
    desired_sans = _desired_sans(hostname, ips, extra_sans)

    async with AsyncSessionLocal() as db:
        active_row = (
            await db.execute(
                select(ApplianceCertificate).where(ApplianceCertificate.is_active.is_(True))
            )
        ).scalar_one_or_none()

        if active_row is not None and active_row.cert_pem:
            covered = _sans_cover(active_row.sans_json, desired_sans)
            # #272 Phase 6 — regenerate ONLY a self-signed cert that no
            # longer covers every desired SAN (e.g. a control-plane VIP
            # was added on promote). Operator-uploaded / CSR-signed certs
            # are left untouched — the operator owns those SANs; we just
            # warn that the VIP isn't covered.
            if active_row.source == CERT_SOURCE_SELF_SIGNED and not covered:
                logger.info(
                    "appliance_self_signed_cert_regenerating",
                    cert_id=str(active_row.id),
                    old_sans=active_row.sans_json,
                    new_sans=desired_sans,
                )
                active_row.is_active = False
                await _generate_activate_deploy(db, hostname, ips, extra_sans)
                return

            # Path A — replay the active cert to disk in case the volume
            # was reset. Decrypts the key inside this function only; the
            # plaintext never leaves the deployer's scope.
            try:
                key_pem = decrypt_str(active_row.key_encrypted)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "appliance_active_cert_decrypt_failed",
                    cert_id=str(active_row.id),
                    error=str(exc),
                )
                return
            if not covered:
                logger.warning(
                    "appliance_active_cert_missing_sans",
                    cert_id=str(active_row.id),
                    source=active_row.source,
                    missing=[s for s in desired_sans if not _sans_cover(active_row.sans_json, [s])],
                )
            deploy_and_reload(active_row.cert_pem, key_pem, name=active_row.name)
            logger.info(
                "appliance_active_cert_redeployed",
                cert_id=str(active_row.id),
                name=active_row.name,
                source=active_row.source,
            )
            return

        # Path B — no active row: generate a self-signed default.
        await _generate_activate_deploy(db, hostname, ips, extra_sans)


async def _generate_activate_deploy(
    db: AsyncSession, hostname: str, ips: list[str], extra_sans: list[str]
) -> None:
    """Generate a self-signed cert, persist it as the active row, deploy.

    Shared by Path B (no active cert) and the Phase 6 regenerate branch
    (a self-signed cert that no longer covers every desired SAN). The
    caller is responsible for deactivating any prior active row before
    calling this — there is only ever one ``is_active=True`` row.
    """
    cert_pem, key_pem, info = _generate_self_signed_cert(hostname, ips, extra_sans)

    row = ApplianceCertificate(
        name=_unique_name("self-signed-default"),
        source=CERT_SOURCE_SELF_SIGNED,
        cert_pem=cert_pem,
        key_encrypted=encrypt_str(key_pem),
        is_active=True,
        activated_at=_dt.datetime.now(_dt.UTC),
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


# #272 Phase 7c — advisory-lock key so only one api replica regenerates
# the shared self-signed cert at a time. The api Deployment runs N pods
# (one per control-plane node), all running the periodic reconcile loop
# below against one shared spatium-appliance-tls Secret + one active
# appliance_certificate row. A Postgres transaction-level advisory lock
# serialises them; losers no-op. Arbitrary stable bigint.
_CERT_RECONCILE_LOCK_KEY = 0x5350433727  # "SPC7c"-ish


async def reconcile_cluster_cert_sans() -> dict[str, object]:
    """Grow the self-signed Web UI cert's SANs to cover every settled
    control-plane member (hostname + node IP) plus the control-plane VIP
    (#272 Phase 7c).

    Only ever touches a **self-signed** active cert — an operator-uploaded
    or CSR-signed cert is left alone (the operator owns those SANs). SAN
    coverage only GROWS: a demoted node's SAN stays in the cert (harmless),
    so a demote never rolls the frontend. Idempotent + coverage-gated, so
    it's a cheap no-op once converged; on growth it regenerates the cert,
    updates the ``spatium-appliance-tls`` Secret, and rolls the frontend
    pods (via ``_generate_activate_deploy`` → ``deploy_and_reload``).

    Runs from a periodic api-side loop (main.lifespan); a per-transaction
    advisory lock keeps the api replicas from regenerating concurrently.
    """
    if not settings.appliance_mode:
        return {"status": "disabled"}

    from sqlalchemy import text  # noqa: PLC0415

    from app.models.appliance import (  # noqa: PLC0415
        APPLIANCE_STATE_APPROVED,
        CLUSTER_ROLE_MEMBER,
        CLUSTER_ROLE_PRIMARY,
        Appliance,
    )
    from app.models.settings import PlatformSettings  # noqa: PLC0415

    async with AsyncSessionLocal() as db:
        # One regenerator at a time across the api replicas. The lock is
        # held until this transaction commits/rolls back; losers bail.
        got = (
            await db.execute(
                text("select pg_try_advisory_xact_lock(:k)"),
                {"k": _CERT_RECONCILE_LOCK_KEY},
            )
        ).scalar()
        if not got:
            return {"status": "locked"}

        active_row = (
            await db.execute(
                select(ApplianceCertificate).where(ApplianceCertificate.is_active.is_(True))
            )
        ).scalar_one_or_none()
        if active_row is None or not active_row.cert_pem:
            # No active cert yet — startup bootstrap owns the first issue.
            return {"status": "no-active-cert"}
        if active_row.source != CERT_SOURCE_SELF_SIGNED:
            # Operator owns this cert + its SANs — never auto-replace it.
            return {"status": "operator-cert"}

        # Stable seed identity, same inputs ensure_self_signed_cert uses.
        hostname = settings.appliance_hostname or "spatiumddi-appliance"
        ips = _parse_host_ips(settings.appliance_host_ips)
        extra_sans = _parse_extra_sans(settings.appliance_extra_cert_sans)

        # Union in every settled control-plane member: node IP as an IP
        # SAN, hostname as a DNS SAN.
        members = (
            (
                await db.execute(
                    select(Appliance).where(
                        Appliance.state == APPLIANCE_STATE_APPROVED,
                        Appliance.cluster_role.in_((CLUSTER_ROLE_PRIMARY, CLUSTER_ROLE_MEMBER)),
                    )
                )
            )
            .scalars()
            .all()
        )
        for m in members:
            if m.node_ip and m.node_ip not in ips:
                ips.append(m.node_ip)
            if m.hostname and m.hostname not in extra_sans:
                extra_sans.append(m.hostname)

        # Control-plane VIP (platform_settings singleton) as an IP SAN.
        ps = (
            await db.execute(select(PlatformSettings).where(PlatformSettings.id == 1))
        ).scalar_one_or_none()
        if ps and ps.control_plane_vip and ps.control_plane_vip not in extra_sans:
            extra_sans.append(ps.control_plane_vip)

        desired = _desired_sans(hostname, ips, extra_sans)
        if _sans_cover(active_row.sans_json, desired):
            return {"status": "covered", "sans": desired}

        logger.info(
            "appliance_cluster_cert_regenerating",
            old_sans=active_row.sans_json,
            new_sans=desired,
            members=[m.hostname for m in members],
        )
        active_row.is_active = False
        await _generate_activate_deploy(db, hostname, ips, extra_sans)
        return {"status": "regenerated", "sans": desired}


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


def _parse_host_ips(env: str) -> list[str]:
    """Parse the comma-separated APPLIANCE_HOST_IPS env into a clean list.

    Drops loopback / link-local / unspecified entries and anything that
    fails to parse as an IP — firstboot's ``ip -o addr`` output should
    only produce sane values, but be defensive against shell-escaping
    quirks or future format changes.
    """
    out: list[str] = []
    for raw in env.split(","):
        s = raw.strip()
        if not s:
            continue
        try:
            ip = ipaddress.ip_address(s)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            continue
        if s not in out:
            out.append(s)
    return out


def _parse_extra_sans(env: str) -> list[str]:
    """Parse the comma-separated APPLIANCE_EXTRA_CERT_SANS env.

    Accepts both IPs (the common case — a control-plane VIP) and DNS
    names. Drops empties, loopback / link-local / unspecified IPs, and
    de-dupes while preserving order. DNS names pass through verbatim.
    """
    out: list[str] = []
    for raw in env.split(","):
        s = raw.strip()
        if not s:
            continue
        try:
            ip = ipaddress.ip_address(s)
            if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
                continue
        except ValueError:
            pass  # not an IP → treat as a DNS name
        if s not in out:
            out.append(s)
    return out


def _desired_sans(hostname: str, ips: list[str], extra_sans: list[str]) -> list[str]:
    """Ordered, de-duped SAN list the active cert ought to cover."""
    out: list[str] = []
    for s in [hostname, *ips, *extra_sans]:
        if s and s not in out:
            out.append(s)
    return out


def _sans_cover(existing: object, desired: list[str]) -> bool:
    """True if every ``desired`` SAN is present in ``existing``.

    ``existing`` is the persisted ``sans_json`` (a list of strings, or
    None for legacy rows). A None / non-list existing set covers nothing,
    forcing a regenerate when desired SANs are present.
    """
    have = set(existing) if isinstance(existing, list) else set()
    return all(s in have for s in desired)


def _generate_self_signed_cert(
    hostname: str, ips: list[str], extra_sans: list[str] | None = None
) -> tuple[str, str, dict[str, object]]:
    """Generate a fresh RSA-2048 self-signed cert + key.

    Subject + Issuer are both ``CN=<hostname>`` so OpenSSL renders
    "self-signed" consistently. SANs include every detected IP plus
    the hostname plus any ``extra_sans`` (e.g. the control-plane VIP) —
    browsers only check SANs since 2017, ignore CN. Each extra SAN is
    emitted as an IPAddress SAN when it parses as an IP, else a DNSName.
    """
    extra_sans = extra_sans or []
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])

    san_entries: list[x509.GeneralName] = [x509.DNSName(hostname)]
    for ip in ips:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    for entry in extra_sans:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(entry)))
        except ValueError:
            san_entries.append(x509.DNSName(entry))

    now = _dt.datetime.now(_dt.UTC)
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
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
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
        "sans": _desired_sans(hostname, ips, extra_sans),
        "fingerprint_sha256": _format_fingerprint(cert.fingerprint(hashes.SHA256())),
        "valid_from": now,
        "valid_to": not_after,
    }
    return cert_pem, key_pem, info
