"""GitHub release listing (Phase 4c) + upgrade-image asset discovery (#199).

Lists published SpatiumDDI releases from the GitHub API and reports the
currently-installed version, so the Releases tab can show "what's
available" + "what you're running". Read-only.

#199 adds upgrade-image asset discovery on top of the same fetch: for
appliances with internet access the control plane can list the releases
that carry the appliance upgrade-image ``.raw.xz`` asset + its
``.sha256`` sidecar, so the Fleet → Upgrade images picker has something
to import from (vs. the air-gap upload path). The raw GitHub response is
fetched + cached once and both the plain release list + the asset-aware
list build from it.

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

import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

_GITHUB_API = "https://api.github.com"
_CACHE_TTL_SECONDS = 60

# Appliance upgrade-image asset-name convention the release workflow
# attaches to every cut (see .github/workflows/release.yml):
#   spatiumddi-appliance-slot-amd64.raw.xz            (stable)
#   spatiumddi-appliance-slot-<VERSION>-amd64.raw.xz  (versioned)
# plus a matching ``.sha256`` sibling for each. Both names point at the
# same bytes; either works for download.
_UPGRADE_IMAGE_ASSET_RE = re.compile(r"^spatiumddi-appliance-slot.*-amd64\.raw\.xz$")


@dataclass
class Release:
    tag: str
    name: str
    published_at: datetime
    body: str
    html_url: str
    is_prerelease: bool
    is_installed: bool


@dataclass
class UpgradeImageRelease:
    """A release that carries an importable appliance upgrade image."""

    tag: str
    name: str
    published_at: datetime
    body: str
    html_url: str
    is_prerelease: bool
    is_installed: bool
    image_asset_url: str
    checksum_asset_url: str
    size_bytes: int | None


# Cache the raw GitHub release dicts so both ``list_releases`` (plain)
# and ``list_available_upgrade_images`` (asset-aware) build from one
# fetch.
_RAW_CACHE: dict[str, tuple[datetime, list[dict]]] = {}


def get_installed_version() -> str:
    """The version currently running — read from settings.version, which
    the compose env populates from ``SPATIUMDDI_VERSION`` in .env."""
    return settings.version or "dev"


def _parse_published(r: dict, fallback: datetime) -> datetime:
    published = r.get("published_at") or r.get("created_at")
    if not isinstance(published, str):
        return fallback
    try:
        return datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return fallback


async def _fetch_raw_releases() -> list[dict] | None:
    """Fetch + cache the most-recent GitHub releases (raw API dicts).

    Returns ``None`` on any error (rate-limited, network unreachable,
    repo name typo) so callers can distinguish "GitHub unreachable"
    from "reachable but no matching releases".
    """
    now = datetime.now(UTC)
    cached = _RAW_CACHE.get("raw")
    if cached and (now - cached[0]).total_seconds() < _CACHE_TTL_SECONDS:
        return cached[1]

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
        return None
    if not isinstance(data, list):
        return None
    _RAW_CACHE["raw"] = (now, data)
    return data


async def list_releases() -> list[Release]:
    """Fetch + cache the most-recent 25 GitHub releases.

    Empty list on any error (rate-limited, network unreachable,
    repo name typo). The UI shows the empty state cleanly.
    """
    data = await _fetch_raw_releases()
    if data is None:
        return []
    now = datetime.now(UTC)
    installed = get_installed_version()
    releases: list[Release] = []
    for r in data[:25]:
        tag = r.get("tag_name", "")
        if not tag:
            continue
        releases.append(
            Release(
                tag=tag,
                name=r.get("name") or tag,
                published_at=_parse_published(r, now),
                body=r.get("body") or "",
                html_url=r.get("html_url", ""),
                is_prerelease=bool(r.get("prerelease", False)),
                is_installed=(tag == installed),
            )
        )
    return releases


def _pick_upgrade_assets(assets: list[dict]) -> tuple[str, str, int | None] | None:
    """Pick the ``.raw.xz`` upgrade-image asset + its ``.sha256`` sibling.

    Returns ``(image_url, checksum_url, size_bytes)`` or ``None`` when a
    release doesn't carry a matched pair. Prefers the versioned asset
    name (longer) but either points at the same bytes.
    """
    by_name = {(a.get("name") or ""): a for a in assets}
    raws = [n for n in by_name if _UPGRADE_IMAGE_ASSET_RE.match(n)]
    for raw_name in sorted(raws, key=len, reverse=True):
        sha_name = raw_name[: -len(".raw.xz")] + ".sha256"
        if sha_name not in by_name:
            continue
        image_url = by_name[raw_name].get("browser_download_url")
        sha_url = by_name[sha_name].get("browser_download_url")
        if image_url and sha_url:
            return image_url, sha_url, by_name[raw_name].get("size")
    return None


def _to_upgrade_image_release(r: dict, installed: str, now: datetime) -> UpgradeImageRelease | None:
    tag = r.get("tag_name", "")
    if not tag:
        return None
    picked = _pick_upgrade_assets(r.get("assets") or [])
    if picked is None:
        return None
    image_url, sha_url, size = picked
    return UpgradeImageRelease(
        tag=tag,
        name=r.get("name") or tag,
        published_at=_parse_published(r, now),
        body=r.get("body") or "",
        html_url=r.get("html_url", ""),
        is_prerelease=bool(r.get("prerelease", False)),
        is_installed=(tag == installed),
        image_asset_url=image_url,
        checksum_asset_url=sha_url,
        size_bytes=size,
    )


async def list_available_upgrade_images() -> tuple[bool, list[UpgradeImageRelease]]:
    """List releases carrying an importable appliance upgrade image.

    Returns ``(github_reachable, rows)``. ``github_reachable`` is false
    on any fetch error so the picker can default to the air-gap upload
    tab; true + empty means GitHub is reachable but no recent release
    carries the appliance upgrade-image assets.
    """
    data = await _fetch_raw_releases()
    if data is None:
        return (False, [])
    now = datetime.now(UTC)
    installed = get_installed_version()
    out: list[UpgradeImageRelease] = []
    for r in data[:25]:
        row = _to_upgrade_image_release(r, installed, now)
        if row is not None:
            out.append(row)
    return (True, out)


async def get_upgrade_image_assets(tag: str) -> UpgradeImageRelease | None:
    """Resolve a single release tag's upgrade-image asset URLs.

    ``None`` when GitHub is unreachable, the tag isn't found, or the
    release doesn't carry a matched ``.raw.xz`` + ``.sha256`` pair.
    """
    data = await _fetch_raw_releases()
    if data is None:
        return None
    now = datetime.now(UTC)
    installed = get_installed_version()
    for r in data:
        if r.get("tag_name") == tag:
            return _to_upgrade_image_release(r, installed, now)
    return None
