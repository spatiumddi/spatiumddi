"""Rolling-upgrade preflight — Kea HA version skew (#637).

Kea 3.0's HA hook is wire-incompatible with peers older than 2.7, so an HA pair
cannot be upgraded node-at-a-time. ``check_kea_ha_version_skew`` surfaces that to
the operator before they press Start.

The tests that matter here are the *scoping* ones. The check is only useful if it
fires for exactly the servers this upgrade will touch, and for exactly the groups
that really are in HA:

* Docker / k8s DHCP agents are never cordoned, drained or slot-swapped by the
  orchestrator — they upgrade through the manual copy-paste path. Counting them
  would let two unrelated containers veto an appliance-cluster upgrade.
* HA is not "≥ 2 Kea members". ``_resolve_failover`` renders the HA hook only
  when every member ALSO has a non-empty ``ha_peer_url``. Without one there is no
  HA relationship and nothing for an upgrade to disrupt.

And the check must never return ``fail``: that sets ``can_start=False``, which
would strand an already-broken pair in its broken state — finishing the upgrade
is precisely what converges both members onto one version.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import DHCPServer, DHCPServerGroup
from app.services.upgrades.preflight import check_kea_ha_version_skew


async def _group(
    db: AsyncSession,
    members: list[tuple[str | None, str, str]],
) -> str:
    """Create a group whose members are (kea_version, deployment_kind, ha_peer_url)."""
    name = f"grp-{uuid.uuid4().hex[:8]}"
    g = DHCPServerGroup(name=name)
    db.add(g)
    await db.flush()
    for kea_version, deployment_kind, peer_url in members:
        db.add(
            DHCPServer(
                name=f"kea-{uuid.uuid4().hex[:6]}",
                driver="kea",
                host="127.0.0.1",
                port=67,
                server_group_id=g.id,
                kea_version=kea_version,
                deployment_kind=deployment_kind,
                ha_peer_url=peer_url,
            )
        )
    await db.commit()
    return name


_URL = "http://peer:8000/"


async def test_docker_pair_is_ignored(db_session: AsyncSession) -> None:
    """The orchestrator never touches docker agents. A mixed-version docker pair
    must not veto an appliance upgrade — the original bug set can_start=False."""
    name = await _group(db_session, [("2.6.5", "docker", _URL), ("3.0.3", "docker", _URL)])
    r = await check_kea_ha_version_skew()
    assert r.level == "ok"
    assert name not in r.detail.get("mixed_major", [])


async def test_k8s_pair_is_ignored(db_session: AsyncSession) -> None:
    await _group(db_session, [("2.6.5", "kubernetes", _URL), ("2.6.5", "kubernetes", _URL)])
    r = await check_kea_ha_version_skew()
    assert r.level == "ok"


async def test_unreported_deployment_kind_is_ignored(db_session: AsyncSession) -> None:
    """deployment_kind NULL = hasn't checked in. Strict 'appliance' match only —
    an 'appliance OR NULL' fallback would lie about a row we know nothing about."""
    await _group(db_session, [("2.6.5", None, _URL), ("2.6.5", None, _URL)])
    r = await check_kea_ha_version_skew()
    assert r.level == "ok"


async def test_two_kea_members_without_peer_url_are_not_an_ha_pair(
    db_session: AsyncSession,
) -> None:
    """The core scoping bug: ≥2 Kea members is only HALF of _resolve_failover's
    predicate. With no ha_peer_url the HA hook is never rendered, so there is no
    pair to disrupt — flagging one would block an upgrade over nothing."""
    await _group(db_session, [("2.6.5", "appliance", ""), ("3.0.3", "appliance", "")])
    r = await check_kea_ha_version_skew()
    assert r.level == "ok"


async def test_partial_peer_url_is_not_an_ha_pair(db_session: AsyncSession) -> None:
    """_resolve_failover requires EVERY member to have a URL, not just one."""
    await _group(db_session, [("2.6.5", "appliance", _URL), ("2.6.5", "appliance", "")])
    r = await check_kea_ha_version_skew()
    assert r.level == "ok"


async def test_single_appliance_member_is_not_an_ha_pair(db_session: AsyncSession) -> None:
    await _group(db_session, [("2.6.5", "appliance", _URL)])
    r = await check_kea_ha_version_skew()
    assert r.level == "ok"


async def test_pre_3_appliance_pair_warns(db_session: AsyncSession) -> None:
    name = await _group(db_session, [("2.6.5", "appliance", _URL), ("2.6.5", "appliance", _URL)])
    r = await check_kea_ha_version_skew()
    assert r.level == "warn"
    assert name in r.detail["pre_3_0"]


async def test_mixed_major_warns_but_never_fails(db_session: AsyncSession) -> None:
    """An already-mixed pair has broken HA right now — but blocking the upgrade
    (fail → can_start=False) would strand it there. Completing the run is what
    converges both members, so this must be a warn."""
    name = await _group(db_session, [("2.6.5", "appliance", _URL), ("3.0.3", "appliance", _URL)])
    r = await check_kea_ha_version_skew()
    assert r.level == "warn"
    assert r.level != "fail"
    assert name in r.detail["mixed_major"]


async def test_all_on_3_is_ok(db_session: AsyncSession) -> None:
    """Self-clearing: once every member is on 3.x the check goes quiet, so it is
    not a permanent nag on every future upgrade."""
    await _group(db_session, [("3.0.3", "appliance", _URL), ("3.0.3", "appliance", _URL)])
    r = await check_kea_ha_version_skew()
    assert r.level == "ok"


async def test_unknown_and_pre_3_are_mutually_exclusive(db_session: AsyncSession) -> None:
    """A group with one silent member and one pre-3.0 member must appear under
    exactly ONE heading — the original code appended it to both, so the detail a
    UI renders contradicted itself."""
    name = await _group(db_session, [("2.6.5", "appliance", _URL), (None, "appliance", _URL)])
    r = await check_kea_ha_version_skew()
    assert r.level == "warn"
    headings = [
        h for h in ("mixed_major", "pre_3_0", "unknown_version") if name in r.detail.get(h, [])
    ]
    assert headings == ["unknown_version"], headings
