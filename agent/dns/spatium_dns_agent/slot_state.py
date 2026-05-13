"""Phase 8f-2 — slot state + deployment introspection for the heartbeat.

The DNS / DHCP agents both report a small ``slot_state`` block on
every heartbeat so the control plane can populate the Fleet view
(Phase 8f-5). Same module shape for both agents so the fields stay
in sync — see ``agent/dhcp/spatium_dhcp_agent/slot_state.py`` for
the DHCP twin.

The agent reads from host paths via read-only bind mounts the
appliance docker-compose drops in (``/etc/spatiumddi-host`` for
role + version stamps, ``/boot/efi-host/grub/grubenv`` for slot
status). On non-appliance deploys (plain docker-compose, k8s) the
mounts don't exist, every read returns None, and the heartbeat
just carries ``deployment_kind`` — the control plane treats absent
values as "no slot state to track" and the Fleet view shows the
row as docker / k8s / unknown without an Upgrade button.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

# Bind-mount targets the appliance docker-compose exposes. Same paths
# the api container uses (just different mount source on the agent
# side: agent compose mounts the agent appliance's host, not the
# control plane's). Falling back to None on read failure keeps the
# heartbeat payload clean across non-appliance deploys.
_HOST_ROLE_CONFIG = Path("/etc/spatiumddi-host/role-config")
_HOST_RELEASE = Path("/etc/spatiumddi-host/appliance-release")
_HOST_GRUBENV = Path("/boot/efi-host/grub/grubenv")
_HOST_SLOT_STATE = Path(
    "/var/lib/spatiumddi-host/release-state/slot-upgrade-pending.state"
)
_PROC_CMDLINE = Path("/proc/cmdline")
_UDEV_DATA = Path("/run/udev/data")

_UUID_RE = re.compile(r"root=UUID=([0-9a-fA-F-]+)")


def detect_deployment_kind() -> str:
    """Best-effort introspection of where the agent is running.

    Order matters: appliance signal (role-config bind mount) wins over
    k8s env vars (which can be present on docker-compose hosts that
    happen to ship a kubectl context) which wins over docker. Returns
    one of ``appliance`` / ``docker`` / ``k8s`` / ``unknown``.
    """
    if _HOST_ROLE_CONFIG.exists():
        return "appliance"
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "k8s"
    # Inside a docker container the kernel exposes /.dockerenv. Also
    # check cgroups as a backup for newer runtimes (podman, rootless)
    # that drop the marker file.
    if Path("/.dockerenv").exists():
        return "docker"
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        # /proc/1/cgroup is optional fallback for runtimes that don't
        # drop /.dockerenv (podman / rootless). When the read fails
        # (cgroups v1 / v2 layout mismatch, namespaced /proc that
        # hides PID 1) treat it as "unknown" — the Fleet UI renders
        # those rows with a Manual upgrade modal instead of an
        # Upgrade button.
        return "unknown"
    if "docker" in cgroup or "containerd" in cgroup:
        return "docker"
    return "unknown"


def read_installed_version() -> str | None:
    """Parse ``APPLIANCE_VERSION=`` out of ``/etc/spatiumddi-host/appliance-release``.

    Only meaningful on appliance deploys; returns None when the file
    isn't mounted (docker / k8s) or doesn't carry the key.
    """
    if not _HOST_RELEASE.exists():
        return None
    try:
        text = _HOST_RELEASE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("APPLIANCE_VERSION="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _current_slot_from_cmdline() -> str | None:
    """Match /proc/cmdline's ``root=UUID=`` against udev's PARTLABEL.

    Mirror of the api-side ``services/appliance/slot.py`` helper; kept
    independent so the agent doesn't pull in the backend package. The
    PARTLABEL of the booted slot maps to ``slot_a`` or ``slot_b``.

    Reads ``/run/udev/data/b<major>:<minor>`` files directly instead of
    shelling out to lsblk — lsblk inside a container without
    ``/dev/sda*`` bind-mounted can list block topology (from /sys) but
    can't read PARTLABEL / UUID, which it derives from the device
    inode. udev populates the same data into /run/udev/data with
    ``S:`` (symlink) lines like ``S:disk/by-partlabel/root_A`` and
    ``S:disk/by-uuid/aa1311ba-...``; parsing those gives us a
    container-friendly lookup with no extra mounts beyond the
    ``/run/udev`` bind we already have.
    """
    try:
        cmdline = _PROC_CMDLINE.read_text()
    except OSError:
        return None
    m = _UUID_RE.search(cmdline)
    if not m:
        return None
    root_uuid = m.group(1).lower()
    try:
        entries = list(_UDEV_DATA.iterdir())
    except OSError:
        return None
    for entry in entries:
        # udev data files for block devices are named ``b<major>:<minor>``.
        # Other entries (``+acpi:…`` for ACPI tags, ``c…`` for char devs,
        # ``n…`` for net devs) aren't relevant here.
        if not entry.name.startswith("b"):
            continue
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # ``S:`` lines are symlinks udev creates under /dev/disk/. We
        # pull PARTLABEL + UUID from the matching subdirectory prefix.
        partlabel: str | None = None
        matches_uuid = False
        for line in text.splitlines():
            if not line.startswith("S:"):
                continue
            value = line[2:].strip()
            if value.startswith("disk/by-partlabel/"):
                # Last segment is the PARTLABEL (preserving GPT case
                # would matter for downstream consumers, but we only
                # compare case-insensitively to root_a / root_b).
                partlabel = value.rsplit("/", 1)[-1]
            elif value.startswith("disk/by-uuid/"):
                if value.rsplit("/", 1)[-1].lower() == root_uuid:
                    matches_uuid = True
        if matches_uuid and partlabel:
            lower = partlabel.lower()
            if lower == "root_a":
                return "slot_a"
            if lower == "root_b":
                return "slot_b"
    return None


def _durable_default_from_grubenv() -> str | None:
    """Parse ``saved_entry`` out of grubenv. None if the bind mount
    isn't present or the file is unreadable."""
    if not _HOST_GRUBENV.exists():
        return None
    try:
        text = _HOST_GRUBENV.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.rstrip("\x00").strip()
        if line.startswith("saved_entry="):
            value = line.split("=", 1)[1].strip()
            if value in ("slot_a", "slot_b"):
                return value
    return None


