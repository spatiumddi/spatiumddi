"""Appliance-host telemetry + trigger-file writers for the supervisor.

Ported from the DNS / DHCP agents in #170 Wave C1 — appliance-host
state used to be collected independently by each service agent (DNS
+ DHCP each shipped their own copy of slot_state.py). The supervisor
now owns this surface; service agents drop the host bind mounts and
let the supervisor's heartbeat carry the appliance row's telemetry +
fire the trigger files.

Module responsibilities:

* **Read** appliance-host state — deployment kind, slot UUID match,
  grubenv durable default, installed appliance version, last
  upgrade state from the .state sidecar, snmpd + chrony sync status.
* **Write** appliance-host trigger files — slot-upgrade pending,
  reboot pending, snmpd reload, chrony reload. Host-side systemd
  ``.path`` units (``spatiumddi-slot-upgrade.path`` /
  ``spatiumddi-reboot-agent.path`` / ``spatiumddi-snmp-reload.path``
  / ``spatiumddi-chrony-reload.path``) notice the writes and fire
  the runner scripts.

Reads happen through host bind mounts the supervisor compose entry
keeps (``/etc/spatiumddi-host`` for role + version, ``/boot/efi-host``
for grubenv, ``/var/lib/spatiumddi-host/release-state`` for the
trigger surface, ``/run/udev`` for slot-UUID lookup). On non-
appliance hosts (dev compose, k8s) the mounts don't exist; every
read returns ``None`` and every trigger-file write is short-
circuited by ``detect_deployment_kind()``'s appliance gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# Bind-mount targets the appliance docker-compose exposes. Same paths
# the api container uses (just different mount source on the agent
# side: agent compose mounts the agent appliance's host, not the
# control plane's). Falling back to None on read failure keeps the
# heartbeat payload clean across non-appliance deploys.
_HOST_ROLE_CONFIG = Path("/etc/spatiumddi-host/role-config")
_HOST_RELEASE = Path("/etc/spatiumddi-host/appliance-release")
_HOST_GRUBENV = Path("/boot/efi-host/grub/grubenv")
_HOST_SLOT_STATE = Path("/var/lib/spatiumddi-host/release-state/slot-upgrade-pending.state")
_PROC_CMDLINE = Path("/proc/cmdline")
_UDEV_DATA = Path("/run/udev/data")

_UUID_RE = re.compile(r"root=UUID=([0-9a-fA-F-]+)")


def detect_runtime() -> str:
    """Issue #183 Phase 7 — the appliance is k3s-only.

    Pre-Phase-7 this function branched on systemctl-is-active-k3s to
    pick between the docker-compose and k3s lifecycle paths. Phase 7
    retires the compose path entirely; the function survives as a
    pure constant for any caller that still reads the runtime tag
    (heartbeat telemetry, console rendering, etc.).
    """
    return "k3s"


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


# #272 — two installer-role variants: ``control-plane`` (the brain +
# k3s etcd seed) and ``appliance`` (a data-plane node that pairs with
# a remote control plane). The supervisor reports its variant on every
# heartbeat so the Fleet UI can split rows into Control plane vs
# Service agents, and the label reconciler can apply the correct
# per-role labels (control-plane ⇒ control-plane label;
# appliance ⇒ operator-assigned).
_KNOWN_VARIANTS = frozenset({"control-plane", "appliance"})
# Pre-#272 installs wrote one of three variant strings; normalise them
# to the two canonical values so a not-yet-reinstalled box reports a
# clean variant (full-stack / frontend-core were both control planes;
# application was the data-plane node).
_LEGACY_VARIANT_ALIASES = {
    "full-stack": "control-plane",
    "frontend-core": "control-plane",
    "application": "appliance",
}


def detect_appliance_variant() -> str | None:
    """Parse ``ROLE=`` out of ``/etc/spatiumddi-host/role-config``.

    The installer wizard's role choice is baked into role-config at
    install time (``spatium-install`` writes ``ROLE=<variant>``);
    firstboot leaves the value untouched across slot swaps. Legacy
    pre-#272 strings are normalised to the two canonical variants.
    Returns ``None`` when the file isn't mounted (docker / k8s) or the
    parsed value isn't one we recognise — the heartbeat handler treats
    ``None`` as "supervisor didn't ship the field" and leaves the
    persisted column alone.
    """
    if not _HOST_ROLE_CONFIG.exists():
        return None
    try:
        text = _HOST_ROLE_CONFIG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("ROLE="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            value = _LEGACY_VARIANT_ALIASES.get(value, value)
            return value if value in _KNOWN_VARIANTS else None
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

    Auto-heals stale ``failed`` states: the host runner renames the
    pending trigger to ``.failed.<ts>`` once it finishes, so the
    presence of the un-suffixed trigger file is the marker of an
    in-flight apply. If state == failed AND the un-suffixed trigger
    isn't present, the failure has already been recorded + processed
    — by definition the operator has had time to observe it (typical
    heartbeat cadence is 30 s) so the Fleet view's State pill
    shouldn't stick on ``failed`` forever. Flip back to ``ready`` so
    the agent's heartbeat naturally clears the chip on the control
    plane within one cycle. The ``.failed.<ts>`` sidecar file still
    exists on disk for forensic / audit lookup.

    Fresh appliances that have never run an upgrade have no .state
    file at all — return ``ready`` rather than ``None`` so the Fleet
    view's State column reads as a positive "healthy + no pending
    work" signal instead of an empty cell that visually looks like
    "agent hasn't reported yet". ``None`` is reserved for genuinely-
    unknown rows (docker / k8s / pre-8f-2).
    """
    if not _HOST_SLOT_STATE.exists():
        return "ready", None
    try:
        text = _HOST_SLOT_STATE.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None, None
    parts = text.split(maxsplit=1)
    state = parts[0] if parts else None
    if state not in ("ready", "in-flight", "done", "failed"):
        return None, None
    # Stale-failed auto-heal — only when no apply is currently in
    # flight (trigger file is the "in-flight" marker; rename to
    # .failed.<ts> on finish).
    if state == "failed" and not _TRIGGER_FILE.exists():
        return "ready", None
    stamp = None
    if len(parts) > 1:
        try:
            stamp = datetime.fromisoformat(parts[1])
        except ValueError:
            stamp = None
    return state, stamp


