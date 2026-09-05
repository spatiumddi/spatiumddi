"""Rolling-upgrade preflight — etcd snapshot freshness (#974).

Every k3s bump before v1.36 was same-minor, where an A/B slot revert IS
the rollback. Across a Kubernetes **minor** it is not: the k3s datastore
lives on the persistent ``/var``, outside the slot, so an older
apiserver would start against a store the newer one has written. The
rollback is slot revert **plus** an etcd restore from before the
upgrade — which makes the age of the newest snapshot part of the
upgrade's safety, and the one input the operator can still act on while
the Start button is unpressed.

The check reads what the seed already reports on every heartbeat
(``Appliance.etcd_snapshots``), so the tests here are about the two
things that make it useful rather than annoying:

* it identifies the seed correctly — including the single-node,
  pre-promote shape where ``cluster_role`` is still NULL, which is most
  appliances; and
* it never returns ``fail``. ``fail`` sets ``can_start=False``, and the
  remedy for a stale snapshot is one ``k3s etcd-snapshot save``, not
  abandoning the run.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appliance import (
    APPLIANCE_STATE_APPROVED,
    CLUSTER_ROLE_MEMBER,
    CLUSTER_ROLE_PRIMARY,
    Appliance,
)
from app.services.upgrades.preflight import check_etcd_snapshot_freshness


def _snap(name: str, age_hours: float) -> dict[str, object]:
    when = datetime.now(UTC) - timedelta(hours=age_hours)
    return {
        "name": name,
        "location": f"/var/lib/rancher/k3s/server/db/snapshots/{name}",
        "size": 4_194_304,
        "created_at": when.isoformat(),
    }


async def _appliance(
    db: AsyncSession,
    *,
    hostname: str,
    cluster_role: str | None = None,
    snapshots: list[dict[str, object]] | None = None,
    k3s_version: str | None = "v1.36.4+k3s1",
    deployment_kind: str = "appliance",
) -> Appliance:
    der = os.urandom(32)
    a = Appliance(
        id=uuid.uuid4(),
        hostname=hostname,
        public_key_der=der,
        public_key_fingerprint=hashlib.sha256(der).hexdigest(),
        state=APPLIANCE_STATE_APPROVED,
        deployment_kind=deployment_kind,
        cluster_role=cluster_role,
        k3s_version=k3s_version,
        etcd_snapshots=snapshots or [],
    )
    db.add(a)
    await db.commit()
    return a


# ── The two verdicts ────────────────────────────────────────────────────


async def test_recent_snapshot_is_ok(db_session: AsyncSession) -> None:
    await _appliance(
        db_session,
        hostname="cp-1",
        cluster_role=CLUSTER_ROLE_PRIMARY,
        snapshots=[_snap("etcd-snapshot-cp-1-1757000000", 1.5)],
    )
    r = await check_etcd_snapshot_freshness()
    assert r.level == "ok"
    assert r.detail["newest"] == "etcd-snapshot-cp-1-1757000000"
    assert r.detail["age_hours"] == 1.5


async def test_stale_snapshot_warns_and_names_the_age(db_session: AsyncSession) -> None:
    """Older than the 6 h cron + an hour of grace."""
    await _appliance(
        db_session,
        hostname="cp-1",
        cluster_role=CLUSTER_ROLE_PRIMARY,
        snapshots=[_snap("etcd-snapshot-cp-1-old", 9)],
    )
    r = await check_etcd_snapshot_freshness()
    assert r.level == "warn"
    assert "9.0 h old" in r.message
    assert "etcd-snapshot save" in r.message


async def test_just_inside_the_grace_window_is_ok(db_session: AsyncSession) -> None:
    """6 h cron + 1 h grace: a snapshot that has only just missed its
    slot, or a heartbeat that has not landed yet, is not a fault."""
    await _appliance(
        db_session,
        hostname="cp-1",
        cluster_role=CLUSTER_ROLE_PRIMARY,
        snapshots=[_snap("etcd-snapshot-cp-1-fresh-enough", 6.5)],
    )
    assert (await check_etcd_snapshot_freshness()).level == "ok"


async def test_empty_inventory_warns(db_session: AsyncSession) -> None:
    await _appliance(db_session, hostname="cp-1", cluster_role=CLUSTER_ROLE_PRIMARY)
    r = await check_etcd_snapshot_freshness()
    assert r.level == "warn"
    assert "no usable etcd snapshot" in r.message


async def test_unparseable_timestamps_read_as_no_snapshot(db_session: AsyncSession) -> None:
    """The inventory is the seed's shape, relayed verbatim. An entry we
    cannot read is not evidence of a snapshot we could restore from."""
    await _appliance(
        db_session,
        hostname="cp-1",
        cluster_role=CLUSTER_ROLE_PRIMARY,
        snapshots=[
            {"name": "broken", "created_at": "not-a-timestamp"},
            {"name": "no-timestamp"},
        ],
    )
    r = await check_etcd_snapshot_freshness()
    assert r.level == "warn"
    assert r.detail["snapshots"] == 2


# ── Finding the seed ────────────────────────────────────────────────────


async def test_single_node_pre_promote_is_the_seed(db_session: AsyncSession) -> None:
    """cluster_role is NULL until a promote, which is most appliances.

    Requiring 'primary' would report "could not identify the etcd seed"
    on the common shape — a warning on every single-node upgrade, which
    is how a check gets ignored.
    """
    await _appliance(
        db_session,
        hostname="solo",
        cluster_role=None,
        snapshots=[_snap("etcd-snapshot-solo-1", 2)],
    )
    r = await check_etcd_snapshot_freshness()
    assert r.level == "ok"
    assert r.detail["seed"] == "solo"


async def test_the_primary_is_read_not_a_member(db_session: AsyncSession) -> None:
    """Members report an empty inventory; only the seed holds snapshots.

    Reading a member would report "no usable snapshot" on a perfectly
    protected cluster.
    """
    await _appliance(db_session, hostname="cp-2", cluster_role=CLUSTER_ROLE_MEMBER)
    await _appliance(
        db_session,
        hostname="cp-1",
        cluster_role=CLUSTER_ROLE_PRIMARY,
        snapshots=[_snap("etcd-snapshot-cp-1-1", 1)],
    )
    r = await check_etcd_snapshot_freshness()
    assert r.level == "ok"
    assert r.detail["seed"] == "cp-1"
    assert r.detail["appliances"] == 2


async def test_two_primaries_warn_instead_of_picking_one(db_session: AsyncSession) -> None:
    """Two rows claiming the seed is a fault in itself.

    Taking whichever the database returned first would report one node's
    inventory as if it were the cluster's — and the wrong one may be the
    one with no snapshots, or with stale ones.
    """
    await _appliance(
        db_session,
        hostname="cp-1",
        cluster_role=CLUSTER_ROLE_PRIMARY,
        snapshots=[_snap("fresh", 1)],
    )
    await _appliance(db_session, hostname="cp-2", cluster_role=CLUSTER_ROLE_PRIMARY)
    r = await check_etcd_snapshot_freshness()
    assert r.level == "warn"
    assert "ambiguous" in r.message
    assert r.detail["primaries"] == ["cp-1", "cp-2"]


async def test_multi_node_without_a_primary_warns(db_session: AsyncSession) -> None:
    """Two members and no primary: the seed cannot be identified, and
    guessing one would report someone else's (empty) inventory as fact."""
    await _appliance(db_session, hostname="cp-1", cluster_role=CLUSTER_ROLE_MEMBER)
    await _appliance(db_session, hostname="cp-2", cluster_role=CLUSTER_ROLE_MEMBER)
    r = await check_etcd_snapshot_freshness()
    assert r.level == "warn"
    assert "identify the etcd seed" in r.message