def _last_upgrade_state_from_sidecar() -> tuple[str | None, datetime | None]:
    """Read ``state stamp`` from the .state sidecar the host-side
    runner maintains. Returns (state, when) or (None, None) when no
    upgrade has ever run on this agent.
    """
    if not _HOST_SLOT_STATE.exists():
        return None, None
    try:
        text = _HOST_SLOT_STATE.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None, None
    parts = text.split(maxsplit=1)
    state = parts[0] if parts else None
    if state not in ("idle", "in-flight", "done", "failed"):
        return None, None
    stamp = None
    if len(parts) > 1:
        try:
            stamp = datetime.fromisoformat(parts[1])
        except ValueError:
            stamp = None
    return state, stamp


_TRIGGER_FILE = Path("/var/lib/spatiumddi-host/release-state/slot-upgrade-pending")
_REBOOT_TRIGGER_FILE = Path("/var/lib/spatiumddi-host/release-state/reboot-pending")


def maybe_fire_fleet_upgrade(
    desired_version: str | None,
    desired_url: str | None,
) -> bool:
    """Phase 8f-4 — write the slot-upgrade trigger when the control
    plane's desired version doesn't match what's installed.

    Returns True if a trigger was fired (caller should log it), False
    otherwise. Idempotent — multiple long-poll cycles with the same
    desired_version produce one trigger, not many: we check whether
    the trigger file already exists (the host-side path unit hasn't
    picked it up yet) before writing a fresh one. We also skip when
    the desired version equals what's already installed.

    Conditions for firing:
      - Not running on an appliance (no /etc/spatiumddi-host) → skip.
      - desired_version is None / empty → skip.
      - desired_version equals installed_appliance_version → skip.
      - Trigger file already present → skip (path unit hasn't picked
        it up yet; don't stack).
      - desired_url is missing → skip (nothing to apply).
    """
    if detect_deployment_kind() != "appliance":
        return False
    if not desired_version or not desired_url:
        return False
    installed = read_installed_version()
    if installed and installed == desired_version:
        return False
    if _TRIGGER_FILE.exists():
        return False
    # The trigger file's parent should already exist on the appliance
    # (firstboot creates /var/lib/spatiumddi/release-state). Bail
    # silently if it doesn't — host setup is broken; the operator
    # will see "upgrade requested but agent couldn't write trigger"
    # in the audit log on the control plane side once the heartbeat
    # comes back without a state change.
    try:
        _TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _TRIGGER_FILE.with_suffix(".new")
        # Two-line format the host runner expects (Phase 8b-3 contract):
        # line 1 = image URL (or path), line 2 = optional checksum URL.
        tmp.write_text(desired_url + "\n", encoding="utf-8")
        tmp.replace(_TRIGGER_FILE)
        return True
    except OSError:
        return False


