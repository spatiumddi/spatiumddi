"""The removable-disk HTTP surface (#989 item 3).

The service layer is covered by ``test_backup_removable_disk.py``; this
file drives the three ROUTES, which is where the allowlist gate, the
audit rows and the row lock live. A code-review pass noted the gap: the
handlers had been exercised by hand and by nothing repeatable.

The load-bearing one is ``test_an_unusable_disk_cannot_be_mounted``. The
reported disk list is the allowlist (#890's rule), so a disk the node
marked unusable — its own root, its ESP, its ``var`` partition, a
filesystem we cannot mount — must be refused even when the request is
crafted by hand rather than clicked in the UI.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.appliance import Appliance
from app.models.audit import AuditLog
from app.models.auth import User

BASE = "/api/v1/appliance/appliances"


async def _admin(db: AsyncSession) -> dict:
    u = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@x.com",
        display_name="T",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(u)
    await db.flush()
    # ``is_effective_superadmin`` touches ``user.groups``, which lazy-loads
    # — and a lazy load inside an async call raises MissingGreenlet rather
    # than returning False.
    await db.refresh(u, ["groups"])
    return {"Authorization": f"Bearer {create_access_token(str(u.id))}"}


def _disk(**over) -> dict:
    row = {
        "device": "/dev/sdb1",
        "by_id": "/dev/disk/by-id/usb-Samsung-part1",
        "fs_uuid": "1234-ABCD",
        "fstype": "exfat",
        "label": "BACKUP",
        "model": "Flash Drive",
        "vendor": "Samsung",
        "serial": "S1",
        "size_bytes": 64_000_000_000,
        "mounted_at": None,
        "usable": True,
        "reason": None,
    }
    row.update(over)
    return row


async def _appliance(db: AsyncSession, *, disks=None, mounts=None, supported=True):
    row = Appliance(
        hostname=f"ddi-{uuid.uuid4().hex[:6]}",
        public_key_der=b"x" * 32,
        public_key_fingerprint=uuid.uuid4().hex * 2,
        state="approved",
        cluster_health={
            "removable": {
                "supported": supported,
                "node_name": "ddi1",
                "disks": disks if disks is not None else [_disk()],
                "mounts": mounts or [],
            }
        },
    )
    db.add(row)
    await db.flush()
    return row


async def test_the_listing_reports_the_node_and_the_path_template(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row = await _appliance(db_session)
    r = await client.get(f"{BASE}/{row.id}/removable", headers=await _admin(db_session))
    assert r.status_code == 200
    body = r.json()
    assert body["reported"] is True
    assert body["node_name"] == "ddi1"
    assert body["root_readable"] is True
    # Served, not re-derived in the UI — one source for the path the
    # operator pastes into a backup destination.
    assert body["path_template"] == "/var/lib/spatiumddi/removable/{name}/spatiumddi"
    assert [d["fs_uuid"] for d in body["disks"]] == ["1234-ABCD"]


async def test_a_node_that_cannot_read_its_root_says_so(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``supported: false`` used to be shipped and read by nobody, so a
    node whose hostPath mount is missing rendered as "no disk plugged
    in" — while /run/udev is a separate mount and the disk list is full."""
    row = await _appliance(db_session, supported=False)
    r = await client.get(f"{BASE}/{row.id}/removable", headers=await _admin(db_session))
    assert r.json()["root_readable"] is False


