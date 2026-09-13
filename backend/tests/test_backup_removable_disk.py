"""Removable (USB) backup disks — the api-side guard (#989 item 3).

**What this exists to prevent.** ``local_volume.write`` does
``root.mkdir(parents=True, exist_ok=True)`` before it writes. Point one
at a removable mountpoint whose disk is not there and it happily creates
the directory and writes the archive — to the appliance's own ``/var``,
with the run reporting success, the retention sweep pruning happily, and
the operator believing they have backups. That is strictly worse than a
destination that never worked, because it displaced the worry.

Four separate conditions produce it and one test catches all four: a
disk ejected from the Fleet UI, a disk yanked without ejecting, a mount
unit that failed, and a run that landed on a node the disk is not
plugged into. The question is always the same — is this path still on a
live mount under the removable root?

``os.path.ismount`` is the right test from inside a container, and that
was MEASURED against a real kernel rather than assumed: with
``mountPropagation: HostToContainer`` a host mount made after the pod
started is visible and reads as a mountpoint, and after an unmount it
reads as a plain directory again. (With the default private propagation
it is invisible entirely — which is why the chart sets it, and why this
guard would otherwise refuse a perfectly healthy disk.)
"""

from __future__ import annotations

import os

import pytest

from app.services.appliance.removable import (
    REMOVABLE_ROOT,
    RemovableError,
    archive_path,
    merge_state,
    normalise_desired,
    removable_bundle,
    removable_bundle_safe,
)
from app.services.backup.targets.base import BackupDestinationError, DestinationConfigError
from app.services.backup.targets.local_volume import LocalVolumeDestination


@pytest.fixture
def fake_mounts(monkeypatch):
    """Declare which paths are live mountpoints, as the kernel would."""
    live: set[str] = set()

    def _ismount(path):
        return str(path).rstrip("/") in live

    monkeypatch.setattr(os.path, "ismount", _ismount)
    monkeypatch.delenv("NODE_NAME", raising=False)
    return live


def _driver() -> LocalVolumeDestination:
    return LocalVolumeDestination()


# ── the guard ───────────────────────────────────────────────────────
def test_a_mounted_removable_disk_is_allowed(fake_mounts):
    fake_mounts.add(f"{REMOVABLE_ROOT}/usb1")
    cfg = {"path": archive_path("usb1")}
    assert str(_driver()._path(cfg)) == f"{REMOVABLE_ROOT}/usb1/spatiumddi"


def test_an_ejected_disk_is_refused_rather_than_written_to(fake_mounts):
    """The whole point. Nothing is mounted, so the path is an ordinary
    directory on the appliance's own /var — and a write there succeeds."""
    cfg = {"path": archive_path("usb1")}
    with pytest.raises(BackupDestinationError) as exc:
        _driver()._path(cfg)
    assert "no removable disk is mounted" in str(exc.value)
    assert "usb1" in str(exc.value)


def test_the_refusal_names_the_node_when_one_is_configured(fake_mounts):
    cfg = {"path": archive_path("usb1"), "node_name": "ddi2"}
    with pytest.raises(BackupDestinationError) as exc:
        _driver()._path(cfg)
    assert "node ddi2" in str(exc.value)


def test_a_run_on_the_wrong_node_says_where_the_disk_is(fake_mounts, monkeypatch):
    """A removable destination is node-local. Reporting only "nothing is
    mounted" would send the operator to look at the disk, which is fine,
    on a node they are not standing at."""
    fake_mounts.add(f"{REMOVABLE_ROOT}/usb1")
    monkeypatch.setenv("NODE_NAME", "ddi1")
    cfg = {"path": archive_path("usb1"), "node_name": "ddi2"}
    with pytest.raises(BackupDestinationError) as exc:
        _driver()._path(cfg)
    msg = str(exc.value)
    assert "on node ddi1" in msg and "node ddi2" in msg


