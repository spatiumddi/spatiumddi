"""GitHub release listing (Phase 4c).

Lists published SpatiumDDI releases from the GitHub API and reports the
currently-installed version, so the Releases tab can show "what's
available" + "what you're running". Read-only.

#294 — the old one-click "apply" half of this module (a trigger file
watched by a host-side ``spatiumddi-update.path`` unit running
``docker-compose pull && up -d``) was removed: it's a pre-#183
docker-compose-era mechanism that does nothing on the k3s appliance
(no compose stack, no such unit). OS upgrades on the appliance go
through the A/B slot image flow (``services/appliance/slot.py``,
surfaced on the Fleet tab); docker/k8s control planes use the
operator-run ``docker compose`` / ``helm upgrade`` commands shown in
the UI's manual-upgrade modal.

Cache: GitHub release listings are public + rate-limited (60 req/h
unauthenticated). 60-second in-memory cache keeps the page snappy
without burning the budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

_GITHUB_API = "https://api.github.com"
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
    now = datetime.now(UTC)
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
            published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
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
