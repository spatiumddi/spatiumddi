"""On-disk cache for the LG collector agent (non-negotiable #5).

Layout under /var/lib/spatium-lg-agent/:
    agent-id                       # UUID, 0600
    agent_token.jwt                # current JWT, 0600
    config/current.json            # last peer-config bundle FETCHED
    config/current.etag
    config/previous.json           # last bundle that APPLIED cleanly (#882)
    config/previous.etag
    config/quarantine.json         # etag of a bundle that failed to apply (#882)
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

import structlog

log = structlog.get_logger(__name__)

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
        # Volume-mount owner may differ; the 0600 hardening is best-effort.
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
        # Best-effort 0600 (volume-mount owner may differ); a racing
        # save_token may already have moved the tmp out from under us.
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
        # Best-effort (volume-mount owner may differ); a concurrent writer
        # may have already replaced the file. See docstring above.
        pass


def save_config(state_dir: Path, bundle: dict[str, Any], etag: str) -> None:
    """Persist the bundle we just FETCHED as ``current`` (atomic rename).

    Deliberately does **not** touch ``previous.json`` (#882). It used to:
    every fetch rotated current→previous before the apply had been
    attempted, so ``previous`` meant "the bundle before this one" rather
    than "the last one that worked". This loop makes that worse than the
    other two agents: it deliberately leaves ``_current_etag`` unadvanced on
    an apply failure in order to retry, so the same bad bundle was re-fetched
    and rotated into ``previous`` on the very next attempt.

    ``previous`` is now written by :func:`commit_config`, after gobgpd has
    been reconfigured successfully.
    """
    cfg_dir = state_dir / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    current = cfg_dir / "current.json"
    tmp = cfg_dir / "current.json.tmp"
    stamped = dict(bundle)
    stamped.setdefault("_schema_version", CACHE_SCHEMA_VERSION)
    tmp.write_text(json.dumps(stamped, indent=2, sort_keys=True))
    # chmod the tmp file BEFORE the rename so the secret is never briefly
    # world-readable at the final path (matches save_token's pattern).
    _chmod_600(tmp)
    tmp.replace(current)
    (cfg_dir / "current.etag").write_text(etag)


def commit_config(state_dir: Path, etag: str) -> None:
    """Promote ``current`` to ``previous`` — the last-known-GOOD bundle (#882).

    Called only once :func:`spatium_lg_agent.gobgp.apply_config` has
    rendered, written and reloaded the bundle without raising.

    Copies rather than renames: ``current`` stays in place because it is
    what the agent re-applies on restart. The copy is 0600'd for the same
    reason ``current`` is — the rendered neighbor block embeds the plaintext
    TCP-MD5 peer password.
    """
    cfg_dir = state_dir / "config"
    current = cfg_dir / "current.json"
    etag_path = cfg_dir / "current.etag"
    if not current.exists() or not etag_path.exists():
        return
    # Guard the precondition rather than assume it: promoting ``current`` is
    # only correct while ``current`` IS the bundle that just applied.
    if etag_path.read_text().strip() != etag:
        log.warning(
            "commit_config_etag_mismatch",
            applied_etag=etag,
            cached_etag=etag_path.read_text().strip(),
        )
        return
    if (cfg_dir / "previous.etag").exists():
        # Already committed — skip the copy. ``commit_config`` is called from
        # more than one point in a successful poll (the structural-apply path
        # and the end-of-poll confirmation), and this bundle can be tens of
        # kilobytes.
        try:
            if (cfg_dir / "previous.etag").read_text().strip() == etag:
                return
        except OSError:
            pass  # unreadable sidecar → fall through and rewrite it
    tmp = cfg_dir / "previous.json.tmp"
    # Copy the bytes rather than re-serialise: ``previous.json`` has to be
    # exactly what ``current.json`` was, because that is what a revert
    # replays. Two independent serialisations could drift.
    tmp.write_text(current.read_text())
    _chmod_600(tmp)
    tmp.replace(cfg_dir / "previous.json")
    etag_tmp = cfg_dir / "previous.etag.tmp"
    etag_tmp.write_text(etag_path.read_text())
    etag_tmp.replace(cfg_dir / "previous.etag")


def load_previous_config(state_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    """The last bundle gobgpd accepted, for the revert path (#882).

    ``(None, None)`` when there is nothing to fall back to. The etag may be
    absent even when the bundle is present: agents upgraded in the field
    carry a ``previous.json`` written by the old rotating
    :func:`save_config`, which never wrote ``previous.etag``.
    """
    cfg_dir = state_dir / "config"
    cfg_path = cfg_dir / "previous.json"
    if not cfg_path.exists():
        return None, None
    try:
        cfg = json.loads(cfg_path.read_text())
    except json.JSONDecodeError:
        return None, None
    etag_path = cfg_dir / "previous.etag"
    etag = etag_path.read_text().strip() if etag_path.exists() else None
    return cfg, etag


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
