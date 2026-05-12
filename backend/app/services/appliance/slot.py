"""Phase 8b-3 — operator-facing slot upgrade backend.

The api container schedules slot upgrades by writing a trigger file
the host-side ``spatiumddi-slot-upgrade.path`` unit watches; the
runner (``/usr/local/bin/spatiumddi-slot-upgrade``) reads the URL,
invokes ``spatium-upgrade-slot apply`` + ``set-next-boot``, and renames
the trigger to ``.done`` or ``.failed``. Mirrors the existing
``releases.py`` (Phase 4c) shape — same trigger-watcher pattern.

Status detection (``get_slot_status``) reads /proc/cmdline + lsblk +
grubenv directly (no need to shell out to spatium-upgrade-slot status
when the api container can do it itself), and surfaces the trial-boot
state so the UI can warn the operator that a slot has been set as the
next boot but not yet committed.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

# Trigger + log paths inside the api container — bind-mounted to the
# host's /var/lib/spatiumddi/release-state/ and /var/log/spatiumddi/
# via the appliance docker-compose. The host's spatiumddi-slot-upgrade
# .path unit watches the same trigger on its side.
_TRIGGER_FILE = Path("/var/lib/spatiumddi-host/release-state/slot-upgrade-pending")
_STATE_FILE = Path("/var/lib/spatiumddi-host/release-state/slot-upgrade-pending.state")
_UPDATE_LOG = Path("/var/log/spatiumddi-host/slot-upgrade.log")

# grubenv inside the api container — the host's /boot/efi/grub/grubenv
# is bind-mounted at /boot/efi-host so we can read it without docker
# socket gymnastics.
_GRUBENV = Path("/boot/efi-host/grub/grubenv")
_PROC_CMDLINE = Path("/proc/cmdline")


_UUID_RE = re.compile(r"root=UUID=([0-9a-fA-F-]+)")


SlotName = Literal["slot_a", "slot_b"]
UpgradeState = Literal["idle", "in-flight", "done", "failed"]


@dataclass
class SlotStatus:
    """Snapshot of A/B slot state for the UI."""

    appliance_mode: bool
    current_slot: SlotName | None
    durable_default: SlotName | None
    is_trial_boot: bool
    upgrade_state: UpgradeState
    upgrade_state_at: str | None
    log_tail: str


def _read_grubenv() -> dict[str, str]:
    """Parse grubenv into a dict. grubenv is a 1024-byte zero-padded
    file with KEY=VALUE lines; we just read the lines we recognise."""
    out: dict[str, str] = {}
    if not _GRUBENV.exists():
        return out
    try:
        text = _GRUBENV.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.rstrip("\x00").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _current_slot_from_cmdline() -> SlotName | None:
    """Resolve which slot we booted from by matching /proc/cmdline's
    root=UUID= against the partition labels via lsblk."""
    try:
        cmdline = _PROC_CMDLINE.read_text()
    except OSError:
        return None
    m = _UUID_RE.search(cmdline)
    if not m:
        return None
    root_uuid = m.group(1).lower()
    try:
        proc = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,PATH,UUID,PARTLABEL"],
            capture_output=True, text=True, check=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None

    def walk(node: dict) -> str | None:
        if (node.get("uuid") or "").lower() == root_uuid:
            partlabel = (node.get("partlabel") or "").lower()
            if partlabel == "root_a":
                return "slot_a"
            if partlabel == "root_b":
                return "slot_b"
        for child in node.get("children") or []:
            r = walk(child)
            if r:
                return r
        return None

    for top in data.get("blockdevices") or []:
        r = walk(top)
        if r:
            return r
    return None


def _upgrade_state_now() -> tuple[UpgradeState, str | None]:
    """Read the .state sidecar the host-side runner maintains. Returns
    ('idle', None) when no upgrade has run recently. Trigger present
    but no .state yet means the runner hasn't picked it up — counts
    as 'in-flight' from the operator's perspective."""
    if _TRIGGER_FILE.exists() and not _STATE_FILE.exists():
        return "in-flight", None
    if _STATE_FILE.exists():
        try:
            text = _STATE_FILE.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return "idle", None
        parts = text.split(maxsplit=1)
        state = parts[0] if parts else ""
        stamp = parts[1] if len(parts) > 1 else None
        if state in ("in-flight", "done", "failed"):
            return state, stamp  # type: ignore[return-value]
    return "idle", None


