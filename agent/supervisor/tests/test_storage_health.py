"""Storage-redundancy collector (#999 Part A).

``read_storage_health`` reads the HOST's ``/proc/mdstat`` + ``/sys/block``
through the supervisor's privileged/hostPID window. These tests build a
fake sysfs tree and point the module constants at it, so the derivation —
which is the part that can be wrong without anybody noticing — runs for
real rather than being asserted structurally.

The case that matters is the one an operator never sees coming: a raid1
that has lost a member reports ``array_state=clean``, because the
remaining member IS consistent. A collector that copied that field
through would report a single point of failure as healthy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spatium_supervisor import appliance_state as st


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _make_md(
    root: Path,
    name: str,
    *,
    level: str = "raid1",
    array_state: str = "clean",
    raid_disks: int = 2,
    members: list[tuple[str, str, str]],
    sync_action: str = "idle",
    sync_completed: str = "none",
    sync_speed: str = "0",
    size_sectors: str = "1953382400",
) -> None:
    """Lay down one ``/sys/block/<name>/md/`` tree.

    ``members`` is ``[(device, state, slot)]`` — the kernel's own
    comma-joined member state, verbatim.
    """
    block = root / name
    _write(block / "size", size_sectors + "\n")
    md = block / "md"
    _write(md / "level", level + "\n")
    _write(md / "array_state", array_state + "\n")
    _write(md / "raid_disks", str(raid_disks) + "\n")
    _write(md / "sync_action", sync_action + "\n")
    _write(md / "sync_completed", sync_completed + "\n")
    _write(md / "sync_speed", sync_speed + "\n")
    for dev, state, slot in members:
        _write(md / f"dev-{dev}" / "state", state + "\n")
        _write(md / f"dev-{dev}" / "slot", slot + "\n")


def _make_mpath(
    root: Path,
    dm_name: str,
    *,
    friendly: str,
    uuid: str,
    paths: list[tuple[str, str | None]],
) -> None:
    """Lay down one ``/sys/block/dm-N/`` multipath map + its path devices.

    ``paths`` is ``[(device, scsi_state_or_None)]``; ``None`` writes no
    ``device/state`` at all, which is what an NVMe path looks like.
    """
    block = root / dm_name
    _write(block / "dm" / "uuid", uuid + "\n")
    _write(block / "dm" / "name", friendly + "\n")
    _write(block / "size", "2097152\n")
    for dev, scsi_state in paths:
        (block / "slaves" / dev).mkdir(parents=True, exist_ok=True)
        if scsi_state is not None:
            _write(root / dev / "device" / "state", scsi_state + "\n")


@pytest.fixture
def sysfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "sys" / "block"
    root.mkdir(parents=True)
    monkeypatch.setattr(st, "_SYS_BLOCK", root)
    monkeypatch.setattr(st, "_PROC_MDSTAT", tmp_path / "proc" / "mdstat")
    return root


# ── the negative: an ordinary single-disk appliance ─────────────────


def test_no_arrays_no_multipath_reports_empty(sysfs: Path) -> None:
    """The common case costs nothing and renders nothing."""
    out = st.read_storage_health()
    assert out == {"md_supported": False, "md_arrays": [], "multipath_maps": []}


def test_md_supported_distinguishes_loaded_from_absent(
    sysfs: Path, tmp_path: Path
) -> None:
    """md loaded with no arrays is NOT the same reading as no md at all.

    Both produce an empty ``md_arrays``; only the first one means the
    absence of arrays is a fact rather than an unavailable reading.
    """
    _write(tmp_path / "proc" / "mdstat", "Personalities : [raid1]\nunused devices: <none>\n")
    out = st.read_storage_health()
    assert out["md_supported"] is True
    assert out["md_arrays"] == []


# ── md derivation ───────────────────────────────────────────────────


def test_healthy_raid1_is_clean_with_redundancy(sysfs: Path) -> None:
    _make_md(
        sysfs,
        "md0",
        members=[("sda1", "in_sync", "0"), ("sdb1", "in_sync", "1")],
    )
    (array,) = st.read_storage_health()["md_arrays"]  # type: ignore[index]
    assert array["state"] == "clean"
    assert array["members_in_sync"] == 2
    assert array["redundancy_remaining"] == 1
    assert array["size_bytes"] == 1953382400 * 512
    assert [m["device"] for m in array["members"]] == ["sda1", "sdb1"]


def test_degraded_raid1_reports_clean_array_state_but_degraded_state(
    sysfs: Path,
) -> None:
    """The whole reason this collector derives rather than copies.

    A raid1 down to one member is internally consistent, so the kernel
    says ``clean``. Reporting that verbatim would show a single point
    of failure as healthy — the exact silence #999 exists to end.
    """
    _make_md(
        sysfs,
        "md0",
        array_state="clean",
        members=[("sda1", "in_sync", "0")],
    )
    (array,) = st.read_storage_health()["md_arrays"]  # type: ignore[index]
    assert array["array_state"] == "clean"
    assert array["state"] == "degraded"
    # One member left, one needed: nothing in reserve.
    assert array["redundancy_remaining"] == 0


def test_faulty_member_counted_and_reported_verbatim(sysfs: Path) -> None:
    _make_md(
        sysfs,
        "md0",
        members=[("sda1", "in_sync", "0"), ("sdb1", "faulty", "none")],
    )
    (array,) = st.read_storage_health()["md_arrays"]  # type: ignore[index]
    assert array["state"] == "degraded"
    assert array["members_faulty"] == 1
    assert array["members_in_sync"] == 1
    # Slotless members sort last, so the good one is still first.
    assert [m["device"] for m in array["members"]] == ["sda1", "sdb1"]


def test_three_way_mirror_missing_one_still_has_redundancy(sysfs: Path) -> None:
    """``2 of 3`` and ``1 of 2`` both report degraded and are not the
    same emergency — which is why severity keys off redundancy."""
    _make_md(
        sysfs,
        "md0",
        raid_disks=3,
        members=[
            ("sda1", "in_sync", "0"),
            ("sdb1", "in_sync", "1"),
            ("sdc1", "faulty", "none"),
        ],
    )
    (array,) = st.read_storage_health()["md_arrays"]  # type: ignore[index]
    assert array["state"] == "degraded"
    assert array["redundancy_remaining"] == 1


def test_raid0_member_loss_is_failed_not_degraded(sysfs: Path) -> None:
    """No redundancy to lose: every member is load-bearing."""
    _make_md(
        sysfs,
        "md0",
        level="raid0",
        members=[("sda1", "in_sync", "0")],
    )
    (array,) = st.read_storage_health()["md_arrays"]  # type: ignore[index]
    assert array["state"] == "failed"
    assert array["min_working_members"] == 2


def test_inactive_array_is_failed(sysfs: Path) -> None:
    _make_md(
        sysfs,
        "md0",
        array_state="inactive",
        members=[("sda1", "in_sync", "0"), ("sdb1", "in_sync", "1")],
    )
    (array,) = st.read_storage_health()["md_arrays"]  # type: ignore[index]
    assert array["state"] == "failed"


def test_failed_to_assemble_array_is_failed_even_with_no_member_count(
    sysfs: Path,
) -> None:
    """An array that never assembled reports ``raid_disks=0`` as well as
    ``array_state=inactive``. Deciding on the count first would demote a
    real failure to a mere "unknown" — so inactive is tested first."""
    _make_md(
        sysfs,
        "md127",
        array_state="inactive",
        raid_disks=0,
        members=[],
    )
    (array,) = st.read_storage_health()["md_arrays"]  # type: ignore[index]
    assert array["state"] == "failed"


def test_imsm_container_is_not_reported_as_an_array(sysfs: Path) -> None:
    """An IMSM / DDF metadata container is permanently ``inactive`` with
    ``raid_disks=0`` on a perfectly healthy box, and the real arrays live
    inside it. Classifying it would open a critical alert nothing can
    ever clear."""
    _make_md(
        sysfs,
        "md127",
        level="container",
        array_state="inactive",
        raid_disks=0,
        members=[],
    )
    # ...while the member array built inside it IS reported.
    _make_md(
        sysfs,
        "md126",
        level="raid1",
        members=[("sda", "in_sync", "0"), ("sdb", "in_sync", "1")],
    )
    arrays = st.read_storage_health()["md_arrays"]
    assert [a["name"] for a in arrays] == ["md126"]  # type: ignore[index,union-attr]


def test_rebuild_progress_and_eta(sysfs: Path) -> None:
    _make_md(
        sysfs,
        "md0",
        array_state="active",
        members=[("sda1", "in_sync", "0"), ("sdb1", "spare", "none")],
        sync_action="recover",
        sync_completed="500 / 1000",
        sync_speed="50",  # KB/s → 100 sectors/s
    )
    (array,) = st.read_storage_health()["md_arrays"]  # type: ignore[index]
    # A recovering mirror has one usable copy — it is degraded until the
    # rebuild finishes, and saying "syncing" would understate that.
    assert array["state"] == "degraded"
    assert array["sync"] == {"action": "recover", "percent": 50.0, "eta_seconds": 5}
    assert array["spares"] == 1


def test_scrub_on_a_healthy_array_is_syncing_not_degraded(sysfs: Path) -> None:
    """A ``check`` scrub touches no redundancy — it must not read as a fault."""
    _make_md(
        sysfs,
        "md0",
        members=[("sda1", "in_sync", "0"), ("sdb1", "in_sync", "1")],
        sync_action="check",
        sync_completed="250 / 1000",
        sync_speed="0",
    )
    (array,) = st.read_storage_health()["md_arrays"]  # type: ignore[index]
    assert array["state"] == "syncing"
    assert array["sync"]["action"] == "check"  # type: ignore[index]
    assert array["sync"]["percent"] == 25.0  # type: ignore[index]
    # sync_speed 0 means no ETA can be computed — absent, never zero.
    assert "eta_seconds" not in array["sync"]  # type: ignore[operator]


def test_unreadable_member_count_is_unknown_not_clean(sysfs: Path) -> None:
    """``raid_disks`` is the denominator every degradation test divides
    by. A 0 standing in for "could not read it" makes all of them false
    and drops the array through to ``clean`` — a green tick with no
    basis. It reports its own state instead."""
    _make_md(
        sysfs,
        "md0",
        members=[("sda1", "in_sync", "0"), ("sdb1", "in_sync", "1")],
    )
    (sysfs / "md0" / "md" / "raid_disks").write_text("garbage\n")
    # `array_state` stays "clean" — the array is running, it just will
    # not say how wide it should be.
    (array,) = st.read_storage_health()["md_arrays"]  # type: ignore[index]
    assert array["state"] == "unknown"
    assert array["members_expected"] is None
    # Not 0 either — a redundancy number nobody can compute is None.
    assert array["redundancy_remaining"] is None
    assert array["min_working_members"] is None


def test_idle_array_reports_no_sync_block(sysfs: Path) -> None:
    _make_md(
        sysfs,
        "md0",
        members=[("sda1", "in_sync", "0"), ("sdb1", "in_sync", "1")],
    )
    (array,) = st.read_storage_health()["md_arrays"]  # type: ignore[index]
    assert "sync" not in array


# ── multipath ───────────────────────────────────────────────────────


def test_unparseable_sync_progress_reports_no_percent(sysfs: Path) -> None:
    """``/sys/block/mdX/size`` is the ARRAY's size, not the per-member
    extent being synced, so it is deliberately NOT used as a fallback
    denominator — it would report a plausible wrong percentage on
    raid5/6. No reading beats a wrong one."""
    _make_md(
        sysfs,
        "md0",
        members=[("sda1", "in_sync", "0"), ("sdb1", "in_sync", "1")],
        sync_action="check",
        sync_completed="none",
    )
    (array,) = st.read_storage_health()["md_arrays"]  # type: ignore[index]
    assert array["sync"] == {"action": "check"}


def test_multipath_map_membership(sysfs: Path) -> None:
    _make_mpath(
        sysfs,
        "dm-0",
        friendly="mpatha",
        uuid="mpath-3600508b400105e210000900000490000",
        paths=[("sdc", "running"), ("sdd", "running")],
    )
    (mp,) = st.read_storage_health()["multipath_maps"]  # type: ignore[index]
    assert mp["name"] == "mpatha"
    assert mp["dm_device"] == "dm-0"
    assert mp["paths_total"] == 2
    assert mp["paths_faulted"] == 0
    # dm's own path verdict needs the device-mapper ioctl (Part B), so
    # every path reports "unknown" — never a fabricated "active".
    assert {p["state"] for p in mp["paths"]} == {"unknown"}  # type: ignore[union-attr]


def test_multipath_offline_path_is_faulted(sysfs: Path) -> None:
    _make_mpath(
        sysfs,
        "dm-0",
        friendly="mpatha",
        uuid="mpath-abc",
        paths=[("sdc", "running"), ("sdd", "offline")],
    )
    (mp,) = st.read_storage_health()["multipath_maps"]  # type: ignore[index]
    assert mp["paths_total"] == 2
    assert mp["paths_faulted"] == 1


def test_multipath_unreadable_scsi_state_is_not_counted_healthy(
    sysfs: Path,
) -> None:
    """An NVMe path has no ``device/state``. Unknown must not inflate a
    clean bill of health, so the count is of DEFINITE faults."""
    _make_mpath(
        sysfs,
        "dm-0",
        friendly="mpatha",
        uuid="mpath-abc",
        paths=[("nvme0n1", None), ("nvme1n1", None)],
    )
    (mp,) = st.read_storage_health()["multipath_maps"]  # type: ignore[index]
    assert mp["paths_faulted"] == 0
    assert [p["device_state"] for p in mp["paths"]] == [None, None]  # type: ignore[union-attr]


def test_non_multipath_dm_device_ignored(sysfs: Path) -> None:
    """An LVM LV or a LUKS mapping is a ``dm-*`` too — only ``mpath-``
    UUIDs are multipath maps."""
    _make_mpath(
        sysfs,
        "dm-0",
        friendly="vg0-lv0",
        uuid="LVM-abcdef",
        paths=[("sda2", "running")],
    )
    assert st.read_storage_health()["multipath_maps"] == []