_TRIGGER_FILE = Path("/var/lib/spatiumddi-host/release-state/slot-upgrade-pending")
_REBOOT_TRIGGER_FILE = Path("/var/lib/spatiumddi-host/release-state/reboot-pending")
# Per-slot installed-version sidecar maintained by ``spatium-upgrade-
# slot sync-versions`` (called by spatiumddi-firstboot at every boot
# + at the end of every apply). Shape: ``{"slot_a": "<version>",
# "slot_b": "<version>"}`` — values may be the literal ``"unstamped"``
# / ``"unreadable"`` / ``"unknown"`` for slots that aren't readable
# from the host. We pass values through verbatim; the Fleet UI
# normalises them in ``slotVersion()``.
_SLOT_VERSIONS_FILE = Path("/var/lib/spatiumddi-host/release-state/slot-versions.json")
# Issue #165 — operator-set timezone trigger + applied-hash sidecar.
# Trigger carries a single line (the IANA tz name); host runner
# rewrites the hash sidecar after a successful apply so the
# supervisor short-circuits the next heartbeat's trigger write.
_TZ_TRIGGER_FILE = Path("/var/lib/spatiumddi-host/release-state/tz-pending")
_TZ_APPLIED_HASH_FILE = Path("/var/lib/spatiumddi-host/release-state/tz-hash")
# Per-slot boot-control trigger files. Each carries a single line:
# the target slot name (``slot_a`` / ``slot_b``). The host-side
# ``spatiumddi-slot-set-next-boot.path`` / ``spatiumddi-slot-set-
# default.path`` units fire on close-after-write rename.
_SET_NEXT_BOOT_TRIGGER_FILE = Path(
    "/var/lib/spatiumddi-host/release-state/slot-set-next-boot-pending"
)
_SET_DEFAULT_TRIGGER_FILE = Path("/var/lib/spatiumddi-host/release-state/slot-set-default-pending")
# Issue #153 — SNMP config rollout. The trigger file carries the
# rendered snmpd.conf body so the host runner doesn't need to re-
# render. The hash sidecar lets the agent skip re-firing after an
# unchanged config bundle picks up across an agent restart.
_SNMP_TRIGGER_FILE = Path("/var/lib/spatiumddi-host/release-state/snmp-config-pending")
_SNMP_HASH_SIDECAR = Path("/var/lib/spatiumddi-host/release-state/snmp-config-hash")
_SNMP_STATUS_SIDECAR = Path("/var/lib/spatiumddi-host/release-state/snmp-status")
# Issue #154 — NTP / chrony equivalents. Same shape as SNMP.
_NTP_TRIGGER_FILE = Path("/var/lib/spatiumddi-host/release-state/ntp-config-pending")
_NTP_HASH_SIDECAR = Path("/var/lib/spatiumddi-host/release-state/ntp-config-hash")
_NTP_STATUS_SIDECAR = Path("/var/lib/spatiumddi-host/release-state/ntp-status")
# Issue #343 — LLDP / lldpd equivalents. Same shape as SNMP / NTP.
_LLDP_TRIGGER_FILE = Path("/var/lib/spatiumddi-host/release-state/lldp-config-pending")
_LLDP_HASH_SIDECAR = Path("/var/lib/spatiumddi-host/release-state/lldp-config-hash")
_LLDP_STATUS_SIDECAR = Path("/var/lib/spatiumddi-host/release-state/lldp-status")
# Issue #285 Phase 2b — firewall apply-state sidecars the host-side
# spatium-firewall-reload runner writes after each apply; echoed on the
# heartbeat so the control plane can see drift + drive the apply alarm.
# The runner writes the bare /var/lib/spatiumddi/release-state path; the
# supervisor reads the same dir through the -host bind mount.
_FIREWALL_APPLIED_HASH_SIDECAR = Path(
    "/var/lib/spatiumddi-host/release-state/firewall-applied-hash"
)
_FIREWALL_APPLIED_STATUS_SIDECAR = Path(
    "/var/lib/spatiumddi-host/release-state/firewall-applied-status"
)
_FIREWALL_BASE_MARKER_SIDECAR = Path(
    "/var/lib/spatiumddi-host/release-state/firewall-applied-base-marker"
)

# #272 Phase 7b — control-plane cluster join/leave. The join trigger
# carries the seed's kubeapi URL + join token; the host-side runner
# (spatium-cluster-join) reconfigures k3s to join the seed as a server
# node and writes the .state sidecar (state\treason) the supervisor
# reads back. The leave trigger has no payload. The token sidecar is
# written by the PRIMARY's host runner from /var/lib/rancher/k3s/
# server/token so the supervisor can report it without mounting the
# k3s server dir.
_CLUSTER_JOIN_TRIGGER_FILE = Path("/var/lib/spatiumddi-host/release-state/cluster-join-pending")
_CLUSTER_LEAVE_TRIGGER_FILE = Path("/var/lib/spatiumddi-host/release-state/cluster-leave-pending")
# Guardrail confirmation markers (#272). The host-side spatium-cluster-join
# runner does DESTRUCTIVE k3s surgery (full cluster-identity wipe + rejoin),
# fired by a systemd .path unit watching these trigger files. To stop a
# stray / accidental / hand-touched file from triggering a wipe, the
# supervisor stamps a magic first line and the runner refuses to act on any
# trigger whose first line isn't an exact match. Must stay byte-identical to
# the constants in spatium-cluster-join.
_CLUSTER_JOIN_CONFIRM = "SPATIUMDDI-CLUSTER-JOIN-CONFIRM-V1"
_CLUSTER_LEAVE_CONFIRM = "SPATIUMDDI-CLUSTER-LEAVE-CONFIRM-V1"
_CLUSTER_JOIN_STATE_SIDECAR = Path("/var/lib/spatiumddi-host/release-state/cluster-join.state")
_K3S_JOIN_TOKEN_SIDECAR = Path("/var/lib/spatiumddi-host/release-state/k3s-join-token")


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
    # Issue #242 — only accept ``https://`` (preferred) or ``file://``
    # (sneakernet / air-gap). Reject ``http://`` so a misconfigured
    # control plane can't downgrade the OS-image fetch to cleartext
    # over the WAN; reject unknown / unscheme'd URLs entirely so a
    # tampered payload can't slip past as a relative path the host
    # runner would resolve.
    desired_url_str = str(desired_url).strip()
    if not desired_url_str:
        return False
    allowed_schemes = ("https://", "file://")
    if not any(desired_url_str.lower().startswith(s) for s in allowed_schemes):
        log.warning(
            "supervisor.appliance_state.rejected_upgrade_url_scheme",
            url_prefix=desired_url_str.split("://", 1)[0][:32],
        )
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
            datetime.now(UTC).isoformat().replace("+00:00", "Z") + "\n",
            encoding="utf-8",
        )
        tmp.replace(_REBOOT_TRIGGER_FILE)
        return True
    except OSError:
        return False


