"""DHCP scope deletion lifecycle — #616 / #617 / #618 / #619.

A scope's pools and reservations are cascade children, not independent rows:
the FK is NOT NULL / ON DELETE CASCADE, uniqueness is keyed on the scope, and
Kea renders reservations nested inside the scope's ``subnet4`` stanza. They used
to be treated as a cascade *leaf*, which left them live and un-stamped under a
hidden parent.

Covers:
  * #617 — soft-deleting a scope stamps its pools + statics into the same batch
  * #617 — the read leaks close: statics list, group-wide MAC conflict check
  * #617 — restore brings the scope back with its pools + reservations
  * #617 — a trashed reservation does not hold the (scope, mac) slot
  * #616 — the agentless write-through fires on the soft path, not just permanent
  * #618 — the IPAM mirror is released when reservations are physically destroyed
  * #619 — ``scope_id`` in a create/update body is a 422, not a silent no-op
  * #619 — a GET → edit → PUT round-trip still works (server-owned fields ignored)
  * #619 — a reservation outside its scope's subnet is refused

Review follow-ups (found by adversarially reviewing the above):
  * #616 — a BLOCK delete cascades scopes into the trash; it owes the same push
  * #617 — `scope.pools` / `.statics` are unconditionally filtered, so anything
    inspecting a TRASHED scope's children must query the child model directly.
    The DHCP importer's conflict preview read them and reported 0/0.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dhcp import DHCPPool, DHCPScope, DHCPServer, DHCPServerGroup, DHCPStaticAssignment
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet


async def _make_admin(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"cs-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Cascade Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_scope(
    db: AsyncSession, *, network: str = "10.50.0.0/24", driver: str = "kea"
) -> tuple[DHCPScope, Subnet, DHCPServerGroup]:
    space = IPSpace(name=f"cs-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.0.0.0/8", name="root")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=network, name="lan")
    db.add(subnet)
    await db.flush()

    group = DHCPServerGroup(name=f"cs-{uuid.uuid4().hex[:6]}")
    db.add(group)
    await db.flush()
    db.add(
        DHCPServer(
            name=f"cs-{uuid.uuid4().hex[:6]}",
            driver=driver,
            host="127.0.0.1",
            server_group_id=group.id,
        )
    )
    scope = DHCPScope(
        group_id=group.id,
        subnet_id=subnet.id,
        name="cascade-scope",
        address_family="ipv4",
    )
    db.add(scope)
    await db.flush()
    return scope, subnet, group


# ── #617 — the cascade itself ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scope_soft_delete_stamps_pools_and_statics(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The children ride the scope's deletion batch instead of being orphaned."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope, _subnet, _group = await _make_scope(db_session)

    pool = DHCPPool(
        scope_id=scope.id, start_ip="10.50.0.100", end_ip="10.50.0.200", pool_type="dynamic"
    )
    static = DHCPStaticAssignment(
        scope_id=scope.id, ip_address="10.50.0.10", mac_address="aa:bb:cc:dd:ee:01"
    )
    db_session.add_all([pool, static])
    await db_session.flush()
    # Capture ids as plain values: once the rows are soft-deleted, refreshing an
    # expired ORM instance re-SELECTs through the global filter and finds nothing.
    scope_id, pool_id, static_id = scope.id, pool.id, static.id
    await db_session.commit()

    resp = await client.delete(f"/api/v1/dhcp/scopes/{scope_id}", headers=headers)
    assert resp.status_code == 204, resp.text

    db_session.expire_all()
    scope_row = (
        await db_session.execute(
            select(DHCPScope)
            .where(DHCPScope.id == scope_id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one()
    pool_row = (
        await db_session.execute(
            select(DHCPPool).where(DHCPPool.id == pool_id).execution_options(include_deleted=True)
        )
    ).scalar_one()
    static_row = (
        await db_session.execute(
            select(DHCPStaticAssignment)
            .where(DHCPStaticAssignment.id == static_id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one()

    assert scope_row.deleted_at is not None
    # The whole point: children stamped, and stamped into the SAME batch, so a
    # single restore brings them back together.
    assert pool_row.deleted_at is not None
    assert static_row.deleted_at is not None
    assert pool_row.deletion_batch_id == scope_row.deletion_batch_id
    assert static_row.deletion_batch_id == scope_row.deletion_batch_id


@pytest.mark.asyncio
async def test_trashed_statics_are_hidden_from_default_selects(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The read leak: a bare select(DHCPStaticAssignment) used to serve a trashed
    scope's reservations because the static itself wasn't soft-delete-aware."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope, _subnet, _group = await _make_scope(db_session)
    db_session.add(
        DHCPStaticAssignment(
            scope_id=scope.id, ip_address="10.50.0.11", mac_address="aa:bb:cc:dd:ee:02"
        )
    )
    await db_session.flush()
    scope_id = scope.id
    await db_session.commit()

    deleted = await client.delete(f"/api/v1/dhcp/scopes/{scope_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    # Holding the trashed scope's UUID must not get you its reservations back.
    listed = await client.get(f"/api/v1/dhcp/scopes/{scope_id}/statics", headers=headers)
    assert listed.status_code == 200
    assert listed.json() == []

    db_session.expire_all()
    rows = (
        (
            await db_session.execute(
                select(DHCPStaticAssignment).where(DHCPStaticAssignment.scope_id == scope_id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_trashed_static_does_not_block_mac_reuse(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The nastiest leak: the group-wide MAC conflict check 409'd against a
    reservation in a scope the operator could no longer see."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope, subnet, group = await _make_scope(db_session)
    mac = "aa:bb:cc:dd:ee:03"
    db_session.add(
        DHCPStaticAssignment(scope_id=scope.id, ip_address="10.50.0.12", mac_address=mac)
    )
    await db_session.flush()
    await db_session.commit()

    deleted = await client.delete(f"/api/v1/dhcp/scopes/{scope.id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    # Same group, same subnet, fresh scope — the partial unique index on
    # (group, subnet) released the slot when the old scope was trashed.
    new_scope = DHCPScope(
        group_id=group.id, subnet_id=subnet.id, name="replacement", address_family="ipv4"
    )
    db_session.add(new_scope)
    await db_session.flush()
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/dhcp/scopes/{new_scope.id}/statics",
        headers=headers,
        json={"ip_address": "10.50.0.12", "mac_address": mac},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_restore_brings_back_pools_and_statics(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """One click puts the scope back whole — the real answer to 'I deleted the
    wrong scope'."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope, _subnet, _group = await _make_scope(db_session)
    db_session.add_all(
        [
            DHCPPool(
                scope_id=scope.id,
                start_ip="10.50.0.100",
                end_ip="10.50.0.200",
                pool_type="dynamic",
            ),
            DHCPStaticAssignment(
                scope_id=scope.id, ip_address="10.50.0.13", mac_address="aa:bb:cc:dd:ee:04"
            ),
        ]
    )
    await db_session.flush()
    scope_id = scope.id
    await db_session.commit()

    deleted = await client.delete(f"/api/v1/dhcp/scopes/{scope_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    restore = await client.post(
        f"/api/v1/admin/trash/dhcp_scope/{scope_id}/restore", headers=headers
    )
    assert restore.status_code == 200, restore.text
    # scope + pool + static
    assert restore.json()["restored"] == 3

    db_session.expire_all()
    statics = (
        (
            await db_session.execute(
                select(DHCPStaticAssignment).where(DHCPStaticAssignment.scope_id == scope_id)
            )
        )
        .scalars()
        .all()
    )
    pools = (
        (await db_session.execute(select(DHCPPool).where(DHCPPool.scope_id == scope_id)))
        .scalars()
        .all()
    )
    assert len(statics) == 1
    assert len(pools) == 1
    assert statics[0].deleted_at is None
    assert pools[0].deleted_at is None


