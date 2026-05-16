"""Tests for the Ed25519 identity store."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from spatium_supervisor.identity import (
    APPLIANCE_ID_FILENAME,
    FINGERPRINT_FILENAME,
    PRIVATE_KEY_FILENAME,
    PUBLIC_KEY_FILENAME,
    clear_appliance_id,
    load_appliance_id,
    load_or_generate,
    save_appliance_id,
)


def test_first_boot_generates_keypair(tmp_path: Path) -> None:
    identity, generated = load_or_generate(tmp_path)
    assert generated is True
    assert isinstance(identity.private_key, Ed25519PrivateKey)
    assert identity.public_key_der  # non-empty
    assert identity.fingerprint == hashlib.sha256(identity.public_key_der).hexdigest()

    # Files persisted with expected names + modes.
    identity_dir = tmp_path / "identity"
    assert (identity_dir / PRIVATE_KEY_FILENAME).exists()
    assert (identity_dir / PUBLIC_KEY_FILENAME).exists()
    assert (identity_dir / FINGERPRINT_FILENAME).exists()
    assert (identity_dir / PRIVATE_KEY_FILENAME).stat().st_mode & 0o777 == 0o600


def test_second_boot_loads_existing_keypair(tmp_path: Path) -> None:
    first, first_generated = load_or_generate(tmp_path)
    second, second_generated = load_or_generate(tmp_path)

    assert first_generated is True
    assert second_generated is False
    # Same on-disk material → same fingerprint + same DER bytes.
    assert second.fingerprint == first.fingerprint
    assert second.public_key_der == first.public_key_der


def test_load_rejects_non_ed25519_private_key(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()

    rsa_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = rsa_priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    (identity_dir / PRIVATE_KEY_FILENAME).write_bytes(pem)

    with pytest.raises(RuntimeError, match="not an Ed25519"):
        load_or_generate(tmp_path)


def test_save_load_clear_appliance_id_round_trip(tmp_path: Path) -> None:
    load_or_generate(tmp_path)  # creates identity/
    assert load_appliance_id(tmp_path) is None

    appliance_id = uuid.uuid4()
    save_appliance_id(tmp_path, appliance_id)
    assert load_appliance_id(tmp_path) == appliance_id

    clear_appliance_id(tmp_path)
    assert load_appliance_id(tmp_path) is None


def test_load_appliance_id_returns_none_on_corrupt_file(tmp_path: Path) -> None:
    load_or_generate(tmp_path)
    (tmp_path / "identity" / APPLIANCE_ID_FILENAME).write_text("not-a-uuid\n")
    assert load_appliance_id(tmp_path) is None