def maybe_fire_timezone(desired_timezone: str | None) -> bool:
    """Issue #165 — write the tz-reload trigger when the operator's
    desired timezone (from ``platform_settings.timezone``) doesn't
    match the value the host runner last applied.

    Returns True if a trigger was fired, False otherwise. Idempotent
    via the ``tz-hash`` sidecar — the host runner writes the IANA
    name it applied; we compare against the desired value and skip
    the rewrite when they match. Empty / None desired means "no
    override" — the supervisor leaves the host alone.

    Strict appliance-only gate (mirrors ``maybe_fire_reboot``):
    docker / k8s / unknown deploys NEVER fire the trigger even if
    the field somehow flips through.
    """
    if detect_deployment_kind() != "appliance":
        return False
    if not desired_timezone or not desired_timezone.strip():
        return False
    desired = desired_timezone.strip()
    # Read the applied-hash sidecar (single-line IANA name) the host
    # runner writes after a successful apply.
    try:
        applied = _TZ_APPLIED_HASH_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        applied = ""
    if applied == desired:
        return False
    try:
        _TZ_TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _TZ_TRIGGER_FILE.with_suffix(".new")
        tmp.write_text(desired + "\n", encoding="utf-8")
        # Atomic rename — the .path unit watches PathChanged which
        # fires on close-after-write of the final path, so the
        # rename ensures the runner sees the complete trigger file
        # rather than a half-written one.
        tmp.replace(_TZ_TRIGGER_FILE)
        return True
    except OSError:
        return False


def read_slot_versions() -> tuple[str | None, str | None]:
    """Read per-slot installed versions from the
    ``slot-versions.json`` sidecar (maintained by ``spatium-upgrade-
    slot sync-versions``).

    Returns ``(slot_a_version, slot_b_version)`` — either or both may
    be ``None`` when the sidecar is missing entirely. ``"unstamped"`` /
    ``"unreadable"`` / ``"unknown"`` sentinel values are passed through
    verbatim; the Fleet UI's ``slotVersion()`` helper normalises them
    to ``"—"``.

    Strict appliance-only — non-appliance deploys don't have the host
    bind mount + the sidecar wouldn't exist anyway. Returns the same
    ``(None, None)`` so the control plane's "only update when not
    None" semantics leaves the columns untouched.
    """
    if detect_deployment_kind() != "appliance":
        return None, None
    if not _SLOT_VERSIONS_FILE.exists():
        return None, None
    try:
        text = _SLOT_VERSIONS_FILE.read_text(encoding="utf-8", errors="replace")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    slot_a = data.get("slot_a") if isinstance(data.get("slot_a"), str) else None
    slot_b = data.get("slot_b") if isinstance(data.get("slot_b"), str) else None
    return slot_a, slot_b


def maybe_fire_set_next_boot(
    desired_slot: str | None,
    current_slot: str | None,
) -> bool:
    """Write the slot-set-next-boot trigger when the control plane
    asks for a slot that isn't already running.

    ``desired_slot`` mirrors the heartbeat response's
    ``desired_next_boot_slot`` (``slot_a`` / ``slot_b`` / ``None``).
    ``current_slot`` is the supervisor's last observed running slot —
    if the operator's intent already matches reality we don't fire
    (the heartbeat handler will auto-clear ``desired_*`` on the next
    tick).

    Strict appliance-only gate (mirrors ``maybe_fire_reboot``). Empty
    intent or invalid slot literal → skip. Idempotent via trigger-
    file presence; the host runner renames the trigger to .done /
    .failed on completion.
    """
    if detect_deployment_kind() != "appliance":
        return False
    if desired_slot not in ("slot_a", "slot_b"):
        return False
    if current_slot == desired_slot:
        return False
    if _SET_NEXT_BOOT_TRIGGER_FILE.exists():
        return False
    try:
        _SET_NEXT_BOOT_TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SET_NEXT_BOOT_TRIGGER_FILE.with_suffix(".new")
        # Single-line payload — host runner reads + invokes
        # ``spatium-upgrade-slot set-next-boot <slot>`` with this
        # value. Validated as a slot literal above so no shell
        # metachars reach the runner.
        tmp.write_text(desired_slot + "\n", encoding="utf-8")
        tmp.replace(_SET_NEXT_BOOT_TRIGGER_FILE)
        return True
    except OSError:
        return False


def maybe_fire_set_default(
    desired_slot: str | None,
    durable_default: str | None,
) -> bool:
    """Write the slot-set-default trigger when the control plane asks
    for a durable default different from the current grub
    ``saved_entry``.

    Same shape as ``maybe_fire_set_next_boot`` but for the durable
    (``grub-set-default``) action: commits a trial boot or durably
    reverts. If the durable default already matches the intent we
    skip (the heartbeat handler will auto-clear ``desired_*`` on the
    next tick).
    """
    if detect_deployment_kind() != "appliance":
        return False
    if desired_slot not in ("slot_a", "slot_b"):
        return False
    if durable_default == desired_slot:
        return False
    if _SET_DEFAULT_TRIGGER_FILE.exists():
        return False
    try:
        _SET_DEFAULT_TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SET_DEFAULT_TRIGGER_FILE.with_suffix(".new")
        tmp.write_text(desired_slot + "\n", encoding="utf-8")
        tmp.replace(_SET_DEFAULT_TRIGGER_FILE)
        return True
    except OSError:
        return False


def maybe_fire_snmp_reload(bundle_block: object) -> bool:
    """Issue #153 — write the snmp-config trigger when the control
    plane's rendered snmpd.conf hash differs from the last one this
    agent applied.

    Strict appliance-only gate — same reasoning as
    ``maybe_fire_reboot``: the host-side ``spatiumddi-snmp-reload``
    units don't exist on docker / k8s deploys; firing the trigger
    there would just leave dead files in a directory that may not
    even exist.

    Returns True if a trigger was fired, False otherwise. Idempotent
    via the hash sidecar — multiple long-poll cycles with the same
    config_hash produce zero triggers. The host runner writes the
    sidecar on successful apply so the next agent restart doesn't
    re-fire either.
    """
    if detect_deployment_kind() != "appliance":
        return False
    if not isinstance(bundle_block, dict):
        return False
    config_hash = str(bundle_block.get("config_hash") or "")
    snmpd_conf = str(bundle_block.get("snmpd_conf") or "")
    enabled = bool(bundle_block.get("enabled"))
    # Empty hash = SNMP disabled and no config to push. Only fire a
    # disable trigger if the agent previously applied a non-empty
    # config (sidecar present and non-empty) — otherwise this is the
    # default "never configured" state and there's nothing to undo.
    last_hash = ""
    if _SNMP_HASH_SIDECAR.exists():
        try:
            last_hash = _SNMP_HASH_SIDECAR.read_text(encoding="utf-8").strip()
        except OSError:
            last_hash = ""
    if config_hash == last_hash:
        return False
    if _SNMP_TRIGGER_FILE.exists():
        return False
    try:
        _SNMP_TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Three-section payload the host runner reads:
        #   line 1:    ``enabled`` | ``disabled`` marker
        #   line 2:    config_hash (sha256 hex, blank when disabled)
        #   line 3+:   rendered snmpd.conf body (already ends with \n)
        # The hash is on the wire (rather than recomputed by the
        # runner) so the agent and host agree on exactly which body
        # was applied — useful when the runner writes the sidecar
        # the agent reads on next bundle to short-circuit re-firing.
        payload = ("enabled\n" if enabled else "disabled\n") + (config_hash + "\n") + snmpd_conf
        tmp = _SNMP_TRIGGER_FILE.with_suffix(".new")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(_SNMP_TRIGGER_FILE)
        return True
    except OSError:
        return False