@pytest.mark.asyncio
async def test_trash_batch_size_counts_cascade_children(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Blast radius must not under-report: the trash row for a scope carrying a
    pool + a reservation is a batch of 3, not 1."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope, _subnet, _group = await _make_scope(db_session)
    db_session.add_all(
        [
            DHCPPool(
                scope_id=scope.id,
                start_ip="10.50.0.100",
                end_ip="10.50.0.200",
                pool_type="dynamic",
            ),
            DHCPStaticAssignment(
                scope_id=scope.id, ip_address="10.50.0.14", mac_address="aa:bb:cc:dd:ee:05"
            ),
        ]
    )
    await db_session.flush()
    scope_id = scope.id
    await db_session.commit()

    deleted = await client.delete(f"/api/v1/dhcp/scopes/{scope_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    listing = await client.get("/api/v1/admin/trash?type=dhcp_scope", headers=headers)
    assert listing.status_code == 200
    entry = next(i for i in listing.json()["items"] if i["id"] == str(scope_id))
    assert entry["batch_size"] == 3

    # The children themselves must NOT be browsable rows — they'd swamp the list.
    assert all(i["type"] == "dhcp_scope" for i in listing.json()["items"])


# ── #616 — agentless write-through fires on the soft path ─────────────────


@pytest.mark.asyncio
async def test_soft_delete_pushes_scope_removal_to_agentless_members(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleted means deleted on every backend. Kea members converge by dropping
    out of the ConfigBundle; agentless members only converge on an explicit push,
    which used to fire on the permanent path only — and the UI never sends it."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope, _subnet, _group = await _make_scope(db_session, driver="windows_dhcp")
    await db_session.commit()

    pushed: list[str] = []

    async def _fake_push(_db: Any, sc: DHCPScope) -> None:
        pushed.append(str(sc.id))

    monkeypatch.setattr(
        "app.services.dhcp.windows_writethrough.push_scope_delete", _fake_push, raising=True
    )

    resp = await client.delete(f"/api/v1/dhcp/scopes/{scope.id}", headers=headers)
    assert resp.status_code == 204, resp.text
    assert pushed == [str(scope.id)], "soft-delete must remove the scope from agentless members"


# ── #618 — IPAM mirror lifecycle ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_permanent_scope_delete_releases_ipam_mirror(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The reservations go via FK CASCADE, which runs no Python — so the mirror
    has to be released explicitly or the IP is stranded at ``static_dhcp``
    forever, pointing at a row Postgres already dropped."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope, subnet, _group = await _make_scope(db_session)
    # Plain values: attribute access on an expired ORM instance lazy-refreshes,
    # which raises MissingGreenlet under the async session.
    scope_id, subnet_id = scope.id, subnet.id
    await db_session.commit()

    created = await client.post(
        f"/api/v1/dhcp/scopes/{scope_id}/statics",
        headers=headers,
        json={"ip_address": "10.50.0.20", "mac_address": "aa:bb:cc:dd:ee:06", "hostname": "res1"},
    )
    assert created.status_code == 201, created.text

    db_session.expire_all()
    mirror = (
        await db_session.execute(
            select(IPAddress).where(
                IPAddress.subnet_id == subnet_id, IPAddress.address == "10.50.0.20"
            )
        )
    ).scalar_one()
    mirror_id = mirror.id
    assert mirror.status == "static_dhcp"
    assert mirror.static_assignment_id is not None

    resp = await client.delete(f"/api/v1/dhcp/scopes/{scope_id}?permanent=true", headers=headers)
    assert resp.status_code == 204, resp.text

    db_session.expire_all()
    mirror_after = (
        await db_session.execute(select(IPAddress).where(IPAddress.id == mirror_id))
    ).scalar_one_or_none()
    # The mirror ROW is deleted, not just freed to "available" (#623). A persisted
    # "available" row still renders as a visible line in the subnet table, so a
    # former reservation kept showing after its scope was gone. Deleting folds the
    # IP back into a free gap and drops it out of the utilization count.
    assert mirror_after is None


# ── #619 — validation gaps ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_static_rejects_scope_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A reservation cannot be re-pointed. That used to be a silent 200-no-op;
    it is now a 422, so the caller learns the field had no effect."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope, _subnet, _group = await _make_scope(db_session)
    await db_session.commit()

    created = await client.post(
        f"/api/v1/dhcp/scopes/{scope.id}/statics",
        headers=headers,
        json={"ip_address": "10.50.0.30", "mac_address": "aa:bb:cc:dd:ee:07"},
    )
    assert created.status_code == 201, created.text
    static_id = created.json()["id"]

    resp = await client.put(
        f"/api/v1/dhcp/statics/{static_id}",
        headers=headers,
        json={"hostname": "renamed", "scope_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_static_outside_scope_subnet_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Kea renders the reservation nested inside the scope's subnet stanza, so an
    out-of-CIDR reservation would ship structurally invalid config."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope, _subnet, _group = await _make_scope(db_session, network="10.50.1.0/24")
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/dhcp/scopes/{scope.id}/statics",
        headers=headers,
        json={"ip_address": "192.168.99.5", "mac_address": "aa:bb:cc:dd:ee:08"},
    )
    assert resp.status_code == 422, resp.text
    assert "outside the scope's subnet" in resp.json()["detail"]


# ── Review follow-ups ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_block_delete_pushes_scope_removal_to_agentless_members(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#616 one level up: an IPBlock soft-delete cascades Subnet -> DHCPScope into
    the trash, so it owes the agentless write-through exactly as the scope and
    subnet paths do. Wiring only the two obvious paths left this one silent."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope, subnet, _group = await _make_scope(db_session, driver="windows_dhcp")
    scope_id = scope.id
    block_id = subnet.block_id
    await db_session.commit()

    pushed: list[str] = []

    async def _fake_push(_db: Any, sc: DHCPScope) -> None:
        pushed.append(str(sc.id))

    monkeypatch.setattr(
        "app.services.dhcp.windows_writethrough.push_scope_delete", _fake_push, raising=True
    )

    resp = await client.delete(f"/api/v1/ipam/blocks/{block_id}", headers=headers)
    assert resp.status_code == 204, resp.text
    assert pushed == [str(scope_id)], "block delete must remove cascaded scopes from Windows"


@pytest.mark.asyncio
async def test_selectin_children_filter_and_opt_out_both_work(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Pin the loader semantics the whole #617 design leans on.

    ``DHCPScope.pools`` / ``.statics`` are selectin-loaded, so each child load is
    its own ORM execute and the global soft-delete filter is applied to it
    independently. Two properties have to hold, in opposite directions:

      * a renderer must NEVER see a soft-deleted child (or a trashed reservation
        would keep being served), and
      * a blast-radius count / purge pre-pass MUST be able to see one when it
        deliberately opts in, or it would under-report what it is about to
        destroy.

    Under the old ``lazy="joined"`` the first property did not hold: the filter
    registers with ``propagate_to_loaders=False``, so a joined child rode the
    parent's statement straight past it.
    """
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope, _subnet, _group = await _make_scope(db_session)
    db_session.add_all(
        [
            DHCPPool(
                scope_id=scope.id,
                start_ip="10.50.0.100",
                end_ip="10.50.0.200",
                pool_type="dynamic",
            ),
            DHCPStaticAssignment(
                scope_id=scope.id, ip_address="10.50.0.40", mac_address="aa:bb:cc:dd:ee:09"
            ),
        ]
    )
    await db_session.flush()
    scope_id = scope.id
    await db_session.commit()

    deleted = await client.delete(f"/api/v1/dhcp/scopes/{scope_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    # Opted in: the children are visible, so a blast-radius count is honest.
    db_session.expire_all()
    trashed = (
        await db_session.execute(
            select(DHCPScope)
            .where(DHCPScope.id == scope_id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one()
    assert trashed.deleted_at is not None
    assert len(trashed.pools) == 1, "include_deleted must reach the selectin child load"
    assert len(trashed.statics) == 1, "include_deleted must reach the selectin child load"

    # Not opted in: a soft-deleted child is invisible even on a LIVE parent, so
    # nothing that renders config can serve it.
    restored = await client.post(
        f"/api/v1/admin/trash/dhcp_scope/{scope_id}/restore", headers=headers
    )
    assert restored.status_code == 200, restored.text

    db_session.expire_all()
    pool_row = (
        await db_session.execute(select(DHCPPool).where(DHCPPool.scope_id == scope_id))
    ).scalar_one()
    pool_row.deleted_at = datetime.now(UTC)
    pool_row.deletion_batch_id = uuid.uuid4()
    await db_session.commit()

    db_session.expire_all()
    live = (
        await db_session.execute(select(DHCPScope).where(DHCPScope.id == scope_id))
    ).scalar_one()
    assert live.deleted_at is None
    assert list(live.pools) == [], "a soft-deleted child must not reach a live parent's collection"


@pytest.mark.asyncio
async def test_create_static_rejects_scope_id_but_tolerates_round_trip(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A body scope_id must 422 (it means something we won't do), but the common
    GET -> edit -> PUT round-trip must still work: StaticResponse carries id /
    created_at / modified_at, and a blanket extra="forbid" would 422 all of them."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope, _subnet, _group = await _make_scope(db_session)
    scope_id = scope.id
    await db_session.commit()

    # scope_id on CREATE -> 422 (path param owns it)
    rejected = await client.post(
        f"/api/v1/dhcp/scopes/{scope_id}/statics",
        headers=headers,
        json={
            "ip_address": "10.50.0.41",
            "mac_address": "aa:bb:cc:dd:ee:0a",
            "scope_id": str(uuid.uuid4()),
        },
    )
    assert rejected.status_code == 422, rejected.text

    created = await client.post(
        f"/api/v1/dhcp/scopes/{scope_id}/statics",
        headers=headers,
        json={"ip_address": "10.50.0.41", "mac_address": "aa:bb:cc:dd:ee:0a"},
    )
    assert created.status_code == 201, created.text
    row = created.json()

    # Round-trip the response back as an update: server-owned fields are ignored,
    # not rejected.
    row["hostname"] = "renamed"
    row.pop("scope_id")
    updated = await client.put(f"/api/v1/dhcp/statics/{row['id']}", headers=headers, json=row)
    assert updated.status_code == 200, updated.text
    assert updated.json()["hostname"] == "renamed"


@pytest.mark.asyncio
async def test_purge_sweep_releases_ipam_mirror_of_reservations(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The nightly purge is a Core DELETE — no per-row Python — so it has to
    release the reservations' IPAM mirrors itself before wiping them (#618).

    Also the first coverage of ``detach_ipam_for_static`` being reached from a
    task session rather than an HTTP request: it lazily imports the IPAM router
    for ``_sync_dns_record``, which no worker path had exercised before.
    """
    from datetime import timedelta

    from app.models.settings import PlatformSettings
    from app.tasks.trash_purge import _sweep

    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope, subnet, _group = await _make_scope(db_session)
    scope_id, subnet_id = scope.id, subnet.id

    if (await db_session.execute(select(PlatformSettings).limit(1))).scalar_one_or_none() is None:
        db_session.add(PlatformSettings())
    await db_session.commit()

    created = await client.post(
        f"/api/v1/dhcp/scopes/{scope_id}/statics",
        headers=headers,
        json={
            "ip_address": "10.50.0.50",
            "mac_address": "aa:bb:cc:dd:ee:0b",
            "hostname": "purgeme",
        },
    )
    assert created.status_code == 201, created.text

    deleted = await client.delete(f"/api/v1/dhcp/scopes/{scope_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    # Backdate the whole batch past the retention window so the sweep takes it.
    old = datetime.now(UTC) - timedelta(days=90)
    for model in (DHCPScope, DHCPPool, DHCPStaticAssignment):
        rows = (
            (
                await db_session.execute(
                    select(model)
                    .where(model.deleted_at.is_not(None))
                    .execution_options(include_deleted=True)
                )
            )
            .scalars()
            .all()
        )
        for r in rows:
            r.deleted_at = old
    await db_session.commit()

    result = await _sweep()
    assert result["per_type"]["dhcp_static_assignment"] >= 1, result

    db_session.expire_all()
    # The mirror ROW is gone (#623). It's now deleted at soft-delete time, and the
    # purge sweep's own remove pass is defense-in-depth — either way the IP is no
    # longer stranded at ``static_dhcp`` and has folded back into a free gap.
    mirror = (
        await db_session.execute(
            select(IPAddress).where(
                IPAddress.subnet_id == subnet_id, IPAddress.address == "10.50.0.50"
            )
        )
    ).scalar_one_or_none()
    assert mirror is None