def test_the_right_node_with_a_live_mount_is_allowed(fake_mounts, monkeypatch):
    fake_mounts.add(f"{REMOVABLE_ROOT}/usb1")
    monkeypatch.setenv("NODE_NAME", "ddi2")
    cfg = {"path": archive_path("usb1"), "node_name": "ddi2"}
    assert _driver()._path(cfg)


def test_an_unknown_node_falls_through_to_the_mountpoint_test(fake_mounts):
    """NODE_NAME is absent on compose and on any pod predating this
    change, so a missing one has to mean "I cannot tell" — the node
    check only ever improves the MESSAGE, and the mountpoint test is
    what actually decides."""
    fake_mounts.add(f"{REMOVABLE_ROOT}/usb1")
    cfg = {"path": archive_path("usb1"), "node_name": "ddi2"}
    assert _driver()._path(cfg)  # allowed: cannot tell, and it IS mounted


def test_the_guard_ignores_ordinary_local_volumes(fake_mounts):
    """A docker-compose install with a plain directory must be
    untouched by any of this."""
    assert _driver()._path({"path": "/var/lib/spatiumddi/backups"})


def test_the_bare_removable_root_is_a_config_error_not_a_missing_disk(fake_mounts):
    """It is where mountpoints live, never a mountpoint itself — so a
    target pointed here could only ever write to /var. Saying "no disk
    is mounted" would send the operator hunting for a disk that is
    plugged in perfectly well."""
    with pytest.raises(DestinationConfigError) as exc:
        _driver()._path({"path": REMOVABLE_ROOT})
    assert "not a destination itself" in str(exc.value)


def test_a_subdirectory_deeper_than_the_archive_dir_still_resolves(fake_mounts):
    """The check walks UP to the nearest mountpoint, so a destination
    pointed at a subfolder of the disk is still recognised as being on
    it."""
    fake_mounts.add(f"{REMOVABLE_ROOT}/usb1")
    assert _driver()._path({"path": f"{REMOVABLE_ROOT}/usb1/spatiumddi/site-a"})


@pytest.mark.asyncio
async def test_test_connection_reports_the_refusal_instead_of_raising(fake_mounts):
    """A test against a target whose disk is out has to come back as a
    failed probe with the reason, not a 500 through the endpoint."""
    result = await _driver().test_connection(config={"path": archive_path("usb1")})
    assert result["ok"] is False
    assert "no removable disk is mounted" in result["error"]


def test_validate_config_does_not_require_the_disk_to_be_present(fake_mounts):
    """``validate_config`` runs at CREATE time too, and a rotated
    off-site disk is legitimately absent then. Refusing there would make
    the destination unusable for the rotation it exists to support."""
    _driver().validate_config({"path": archive_path("usb1")})  # no raise


def test_node_name_must_be_a_string():
    with pytest.raises(DestinationConfigError):
        _driver().validate_config({"path": "/var/lib/x", "node_name": 7})


def test_node_name_is_an_offered_config_field():
    """It has to be in ``config_fields`` or the UI form cannot set it —
    the picker writes it, but an operator editing the target by hand
    would silently lose it."""
    assert "node_name" in {f.name for f in _driver().config_fields}


# ── the desired-set validation ──────────────────────────────────────
def test_a_name_that_would_escape_the_removable_root_is_refused():
    with pytest.raises(RemovableError):
        normalise_desired([{"name": "../../etc", "fs_uuid": "1234-ABCD", "fstype": "exfat"}])


def test_vfat_is_refused_and_says_why():
    with pytest.raises(RemovableError) as exc:
        normalise_desired([{"name": "a", "fs_uuid": "1234-ABCD", "fstype": "vfat"}])
    assert "4 GiB" in str(exc.value)