# ── #272 Phase 7b — control-plane cluster join/leave ────────────────


def maybe_fire_cluster_join(
    desired_cluster_role: str | None,
    server_url: str | None,
    join_token: str | None,
) -> bool:
    """Write the cluster-join trigger when the control plane asks this
    node to join the k3s control-plane cluster as a server.

    ``desired_cluster_role`` mirrors the heartbeat response field —
    only ``"member"`` fires a join. ``server_url`` + ``join_token`` are
    the seed's coordinates. The host-side runner reconfigures k3s +
    writes the ``.state`` sidecar; the supervisor reports that state on
    subsequent heartbeats so the backend can settle ``cluster_role`` +
    drop the desired-state.

    Strict appliance-only gate (mirrors ``maybe_fire_reboot``).
    Idempotent via trigger-file presence — once written, we don't
    stack writes until the host runner consumes it (renaming to
    ``.done`` / ``.failed``). The trigger payload is three lines: the
    ``_CLUSTER_JOIN_CONFIRM`` guardrail marker, then ``server_url``,
    then ``join_token``. The runner refuses any trigger whose first
    line isn't the marker, so a stray file can't fire a wipe.
    """
    if detect_deployment_kind() != "appliance":
        return False
    if desired_cluster_role != "member":
        return False
    if not server_url or not join_token:
        return False
    if _CLUSTER_JOIN_TRIGGER_FILE.exists():
        return False
    try:
        _CLUSTER_JOIN_TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CLUSTER_JOIN_TRIGGER_FILE.with_suffix(".new")
        tmp.write_text(f"{_CLUSTER_JOIN_CONFIRM}\n{server_url}\n{join_token}\n", encoding="utf-8")
        tmp.replace(_CLUSTER_JOIN_TRIGGER_FILE)
        return True
    except OSError:
        return False


def maybe_fire_cluster_leave(desired_cluster_role: str | None) -> bool:
    """Write the cluster-leave trigger when the control plane asks this
    node to leave the cluster (``desired_cluster_role == "none"``).

    Payload is two lines: the ``_CLUSTER_LEAVE_CONFIRM`` guardrail
    marker then a timestamp. The runner refuses any trigger whose first
    line isn't the marker. Strict appliance-only + idempotent via
    trigger-file presence.
    """
    if detect_deployment_kind() != "appliance":
        return False
    if desired_cluster_role != "none":
        return False
    if _CLUSTER_LEAVE_TRIGGER_FILE.exists():
        return False
    try:
        _CLUSTER_LEAVE_TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CLUSTER_LEAVE_TRIGGER_FILE.with_suffix(".new")
        ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        tmp.write_text(f"{_CLUSTER_LEAVE_CONFIRM}\n{ts}\n", encoding="utf-8")
        tmp.replace(_CLUSTER_LEAVE_TRIGGER_FILE)
        return True
    except OSError:
        return False


def read_cluster_join_state() -> tuple[str | None, str | None]:
    """Return ``(cluster_join_state, cluster_join_reason)`` from the
    ``.state`` sidecar the host runner writes (``state\\treason``).

    Appliance-only; ``(None, None)`` when the sidecar is missing so the
    backend's "only update when not None" semantics leave the columns
    alone on nodes that have never joined/left.
    """
    if detect_deployment_kind() != "appliance":
        return None, None
    if not _CLUSTER_JOIN_STATE_SIDECAR.exists():
        return None, None
    try:
        raw = _CLUSTER_JOIN_STATE_SIDECAR.read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if not raw:
        return None, None
    state, _, reason = raw.partition("\t")
    return (state or None), (reason or None)


def read_k3s_join_token() -> str | None:
    """Return the seed's k3s node-token from the sidecar the PRIMARY's
    host runner copies out of ``/var/lib/rancher/k3s/server/token``.

    Only the seed has this sidecar; ``None`` everywhere else (the
    backend leaves the column untouched). Reading a sidecar rather than
    the k3s server dir keeps the supervisor pod from needing that
    sensitive path mounted.
    """
    if detect_deployment_kind() != "appliance":
        return None
    if not _K3S_JOIN_TOKEN_SIDECAR.exists():
        return None
    try:
        tok = _K3S_JOIN_TOKEN_SIDECAR.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return tok or None


def read_snmpd_running() -> bool | None:
    """Read snmpd's last-reported status from the sidecar the host-
    side runner writes after each apply. ``True`` = snmpd is running,
    ``False`` = stopped, ``None`` = unknown (sidecar missing /
    unreadable / non-appliance)."""
    if not _SNMP_STATUS_SIDECAR.exists():
        return None
    try:
        text = _SNMP_STATUS_SIDECAR.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text == "running":
        return True
    if text == "stopped":
        return False
    return None


def maybe_fire_lldp_reload(bundle_block: object) -> bool:
    """Issue #343 — write the lldp-config trigger when the control plane's
    rendered lldpd config hash differs from the last one this agent applied.

    Identical idempotency + appliance-only gate to ``maybe_fire_snmp_reload``.
    The payload is four sections (LLDP carries an extra ``daemon_args`` line
    for the CDP/EDP/FDP/SONMP reception flags, which live in
    ``/etc/default/lldpd`` rather than the lldpcli conf body):

        line 1:   ``enabled`` | ``disabled`` marker
        line 2:   config_hash (sha256 hex, blank when disabled)
        line 3:   daemon_args (``-c -e`` …, blank when none)
        line 4+:  rendered /etc/lldpd.d/spatium.conf body (ends with \\n)
    """
    if detect_deployment_kind() != "appliance":
        return False
    if not isinstance(bundle_block, dict):
        return False
    config_hash = str(bundle_block.get("config_hash") or "")
    lldpd_conf = str(bundle_block.get("lldpd_conf") or "")
    daemon_args = str(bundle_block.get("daemon_args") or "")
    enabled = bool(bundle_block.get("enabled"))
    last_hash = ""
    if _LLDP_HASH_SIDECAR.exists():
        try:
            last_hash = _LLDP_HASH_SIDECAR.read_text(encoding="utf-8").strip()
        except OSError:
            last_hash = ""
    if config_hash == last_hash:
        return False
    if _LLDP_TRIGGER_FILE.exists():
        return False
    try:
        _LLDP_TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            ("enabled\n" if enabled else "disabled\n")
            + (config_hash + "\n")
            + (daemon_args + "\n")
            + lldpd_conf
        )
        tmp = _LLDP_TRIGGER_FILE.with_suffix(".new")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(_LLDP_TRIGGER_FILE)
        return True
    except OSError:
        return False


def read_lldpd_running() -> bool | None:
    """Read lldpd's last-reported status from the sidecar the host-side
    runner writes after each apply. ``True`` = running, ``False`` = stopped,
    ``None`` = unknown (sidecar missing / unreadable / non-appliance)."""
    if not _LLDP_STATUS_SIDECAR.exists():
        return None
    try:
        text = _LLDP_STATUS_SIDECAR.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text == "running":
        return True
    if text == "stopped":
        return False
    return None


