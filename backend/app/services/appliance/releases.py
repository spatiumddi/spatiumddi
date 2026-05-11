"""GitHub release listing + scheduled-apply trigger (Phase 4c).

The api container can't run ``docker-compose pull && up -d`` directly
on the host — and even if it could, it can't gracefully recreate
itself mid-request. Two-process design:

* This module: lists releases from the GitHub API + writes a trigger
  file (``/var/lib/spatiumddi/release-pending``) with the requested
  tag. The endpoint returns 202 Accepted and exits the request.
* Host-side ``/usr/local/bin/spatiumddi-update`` runs as a systemd
  oneshot driven by a Path unit watching the trigger file. It edits
  ``SPATIUMDDI_VERSION`` in ``/etc/spatiumddi/.env``, runs
  ``docker-compose pull && docker-compose up -d``, then renames the
  trigger so it doesn't re-fire. Log goes to
  ``/var/log/spatiumddi/update.log`` for the UI to tail.

Cache: GitHub release listings are public + rate-limited (60 req/h
unauthenticated). 60-second in-memory cache keeps the page snappy
without burning the budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

_GITHUB_API = "https://api.github.com"
# Paths inside the api container — bind-mounted to the host's
# /var/lib/spatiumddi/release-state/ and /var/log/spatiumddi/ via
# the appliance docker-compose. The host's spatiumddi-update.path
# unit watches the same trigger file on its side.
_TRIGGER_FILE = Path("/var/lib/spatiumddi-host/release-state/release-pending")
_UPDATE_LOG = Path("/var/log/spatiumddi-host/update.log")
_CACHE_TTL_SECONDS = 60


@dataclass
class Release:
    tag: str
    name: str
    published_at: datetime
    body: str
    html_url: str
    is_prerelease: bool
    is_installed: bool


_CACHE: dict[str, tuple[datetime, list[Release]]] = {}


def get_installed_version() -> str:
    """The version currently running — read from settings.version, which
    the compose env populates from ``SPATIUMDDI_VERSION`` in .env."""
    return settings.version or "dev"


async def list_releases() -> list[Release]:
    """Fetch + cache the most-recent 25 GitHub releases.

    Empty list on any error (rate-limited, network unreachable,
    repo name typo). The UI shows the empty state cleanly.
    """
    now = datetime.now(timezone.utc)
    cached = _CACHE.get("releases")
    if cached and (now - cached[0]).total_seconds() < _CACHE_TTL_SECONDS:
        return cached[1]

    installed = get_installed_version()
    url = f"{_GITHUB_API}/repos/{settings.github_repo}/releases"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("appliance_releases_fetch_failed", error=str(exc))
        return []

    releases: list[Release] = []
    for r in data[:25]:
        tag = r.get("tag_name", "")
        if not tag:
            continue
        published = r.get("published_at") or r.get("created_at")
        try:
            published_at = datetime.fromisoformat(
                published.replace("Z", "+00:00")
            )
        except (AttributeError, ValueError):
            published_at = now
        releases.append(
            Release(
                tag=tag,
                name=r.get("name") or tag,
                published_at=published_at,
                body=r.get("body") or "",
                html_url=r.get("html_url", ""),
                is_prerelease=bool(r.get("prerelease", False)),
                is_installed=(tag == installed),
            )
        )
    _CACHE["releases"] = (now, releases)
    return releases


def schedule_apply(tag: str) -> None:
    """Drop the trigger file the host-side updater watches.

    Atomic via ``.new`` sibling + replace so the Path unit doesn't
    fire on a half-written file. Raises if the trigger dir isn't
    writable (e.g. dev environment without the appliance volume).
    """
    if not settings.appliance_mode:
        raise RuntimeError(
            "release apply is only supported on the SpatiumDDI OS appliance"
        )
    _TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _TRIGGER_FILE.with_suffix(".new")
    tmp.write_text(f"{tag.strip()}\n", encoding="utf-8")
    tmp.replace(_TRIGGER_FILE)
    logger.info("appliance_release_scheduled", tag=tag)


def get_update_log_tail(lines: int = 80) -> str:
    """Return the last ``lines`` lines of /var/log/spatiumddi/update.log.

    The UI polls this while an apply is in flight to show progress.
    Empty string when the file doesn't exist (no apply has ever run).
    """
    if not _UPDATE_LOG.exists():
        return ""
    try:
        text = _UPDATE_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def is_apply_in_flight() -> bool:
    """True when a trigger file is present (host hasn't processed it yet).

    The host's update runner renames the file to ``.done`` once it's
    finished, so its presence is the "still working" signal.
    """
    return _TRIGGER_FILE.exists()