def test_one_disk_may_not_be_mounted_under_two_names():
    """Two units would race for the same device: systemd mounts it at
    whichever started first and the other fails, so the operator sees a
    configured disk that never mounts and no explanation."""
    with pytest.raises(RemovableError) as exc:
        normalise_desired(
            [
                {"name": "a", "fs_uuid": "1234-ABCD", "fstype": "exfat"},
                {"name": "b", "fs_uuid": "1234-ABCD", "fstype": "exfat"},
            ]
        )
    assert "already mounted under another name" in str(exc.value)


def test_only_the_fields_the_runner_reads_reach_the_hash():
    """A label or an added-at stamp changing must NOT re-fire an apply —
    that would tear down and remount a live destination because somebody
    renamed a disk in the UI."""
    a = removable_bundle([{"name": "u", "fs_uuid": "1234-ABCD", "fstype": "exfat"}])
    b = removable_bundle(
        [
            {
                "name": "u",
                "fs_uuid": "1234-ABCD",
                "fstype": "exfat",
                "label": "renamed",
                "added_at": "2026-01-01T00:00:00Z",
            }
        ]
    )
    assert a["config_hash"] == b["config_hash"]
    assert a["mounts"] == b["mounts"]


def test_an_empty_desired_set_is_disabled_but_still_hashes_to_a_real_digest():
    """An eject must NOT hash to the empty string.

    ``""`` is what a MISSING applied-hash sidecar reads as, so an empty
    hash makes ``_fire_host_config`` short-circuit — an eject after a
    failed apply would fire nothing and leave the disk mounted on the
    host forever. It also corrupts the shared #387 fire-state ledger,
    whose writer emits ``f"{hash}\t{attempts}\t{iso}"`` and whose reader
    ``.strip()``s the leading tab back off, so a successful eject would
    report ``retrying`` for the rest of the node's life.

    ``enabled: False`` is what carries the disable semantics.
    """
    bundle = removable_bundle([])
    assert bundle["enabled"] is False
    assert bundle["mounts"] == []
    assert len(bundle["config_hash"]) == 64  # a real sha256, never ""
    # Deterministic, so an unchanged empty set never re-fires.
    assert bundle["config_hash"] == removable_bundle([])["config_hash"]
    # And distinct from any non-empty set.
    assert (
        bundle["config_hash"]
        != removable_bundle([{"name": "u", "fs_uuid": "1234-ABCD", "fstype": "exfat"}])[
            "config_hash"
        ]
    )


def test_an_unvalidatable_row_never_ships_the_teardown_command():
    """The heartbeat must not raise — but the fallback must not be an
    empty mount list either.

    On this plane an empty list is not inert: it is the instruction that
    unmounts every disk the node owns. So a desired set that stops
    validating (a rule tightened in a later release, a restored older
    backup, a hand-edited row) would silently tear down every removable
    backup disk, with no audit row and no alert. ``mounts=None`` is "no
    instruction", which the supervisor acts on by doing nothing.
    """
    bundle = removable_bundle_safe([{"name": "!!!"}])
    assert bundle["mounts"] is None
    assert bundle["error"]
    # And the strict form still raises, so callers that can report it do.
    with pytest.raises(RemovableError):
        removable_bundle([{"name": "!!!"}])


def test_one_bad_row_does_not_hide_the_working_mounts():
    """``merge_state`` skips bad entries INDIVIDUALLY, matching the host
    runner. Discarding the whole list would blank the UI at the exact
    moment the operator needs to see which disk still works."""
    rows = merge_state(
        [
            {"name": "good", "fs_uuid": "1234-ABCD", "fstype": "exfat"},
            {"name": "!!!bad", "fs_uuid": "", "fstype": "ext4"},
        ],
        _health([{"name": "good", "mounted": True}]),
    )
    assert [r["name"] for r in rows] == ["good"]
    assert rows[0]["state"] == "mounted"


# ── desired ∪ reported ──────────────────────────────────────────────
def _health(mounts=None, node="ddi1"):
    return {
        "removable": {
            "supported": True,
            "node_name": node,
            "disks": [],
            "mounts": mounts or [],
        }
    }


