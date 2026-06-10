"""Unit tests for the appliance SSH renderer + bundle (issue #157).

Covers:

* render determinism (same settings → same bytes)
* public-key format validation (accept ed25519 / rsa / ecdsa; reject
  garbage / control chars / embedded newlines / type-mismatch)
* lockout-safety helper (password-auth-off + zero valid keys → unsafe)
* privileged-port handling is on the router side; here we just render
* ssh_bundle stable shape + config_hash stability
"""

from __future__ import annotations

import base64

from app.models.settings import PlatformSettings
from app.services.appliance.ssh import (
    is_valid_public_key,
    key_fingerprint,
    render_authorized_keys,
    render_sshd_config,
    ssh_bundle,
    validate_lockout_safe,
)


# A real (structurally valid) ed25519 public key blob: the OpenSSH wire
# format is len-prefixed "ssh-ed25519" + 32-byte key. Build it so the
# embedded-type check in is_valid_public_key passes.
def _make_ed25519_key() -> str:
    name = b"ssh-ed25519"
    key = b"\x00" * 32
    blob = len(name).to_bytes(4, "big") + name + len(key).to_bytes(4, "big") + key
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii") + " alice@host"


def _make_rsa_key() -> str:
    name = b"ssh-rsa"
    # exponent + modulus — content is irrelevant to the structural check.
    e = b"\x01\x00\x01"
    n = b"\xab" * 256
    blob = (
        len(name).to_bytes(4, "big")
        + name
        + len(e).to_bytes(4, "big")
        + e
        + len(n).to_bytes(4, "big")
        + n
    )
    return "ssh-rsa " + base64.b64encode(blob).decode("ascii")


_ED = _make_ed25519_key()
_RSA = _make_rsa_key()


def test_accepts_valid_ed25519_and_rsa() -> None:
    assert is_valid_public_key(_ED)
    assert is_valid_public_key(_RSA)


def test_rejects_garbage_and_control_chars() -> None:
    assert not is_valid_public_key("")
    assert not is_valid_public_key("not a key")
    assert not is_valid_public_key("ssh-ed25519")  # no blob
    assert not is_valid_public_key("ssh-ed25519 notbase64!!!")
    # Embedded newline (would inject a second authorized_keys line).
    assert not is_valid_public_key(_ED + "\nssh-rsa AAAA")
    # Embedded control char.
    assert not is_valid_public_key("ssh-ed25519 \x07 AAAA")
    # Type / embedded-name mismatch: declare ssh-rsa but ship ed25519 blob.
    parts = _ED.split()
    mismatched = "ssh-rsa " + parts[1]
    assert not is_valid_public_key(mismatched)
    # Disallowed type.
    assert not is_valid_public_key("ssh-banana AAAAB3")


def test_comment_with_control_char_rejected() -> None:
    parts = _ED.split()
    bad = f"{parts[0]} {parts[1]} bad\x01comment"
    assert not is_valid_public_key(bad)


def test_fingerprint_stable_and_none_on_garbage() -> None:
    fp = key_fingerprint(_ED)
    assert fp is not None and fp.startswith("SHA256:")
    assert key_fingerprint(_ED) == fp  # deterministic
    assert key_fingerprint("garbage") is None


def test_validate_lockout_safe() -> None:
    # Password auth on → always safe regardless of keys.
    assert validate_lockout_safe([], True)
    # Password auth off + zero keys → unsafe.
    assert not validate_lockout_safe([], False)
    # Password auth off + a garbage "key" → still unsafe (must be valid).
    assert not validate_lockout_safe([{"public_key": "garbage"}], False)
    # Password auth off + a valid key → safe.
    assert validate_lockout_safe([{"public_key": _ED}], False)


def test_render_authorized_keys_deterministic() -> None:
    s = PlatformSettings(
        id=1,
        ssh_authorized_keys=[
            {"name": "alice", "public_key": _ED, "comment": "laptop"},
            {"name": "", "public_key": _RSA, "comment": ""},
            # A garbage entry must be dropped, not rendered.
            {"name": "x", "public_key": "junk", "comment": ""},
        ],
    )
    body1 = render_authorized_keys(s)
    body2 = render_authorized_keys(s)
    assert body1 == body2
    assert _ED in body1
    assert _RSA in body1
    assert "junk" not in body1
    # Name/comment fold into a single managed tag.
    assert "# spatium:alice / laptop" in body1


def test_render_sshd_config_directives() -> None:
    s = PlatformSettings(
        id=1,
        ssh_port=2222,
        ssh_password_auth_enabled=False,
        ssh_allow_root_login=True,
        ssh_authorized_keys=[{"public_key": _ED}],
    )
    conf = render_sshd_config(s)
    assert "Port 2222" in conf
    assert "PasswordAuthentication no" in conf
    assert "PermitRootLogin yes" in conf
    # Defaults.
    d = render_sshd_config(PlatformSettings(id=1))
    assert "Port 22" in d
    assert "PasswordAuthentication yes" in d
    assert "PermitRootLogin no" in d


def test_ssh_bundle_default_state_disabled_shape() -> None:
    # Pristine default = password auth on, no keys, port 22, no root, no
    # source scope → "disabled" (managed-off) with empty hash.
    s = PlatformSettings(id=1)
    block = ssh_bundle(s)
    assert block["enabled"] is False
    assert block["config_hash"] == ""
    assert block["password_auth"] is True
    assert block["key_count"] == 0
    assert block["ssh_port"] == 22
    assert block["allowed_source_networks"] == []
    # Stable key set even when disabled.
    for key in (
        "enabled",
        "config_hash",
        "authorized_keys",
        "sshd_conf",
        "ssh_port",
        "allowed_source_networks",
        "password_auth",
        "key_count",
    ):
        assert key in block


def test_ssh_bundle_enabled_when_keys_present_and_hash_stable() -> None:
    s = PlatformSettings(
        id=1,
        ssh_authorized_keys=[{"name": "a", "public_key": _ED, "comment": ""}],
    )
    block = ssh_bundle(s)
    assert block["enabled"] is True
    assert block["config_hash"]  # non-empty
    assert block["key_count"] == 1
    assert _ED in block["authorized_keys"]
    # Deterministic hash.
    assert ssh_bundle(s)["config_hash"] == block["config_hash"]
    # Changing a key flips the hash.
    s.ssh_authorized_keys = [{"name": "a", "public_key": _RSA, "comment": ""}]
    assert ssh_bundle(s)["config_hash"] != block["config_hash"]


def test_ssh_bundle_enabled_when_non_default_port() -> None:
    # No keys, password auth on, but a non-default port = managed (enabled).
    s = PlatformSettings(id=1, ssh_port=2222)
    block = ssh_bundle(s)
    assert block["enabled"] is True
    assert block["ssh_port"] == 2222
    assert block["key_count"] == 0
