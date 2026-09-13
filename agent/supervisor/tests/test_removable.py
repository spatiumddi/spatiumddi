"""Removable (USB) backup-disk collector + trigger plane (#989 item 3).

The collector reads the HOST's ``/run/udev/data`` and ``/sys`` through
the supervisor's privileged/hostPID window. These tests build a fake
udev database and sysfs tree and point the module constants at them, so
the classification runs for real rather than being asserted
structurally — the part that can be wrong without anybody noticing is
which disks are marked *usable*, and getting that wrong in the
permissive direction offers the appliance's own root partition as a
backup destination.

The case that matters, and the reason the reserved-label rule exists:
an appliance that BOOTS from USB reports its own root, ESP and STATE
partitions as ``ID_BUS=usb`` with real filesystems on them. Every one of
them would otherwise pass a naive "is it USB and does it have a
filesystem" test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spatium_supervisor import appliance_state as st


def _udev(root: Path, devnum: str, props: dict[str, str], links: list[str] | None = None) -> None:
    """Write one /run/udev/data/b<maj:min> record."""
    root.mkdir(parents=True, exist_ok=True)
    lines = [f"E:{k}={v}" for k, v in props.items()]
    lines += [f"S:{s}" for s in (links or [])]
    (root / f"b{devnum}").write_text("\n".join(lines) + "\n")


def _sysfs(root: Path, kname: str, devnum: str, *, sectors: int = 125045424) -> None:
    """Wire /sys/dev/block/<maj:min> → /sys/class/block/<kname>."""
    block = root / "class" / "block" / kname
    block.mkdir(parents=True, exist_ok=True)
    (block / "size").write_text(f"{sectors}\n")
    (block / "holders").mkdir(exist_ok=True)
    devdir = root / "dev" / "block"
    devdir.mkdir(parents=True, exist_ok=True)
    (devdir / devnum).symlink_to(block)


@pytest.fixture
def fake_host(tmp_path, monkeypatch):
    udev = tmp_path / "udev"
    sysfs = tmp_path / "sys"
    removable = tmp_path / "removable"
    removable.mkdir()
    monkeypatch.setattr(st, "_UDEV_DATA", udev)
    monkeypatch.setattr(st, "_SYS_DEV_BLOCK", sysfs / "dev" / "block")
    monkeypatch.setattr(st, "_REMOVABLE_ROOT", removable)
    return {"udev": udev, "sysfs": sysfs, "removable": removable}


def _read(fake_host, monkeypatch, mounts: dict[str, str] | None = None, host_root=None):
    monkeypatch.setattr(st, "_host_mounted_sources", lambda: mounts or {})
    monkeypatch.setattr(st, "_booted_disk_kname", lambda: None)
    # Point the absolute /sys/class/block lookups at the fake tree.
    real_pathlib_path = st.Path
    sysfs = fake_host["sysfs"]

    def _fake_path(arg, *rest):
        p = real_pathlib_path(arg, *rest)
        s = str(p)
        if s.startswith("/sys/class/block"):
            return sysfs / "class" / "block" / s[len("/sys/class/block/") :]
        return p

    monkeypatch.setattr(st, "Path", _fake_path)
    try:
        return st.read_removable_disks(host_root or fake_host["removable"])
    finally:
        monkeypatch.setattr(st, "Path", real_pathlib_path)


def test_a_plain_usb_exfat_disk_is_usable(fake_host, monkeypatch):
    _udev(
        fake_host["udev"],
        "8:17",
        {
            "ID_BUS": "usb",
            "ID_FS_TYPE": "exfat",
            "ID_FS_UUID": "1234-ABCD",
            "ID_FS_LABEL": "BACKUP",
            "ID_MODEL": "Flash_Drive",
            "ID_VENDOR": "Samsung",
            "ID_SERIAL_SHORT": "S123",
        },
        ["disk/by-id/usb-Samsung_Flash_Drive-0:0-part1", "disk/by-uuid/1234-ABCD"],
    )
    _sysfs(fake_host["sysfs"], "sdb1", "8:17")
    rows = _read(fake_host, monkeypatch)
    assert len(rows) == 1
    row = rows[0]
    assert row["usable"] is True and row["reason"] is None
    assert row["fs_uuid"] == "1234-ABCD"
    assert row["device"] == "/dev/sdb1"
    assert row["by_id"] == "/dev/disk/by-id/usb-Samsung_Flash_Drive-0:0-part1"
    # sysfs sectors are always 512 B regardless of the device's own
    # logical block size — reporting 64 GB as 32 GB would have an
    # operator believe an archive fits when it does not.
    assert row["size_bytes"] == 125045424 * 512
    # Underscores in udev's model/vendor are its escaping, not the name
    # printed on the disk.
    assert row["model"] == "Flash Drive"


def test_the_appliances_own_root_on_a_usb_boot_disk_is_refused(fake_host, monkeypatch):
    """The reason the reserved-label rule exists at all.

    An appliance booted from a USB stick reports its own root partition
    as a removable candidate with a perfectly good ext4 on it. Offering
    it as a backup destination is the worst outcome this feature could
    produce.
    """
    for devnum, label, kname in (("8:2", "root_a", "sda2"), ("8:3", "STATE", "sda3")):
        _udev(
            fake_host["udev"],
            devnum,
            {
                "ID_BUS": "usb",
                "ID_FS_TYPE": "ext4",
                "ID_FS_UUID": f"uuid-{kname}",
                "ID_FS_LABEL": label,
            },
        )
        _sysfs(fake_host["sysfs"], kname, devnum)
    rows = _read(fake_host, monkeypatch)
    assert len(rows) == 2
    assert all(r["usable"] is False for r in rows)
    assert all("appliance's own partitions" in (r["reason"] or "") for r in rows)


def test_a_partlabel_match_is_refused_too(fake_host, monkeypatch):
    """The GPT name and the filesystem label are set independently, so
    a root slot whose filesystem label was changed still has to be
    caught by its partition name."""
    _udev(
        fake_host["udev"],
        "8:2",
        {
            "ID_BUS": "usb",
            "ID_FS_TYPE": "ext4",
            "ID_FS_UUID": "aaaa-bbbb",
            "ID_FS_LABEL": "something-else",
            "ID_PART_ENTRY_NAME": "root_B",
        },
    )
    _sysfs(fake_host["sysfs"], "sda2", "8:2")
    rows = _read(fake_host, monkeypatch)
    assert rows[0]["usable"] is False
    assert "appliance's own partitions" in rows[0]["reason"]


def test_vfat_is_refused_and_says_why(fake_host, monkeypatch):
    """FAT32 caps a single file at 4 GiB, so a backup that outgrows it
    fails at the END of a long run. Refusing up front with the reason
    beats an EFBIG an hour in."""
    _udev(
        fake_host["udev"],
        "8:17",
        {"ID_BUS": "usb", "ID_FS_TYPE": "vfat", "ID_FS_UUID": "AAAA-1111"},
    )
    _sysfs(fake_host["sysfs"], "sdb1", "8:17")
    rows = _read(fake_host, monkeypatch)
    assert rows[0]["usable"] is False
    assert "4 GiB" in rows[0]["reason"]


def test_a_disk_with_no_uuid_is_refused(fake_host, monkeypatch):
    """``What=`` needs something stable: a kernel name is reassigned on
    the next plug, so a UUID-less filesystem has nothing to mount by."""
    _udev(fake_host["udev"], "8:17", {"ID_BUS": "usb", "ID_FS_TYPE": "ext4"})
    _sysfs(fake_host["sysfs"], "sdb1", "8:17")
    rows = _read(fake_host, monkeypatch)
    assert rows[0]["usable"] is False
    assert "no UUID" in rows[0]["reason"]


def test_a_disk_in_use_elsewhere_is_refused_with_its_mountpoint(fake_host, monkeypatch):
    _udev(
        fake_host["udev"],
        "8:17",
        {"ID_BUS": "usb", "ID_FS_TYPE": "ext4", "ID_FS_UUID": "aaaa-bbbb"},
    )
    _sysfs(fake_host["sysfs"], "sdb1", "8:17")
    rows = _read(fake_host, monkeypatch, mounts={"/dev/sdb1": "/mnt/something"})
    assert rows[0]["usable"] is False
    assert "/mnt/something" in rows[0]["reason"]


def test_a_disk_mounted_under_our_own_root_stays_usable(fake_host, monkeypatch):
    """Our own mount must not read as "already mounted elsewhere", or a
    disk would go unusable the moment it started working.

    **Modelled at the PRODUCTION path shape, deliberately.** The first
    version of this test derived both the comparison root and the
    mountinfo path from one fixture value, i.e. it built the single
    world where the container and host namespaces coincide — and so
    could not fail while the shipped code compared a host mountinfo path
    (``/var/lib/spatiumddi/removable/usb1``, which is what
    ``/proc/1/mountinfo`` yields under ``hostPID: true``) against the
    CONTAINER root ``/host-removable``. That comparison can never match,
    so every disk the feature successfully mounted reported
    ``usable: false, "already mounted at …"`` and could not be
    re-mounted.
    """
    _udev(
        fake_host["udev"],
        "8:17",
        {"ID_BUS": "usb", "ID_FS_TYPE": "ext4", "ID_FS_UUID": "aaaa-bbbb"},
    )
    _sysfs(fake_host["sysfs"], "sdb1", "8:17")
    here = "/var/lib/spatiumddi/removable/usb1"
    rows = _read(
        fake_host,
        monkeypatch,
        mounts={"/dev/sdb1": here},
        host_root=st._REMOVABLE_HOST_ROOT,
    )
    assert rows[0]["usable"] is True, rows[0]["reason"]
    assert rows[0]["mounted_at"] == here


def test_the_host_and_container_roots_are_different_values(monkeypatch):
    """Pinned because conflating them is silent and catastrophic: the
    read root is this container's bind, the comparison root is the same
    directory as host init names it."""
    assert st._REMOVABLE_ROOT != st._REMOVABLE_HOST_ROOT
    assert str(st._REMOVABLE_HOST_ROOT) == "/var/lib/spatiumddi/removable"


def test_the_appliances_var_partition_is_refused(fake_host, monkeypatch):
    """The one the first draft missed, and the worst to miss.

    ``spatium-install`` labels partition 6 ``var`` (both the GPT name and
    the ext4 label); it is the whole remaining disk and holds PostgreSQL,
    the container images and /var/lib/spatiumddi itself. Mounting it
    under the removable root binds the same superblock twice, so archives
    land on the appliance's own /var while ``ismount`` passes, the 0500
    guard passes and the run reports success — every advertised defence
    green. Asserted with mountinfo EMPTY, because the mount check is
    documented best-effort and fails open.
    """
    _udev(
        fake_host["udev"],
        "8:6",
        {
            "ID_BUS": "usb",
            "ID_FS_TYPE": "ext4",
            "ID_FS_UUID": "cafe-1234",
            "ID_FS_LABEL": "var",
        },
    )
    _sysfs(fake_host["sysfs"], "sda6", "8:6")
    rows = _read(fake_host, monkeypatch)
    assert rows[0]["usable"] is False
    assert "appliance's own partitions" in rows[0]["reason"]


def test_the_esp_is_refused_for_being_ours_not_for_being_vfat(fake_host, monkeypatch):
    """Ordering matters more than it looks. The ESP is vfat with
    PARTLABEL ``esp``; answering the filesystem question first tells the
    operator of a USB-booted appliance to "reformat as exfat, ext4" —
    an instruction to reformat the partition the box boots from,
    rendered next to a Mount button."""
    _udev(
        fake_host["udev"],
        "8:1",
        {
            "ID_BUS": "usb",
            "ID_FS_TYPE": "vfat",
            "ID_FS_UUID": "1111-2222",
            "ID_PART_ENTRY_NAME": "ESP",
        },
    )
    _sysfs(fake_host["sysfs"], "sda1", "8:1")
    rows = _read(fake_host, monkeypatch)
    assert rows[0]["usable"] is False
    assert "appliance's own partitions" in rows[0]["reason"]
    assert "reformat" not in rows[0]["reason"]


def test_non_usb_and_partition_table_parents_are_not_candidates(fake_host, monkeypatch):
    # An internal SATA disk.
    _udev(
        fake_host["udev"],
        "8:1",
        {"ID_BUS": "ata", "ID_FS_TYPE": "ext4", "ID_FS_UUID": "cccc-dddd"},
    )
    _sysfs(fake_host["sysfs"], "sda1", "8:1")
    # The USB disk's partition-table PARENT — no filesystem of its own.
    _udev(
        fake_host["udev"],
        "8:16",
        {"ID_BUS": "usb", "ID_PART_TABLE_TYPE": "gpt", "ID_MODEL": "Flash_Drive"},
    )
    _sysfs(fake_host["sysfs"], "sdb", "8:16")
    assert _read(fake_host, monkeypatch) == []


def test_mount_state_reports_mounted_vs_not(fake_host, monkeypatch, tmp_path):
    """``read_removable_mounts`` decides "mounted" with the same test the
    backup driver applies from inside the api pod, so the Fleet UI and
    the destination cannot disagree about whether the disk is there."""
    root = fake_host["removable"]
    (root / "usb1").mkdir()
    (root / "usb2").mkdir()
    monkeypatch.setattr(st.os.path, "ismount", lambda p: str(p).endswith("usb1"))
    monkeypatch.setattr(
        st.os,
        "statvfs",
        lambda p: type("S", (), {"f_blocks": 1000, "f_frsize": 4096, "f_bavail": 250})(),
    )
    rows = {r["name"]: r for r in st.read_removable_mounts(root)}
    assert rows["usb1"]["mounted"] is True
    assert rows["usb1"]["total_bytes"] == 1000 * 4096
    assert rows["usb1"]["free_bytes"] == 250 * 4096
    assert rows["usb2"]["mounted"] is False
    # No sizes on an unmounted one — a zero would read as a full disk.
    assert "free_bytes" not in rows["usb2"]


def test_supported_false_when_the_root_is_absent(tmp_path, monkeypatch):
    """A node that cannot look must be distinguishable from one with no
    disks. Without this the Fleet UI renders both as an empty, healthy-
    looking list."""
    monkeypatch.setattr(st, "_UDEV_DATA", tmp_path / "nope")
    state = st.read_removable_state(tmp_path / "missing")
    assert state["supported"] is False
    assert state["disks"] == [] and state["mounts"] == []


# ── the desired-state trigger half ──────────────────────────────────


def test_the_trigger_is_appliance_only(monkeypatch, tmp_path):
    """The host-side units do not exist on docker / k8s deploys, so
    firing there leaves a file nothing will ever consume."""
    monkeypatch.setattr(st, "detect_deployment_kind", lambda: "k8s")
    assert st.maybe_fire_removable_reload({"config_hash": "x", "mounts": [{"name": "a"}]}) is False


def test_the_trigger_payload_is_marker_hash_then_json(monkeypatch, tmp_path):
    trigger = tmp_path / "removable-config-pending"
    monkeypatch.setattr(st, "detect_deployment_kind", lambda: "appliance")
    monkeypatch.setattr(st, "_REMOVABLE_TRIGGER_FILE", trigger)
    monkeypatch.setattr(st, "_REMOVABLE_HASH_SIDECAR", tmp_path / "removable-config-hash")
    mounts = [{"name": "usb1", "fs_uuid": "1234-ABCD", "fstype": "exfat"}]
    assert st.maybe_fire_removable_reload({"config_hash": "abc", "mounts": mounts}) is True
    lines = trigger.read_text().splitlines()
    assert lines[0] == "enabled"
    assert lines[1] == "abc"
    assert json.loads("\n".join(lines[2:]))["mounts"] == mounts


def test_an_empty_desired_set_fires_a_disable_trigger(monkeypatch, tmp_path):
    """An empty set is how an EJECT reaches the host — it is not an
    off-switch for the feature. If this fired nothing, ejecting a disk
    from the UI would leave it mounted on the node forever.

    Fires with NO applied-hash sidecar present, which is the case the
    first draft could not handle: the empty set used to hash to ``""``,
    which is also what a missing sidecar reads as, so an eject after a
    failed apply short-circuited and did nothing at all.
    """
    trigger = tmp_path / "removable-config-pending"
    sidecar = tmp_path / "removable-config-hash"
    monkeypatch.setattr(st, "detect_deployment_kind", lambda: "appliance")
    monkeypatch.setattr(st, "_REMOVABLE_TRIGGER_FILE", trigger)
    monkeypatch.setattr(st, "_REMOVABLE_HASH_SIDECAR", sidecar)
    assert not sidecar.exists()
    empty_hash = "e" * 64
    assert st.maybe_fire_removable_reload({"config_hash": empty_hash, "mounts": []}) is True
    assert trigger.read_text().splitlines()[0] == "disabled"


def test_a_null_mount_list_is_no_instruction_not_a_teardown(monkeypatch, tmp_path):
    """The control plane sends ``mounts: None`` when the stored desired
    set could not be validated. An empty LIST is this plane's teardown
    command, so treating the two alike would have one unparseable row
    silently unmount every backup disk on the node."""
    trigger = tmp_path / "removable-config-pending"
    monkeypatch.setattr(st, "detect_deployment_kind", lambda: "appliance")
    monkeypatch.setattr(st, "_REMOVABLE_TRIGGER_FILE", trigger)
    monkeypatch.setattr(st, "_REMOVABLE_HASH_SIDECAR", tmp_path / "h")
    assert st.maybe_fire_removable_reload({"config_hash": "", "mounts": None}) is False
    assert not trigger.exists()


def test_an_empty_config_hash_never_fires(monkeypatch, tmp_path):
    """An empty hash is what a MISSING sidecar reads as, so acting on it
    corrupts the shared fire-state ledger (its writer emits a leading
    tab that the reader strips back off, turning the attempt count into
    the hash). A control plane too old to send a real digest gets
    nothing rather than something wrong."""
    trigger = tmp_path / "removable-config-pending"
    monkeypatch.setattr(st, "detect_deployment_kind", lambda: "appliance")
    monkeypatch.setattr(st, "_REMOVABLE_TRIGGER_FILE", trigger)
    monkeypatch.setattr(st, "_REMOVABLE_HASH_SIDECAR", tmp_path / "h")
    mounts = [{"name": "u", "fs_uuid": "1234-ABCD", "fstype": "exfat"}]
    assert st.maybe_fire_removable_reload({"config_hash": "", "mounts": mounts}) is False
    assert not trigger.exists()


def test_an_unchanged_hash_does_not_re_fire(monkeypatch, tmp_path):
    trigger = tmp_path / "removable-config-pending"
    sidecar = tmp_path / "removable-config-hash"
    sidecar.write_text("abc\n")
    monkeypatch.setattr(st, "detect_deployment_kind", lambda: "appliance")
    monkeypatch.setattr(st, "_REMOVABLE_TRIGGER_FILE", trigger)
    monkeypatch.setattr(st, "_REMOVABLE_HASH_SIDECAR", sidecar)
    assert st.maybe_fire_removable_reload({"config_hash": "abc", "mounts": [{"x": 1}]}) is False
    assert not trigger.exists()


def test_the_plane_is_registered_for_apply_health():
    """A removable mount that keeps failing to apply is exactly the case
    where the operator sees a configured disk in the UI and a backup
    that never lands, so the plane has to be on the reported list."""
    assert "removable" in {name for name, _, _ in st._HOST_CONFIG_PLANES}