# ── Firewall apply-state read-back (#285 Phase 2b) ───────────────────


def _read_release_state_line(path: Path) -> str | None:
    """Return the stripped single-line content of a release-state sidecar,
    or None when missing / unreadable / empty."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def read_firewall_applied_hash() -> str | None:
    """sha256 of the drop-in the firewall runner last applied (#285 Phase 2b).
    None when never applied / non-appliance → the control plane leaves the
    column untouched."""
    return _read_release_state_line(_FIREWALL_APPLIED_HASH_SIDECAR)


def read_firewall_applied_status() -> str | None:
    """The firewall runner's last outcome — ``ok`` / ``error:*`` / ``reverted``
    (#285 Phase 2b). None when the sidecar is missing."""
    return _read_release_state_line(_FIREWALL_APPLIED_STATUS_SIDECAR)


def read_firewall_base_marker() -> str | None:
    """sha256 of the base /etc/nftables.conf the firewall runner last applied
    against (#285 Phase 2b) — lets the control plane tell a node still on the
    pre-#285 LAN-wide base apart from a hardened one. None when missing."""
    return _read_release_state_line(_FIREWALL_BASE_MARKER_SIDECAR)


# ── LLDP neighbour discovery (#347) ──────────────────────────────────


def _lldp_list(x: object) -> list:
    """lldpd's json0 wraps even single items in arrays; tolerate both."""
    if isinstance(x, list):
        return x
    return [x] if x is not None else []


def _lldp_scalar(x: object) -> str:
    """Pull a leaf string from a json0 node — ``[{"value": v}]`` / ``{"value":
    v}`` / a bare scalar all collapse to ``str(v)``."""
    items = _lldp_list(x)
    if not items:
        return ""
    first = items[0]
    if isinstance(first, dict):
        return str(first.get("value", "")).strip()
    return str(first).strip()


def _lldp_chassis(iface: dict) -> dict:
    """Return the chassis object for an interface entry, tolerating both the
    json0 array shape (``"chassis": [{"id": ...}]``) and the name-keyed
    ``json`` shape (``"chassis": {"sw1": {"id": ...}}``)."""
    raw = iface.get("chassis")
    items = _lldp_list(raw)
    if items and isinstance(items[0], dict):
        c = items[0]
        # Name-keyed single-entry dict (no "id" key) → unwrap the inner value.
        # The KEY is the chassis sys-name in this shape, so fold it in as
        # ``name`` (unless the inner dict already carries one) — otherwise
        # remote_sys_name is silently lost for those lldpd builds.
        if "id" not in c and len(c) == 1:
            key, inner = next(iter(c.items()))
            if isinstance(inner, dict):
                return inner if "name" in inner else {**inner, "name": key}
        return c
    return {}


def _parse_lldp_neighbours(doc: object) -> list[dict]:
    """Flatten ``lldpcli show neighbors -f json0`` into neutral neighbour
    dicts the control plane ingests. Defensive — skips malformed entries
    rather than raising, so one weird neighbour can't drop the whole batch.
    """
    out: list[dict] = []
    if not isinstance(doc, dict):
        return out
    for lldp in _lldp_list(doc.get("lldp")):
        if not isinstance(lldp, dict):
            continue
        for iface in _lldp_list(lldp.get("interface")):
            if not isinstance(iface, dict):
                continue
            # ``_lldp_scalar`` handles both a plain ``"eth0"`` and json0's
            # array-wrapped ``[{"value": "eth0"}]`` forms uniformly.
            local = _lldp_scalar(iface.get("name"))
            chassis = _lldp_chassis(iface)
            ports = _lldp_list(iface.get("port"))
            port = ports[0] if ports and isinstance(ports[0], dict) else {}

            chassis_id = _lldp_scalar(chassis.get("id"))
            port_id = _lldp_scalar(port.get("id"))
            if not local or not chassis_id or not port_id:
                continue  # the identity tuple must be complete

            caps = [
                str(c.get("type"))
                for c in _lldp_list(chassis.get("capability"))
                if isinstance(c, dict) and c.get("enabled") and c.get("type")
            ]
            out.append(
                {
                    "local_iface": local[:64],
                    "remote_chassis_id": chassis_id[:255],
                    "remote_port_id": port_id[:255],
                    "remote_port_descr": (_lldp_scalar(port.get("descr")) or None),
                    "remote_sys_name": (_lldp_scalar(chassis.get("name")) or None),
                    "remote_sys_descr": (_lldp_scalar(chassis.get("descr")) or None),
                    "remote_mgmt_ip": (_lldp_scalar(chassis.get("mgmt-ip")) or None),
                    "remote_caps": (",".join(caps) or None),
                }
            )
    return out


def read_lldp_neighbours() -> list[dict] | None:
    """Run ``lldpcli show neighbors -f json0`` + parse it (#347).

    Returns the neighbour list (possibly empty) on a successful run, or
    ``None`` when not on an appliance / lldpcli is unavailable or errors — so
    the control plane leaves the stored set alone rather than wiping it on a
    transient failure. An empty list DOES wipe (lldpd ran, saw nothing)."""
    if detect_deployment_kind() != "appliance":
        return None
    try:
        proc = subprocess.run(
            ["lldpcli", "-f", "json0", "show", "neighbors"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return _parse_lldp_neighbours(doc)


def maybe_fire_ntp_reload(bundle_block: object) -> bool:
    """Issue #154 — write the ntp-config trigger when the control
    plane's rendered chrony.conf hash differs from the last applied.

    Identical idempotency shape to ``maybe_fire_snmp_reload``:
    appliance-only gate, hash sidecar lookup, single trigger file
    rename, fail silent on OSError. Different paths so the SNMP and
    NTP pipelines never collide.
    """
    if detect_deployment_kind() != "appliance":
        return False
    if not isinstance(bundle_block, dict):
        return False
    config_hash = str(bundle_block.get("config_hash") or "")
    chrony_conf = str(bundle_block.get("chrony_conf") or "")
    allow_clients = bool(bundle_block.get("allow_clients"))
    last_hash = ""
    if _NTP_HASH_SIDECAR.exists():
        try:
            last_hash = _NTP_HASH_SIDECAR.read_text(encoding="utf-8").strip()
        except OSError:
            last_hash = ""
    if config_hash == last_hash:
        return False
    if _NTP_TRIGGER_FILE.exists():
        return False
    try:
        _NTP_TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Four-line header:
        #   line 1: marker — ``enabled`` is always the case for
        #           chrony (it's always running on the appliance);
        #           kept for shape-parity with the SNMP runner.
        #   line 2: ``allow_clients`` — ``true`` / ``false`` so the
        #           runner knows whether to open the UDP 123 nft
        #           drop-in.
        #   line 3: config_hash (sha256 hex)
        #   line 4+: rendered chrony.conf body
        payload = (
            "enabled\n"
            + ("true\n" if allow_clients else "false\n")
            + (config_hash + "\n")
            + chrony_conf
        )
        tmp = _NTP_TRIGGER_FILE.with_suffix(".new")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(_NTP_TRIGGER_FILE)
        return True
    except OSError:
        return False


def read_ntp_sync_state() -> str | None:
    """Read chrony's last-reported sync state from the sidecar the
    host-side runner refreshes on each apply. One of ``synchronized``
    / ``unsynchronized`` / ``unknown``, or ``None`` on docker / k8s
    (no sidecar mounted)."""
    if not _NTP_STATUS_SIDECAR.exists():
        return None
    try:
        text = _NTP_STATUS_SIDECAR.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text in ("synchronized", "unsynchronized", "unknown"):
        return text
    return None


_HOST_K3S_KUBECONFIG = Path("/etc/spatiumddi-host/rancher/k3s/k3s.yaml")
# Fallback when the bind mount is the supervisor compose's
# ``/etc/rancher/k3s`` path-through instead of the
# ``/etc/spatiumddi-host`` mount. Both layouts exist in the wild.
_DIRECT_K3S_KUBECONFIG = Path("/etc/rancher/k3s/k3s.yaml")
_K3S_VERSION_SIDECAR = Path("/usr/share/doc/k3s/.version")

# Issue #285 Phase 1 — firewall prerequisites. The k3s config files are
# already reachable via the ``/etc/rancher/k3s`` (kubeconfig) bind mount;
# the live base nftables.conf is exposed by a dedicated read-only File
# mount the supervisor DaemonSet adds in #285 (absent on older charts /
# non-appliance, where the reader returns None).
_K3S_CIDRS_DROPIN = Path("/etc/rancher/k3s/config.yaml.d/spatium-cidrs.yaml")
_K3S_MAIN_CONFIG = Path("/etc/rancher/k3s/config.yaml")
_K3S_CONFIG_DIR = Path("/etc/rancher/k3s/config.yaml.d")
_HOST_NFTABLES_CONF = Path("/etc/nftables-host.conf")


def read_k3s_version() -> str | None:
    """Issue #183 Phase 5 — installed k3s version stamped by the
    build-time ``fetch-k3s.sh`` script. Single value per slot; the
    Fleet UI shows it next to the appliance row.

    Returns ``None`` when the sidecar is missing — pre-#183 slots or
    non-appliance deploys.
    """
    if not _K3S_VERSION_SIDECAR.exists():
        return None
    try:
        text = _K3S_VERSION_SIDECAR.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def read_kubeconfig() -> str | None:
    """Issue #183 Phase 5 — operator kubeconfig payload.

    Reads ``k3s.yaml`` straight off the slot's /etc overlay (the
    admin kubeconfig k3s writes on first start). Returns the raw
    YAML text — the backend rewrites the ``server:`` field for
    operator reachability (using the appliance's last-seen IP) and
    Fernet-encrypts before persisting.

    Returns ``None`` when:
      * The supervisor isn't on a k3s appliance (``detect_runtime()
        != "k3s"``)
      * The kubeconfig file doesn't exist yet (k3s.service hasn't
        started, or first-boot is still warming up)

    Shipping the cleartext kubeconfig over the heartbeat is safe —
    the heartbeat channel is already mTLS-encrypted end-to-end with
    the supervisor's cert. At-rest encryption is the backend's job.
    """
    if detect_runtime() != "k3s":
        return None
    for path in (_HOST_K3S_KUBECONFIG, _DIRECT_K3S_KUBECONFIG):
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                continue
    return None


# Issue #183 Phase 6 — k3s server cert expiry probe. The k3s
# default serving cert lives at one of two paths depending on
# whether the supervisor's host bind mount picked up /var/lib/
# rancher (the standard layout) or /var/lib/spatiumddi-host/
# rancher (the bind-mount-everything fallback). Probe both.
_K3S_SERVING_CERT_CANDIDATES = (
    Path("/var/lib/spatiumddi-host/rancher/k3s/server/tls/serving-kube-apiserver.crt"),
    Path("/var/lib/rancher/k3s/server/tls/serving-kube-apiserver.crt"),
)


def read_k3s_api_cert_expiry() -> str | None:
    """Issue #183 Phase 6 — k3s server-cert ``Not After`` timestamp.

    k3s rotates this cert automatically (1-year default). Surfacing
    the expiry on the heartbeat lets the Fleet UI render an
    "expires in N days" chip + drives the
    ``k3s_api_cert_expiring`` alert rule at 30 / 7 day thresholds.

    Returns the ISO-8601 UTC timestamp string the backend stores in
    ``Appliance.k3s_api_cert_expires_at``. ``None`` when the cert
    isn't readable (not k3s, no bind mount, file missing, parse
    error). The backend's "only update when not None" semantics
    leave the column untouched in those cases.
    """
    if detect_runtime() != "k3s":
        return None
    for path in _K3S_SERVING_CERT_CANDIDATES:
        if not path.exists():
            continue
        try:
            pem = path.read_bytes()
        except OSError:
            continue
        try:
            # cryptography is already a supervisor dep (cert_auth
            # signs heartbeat bodies with the supervisor's Ed25519
            # key). Lazy-import so non-cert-bearing imports stay
            # fast.
            from cryptography import x509  # noqa: PLC0415

            cert = x509.load_pem_x509_certificate(pem)
        except (ValueError, ImportError):
            return None
        # cert.not_valid_after_utc lands in cryptography 42+; the
        # older naïve-datetime accessor is deprecated but still
        # works as a fallback for forks that pin an older version.
        not_after = getattr(cert, "not_valid_after_utc", None) or getattr(
            cert, "not_valid_after", None
        )
        if not_after is None:
            return None
        if not_after.tzinfo is None:
            from datetime import timezone  # noqa: PLC0415

            not_after = not_after.replace(tzinfo=timezone.utc)
        return not_after.isoformat()
    return None


def read_cluster_health() -> dict[str, object] | None:
    """Issue #183 Phase 4 — local k3s health summary for the
    heartbeat's slow drift channel.

    Probes the local kubeapi for:
      * Node count + how many report ``Ready``
      * Pod count in the ``spatium`` namespace + per-phase breakdown
      * Whether the kubeapi itself reports ``/readyz`` ok

    Returns ``None`` when k3s isn't the runtime (no probe attempted)
    or ``{}`` when probes fail (kubeapi unreachable from a
    runtime-claimed-as-k3s appliance, which itself is signal).

    Pure read-only — never mutates cluster state. Sub-2s total probe
    budget so the heartbeat-collect step stays cheap.
    """
    if detect_runtime() != "k3s":
        return None

    # Lazy import — non-appliance / non-k3s deployments don't load
    # the kubeapi client.
    from . import k8s_api  # noqa: PLC0415

    summary: dict[str, object] = {"kubeapi_ready": False}
    if not k8s_api.check_kubeapi_ready(timeout=1.5):
        return summary
    summary["kubeapi_ready"] = True

    # Node count via the kubeapi list. Each node carries a
    # ``conditions`` array; the ``Ready`` condition's ``status`` is
    # ``True`` when the kubelet says so.
    try:
        status_code, body = k8s_api._request("GET", "/api/v1/nodes")
    except (RuntimeError, OSError):
        return summary
    if status_code == 200:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            data = {}
        nodes = data.get("items") or []
        ready = 0
        for n in nodes:
            for cond in (n.get("status") or {}).get("conditions") or []:
                if cond.get("type") == "Ready" and cond.get("status") == "True":
                    ready += 1
                    break
        summary["nodes_total"] = len(nodes)
        summary["nodes_ready"] = ready

    # Pod count in the spatium namespace, grouped by phase.
    try:
        status_code, body = k8s_api._request("GET", "/api/v1/namespaces/spatium/pods")
    except (RuntimeError, OSError):
        return summary
    if status_code == 200:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            data = {}
        pods = data.get("items") or []
        phase_counts: dict[str, int] = {}
        for p in pods:
            phase = (p.get("status") or {}).get("phase") or "Unknown"
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
        summary["pods_total"] = len(pods)
        summary["pods_by_phase"] = phase_counts
    elif status_code == 404:
        # Namespace doesn't exist yet — no role assigned, no pods.
        summary["pods_total"] = 0
        summary["pods_by_phase"] = {}

    return summary


def read_node_ip() -> str | None:
    """Return this node's k3s-registered InternalIP (#272 Phase 7b).

    The control-plane promote endpoint needs the SEED's real, routable
    node IP to build the ``--server https://<ip>:6443`` join URL handed
    to joiners. ``last_seen_ip`` can't be used: the supervisor heartbeats
    from inside the cluster, so the control plane sees its POD IP
    (10.42.x.x), which a joiner can't reach. The node's InternalIP is the
    address k3s itself uses for the apiserver + etcd, so it's the correct
    join target.

    Looks the local node up by NODE_NAME (downward-API ``spec.nodeName``,
    set in the supervisor DaemonSet) and returns the first ``InternalIP``
    address. ``None`` when not k3s, NODE_NAME is unset, or the probe
    fails — the backend's "only update when not None" semantics then
    leave the column untouched.
    """
    if detect_runtime() != "k3s":
        return None
    node_name = os.environ.get("NODE_NAME") or os.environ.get("APPLIANCE_HOSTNAME")
    if not node_name:
        return None

    from urllib.parse import quote  # noqa: PLC0415

    from . import k8s_api  # noqa: PLC0415

    try:
        status_code, body = k8s_api._request("GET", f"/api/v1/nodes/{quote(node_name)}")
    except (RuntimeError, OSError):
        return None
    if status_code != 200:
        return None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    for addr in (data.get("status") or {}).get("addresses") or []:
        if addr.get("type") == "InternalIP" and addr.get("address"):
            return str(addr["address"])
    return None


# ── Issue #285 Phase 1 — fleet-firewall prerequisites ────────────────


def read_node_ips() -> list[str] | None:
    """Every k3s-registered InternalIP for this node (#285 Phase 1).

    Unlike ``read_node_ip()`` (the first InternalIP, the join-URL
    source), this returns ALL InternalIPs — both families on a dual-stack
    cluster — so the firewall compiler can derive a family-split peer set
    (``/32`` v4 + ``/128`` v6) instead of fabricating a garbage ``/32``
    from a v6 address. Returns ``None`` when not k3s / NODE_NAME unset /
    probe fails / no InternalIP found, so the backend's "only update when
    not None" semantics leave the column alone.
    """
    if detect_runtime() != "k3s":
        return None
    node_name = os.environ.get("NODE_NAME") or os.environ.get("APPLIANCE_HOSTNAME")
    if not node_name:
        return None

    from urllib.parse import quote  # noqa: PLC0415

    from . import k8s_api  # noqa: PLC0415

    try:
        status_code, body = k8s_api._request("GET", f"/api/v1/nodes/{quote(node_name)}")
    except (RuntimeError, OSError):
        return None
    if status_code != 200:
        return None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    ips = [
        str(addr["address"])
        for addr in (data.get("status") or {}).get("addresses") or []
        if addr.get("type") == "InternalIP" and addr.get("address")
    ]
    return ips or None


def _scan_flannel_backend(text: str) -> str | None:
    """Return the ``flannel-backend:`` value from a k3s YAML config body,
    or None when the key isn't present."""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("flannel-backend:"):
            return s.split(":", 1)[1].strip().strip('"').strip("'") or None
    return None


def read_cluster_cidrs() -> tuple[str | None, str | None, str | None]:
    """Parse ``(pod_cidr, service_cidr, dataplane_backend)`` from the k3s
    config (#285 / #302 Phase 1).

    pod/service CIDR come from the ``spatium-cidrs.yaml`` drop-in the
    install wizard writes (``cluster-cidr`` / ``service-cidr``; may be a
    comma-joined dual-stack pair). ``flannel-backend`` is scanned across
    the main ``config.yaml`` + every ``config.yaml.d/*.yaml`` drop-in,
    defaulting to ``vxlan`` (k3s upstream default) when unset.

    Returns ``(None, None, None)`` off k3s. On a k3s appliance the
    backend always resolves (defaults to ``vxlan``); pod/service CIDR may
    still be None on a pre-#302 install with no drop-in, so the backend's
    "only update when not None" semantics leave those columns alone.
    """
    if detect_runtime() != "k3s":
        return None, None, None

    pod_cidr: str | None = None
    service_cidr: str | None = None
    if _K3S_CIDRS_DROPIN.exists():
        try:
            for line in _K3S_CIDRS_DROPIN.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                s = line.strip()
                if s.startswith("cluster-cidr:"):
                    pod_cidr = s.split(":", 1)[1].strip().strip('"').strip("'") or None
                elif s.startswith("service-cidr:"):
                    service_cidr = s.split(":", 1)[1].strip().strip('"').strip("'") or None
        except OSError as exc:
            # Drop-in unreadable (perms / mid-write) — fall through with
            # pod/service CIDR as None (the backend leaves those columns
            # alone). Note it so a recurring bind-mount issue isn't silent.
            log.debug("supervisor.cluster_cidrs.dropin_read_failed", error=str(exc))

    # flannel-backend: main config first, then drop-ins in sorted order;
    # first match wins. Default to the k3s upstream ``vxlan`` when nothing
    # set it explicitly.
    backend: str | None = None
    candidates = [_K3S_MAIN_CONFIG]
    try:
        candidates += sorted(_K3S_CONFIG_DIR.glob("*.yaml"))
    except OSError as exc:
        # config.yaml.d enumeration failed — continue with the main
        # config + the vxlan default rather than erroring.
        log.debug("supervisor.cluster_cidrs.dropin_glob_failed", error=str(exc))
    for path in candidates:
        if not path.exists():
            continue
        try:
            found = _scan_flannel_backend(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if found:
            backend = found
            break
    if backend is None:
        backend = "vxlan"

    return pod_cidr, service_cidr, backend


def read_base_conf_marker() -> tuple[str | None, bool | None]:
    """Hash the live base ``/etc/nftables.conf`` + detect the legacy
    LAN-wide k3s accept (#285 Phase 1).

    Returns ``(sha256_hex, lanwide_k3s_present)``. The boolean is the
    self-describing "is this the legacy LAN-wide base or a hardened one"
    signal that gates the UI/compliance claim; the hash is for generic
    change detection. ``(None, None)`` when the file isn't mounted
    (non-appliance / older chart without the read-only mount), so the
    backend leaves both columns untouched.
    """
    if detect_deployment_kind() != "appliance":
        return None, None
    if not _HOST_NFTABLES_CONF.exists():
        return None, None
    try:
        raw = _HOST_NFTABLES_CONF.read_bytes()
    except OSError:
        return None, None
    marker = hashlib.sha256(raw).hexdigest()
    # The legacy LAN-wide accept carries the literal ``comment "k3s-ha"``
    # tag (see appliance/mkosi.extra/etc/nftables.conf). Its presence ⇒
    # etcd/kubelet are still LAN-wide; its absence ⇒ a hardened base.
    lanwide = 'comment "k3s-ha"' in raw.decode("utf-8", errors="replace")
    return marker, lanwide


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

    last_state, last_state_at = _last_upgrade_state_from_sidecar() if is_appliance else (None, None)

    slot_a_version, slot_b_version = read_slot_versions()
    cluster_health = read_cluster_health() if is_appliance else None
    k3s_version = read_k3s_version() if is_appliance else None
    kubeconfig = read_kubeconfig() if is_appliance else None
    k3s_api_cert_expires_at = read_k3s_api_cert_expiry() if is_appliance else None
    node_ip = read_node_ip() if is_appliance else None

    # Issue #285 Phase 1 — firewall prerequisites.
    node_ips = read_node_ips() if is_appliance else None
    pod_cidr, service_cidr, dataplane_backend = (
        read_cluster_cidrs() if is_appliance else (None, None, None)
    )
    base_conf_marker, base_lanwide_k3s = read_base_conf_marker() if is_appliance else (None, None)

    return {
        "deployment_kind": deployment_kind,
        # #272 Phase 1 — installer-role variant the supervisor is
        # running on. Only meaningful on appliance deploys; None on
        # docker / k8s.
        "appliance_variant": (detect_appliance_variant() if is_appliance else None),
        "installed_appliance_version": (read_installed_version() if is_appliance else None),
        "current_slot": current_slot,
        "durable_default": durable_default,
        "slot_a_version": slot_a_version,
        "slot_b_version": slot_b_version,
        "is_trial_boot": is_trial_boot,
        "last_upgrade_state": last_state,
        "last_upgrade_state_at": last_state_at.isoformat() if last_state_at else None,
        # Issue #153 — surfaces in the Fleet view next to deployment
        # kind so operators see at a glance which appliances actually
        # have snmpd running. None on non-appliance deploys.
        "snmpd_running": read_snmpd_running() if is_appliance else None,
        # Issue #154 — chrony sync state from ``chronyc tracking``,
        # captured by the host-side runner on apply. ``synchronized``
        # = leap status OK + reference set; ``unsynchronized`` = no
        # reference / stratum >= 16; ``unknown`` = chronyc unreadable
        # (transient at boot). None on non-appliance deploys.
        "ntp_sync_state": read_ntp_sync_state() if is_appliance else None,
        # Issue #343 — lldpd running state from the host-side runner's
        # status sidecar. None on non-appliance deploys.
        "lldpd_running": read_lldpd_running() if is_appliance else None,
        # Issue #347 — LLDP neighbours discovered by local lldpd. None when
        # not collected (off-appliance / lldpcli error) so the backend leaves
        # the stored set alone; an empty list means "ran, saw none" (wipe).
        "lldp_neighbours": read_lldp_neighbours() if is_appliance else None,
        # Issue #183 Phase 4 — local k3s health summary. Slow drift
        # signals that ride the heartbeat (fast actions go through
        # the proxy). Empty dict on non-k3s appliances; never None
        # so the backend's "overwrite verbatim" semantics clear stale
        # health when k3s is disabled.
        "cluster_health": cluster_health if cluster_health is not None else {},
        # Issue #183 Phase 5 — operator-facing k3s metadata. The
        # backend rewrites the kubeconfig's ``server:`` field for
        # operator reachability + Fernet-encrypts before persisting.
        # ``None`` when k3s isn't the runtime (backend then leaves
        # the column untouched).
        "k3s_version": k3s_version,
        "kubeconfig": kubeconfig,
        # Issue #183 Phase 6 — k3s server-cert ``Not After``
        # timestamp (ISO-8601 UTC) so the backend can drive expiry
        # alerts. None when not k3s; the backend leaves the column
        # untouched on null.
        "k3s_api_cert_expires_at": k3s_api_cert_expires_at,
        # #272 Phase 7b — control-plane cluster join/leave telemetry.
        # ``k3s_join_token`` only present on the seed; join state from
        # the host runner's .state sidecar. All None on non-appliance.
        "k3s_join_token": read_k3s_join_token() if is_appliance else None,
        "cluster_join_state": (read_cluster_join_state()[0] if is_appliance else None),
        "cluster_join_reason": (read_cluster_join_state()[1] if is_appliance else None),
        # #272 Phase 7b — the node's real routable InternalIP. The
        # promote endpoint builds the k3s join URL from the seed's
        # node_ip; ``last_seen_ip`` is the supervisor POD IP (10.42.x.x),
        # which joiners can't reach. None on non-appliance / non-k3s.
        "node_ip": node_ip,
        # Issue #285 Phase 1 — fleet-firewall prerequisites. All None
        # off-appliance / off-k3s so the backend's "only update when not
        # None" semantics leave the columns alone. Purely additive
        # telemetry — nothing here changes a live firewall yet.
        "node_ips": node_ips,
        "pod_cidr": pod_cidr,
        "service_cidr": service_cidr,
        "dataplane_backend": dataplane_backend,
        "base_conf_marker": base_conf_marker,
        "base_lanwide_k3s": base_lanwide_k3s,
        # Issue #285 Phase 2b — firewall apply-state read-back. None
        # off-appliance so the backend's "only-when-not-None" upsert never
        # blanks the columns. Surfaces what the runner already writes.
        "firewall_applied_hash": read_firewall_applied_hash() if is_appliance else None,
        "firewall_applied_status": read_firewall_applied_status() if is_appliance else None,
        "firewall_base_marker": read_firewall_base_marker() if is_appliance else None,
    }