def get_slot_status() -> SlotStatus:
    """Compose the full status surface the UI needs.

    Returns appliance_mode=False on non-appliance deploys (the api
    container's mounts won't be present). The frontend uses that to
    hide the OS-upgrade section without an explicit feature flag.
    """
    if not settings.appliance_mode:
        return SlotStatus(
            appliance_mode=False,
            current_slot=None,
            durable_default=None,
            is_trial_boot=False,
            upgrade_state="idle",
            upgrade_state_at=None,
            log_tail="",
        )

    current = _current_slot_from_cmdline()
    grubenv = _read_grubenv()
    durable_raw = grubenv.get("saved_entry") or ""
    durable: SlotName | None = (
        "slot_a" if durable_raw == "slot_a"
        else "slot_b" if durable_raw == "slot_b"
        else None
    )
    is_trial = bool(current and durable and current != durable)
    state, stamp = _upgrade_state_now()

    return SlotStatus(
        appliance_mode=True,
        current_slot=current,
        durable_default=durable,
        is_trial_boot=is_trial,
        upgrade_state=state,
        upgrade_state_at=stamp,
        log_tail=get_update_log_tail(),
    )


def schedule_apply(image_url: str, checksum_url: str | None = None) -> None:
    """Drop the trigger file the host-side slot-upgrade runner watches.

    Two lines: image URL/path on line 1, optional checksum URL/path
    on line 2. Atomic via ``.new`` sibling + replace so the Path unit
    fires exactly once on close-after-write. Raises if appliance_mode
    is off or the trigger dir isn't writable.
    """
    if not settings.appliance_mode:
        raise RuntimeError("slot upgrade is only supported on the SpatiumDDI OS appliance")
    image_url = image_url.strip()
    if not image_url:
        raise ValueError("image_url is required")
    if not (image_url.startswith(("http://", "https://")) or image_url.startswith("/")):
        raise ValueError("image_url must be an http(s) URL or absolute filesystem path")
    if checksum_url:
        checksum_url = checksum_url.strip()
        if not (checksum_url.startswith(("http://", "https://")) or checksum_url.startswith("/")):
            raise ValueError("checksum_url must be an http(s) URL or absolute filesystem path")

    body = image_url + "\n"
    if checksum_url:
        body += checksum_url + "\n"

    _TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Clear any prior .state so the UI reads "in-flight" until the runner
    # writes its own state.
    if _STATE_FILE.exists():
        try:
            _STATE_FILE.unlink()
        except OSError as exc:
            # Best-effort cleanup — the host-side runner will overwrite
            # .state once it picks up the trigger, so a stale .state from
            # the previous run is at worst a brief UI mis-read.
            logger.warning(
                "appliance_slot_upgrade_state_cleanup_failed",
                state_file=str(_STATE_FILE), error=str(exc),
            )
    tmp = _TRIGGER_FILE.with_suffix(".new")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(_TRIGGER_FILE)
    logger.info("appliance_slot_upgrade_scheduled", image_url=image_url,
                checksum=bool(checksum_url))


def get_update_log_tail(lines: int = 120) -> str:
    """Return the last ``lines`` lines of /var/log/spatiumddi/slot-
    upgrade.log. Empty string when no upgrade has ever run."""
    if not _UPDATE_LOG.exists():
        return ""
    try:
        text = _UPDATE_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def is_apply_in_flight() -> bool:
    state, _ = _upgrade_state_now()
    return state == "in-flight"