async def test_node_name_is_never_the_string_none(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``str(None)`` is ``"None"`` and truthy, so ``str(x) or None`` never
    fires. The UI rendered "Disks here are on node None", which an
    operator then copies into a destination and refuses every backup."""
    row = await _appliance(db_session)
    row.cluster_health = {"removable": {"supported": True, "disks": [], "mounts": []}}
    await db_session.flush()
    r = await client.get(f"{BASE}/{row.id}/removable", headers=await _admin(db_session))
    assert r.json()["node_name"] is None


async def test_mounting_records_an_audit_row_and_the_destination_path(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row = await _appliance(db_session)
    h = await _admin(db_session)
    r = await client.post(
        f"{BASE}/{row.id}/removable/mount",
        headers=h,
        json={"fs_uuid": "1234-ABCD", "name": "backup-usb"},
    )
    assert r.status_code == 200, r.text
    mounts = r.json()["mounts"]
    assert [m["name"] for m in mounts] == ["backup-usb"]
    assert mounts[0]["path"] == "/var/lib/spatiumddi/removable/backup-usb/spatiumddi"
    # The disk is in the port and not mounted yet — `present`, not
    # `waiting`, which would read as "go and plug the disk in".
    assert mounts[0]["state"] == "present"
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "appliance.removable.mount")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_an_unusable_disk_cannot_be_mounted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The reported list is the allowlist. Refusing only in the UI would
    leave the appliance's own root partition mountable by hand."""
    row = await _appliance(
        db_session,
        disks=[_disk(usable=False, reason="this is one of the appliance's own partitions")],
    )
    r = await client.post(
        f"{BASE}/{row.id}/removable/mount",
        headers=await _admin(db_session),
        json={"fs_uuid": "1234-ABCD", "name": "nope"},
    )
    assert r.status_code == 422
    assert "own partitions" in r.json()["detail"]


async def test_a_disk_the_node_is_not_reporting_is_a_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row = await _appliance(db_session)
    r = await client.post(
        f"{BASE}/{row.id}/removable/mount",
        headers=await _admin(db_session),
        json={"fs_uuid": "dead-beef", "name": "x"},
    )
    assert r.status_code == 404


@pytest.mark.parametrize("name", ["../../etc", "with space", "", "a" * 33, "-lead"])
async def test_an_unusable_name_is_refused(
    client: AsyncClient, db_session: AsyncSession, name: str
) -> None:
    """The name becomes a directory AND part of a systemd unit filename."""
    row = await _appliance(db_session)
    r = await client.post(
        f"{BASE}/{row.id}/removable/mount",
        headers=await _admin(db_session),
        json={"fs_uuid": "1234-ABCD", "name": name},
    )
    assert r.status_code == 422


async def test_a_node_that_has_not_reported_refuses_the_mount(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row = await _appliance(db_session)
    row.cluster_health = {}
    await db_session.flush()
    r = await client.post(
        f"{BASE}/{row.id}/removable/mount",
        headers=await _admin(db_session),
        json={"fs_uuid": "1234-ABCD", "name": "x"},
    )
    assert r.status_code == 409


async def test_eject_removes_the_mount_and_audits_it(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row = await _appliance(db_session)
    h = await _admin(db_session)
    await client.post(
        f"{BASE}/{row.id}/removable/mount",
        headers=h,
        json={"fs_uuid": "1234-ABCD", "name": "usb1"},
    )
    r = await client.delete(f"{BASE}/{row.id}/removable/usb1", headers=h)
    assert r.status_code == 200
    assert r.json()["mounts"] == []
    # Ejecting again is a 404, not a silent success. The request is made
    # OUTSIDE the assert: `python -O` strips assert statements, so a call
    # inside one silently stops happening and the test passes by not
    # running.
    again = await client.delete(f"{BASE}/{row.id}/removable/usb1", headers=h)
    assert again.status_code == 404
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "appliance.removable.eject")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_the_heartbeat_bundle_never_ships_a_teardown_for_a_bad_row(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An empty mount list is this plane's tear-down command, so a stored
    row that stops validating must not produce one."""
    from app.services.appliance.removable import removable_bundle_safe

    bundle = removable_bundle_safe([{"name": "!!!", "fs_uuid": "x", "fstype": "ext4"}])
    assert bundle["mounts"] is None
    assert bundle["error"]


async def test_a_name_is_normalised_to_lowercase_not_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Load-bearing, not a convenience: the host runner's allowlist has
    no lowercasing step of its own, so if this side ever stopped
    normalising, a name typed in capitals would pass the API and be
    DROPPED by the runner — which now fails the whole apply."""
    row = await _appliance(db_session)
    r = await client.post(
        f"{BASE}/{row.id}/removable/mount",
        headers=await _admin(db_session),
        json={"fs_uuid": "1234-ABCD", "name": "Backup-USB"},
    )
    assert r.status_code == 200
    assert r.json()["mounts"][0]["name"] == "backup-usb"