def maybe_fire_reboot(reboot_requested: bool) -> bool:
    """Phase 8f-8 — write the reboot trigger when the control plane
    has stamped ``reboot_requested=True`` on the server row.

    Strict appliance-only gate: a docker / k8s / unknown agent NEVER
    fires the trigger even if the field somehow flips through. The
    host-side ``spatiumddi-reboot-agent.path`` unit + the
    ``/var/lib/spatiumddi-host/release-state`` bind mount only exist
    on a SpatiumDDI appliance — but defence in depth is cheap.

    Returns True if a trigger was fired, False otherwise. Idempotent —
    if the trigger file already exists (host runner hasn't picked it
    up yet) we skip rather than stacking writes.
    """
    if detect_deployment_kind() != "appliance":
        return False
    if not reboot_requested:
        return False
    if _REBOOT_TRIGGER_FILE.exists():
        return False
    try:
        _REBOOT_TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _REBOOT_TRIGGER_FILE.with_suffix(".new")
        # One-line marker — the host runner doesn't actually need any
        # payload, just the path-changed event. Stamp + UTC time so
        # the operator can debug from /var/log/spatiumddi if needed.
        tmp.write_text(
            datetime.utcnow().isoformat() + "Z\n", encoding="utf-8"
        )
        tmp.replace(_REBOOT_TRIGGER_FILE)
        return True
    except OSError:
        return False


def collect() -> dict[str, object]:
    """Snapshot the agent's slot + deployment state for the heartbeat.

    Returns a dict the heartbeat client merges into its outbound body.
    Every value is JSON-serialisable; missing data is represented as
    None so the control plane's "only update when not None" semantics
    leave the DB columns untouched for non-appliance agents.
    """
    deployment_kind = detect_deployment_kind()
    is_appliance = deployment_kind == "appliance"

    current_slot = _current_slot_from_cmdline() if is_appliance else None
    durable_default = _durable_default_from_grubenv() if is_appliance else None
    is_trial_boot: bool | None = None
    if current_slot and durable_default:
        is_trial_boot = current_slot != durable_default

    last_state, last_state_at = (
        _last_upgrade_state_from_sidecar() if is_appliance else (None, None)
    )

    return {
        "deployment_kind": deployment_kind,
        "installed_appliance_version": (
            read_installed_version() if is_appliance else None
        ),
        "current_slot": current_slot,
        "durable_default": durable_default,
        "is_trial_boot": is_trial_boot,
        "last_upgrade_state": last_state,
        "last_upgrade_state_at": last_state_at.isoformat() if last_state_at else None,
    }
