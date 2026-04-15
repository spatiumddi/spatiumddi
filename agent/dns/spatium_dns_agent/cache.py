"""On-disk cache for the agent (non-negotiable #5).

Layout under /var/lib/spatium-dns-agent/:
    agent-id                       # UUID, 0600
    agent_token.jwt                # current JWT, 0600
    config/current.json
    config/current.etag
    config/previous.json
    rendered/                      # managed by driver
    ops/{inflight,failed}/
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


def ensure_layout(state_dir: Path) -> None:
    for sub in ("config", "rendered", "tsig", "ops/inflight", "ops/failed"):
        (state_dir / sub).mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)


def load_or_create_agent_id(state_dir: Path) -> str:
    path = state_dir / "agent-id"
    if path.exists():
        return path.read_text().strip()
    aid = str(uuid.uuid4())
    path.write_text(aid)
    os.chmod(path, 0o600)
    return aid


def load_token(state_dir: Path) -> str | None:
    path = state_dir / "agent_token.jwt"
    if not path.exists():
        return None
    return path.read_text().strip()


def save_token(state_dir: Path, token: str) -> None:
    path = state_dir / "agent_token.jwt"
    tmp = path.with_suffix(".jwt.tmp")
    tmp.write_text(token)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


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
    tmp.write_text(json.dumps(bundle, indent=2, sort_keys=True))
    if current.exists():
        current.replace(previous)
    tmp.replace(current)
    (cfg_dir / "current.etag").write_text(etag)