# ── Clock skew ──────────────────────────────────────────────────────────


async def test_a_future_dated_snapshot_is_reported_as_clock_skew(
    db_session: AsyncSession,
) -> None:
    """A future stamp is the one case where age silently passes.

    ``now - created_at`` goes negative, which is < the staleness bound,
    so an unguarded check calls a meaningless timestamp fresh — the same
    trap #925 hit with the beat heartbeat.
    """
    await _appliance(
        db_session,
        hostname="cp-1",
        cluster_role=CLUSTER_ROLE_PRIMARY,
        snapshots=[_snap("etcd-snapshot-cp-1-future", -3)],
    )
    r = await check_etcd_snapshot_freshness()
    assert r.level == "warn"
    assert "FUTURE" in r.message
    assert "NTP" in r.message


async def test_small_clock_jitter_is_clamped_not_reported(db_session: AsyncSession) -> None:
    """A few seconds of disagreement between two clocks is normal. It
    must not print "-0.0 h old" or trip the skew warning."""
    await _appliance(
        db_session,
        hostname="cp-1",
        cluster_role=CLUSTER_ROLE_PRIMARY,
        snapshots=[_snap("etcd-snapshot-cp-1-just-now", -1 / 60)],  # 1 min ahead
    )
    r = await check_etcd_snapshot_freshness()
    assert r.level == "ok"
    assert r.detail["age_hours"] == 0.0
    assert "-" not in r.message.split("(")[0]


# ── Scoping ─────────────────────────────────────────────────────────────


async def test_no_appliances_is_ok(db_session: AsyncSession) -> None:
    """Docker / plain-k8s control plane: no k3s, no A/B slots, nothing
    this check is about."""
    r = await check_etcd_snapshot_freshness()
    assert r.level == "ok"
    assert r.detail["appliances"] == 0


async def test_non_appliance_rows_are_ignored(db_session: AsyncSession) -> None:
    await _appliance(
        db_session,
        hostname="dockerbox",
        deployment_kind="docker",
        cluster_role=CLUSTER_ROLE_PRIMARY,
    )
    r = await check_etcd_snapshot_freshness()
    assert r.level == "ok"
    assert r.detail["appliances"] == 0


async def test_k3s_versions_ride_along_for_the_minor_comparison(
    db_session: AsyncSession,
) -> None:
    """The check cannot know whether the TARGET crosses a minor — the
    target is a CalVer tag and the k3s it bakes is not known until the
    image boots. Reporting the current versions is what lets the
    operator make that call from the release notes.
    """
    await _appliance(
        db_session,
        hostname="cp-1",
        cluster_role=CLUSTER_ROLE_PRIMARY,
        k3s_version="v1.35.6+k3s1",
        snapshots=[_snap("s", 1)],
    )
    r = await check_etcd_snapshot_freshness()
    assert r.detail["k3s_versions"] == ["v1.35.6+k3s1"]


async def test_never_fails(db_session: AsyncSession) -> None:
    """fail sets can_start=False. The remedy for a stale snapshot is one
    command on the seed, not abandoning the upgrade."""
    await _appliance(db_session, hostname="cp-1", cluster_role=CLUSTER_ROLE_PRIMARY)
    assert (await check_etcd_snapshot_freshness()).level != "fail"
