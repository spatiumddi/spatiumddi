"""Issue #155 — appliance APT sources / proxy / GPG-key host config.

Third leg of the "Settings → Host services" surface alongside SNMP
(#153) and NTP (#154). Source-of-truth lives on ``platform_settings``
(singleton); this module renders the apt config artifacts from those
columns and folds them into the supervisor ConfigBundle long-poll so
every appliance host (local + remote agents) picks them up the same way
the SNMP / chrony config does.

Unlike SNMP/NTP there is no host *daemon* to restart — the artifacts are
plain files apt reads on the next ``apt-get update`` /
``unattended-upgrades`` run. The host-side runner's job is to
**validate before swapping**: a bad ``sources.list`` (wrong scheme,
unsigned repo, unreachable mirror) bricks ``apt update`` and there's no
GUI to recover from a broken APT config short of SSH.

Management is **opt-in** (``apt_managed`` default False): an appliance
ships with Debian's baked ``/etc/apt/sources.list`` and SpatiumDDI does
not touch it until an operator enables management. The default
``apt_sources`` rows mirror the baked Debian 13 set so enabling is a
one-toggle move that keeps the existing repos.

Artifacts rendered (all written under the ``/etc`` overlay's upper
layer, so they survive A/B slot swaps):

* ``/etc/apt/sources.list.d/spatiumddi.list`` — the managed repos
* ``/etc/apt/apt.conf.d/95spatiumddi-proxy`` — proxy config (if any)
* ``/etc/apt/auth.conf.d/spatiumddi.conf`` — private-mirror creds (0600)
* ``/etc/apt/keyrings/spatiumddi-<key_id>.asc`` — one per GPG key

Secrets (GPG armoured key text, auth passwords) are Fernet-encrypted at
rest and decrypted here only when assembling the bundle — same shape as
the SNMP community in #153.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core.crypto import decrypt_str
from app.models.settings import PlatformSettings

# Where the per-key armoured files land on the host. The ``signed-by``
# option on each sources.list entry points here.
_KEYRING_DIR = "/etc/apt/keyrings"


def _keyring_path(key_id: str) -> str:
    return f"{_KEYRING_DIR}/spatiumddi-{key_id}.asc"


def _as_words(value: Any) -> str:
    """``suites`` / ``components`` may arrive as a list or a pre-joined
    string; normalise to a single space-separated token list."""
    if isinstance(value, list):
        return " ".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def render_sources_list(settings: PlatformSettings) -> str:
    """Render ``/etc/apt/sources.list.d/spatiumddi.list``.

    One classic one-line ``deb`` entry per enabled source. A source that
    names a ``signed_by_key_id`` gets a ``[signed-by=…]`` option pointing
    at the matching keyring file the bundle ships alongside.
    """
    lines = [
        "# Managed by SpatiumDDI — edits will be overwritten on next config push.",
        "# Source of truth: Settings → Appliance → APT in the SpatiumDDI UI.",
        "",
    ]
    for src in settings.apt_sources or []:
        if not isinstance(src, dict) or not src.get("enabled", True):
            continue
        uri = str(src.get("uri") or "").strip()
        suites = _as_words(src.get("suites"))
        components = _as_words(src.get("components"))
        if not uri or not suites:
            continue
        opts = ""
        key_id = str(src.get("signed_by_key_id") or "").strip()
        if key_id:
            opts = f"[signed-by={_keyring_path(key_id)}] "
        name = str(src.get("name") or "").strip()
        if name:
            lines.append(f"# {name}")
        lines.append(f"deb {opts}{uri} {suites} {components}".rstrip())
    return "\n".join(lines) + "\n"


def render_proxy_conf(settings: PlatformSettings) -> str:
    """Render ``/etc/apt/apt.conf.d/95spatiumddi-proxy`` — empty string
    when no proxy is configured (the runner then removes the file)."""
    http = (settings.apt_proxy_http or "").strip()
    https = (settings.apt_proxy_https or "").strip()
    no_proxy = (settings.apt_proxy_no_proxy or "").strip()
    if not http and not https:
        return ""
    lines = [
        "// Managed by SpatiumDDI — Settings → Appliance → APT.",
    ]
    if http:
        lines.append(f'Acquire::http::Proxy "{http}";')
    if https:
        lines.append(f'Acquire::https::Proxy "{https}";')
    # apt bypasses the proxy per-host via an explicit DIRECT override.
    for host in (h.strip() for h in no_proxy.split(",")):
        if not host:
            continue
        if http:
            lines.append(f'Acquire::http::Proxy::{host} "DIRECT";')
        if https:
            lines.append(f'Acquire::https::Proxy::{host} "DIRECT";')
    return "\n".join(lines) + "\n"


def render_auth_conf(settings: PlatformSettings) -> str:
    """Render ``/etc/apt/auth.conf.d/spatiumddi.conf`` (netrc-style) from
    the decrypted private-mirror credentials. Empty when none set; the
    runner writes it 0600."""
    out: list[str] = ["# Managed by SpatiumDDI — Settings → Appliance → APT."]
    any_entry = False
    for entry in settings.apt_auth or []:
        if not isinstance(entry, dict):
            continue
        machine = str(entry.get("machine") or "").strip()
        login = str(entry.get("login") or "").strip()
        password = _decrypt(entry.get("password_enc"))
        if not machine or not login or password is None:
            continue
        out.append(f"machine {machine} login {login} password {password}")
        any_entry = True
    return ("\n".join(out) + "\n") if any_entry else ""


def apt_keyrings(settings: PlatformSettings) -> dict[str, str]:
    """``{key_id: armoured_text}`` for every configured GPG key, decrypted
    from the Fernet-at-rest store. Undecryptable / malformed entries are
    skipped."""
    out: dict[str, str] = {}
    for key in settings.apt_gpg_keys or []:
        if not isinstance(key, dict):
            continue
        key_id = str(key.get("key_id") or "").strip()
        armour = _decrypt(key.get("armoured_text_enc"))
        if key_id and armour:
            out[key_id] = armour
    return out


def _decrypt(token: Any) -> str | None:
    """Decrypt a Fernet string token (the URL-safe-base64 form Fernet
    emits, stored inline in JSONB). None / undecryptable → None."""
    if not token or not isinstance(token, str):
        return None
    try:
        return decrypt_str(token.encode("ascii"))
    except Exception:  # noqa: BLE001 — bad ciphertext = treat as unset
        return None


def apt_bundle(settings: PlatformSettings) -> dict[str, Any]:
    """Build the ``apt_settings`` block the supervisor ConfigBundle ships.

    When ``apt_managed`` is False the block is the disabled shape
    (``enabled=False``, empty hash) — the host runner then removes the
    managed drop-in so Debian's baked ``sources.list`` takes back over.
    The ``config_hash`` covers every rendered artifact so any change
    shifts it and the supervisor re-fires the host trigger.
    """
    if not settings.apt_managed:
        return {
            "enabled": False,
            "config_hash": "",
            "sources_list": "",
            "proxy_conf": "",
            "auth_conf": "",
            "keyrings": {},
            "unattended_upgrades_enabled": bool(settings.apt_unattended_upgrades_enabled),
        }

    sources_list = render_sources_list(settings)
    proxy_conf = render_proxy_conf(settings)
    auth_conf = render_auth_conf(settings)
    keyrings = apt_keyrings(settings)
    unattended = bool(settings.apt_unattended_upgrades_enabled)

    # Change-detection fingerprint. NOT computed over the decrypted secrets
    # (auth_conf cleartext / keyring armour) — instead over the stable
    # encrypted-at-rest tokens, which change in the DB exactly when the
    # operator changes a secret (and never otherwise). This keeps the
    # re-fire trigger correct while keeping cleartext passwords out of the
    # digest. ``usedforsecurity=False`` documents that this is a config
    # fingerprint, not password hashing.
    secret_fp = {
        "auth": sorted(
            [
                str(a.get("machine") or ""),
                str(a.get("login") or ""),
                str(a.get("password_enc") or ""),
            ]
            for a in (settings.apt_auth or [])
            if isinstance(a, dict)
        ),
        "keys": sorted(
            [str(k.get("key_id") or ""), str(k.get("armoured_text_enc") or "")]
            for k in (settings.apt_gpg_keys or [])
            if isinstance(k, dict)
        ),
    }
    canonical = json.dumps(
        {
            "sources_list": sources_list,
            "proxy_conf": proxy_conf,
            "secret_fp": secret_fp,
            "unattended_upgrades_enabled": unattended,
        },
        sort_keys=True,
    )
    config_hash = hashlib.sha256(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()
    return {
        "enabled": True,
        "config_hash": config_hash,
        "sources_list": sources_list,
        "proxy_conf": proxy_conf,
        "auth_conf": auth_conf,
        "keyrings": keyrings,
        "unattended_upgrades_enabled": unattended,
    }