def test_a_disk_the_node_reports_mounted_reads_as_mounted():
    desired = [{"name": "u", "fs_uuid": "1234-ABCD", "fstype": "exfat"}]
    health = _health([{"name": "u", "mounted": True, "total_bytes": 100, "free_bytes": 40}])
    row = merge_state(desired, health)[0]
    assert row["state"] == "mounted"
    assert row["free_bytes"] == 40
    assert row["path"] == archive_path("u")


def test_a_configured_disk_that_is_unplugged_reads_as_waiting_not_failed():
    """A rotated off-site disk is legitimately absent for days. Calling
    that a failure either trains the operator to ignore the field or
    pushes them to delete the mount every time they take the disk home."""
    desired = [{"name": "u", "fs_uuid": "1234-ABCD", "fstype": "exfat"}]
    assert merge_state(desired, _health([]))[0]["state"] == "waiting"


def test_a_node_that_has_not_reported_is_unreported_not_waiting():
    """UNKNOWN is never the same as "the disk is not plugged in": a
    supervisor too old to look reports nothing, and rendering that as
    "waiting" tells the operator to go and check a cable for no reason."""
    desired = [{"name": "u", "fs_uuid": "1234-ABCD", "fstype": "exfat"}]
    assert merge_state(desired, {})[0]["state"] == "unreported"


def test_a_plugged_in_disk_whose_mount_failed_is_present_not_waiting():
    """The distinction the whole disk list exists for.

    ``waiting`` renders as "the disk is not plugged in", which is right
    for a rotated off-site disk and actively misleading for a disk
    sitting in the port whose mount unit failed (dirty exFAT after a
    yank, a reformat, a changed UUID). Without this the UI tells the
    operator to plug in a disk that is already plugged in, while the
    same disk appears in Detected disks directly below.
    """
    desired = [{"name": "u", "fs_uuid": "1234-ABCD", "fstype": "exfat"}]
    health = {
        "removable": {
            "supported": True,
            "node_name": "ddi1",
            "disks": [{"fs_uuid": "1234-ABCD", "usable": True}],
            "mounts": [],
        }
    }
    row = merge_state(desired, health)[0]
    assert row["state"] == "present"
    assert row["present"] is True


def test_a_node_that_cannot_read_its_removable_root_is_blind_not_waiting():
    """``/run/udev`` is a separate mount from the removable root, so a
    node whose hostPath bind is missing still reports a full disk list
    while every mount reads as absent. Rendering that as "your disk is
    not plugged in" sends the operator to check a cable for no reason."""
    desired = [{"name": "u", "fs_uuid": "1234-ABCD", "fstype": "exfat"}]
    health = {
        "removable": {
            "supported": False,
            "node_name": "ddi1",
            "disks": [{"fs_uuid": "1234-ABCD", "usable": True}],
            "mounts": [],
        }
    }
    assert merge_state(desired, health)[0]["state"] == "blind"


@pytest.mark.parametrize("bad", ["12 GB", 1.5, None, True, {"x": 1}])
def test_a_bad_size_from_the_heartbeat_is_nulled_not_raised(bad):
    """``cluster_health`` is stored verbatim with no inner-shape
    validation, and ``RemovableMount`` types these ``int | None`` — so an
    uncoerced value raises ValidationError inside the response and 500s
    the GET, the mount AND the eject at once, leaving the operator
    unable even to eject the mount that is breaking the page."""
    desired = [{"name": "u", "fs_uuid": "1234-ABCD", "fstype": "exfat"}]
    row = merge_state(desired, _health([{"name": "u", "mounted": True, "total_bytes": bad}]))[0]
    assert row["total_bytes"] is None or isinstance(row["total_bytes"], int)
    assert not isinstance(row["total_bytes"], bool)
