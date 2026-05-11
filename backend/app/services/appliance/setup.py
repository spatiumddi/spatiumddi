"""Web first-boot setup wizard state (Phase 4g).

A single boolean per appliance: "has the operator walked through the
first-boot wizard yet?". File-backed (matches the maintenance flag
pattern) so it survives container recreates + factory resets cleanly.

The frontend reads ``/api/v1/appliance/setup`` on every authenticated
load; when ``complete`` is false AND the user is superadmin AND the
deployment is appliance-mode, the UI shows the wizard banner / auto-
redirects on first /appliance hit.

Marking complete is operator-triggered ("Finish setup") rather than
auto-derived from "password changed + cert uploaded" — operators may
deliberately skip optional steps and we should respect that.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

# Same bind-mounted RW dir as the release trigger + maintenance flag.
# Owned by uid 1000 (firstboot chowns) so the api's app user can write.
_FLAG_FILE = Path("/var/lib/spatiumddi-host/release-state/setup-complete.flag")


def is_setup_complete() -> bool:
    return _FLAG_FILE.exists()


def mark_setup_complete(by_username: str) -> str:
    """Create the flag file. Returns the ISO timestamp written."""
    if not settings.appliance_mode:
        raise RuntimeError(
            "setup wizard is only supported on the SpatiumDDI OS appliance"
        )
    _FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).isoformat()
    _FLAG_FILE.write_text(
        f"completed_at={stamp}\ncompleted_by={by_username}\n",
        encoding="utf-8",
    )
    logger.info("appliance_setup_complete", by=by_username)
    return stamp


def get_setup_state() -> dict[str, object]:
    """Return the wizard state shape consumed by the UI."""
    complete = is_setup_complete()
    completed_at: str | None = None
    completed_by: str | None = None
    if complete:
        try:
            for line in _FLAG_FILE.read_text(encoding="utf-8").splitlines():
                if line.startswith("completed_at="):
                    completed_at = line.split("=", 1)[1]
                elif line.startswith("completed_by="):
                    completed_by = line.split("=", 1)[1]
        except OSError:
            pass
    return {
        "complete": complete,
        "completed_at": completed_at,
        "completed_by": completed_by,
    }
