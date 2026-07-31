"""Startup cert bootstrap adopts the cert firstboot already deployed (#767).

A fresh appliance had TWO self-signed cert generators racing: firstboot
mints one into the ``spatium-appliance-tls`` Secret before the chart
bootstraps (so the frontend nginx serves it from its first request), and
then the api's startup bootstrap minted a rival one, PATCHed the Secret,
and rolled the frontend. The operator saw two browser trust prompts and
a frontend restart mid-boot.

These cover the adopt path: when the Secret already holds a usable cert
covering every desired SAN, it becomes the active row and NOTHING is
deployed. Everything that isn't adoptable still falls through to the
original generate-and-deploy behaviour.
"""

from __future__ import annotations

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appliance import CERT_SOURCE_SELF_SIGNED, ApplianceCertificate
from app.services.appliance import bootstrap
from app.services.appliance.bootstrap import (
    _generate_self_signed_cert,
    ensure_self_signed_cert,
)
from app.services.appliance.tls import _format_fingerprint


def _firstboot_cert() -> tuple[str, str]:
    """A stand-in for what ``spatiumddi-firstboot`` writes into the Secret.

    Same shape: the appliance hostname + its host IP, plus the
    ``localhost`` / ``127.0.0.1`` SANs firstboot always adds — i.e. a
    strict superset of what the api would ask for.
    """
    cert_pem, key_pem, _info = _generate_self_signed_cert(
        "ddi1", ["192.168.0.199"], ["localhost", "127.0.0.1"]
    )
    return cert_pem, key_pem


def _appliance_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.settings, "appliance_mode", True)
    monkeypatch.setattr(bootstrap.settings, "appliance_hostname", "ddi1")
    monkeypatch.setattr(bootstrap.settings, "appliance_host_ips", "192.168.0.199")
    monkeypatch.setattr(bootstrap.settings, "appliance_extra_cert_sans", "")


class _DeploySpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, cert_pem: str, key_pem: str, *, name: str = "") -> bool:
        self.calls.append((cert_pem, key_pem))
        return True


async def _active_row(db: AsyncSession) -> ApplianceCertificate:
    return (
        await db.execute(
            select(ApplianceCertificate).where(ApplianceCertificate.is_active.is_(True))
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_adopts_deployed_cert_without_redeploying(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Secret's cert becomes the active row, and nothing is deployed."""
    _appliance_settings(monkeypatch)
    cert_pem, key_pem = _firstboot_cert()
    spy = _DeploySpy()
    monkeypatch.setattr(bootstrap, "read_deployed_cert", lambda: (cert_pem, key_pem))
    monkeypatch.setattr(bootstrap, "deploy_and_reload", spy)

    await ensure_self_signed_cert()

    row = await _active_row(db_session)
    assert row.source == CERT_SOURCE_SELF_SIGNED
    # The adopted PEM is byte-identical to what nginx is already serving —
    # this is the whole point: one cert identity across the boot.
    assert row.cert_pem == cert_pem
    assert row.name.startswith("self-signed-firstboot-")
    # ...and the frontend was NOT rolled.
    assert spy.calls == []


@pytest.mark.asyncio
async def test_adopted_fingerprint_matches_the_served_cert(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metadata is parsed from the real cert, not assumed."""
    _appliance_settings(monkeypatch)
    cert_pem, key_pem = _firstboot_cert()
    monkeypatch.setattr(bootstrap, "read_deployed_cert", lambda: (cert_pem, key_pem))
    monkeypatch.setattr(bootstrap, "deploy_and_reload", _DeploySpy())

    await ensure_self_signed_cert()

    row = await _active_row(db_session)
    expected = _format_fingerprint(
        x509.load_pem_x509_certificate(cert_pem.encode()).fingerprint(hashes.SHA256())
    )
    assert row.fingerprint_sha256 == expected
    assert row.subject_cn == "ddi1"
    # firstboot's extra SANs are recorded as-is, not trimmed to what the
    # api happened to ask for.
    sans = set(row.sans_json or [])
    assert {"ddi1", "192.168.0.199", "localhost", "127.0.0.1"} <= sans


@pytest.mark.asyncio
async def test_generates_when_deployed_cert_misses_a_san(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Secret cert that doesn't cover the VIP is not adoptable."""
    _appliance_settings(monkeypatch)
    monkeypatch.setattr(bootstrap.settings, "appliance_extra_cert_sans", "192.168.0.240")
    cert_pem, key_pem = _firstboot_cert()  # no VIP in its SANs
    spy = _DeploySpy()
    monkeypatch.setattr(bootstrap, "read_deployed_cert", lambda: (cert_pem, key_pem))
    monkeypatch.setattr(bootstrap, "deploy_and_reload", spy)

    await ensure_self_signed_cert()

    row = await _active_row(db_session)
    assert row.cert_pem != cert_pem
    assert "192.168.0.240" in set(row.sans_json or [])
    # Fresh cert → it MUST be pushed to the Secret + the frontend rolled.
    assert len(spy.calls) == 1


@pytest.mark.asyncio
async def test_generates_when_key_does_not_match_cert(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mismatched cert/key pair in the Secret is never adopted."""
    _appliance_settings(monkeypatch)
    cert_pem, _key_pem = _firstboot_cert()
    _other_cert, other_key = _firstboot_cert()  # different keypair
    spy = _DeploySpy()
    monkeypatch.setattr(bootstrap, "read_deployed_cert", lambda: (cert_pem, other_key))
    monkeypatch.setattr(bootstrap, "deploy_and_reload", spy)

    await ensure_self_signed_cert()

    row = await _active_row(db_session)
    assert row.cert_pem != cert_pem
    assert len(spy.calls) == 1


@pytest.mark.asyncio
async def test_generates_when_secret_is_unreadable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not on k8s / Secret absent → the original generate path."""
    _appliance_settings(monkeypatch)
    spy = _DeploySpy()
    monkeypatch.setattr(bootstrap, "read_deployed_cert", lambda: None)
    monkeypatch.setattr(bootstrap, "deploy_and_reload", spy)

    await ensure_self_signed_cert()

    row = await _active_row(db_session)
    assert row.name.startswith("self-signed-default-")
    assert len(spy.calls) == 1


@pytest.mark.asyncio
async def test_existing_active_row_still_wins_over_the_secret(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adoption is Path B only — an existing active row is still re-deployed.

    Guards the upgrade case: an appliance that already has an operator's
    cert in the DB must not adopt whatever happens to be in the Secret.
    """
    _appliance_settings(monkeypatch)
    existing_pem, existing_key = _firstboot_cert()
    from app.core.crypto import encrypt_str

    db_session.add(
        ApplianceCertificate(
            name="operator-cert",
            source="uploaded",
            cert_pem=existing_pem,
            key_encrypted=encrypt_str(existing_key),
            is_active=True,
            subject_cn="ddi1",
            sans_json=["ddi1", "192.168.0.199"],
        )
    )
    await db_session.commit()

    secret_pem, secret_key = _firstboot_cert()
    spy = _DeploySpy()
    monkeypatch.setattr(bootstrap, "read_deployed_cert", lambda: (secret_pem, secret_key))
    monkeypatch.setattr(bootstrap, "deploy_and_reload", spy)

    await ensure_self_signed_cert()

    row = await _active_row(db_session)
    assert row.name == "operator-cert"
    # Path A re-deploys the DB's cert over whatever the Secret held.
    assert spy.calls == [(existing_pem, existing_key)]
