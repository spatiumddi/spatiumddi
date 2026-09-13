"""Removable (USB) backup disks on the appliance (#989 item 3).

The single-appliance operator with no NAS and no cloud account backs up
to a USB disk. ``local_volume`` writes to a path inside the api / worker
pods, so until this existed there was no supported way to point one at a
block device: #971 solved the same problem for NFS by speaking the
protocol in USERSPACE, and a block device has no userspace escape hatch.

This module is the ONLY place the desired set is validated and the only
place the reported state is classified, so the heartbeat bundle, the
REST surface, the Fleet UI and the copilot tool cannot disagree about
whether a disk is usable — the #999 ``storage_health`` shape.

Three properties are load-bearing, in descending order of how badly
getting them wrong would hurt:

**The mount must be proven, never assumed.** The failure this whole
feature must never produce is a backup that reports success while
writing to the appliance's own ``/var`` because the disk was ejected,
yanked, or never mounted — an operator with no backups and a green
screen. Three independent things stop that, and they were built in this
order deliberately: the mountpoint directory is ``0500`` root-owned
whenever nothing is mounted on it (the kernel's refusal, which holds
even if every check above it is wrong); the api-side driver refuses a
path under the removable root that is not a live mountpoint; and the
control plane records which node the disk is on so the error names it.

**Propagation is part of the contract, not a chart detail.** A hostPath
volume defaults to PRIVATE mount propagation, under which a mount the
host makes after the pod started is invisible inside it — the pod sees
the empty underlying directory and every write lands on ``/var`` with
nothing reporting a problem. Measured, not assumed: with private
propagation a file written to the host mount is simply absent in the
container. ``mountPropagation: HostToContainer`` on the api, worker and
supervisor mounts is what makes the whole design work.

**The name is operator-chosen and reaches a unit filename.** It becomes
a directory under the removable root AND, escaped, the name of a systemd
``.mount`` unit — so it is validated here against an allowlist, and
again by the host runner, which builds its own argv. Neither side trusts
the other: this side knows who the operator is, and only that side knows
what is on the disk at the moment of the mount.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

#: Where the host mount plane puts removable filesystems, as the api /
#: worker containers see it. Shared with the backup driver's guard —
#: importing it from one place is what stops the two drifting into
#: disagreeing about which paths are removable.
REMOVABLE_ROOT = "/var/lib/spatiumddi/removable"

#: The subdirectory inside each mounted disk that archives are written
#: to. Not the mount root: ext4 carries real ownership and the api runs
#: as uid 1000, so SOMETHING has to be owned by it — and chowning the
#: root of a disk that may hold the operator's other data is ruder than
#: creating one directory on it.
ARCHIVE_SUBDIR = "spatiumddi"

#: Filesystems a removable backup disk may carry, mirroring the
#: supervisor collector and the host runner.
#:
#: vfat is absent for a reason worth stating rather than implying: FAT32
#: caps a single file at 4 GiB, so an estate whose archive grows past
#: that fails mid-run, at the END of a long backup, on a destination
#: that had worked for months.
SUPPORTED_FSTYPES = ("ext4", "exfat")

#: A mount name becomes a directory and an escaped systemd unit
#: filename. Lowercase so two names cannot differ only by case on a
#: case-insensitive filesystem; length-capped because the escaped unit
#: name has to stay inside systemd's own limit with the path prefix on
#: the front.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

#: ext4 gives a real UUID; exFAT gives the 8-hex FAT volume serial
#: (``1234-ABCD``). Both are interpolated into ``What=`` and into a
#: systemd device unit name.
_UUID_RE = re.compile(r"^[A-Za-z0-9-]{4,64}$")


class RemovableError(ValueError):
    """An operator-facing refusal, complete enough to act on."""


def validate_name(name: str) -> str:
    """Normalise + check a mount name, or raise :class:`RemovableError`."""
    candidate = (name or "").strip().lower()
    if not candidate:
        raise RemovableError("a name is required")
    if not _NAME_RE.match(candidate):
        raise RemovableError(
            f"{name!r} is not a usable name — use lowercase letters, digits, "
            "'-' or '_', starting with a letter or digit, at most 32 characters. "
            "The name becomes a directory on the appliance and part of a systemd "
            "unit filename."
        )
    return candidate


def validate_fs_uuid(fs_uuid: str) -> str:
    candidate = (fs_uuid or "").strip()
    if not _UUID_RE.match(candidate):
        raise RemovableError(
            f"{fs_uuid!r} is not a usable filesystem UUID. A disk with no UUID "
            "cannot be mounted reliably — the kernel device name is reassigned "
            "on the next plug."
        )
    return candidate


def validate_fstype(fstype: str) -> str:
    candidate = (fstype or "").strip().lower()
    if candidate not in SUPPORTED_FSTYPES:
        extra = ""
        if candidate in ("vfat", "msdos", "fat32"):
            extra = " FAT32 caps a single file at 4 GiB, which a backup archive can exceed."
        raise RemovableError(
            f"{fstype!r} is not supported — reformat the disk as "
            f"{' or '.join(SUPPORTED_FSTYPES)}.{extra}"
        )
    return candidate


def archive_path(name: str) -> str:
    """The backup destination path for a mount, as the pods see it."""
    return f"{REMOVABLE_ROOT}/{name}/{ARCHIVE_SUBDIR}"


def normalise_desired(entries: Any) -> list[dict[str, Any]]:
    """Validate a desired-mount list, raising on the first bad entry.

    Returns a list carrying only the fields the host runner reads plus
    the operator-facing label — never the whole reported disk row, so a
    field the supervisor starts reporting later cannot silently become
    part of the desired state.
    """
    if not isinstance(entries, list):
        raise RemovableError("the desired mount list must be a list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_uuids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RemovableError("each desired mount must be an object")
        name = validate_name(str(entry.get("name") or ""))
        fs_uuid = validate_fs_uuid(str(entry.get("fs_uuid") or ""))
        fstype = validate_fstype(str(entry.get("fstype") or ""))
        if name in seen:
            raise RemovableError(f"{name!r} is already used by another mount on this node")
        # Two names for one filesystem would race for the same device:
        # systemd would mount it at whichever unit started first and the
        # other would fail, so the operator would see one configured
        # disk that never mounts and no explanation.
        if fs_uuid in seen_uuids:
            raise RemovableError(
                f"the disk with UUID {fs_uuid} is already mounted under another name"
            )
        seen.add(name)
        seen_uuids.add(fs_uuid)
        row: dict[str, Any] = {"name": name, "fs_uuid": fs_uuid, "fstype": fstype}
        label = entry.get("label")
        if isinstance(label, str) and label.strip():
            # Carried for the UI only — never for the mount, because a
            # disk label is set on the DISK and is neither unique nor
            # constrained to a safe character set.
            row["label"] = label.strip()[:64]
        added_at = entry.get("added_at")
        if isinstance(added_at, str) and added_at.strip():
            row["added_at"] = added_at.strip()[:40]
        out.append(row)
    return out


def removable_bundle(desired: Any) -> dict[str, Any]:
    """The per-appliance heartbeat block the supervisor hashes + applies.

    Only the three fields the host runner acts on reach the hash. The
    label and the added-at stamp are operator metadata, and folding them
    in would re-fire an apply — tearing down and remounting a live
    destination — because somebody renamed a disk in the UI.

    **An empty desired set hashes to a real digest, not to ``""``.**
    Every sibling plane uses the empty string for "this feature is
    switched off", and copying that here is wrong in two ways at once,
    because on this plane an empty set is an ordinary, frequent operator
    action (eject) rather than a one-off:

    * ``""`` is also what ``_read_release_state_line`` returns for a
      MISSING applied-hash sidecar, so ``_fire_host_config`` would
      short-circuit and an eject after a failed apply would fire
      nothing at all — leaving the disk mounted on the host forever
      while the UI shows no mounts.
    * ``_write_fire_state`` writes ``f"{hash}\t{attempts}\t{iso}"`` and
      ``_read_fire_state`` does ``.strip()``, which eats a LEADING tab —
      so an empty hash round-trips as ``hash="1"``. That pins the #387
      backoff at one attempt (re-firing every tick forever, the flood
      the guard exists to prevent) and makes a *successful* eject report
      ``retrying`` for the rest of the node's life.

    The ``enabled: False`` flag on the payload's first line is what
    carries the disable semantics to the runner, so nothing downstream
    needs the hash to be empty.
    """
    mounts = normalise_desired(desired or [])
    wire = [{"name": m["name"], "fs_uuid": m["fs_uuid"], "fstype": m["fstype"]} for m in mounts]
    body = json.dumps(wire, sort_keys=True, separators=(",", ":"))
    return {
        "enabled": bool(wire),
        "config_hash": hashlib.sha256(body.encode()).hexdigest(),
        "mounts": wire,
    }


def removable_bundle_safe(desired: Any) -> dict[str, Any]:
    """``removable_bundle``, but never raising and never destructive.

    This renders inside the heartbeat, so it cannot raise: a 500 there
    stops the node converging on the firewall, its roles and its
    upgrades too, because one hand-edited row is malformed.

    **What it must not do is fall back to an empty mount list**, which
    the first draft did. On this plane an empty list is not an inert
    "nothing configured" — it is the instruction that tears down every
    mount the node owns. So a desired set that stops validating (a rule
    tightened in a later release, a restore of an older backup under
    newer rules, a hand-edited row) would silently unmount every
    removable backup disk on that node, with no audit row and no alert.

    Instead it reports the failure and ships ``mounts=None``, which
    ``maybe_fire_removable_reload`` reads as "no instruction" and acts
    on by doing nothing. Stuck-and-visible beats silently-destructive.
    """
    try:
        return removable_bundle(desired)
    except RemovableError as exc:
        return {"enabled": False, "config_hash": "", "mounts": None, "error": str(exc)}


def removable_disk_fields(entry: Any) -> dict[str, Any]:
    """One reported USB disk, coerced field-by-field (#989 item 3).

    Module-level and shared with the copilot's row builder — the
    ``mtu_fields`` pattern, for the same reason its docstring gives: two
    hand-written field lists over one heartbeat blob drift, and these
    two already had (the copilot's omitted the vendor and model an
    operator identifies a disk by, and left ``size_bytes`` uncoerced).

    ``cluster_health`` is stored VERBATIM from the heartbeat with no
    inner-shape validation, so every value has to be coerced before it
    reaches a typed field or one wrong type from a supervisor 500s the
    whole endpoint.
    """
    data = entry if isinstance(entry, dict) else {}
    raw_size = data.get("size_bytes")
    return {
        "device": str(data.get("device") or ""),
        "by_id": str(data.get("by_id") or ""),
        "fs_uuid": str(data.get("fs_uuid") or ""),
        "fstype": str(data.get("fstype") or ""),
        "label": str(data.get("label") or ""),
        "model": str(data.get("model") or ""),
        "vendor": str(data.get("vendor") or ""),
        "serial": str(data.get("serial") or ""),
        "size_bytes": (
            int(raw_size)
            if isinstance(raw_size, (int, float)) and not isinstance(raw_size, bool)
            else None
        ),
        "mounted_at": (str(data["mounted_at"]) if data.get("mounted_at") else None),
        "usable": bool(data.get("usable")),
        "reason": (str(data["reason"]) if data.get("reason") else None),
    }


def report(cluster_health: Any) -> dict[str, Any] | None:
    """The supervisor's reported removable block, or None if unreported.

    None is UNKNOWN and is never the same as "no disks": a supervisor
    too old to look reports nothing, and rendering that as an empty list
    would tell the operator their disk is not plugged in when nobody
    asked.
    """
    if not isinstance(cluster_health, dict):
        return None
    block = cluster_health.get("removable")
    if not isinstance(block, dict):
        return None
    return block


def _coerce_size(value: Any) -> int | None:
    """An int from ``cluster_health``, or None.

    Coerced HERE, at the point the value is pulled out of the verbatim
    heartbeat blob, rather than at each consumer. ``cluster_health`` is
    stored with no inner-shape validation, and ``RemovableMount`` types
    these as ``int | None`` — so a supervisor reporting a float or a
    string would raise ValidationError inside the response and 500 the
    GET, the mount AND the eject at once, leaving the operator unable
    even to eject the mount that is breaking the page.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def merge_state(desired: Any, cluster_health: Any) -> list[dict[str, Any]]:
    """Join the desired set against what the node reports (#989 item 3).

    Each row gets a ``state``:

    ``mounted``    the disk is there and the filesystem is live
    ``waiting``    configured, armed, and the disk is not plugged in
    ``present``    configured, the disk IS plugged in, and it is not
                   mounted — so something failed, not "go find the disk"
    ``blind``      the node cannot read its removable root at all
    ``unreported`` the node has not said (too old, or offline)

    ``waiting`` is deliberately not an error: a removable disk being
    absent is the NORMAL condition for a rotated off-site disk, and
    calling it a failure would either train the operator to ignore the
    field or push them to delete the mount every time they take the disk
    home. ``present`` is the one that IS a fault, and separating the two
    is the whole reason the node reports its disk list as well as its
    mount list — without it the UI says "the disk is not plugged in"
    about a disk sitting in the port.

    Bad entries are skipped INDIVIDUALLY rather than discarding the
    whole list, matching the host runner (which drops one entry and
    keeps its neighbour): one unparseable row must not hide the mounts
    that are working.
    """
    wanted: list[dict[str, Any]] = []
    for entry in desired or []:
        try:
            wanted.extend(normalise_desired([entry]))
        except RemovableError:
            continue
    block = report(cluster_health)
    reported = {}
    if block is not None:
        for row in block.get("mounts") or []:
            if isinstance(row, dict) and row.get("name"):
                reported[str(row["name"])] = row
    disks_by_uuid = {}
    if block is not None:
        for row in block.get("disks") or []:
            if isinstance(row, dict) and row.get("fs_uuid"):
                disks_by_uuid[str(row["fs_uuid"])] = row
    blind = block is not None and not block.get("supported", True)

    out: list[dict[str, Any]] = []
    for row in wanted:
        live = reported.get(row["name"])
        present = row["fs_uuid"] in disks_by_uuid
        if block is None:
            state = "unreported"
        elif live is not None and live.get("mounted"):
            state = "mounted"
        elif blind:
            state = "blind"
        elif present:
            state = "present"
        else:
            state = "waiting"
        out.append(
            {
                **row,
                "state": state,
                "path": archive_path(row["name"]),
                "mountpoint": f"{REMOVABLE_ROOT}/{row['name']}",
                "total_bytes": _coerce_size((live or {}).get("total_bytes")),
                "free_bytes": _coerce_size((live or {}).get("free_bytes")),
                "present": present,
            }
        )
    return out


__all__ = [
    "ARCHIVE_SUBDIR",
    "REMOVABLE_ROOT",
    "SUPPORTED_FSTYPES",
    "RemovableError",
    "archive_path",
    "merge_state",
    "normalise_desired",
    "removable_bundle",
    "removable_bundle_safe",
    "removable_disk_fields",
    "stamp_node_name",
    "report",
    "validate_fs_uuid",
    "validate_fstype",
    "validate_name",
]


async def stamp_node_name(db: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Fill a removable destination's ``node_name`` from the fleet.

    A removable destination is NODE-LOCAL: the disk is plugged into one
    machine, and a backup that lands on another node has to say which
    one rather than reporting only that nothing is mounted. That is the
    third of the three defences the docs promise — and in the first
    draft it was unreachable from the product, because the field was
    documented as "set for you when you pick a removable disk" and
    nothing ever set it. The backup-target form renders ``config_fields``
    generically, so the operator would have had to type a Kubernetes
    node name into a blank box whose own description told them they did
    not have to.

    Derived rather than asked for, because the control plane already
    knows it: exactly one appliance has this mount name in its desired
    set, and that appliance reported its own node name on the heartbeat.
    An operator-supplied value is never overwritten, and an ambiguous or
    unknown name is left empty — a wrong node name refuses every backup,
    so a guess is worse than nothing.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.models.appliance import Appliance  # noqa: PLC0415

    if str(config.get("node_name") or "").strip():
        return config
    path = str(config.get("path") or "")
    root = REMOVABLE_ROOT.rstrip("/") + "/"
    if not path.startswith(root):
        return config
    name = path[len(root) :].split("/", 1)[0]
    if not name:
        return config

    rows = (await db.execute(select(Appliance).where(Appliance.revoked_at.is_(None)))).scalars()
    matches: list[str] = []
    for row in rows:
        names = {
            str(m.get("name"))
            for m in (row.desired_removable_mounts or [])
            if isinstance(m, dict) and m.get("name")
        }
        if name not in names:
            continue
        node = (report(row.cluster_health) or {}).get("node_name")
        if isinstance(node, str) and node.strip():
            matches.append(node.strip())
    if len(set(matches)) == 1:
        return {**config, "node_name": matches[0]}
    return config
