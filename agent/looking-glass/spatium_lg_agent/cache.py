"""On-disk cache for the LG collector agent (non-negotiable #5).

Layout under /var/lib/spatium-lg-agent/:
    agent-id                       # UUID, 0600
    agent_token.jwt                # current JWT, 0600
    config/current.json            # last-known-good peer-config ConfigBundle
    config/current.etag
    config/previous.json
    rendered/gobgpd.json           # last rendered gobgpd config (for audit/debug)
    .ready                         # stamped after the first successful RIB poll+apply

This is the non-negotiable #5 last-known-good peer-config cache: on
startup the agent preloads and re-applies the cached bundle to gobgpd
BEFORE its first successful poll of the control plane, so already-configured
BGP sessions stay up (and freshly-booted ones can still come up) even if
the control plane is unreachable.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

# Schema version for the cached ConfigBundle.
CACHE_SCHEMA_VERSION = 1


def ensure_layout(state_dir: Path) -> None:
    for sub in ("config", "rendered"):
        (state_dir / sub).mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except PermissionError:
        # Volume-mount owner may differ; best-effort only.
        pass


def load_or_create_agent_id(state_dir: Path) -> str:
    path = state_dir / "agent-id"
    if path.exists():
        return path.read_text().strip()
    aid = str(uuid.uuid4())
    path.write_text(aid)
    try:
        os.chmod(path, 0o600)
    except PermissionError:
        pass
    return aid


def load_token(state_dir: Path) -> str | None:
    path = state_dir / "agent_token.jwt"
    if not path.exists():
        return None
    tok = path.read_text().strip()
    return tok or None


def save_token(state_dir: Path, token: str) -> None:
    path = state_dir / "agent_token.jwt"
    tmp = path.with_suffix(".jwt.tmp")
    tmp.write_text(token)
    try:
        os.chmod(tmp, 0o600)
    except (PermissionError, FileNotFoundError):
        pass
    try:
        tmp.replace(path)
    except FileNotFoundError:
        # Another thread already moved our tmp (concurrent save_token).
        pass


def load_config(state_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    cfg_path = state_dir / "config" / "current.json"
    etag_path = state_dir / "config" / "current.etag"
    if not cfg_path.exists():
        return None, None
    try:
        cfg = json.loads(cfg_path.read_text())
    except json.JSONDecodeError:
        return None, None
    etag = etag_path.read_text().strip() if etag_path.exists() else None
    return cfg, etag


def _chmod_600(path: Path) -> None:
    """0600 a just-written file, best-effort (mirrors save_token / the
    agent-id writer). Both the cached bundle and the rendered gobgpd
    config embed the plaintext TCP-MD5 peer password, so they must not be
    world-readable on disk."""
    try:
        os.chmod(path, 0o600)
    except (PermissionError, FileNotFoundError):
        pass


def save_config(state_dir: Path, bundle: dict[str, Any], etag: str) -> None:
    """Atomic replace — write new, move current→previous, rename tmp→current."""
    cfg_dir = state_dir / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    current = cfg_dir / "current.json"
    previous = cfg_dir / "previous.json"
    tmp = cfg_dir / "current.json.tmp"
    stamped = dict(bundle)
    stamped.setdefault("_schema_version", CACHE_SCHEMA_VERSION)
    tmp.write_text(json.dumps(stamped, indent=2, sort_keys=True))
    # chmod the tmp file BEFORE the rename so the secret is never briefly
    # world-readable at the final path (matches save_token's pattern).
    _chmod_600(tmp)
    if current.exists():
        current.replace(previous)
    tmp.replace(current)
    (cfg_dir / "current.etag").write_text(etag)


def save_rendered_gobgpd(state_dir: Path, rendered: dict[str, Any]) -> Path:
    """Write the rendered gobgpd config under rendered/ for audit/debug."""
    path = state_dir / "rendered" / "gobgpd.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rendered, indent=2, sort_keys=True))
    # The rendered neighbor block carries auth-password in plaintext.
    _chmod_600(tmp)
    tmp.replace(path)
    return path


def touch_ready_marker(state_dir: Path) -> None:
    """Stamp ``<state_dir>/.ready`` after the first successful RIB poll+apply.

    Caller (``rib.py``) MUST only invoke this after a successful GoBGP
    poll + control-plane push — a failed cycle must not flip readiness
    true. Idempotent — touching an already-stamped marker is a no-op.
    """
    marker = state_dir / ".ready"
    marker.touch(exist_ok=True)
