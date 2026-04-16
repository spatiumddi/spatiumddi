"""On-disk cache for the DHCP agent (non-negotiable #5).

Layout under /var/lib/spatium-dhcp-agent/:
    agent-id                        # UUID, 0600
    agent_token.jwt                 # current JWT, 0600
    config/current.json             # last-known-good ConfigBundle
    config/current.etag
    config/previous.json
    rendered/kea-dhcp4.json         # rendered Kea config (last applied)
    leases/pending.jsonl            # lease events not yet posted to the control plane
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

# Schema version for the cached ConfigBundle — see DHCP.md §6.
CACHE_SCHEMA_VERSION = 1


def ensure_layout(state_dir: Path) -> None:
    for sub in ("config", "rendered", "leases"):
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
        # Volume-mount owner may differ; best-effort only.
        # Volume-mount owner may differ; best-effort only.
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


def save_config(state_dir: Path, bundle: dict[str, Any], etag: str) -> None:
    """Atomic replace — write new, move current→previous, rename tmp→current."""
    cfg_dir = state_dir / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    current = cfg_dir / "current.json"
    previous = cfg_dir / "previous.json"
    tmp = cfg_dir / "current.json.tmp"
    # Stamp the schema version so older/newer agents can detect incompatible caches.
    stamped = dict(bundle)
    stamped.setdefault("_schema_version", CACHE_SCHEMA_VERSION)
    tmp.write_text(json.dumps(stamped, indent=2, sort_keys=True))
    if current.exists():
        current.replace(previous)
    tmp.replace(current)
    (cfg_dir / "current.etag").write_text(etag)


def save_rendered_kea(state_dir: Path, rendered: dict[str, Any]) -> Path:
    """Write the rendered Kea dhcp4 JSON under rendered/ for audit/debug."""
    path = state_dir / "rendered" / "kea-dhcp4.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rendered, indent=2, sort_keys=True))
    tmp.replace(path)
    return path
