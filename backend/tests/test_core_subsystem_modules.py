"""#1068 — ``core.dns`` / ``core.dhcp``: turning a whole subsystem off.

Three properties are load-bearing here, and each one is a way the naive
implementation goes wrong:

1. The gate must NOT reach the agent routers. ``require_module`` answers
   404, and 404 is the status that makes an agent discard its JWT and
   re-bootstrap from its PSK. Gating the agent path would not disable a
   fleet — it would put every live agent into a re-bootstrap loop.
2. A module is only enabled if its ``requires`` chain is. A child left
   independently on under a disabled parent keeps a router mounted and a
   sidebar row visible, pointing at a subsystem the operator turned off.
3. Disabling is refused while the subsystem still owns rows. Since the
   agents keep running either way, the alternative is live state hidden
   behind a 404 with no way to reach it from the UI.

Every test states the disabled module with a ``FeatureModule`` row rather
than leaning on a shipped default — defaults are product policy and get
revised (#1069), and the suite patches them all to enabled anyway.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.feature_module import FeatureModule
from app.services import feature_modules

pytestmark = pytest.mark.asyncio


async def _superadmin(db: AsyncSession) -> tuple[User, str]:
    u = User(
        username=f"a-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:6]}@x.com",
        display_name="Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(u)
    await db.flush()
    return u, create_access_token(str(u.id))


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _disable(db: AsyncSession, module_id: str) -> None:
    db.add(FeatureModule(id=module_id, enabled=False))
    await db.flush()
    feature_modules.invalidate_cache()


# ── 1. The operator surface goes away ────────────────────────────────


# Real, parameter-free GET routes behind each core gate, one per router
# include so a gate dropped from any single include is caught.
#
# These MUST be paths that exist — ``test_every_gated_path_is_a_real_route``
# below pins that. The first cut of this file used /dhcp/scopes and
# /dns/zones, neither of which is a route (scopes hang off a server group,
# zones off a DNS group), so the 404 assertions passed while testing
# nothing at all. The negative control caught it; the guard makes it
# impossible to reintroduce.
GATED_PATHS: list[tuple[str, str]] = [
    ("core.dhcp", "/api/v1/dhcp/servers"),
    ("core.dhcp", "/api/v1/dhcp/server-groups"),
    ("core.dhcp", "/api/v1/dhcp/leases"),
    ("core.dns", "/api/v1/dns/groups"),
    ("core.dns", "/api/v1/dns/blocklists"),
    ("core.dns", "/api/v1/dns/dnssec-policies"),
]


def test_every_gated_path_is_a_real_route() -> None:
    """A path that does not exist answers 404 for every reason at once.

    Without this, a typo in ``GATED_PATHS`` turns the gate tests into
    tests of nothing while they stay green.
    """
    from app.main import create_app

    documented = set(create_app().openapi()["paths"])
    for _module_id, path in GATED_PATHS:
        assert path in documented, (
            f"{path} is not a documented route, so asserting 404 on it proves "
            "nothing about the feature-module gate."
        )


@pytest.mark.parametrize(("module_id", "path"), GATED_PATHS)
async def test_operator_surface_404s_when_the_subsystem_is_off(
    client: AsyncClient, db_session: AsyncSession, module_id: str, path: str
) -> None:
    _u, token = await _superadmin(db_session)
    await _disable(db_session, module_id)
    await db_session.commit()

    resp = await client.get(path, headers=_hdr(token))
    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize(("module_id", "path"), GATED_PATHS)
async def test_operator_surface_is_reachable_while_the_subsystem_is_on(
    client: AsyncClient, db_session: AsyncSession, module_id: str, path: str
) -> None:
    """Negative control — proves the 404 above comes from the gate and not
    from the route being absent or the fixture being wrong."""
    _u, token = await _superadmin(db_session)
    await db_session.commit()

    resp = await client.get(path, headers=_hdr(token))
    assert resp.status_code != 404, resp.text


# ── 2. …but the fleet does not ───────────────────────────────────────


@pytest.mark.parametrize(
    ("module_id", "path"),
    [
        ("core.dhcp", "/api/v1/dhcp/agents/register"),
        ("core.dhcp", "/api/v1/dhcp/agents/config"),
        ("core.dhcp", "/api/v1/dhcp/agents/heartbeat"),
        ("core.dns", "/api/v1/dns/agents/register"),
        ("core.dns", "/api/v1/dns/agents/config"),
        ("core.dns", "/api/v1/dns/agents/heartbeat"),
    ],
)
async def test_agent_endpoints_never_404_when_the_subsystem_is_off(
    client: AsyncClient, db_session: AsyncSession, module_id: str, path: str
) -> None:
    """The whole reason the agent routers are mounted separately.

    A 404 here makes an agent throw its JWT away and re-bootstrap from the
    PSK, forever, against a surface that keeps answering 404. Whatever the
    unauthenticated response is (401 / 403 / 422), it must not be 404.
    """
    await _disable(db_session, module_id)
    await db_session.commit()

    for resp in (await client.get(path), await client.post(path, json={})):
        assert resp.status_code != 404, f"{path} → 404 with {module_id} off: {resp.text}"


# ── 3. Children follow their parent ──────────────────────────────────


@pytest.mark.parametrize(
    ("parent", "child"),
    [
        ("core.dhcp", "dhcp.import"),
        ("core.dhcp", "ipv6.router_advertisements"),
        ("core.dns", "dns.import"),
        ("core.dns", "dns.dynamic_update_acl"),
        ("core.dns", "security.dns_threat"),
        ("core.dns", "security.dnsbl"),
    ],
)
async def test_a_child_resolves_off_when_its_parent_is_off(
    db_session: AsyncSession, parent: str, child: str
) -> None:
    assert child in await feature_modules.get_enabled_modules(db_session)

    await _disable(db_session, parent)
    await db_session.flush()
    enabled = await feature_modules.get_enabled_modules(db_session)

    assert parent not in enabled
    assert child not in enabled, (
        f"{child} declares requires=({parent!r},) but stayed enabled with its "
        "parent off — the router would still be mounted."
    )


async def test_a_child_explicitly_on_still_follows_its_parent(
    db_session: AsyncSession,
) -> None:
    """An operator row saying ON does not outrank a disabled parent.

    This is the case a display-only 'requires' note would get wrong: the
    row exists and says enabled, so every naive reader reports it on.
    """
    db_session.add(FeatureModule(id="dhcp.import", enabled=True))
    await _disable(db_session, "core.dhcp")
    await db_session.flush()

    assert "dhcp.import" not in await feature_modules.get_enabled_modules(db_session)


async def test_child_tools_are_stripped_with_the_parent(
    db_session: AsyncSession,
) -> None:
    """The MCP filter reads the same resolved set, so tagging a tool with
    a child module is enough — it need not also name the parent."""
    await _disable(db_session, "core.dns")
    await db_session.flush()
    enabled = await feature_modules.get_enabled_modules(db_session)

    surviving = feature_modules.filter_to_enabled_tools(
        enabled_modules=enabled,
        tool_modules={
            "list_dns_zones": "core.dns",
            "find_dns_import_preview": "dns.import",
            "list_dhcp_servers": "core.dhcp",
            "global_search": None,
        },
    )
    assert surviving == {"list_dhcp_servers", "global_search"}


# ── 4. Disabling is refused while the subsystem owns rows ────────────


async def test_disabling_dhcp_is_refused_while_a_server_exists(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.models.dhcp import DHCPServer, DHCPServerGroup

    _u, token = await _superadmin(db_session)
    group = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    db_session.add(group)
    await db_session.flush()
    db_session.add(
        DHCPServer(
            name=f"kea-{uuid.uuid4().hex[:6]}",
            driver="kea",
            host="10.0.0.5",
            server_group_id=group.id,
        )
    )
    await db_session.commit()

    resp = await client.patch(
        "/api/v1/admin/feature-modules/core.dhcp",
        json={"enabled": False},
        headers=_hdr(token),
    )
    assert resp.status_code == 422, resp.text
    assert "DHCP server" in resp.json()["detail"]

    # Refused before anything was written — no override row, still enabled.
    feature_modules.invalidate_cache()
    assert "core.dhcp" in await feature_modules.get_enabled_modules(db_session)


async def test_disabling_dns_is_refused_while_a_zone_exists(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.models.dns import DNSServerGroup, DNSZone

    _u, token = await _superadmin(db_session)
    group = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    db_session.add(group)
    await db_session.flush()
    db_session.add(DNSZone(name=f"z{uuid.uuid4().hex[:6]}.test", group_id=group.id))
    await db_session.commit()

    resp = await client.patch(
        "/api/v1/admin/feature-modules/core.dns",
        json={"enabled": False},
        headers=_hdr(token),
    )
    assert resp.status_code == 422, resp.text
    assert "DNS zone" in resp.json()["detail"]


async def test_disabling_is_allowed_once_the_subsystem_is_empty(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _u, token = await _superadmin(db_session)
    await db_session.commit()

    resp = await client.patch(
        "/api/v1/admin/feature-modules/core.dhcp",
        json={"enabled": False},
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False


async def test_enabling_is_never_refused(client: AsyncClient, db_session: AsyncSession) -> None:
    """The guard is one-directional. Turning a subsystem back ON with rows
    present is exactly the recovery path and must never be blocked."""
    from app.models.dhcp import DHCPServerGroup

    _u, token = await _superadmin(db_session)
    db_session.add(DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", description=""))
    await _disable(db_session, "core.dhcp")
    await db_session.commit()

    resp = await client.patch(
        "/api/v1/admin/feature-modules/core.dhcp",
        json={"enabled": True},
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True


# ── 5. Registration cannot undo the refusal ──────────────────────────


@pytest.mark.parametrize(
    ("module_id", "path"),
    [
        ("core.dhcp", "/api/v1/dhcp/agents/register"),
        ("core.dns", "/api/v1/dns/agents/register"),
    ],
)
async def test_registration_is_declined_403_not_404_when_the_subsystem_is_off(
    client: AsyncClient, db_session: AsyncSession, module_id: str, path: str
) -> None:
    """Register is the one agent call that CREATES a server row.

    Left open, it undoes the refuse-while-populated guard: empty the
    subsystem, disable it, and a still-running agent re-registers seconds
    later — a live server stranded behind a 404 surface. It must decline,
    and it must decline with 403: a 404 is precisely the signal that makes
    an agent throw its JWT away and re-bootstrap, so 404 here would loop
    forever instead of stopping.
    """
    await _disable(db_session, module_id)
    await db_session.commit()

    resp = await client.post(path, json={})
    assert resp.status_code != 404, resp.text
    # 401/403 both acceptable shapes depending on which gate answers
    # first (the PSK check may run before ours); what must never happen
    # is a 404, or a 200 that creates a row.
    assert resp.status_code in (401, 403), resp.text


# ── 6. Disabling resolves open alerts rather than freezing them ──────


async def test_disabling_resolves_open_alerts_instead_of_stranding_them(
    db_session: AsyncSession,
) -> None:
    """The gate must not ``continue`` past the open/resolve reconciliation.

    A DHCP pool-exhaustion event that was firing when the operator
    switched DHCP off would otherwise stay open forever, with no
    evaluator left that could ever close it.
    """
    from app.models.alerts import AlertEvent, AlertRule
    from app.services.alerts import RULE_TYPE_DHCP_POOL_EXHAUSTION, evaluate_all

    rule = AlertRule(
        name="pool exhaustion",
        rule_type=RULE_TYPE_DHCP_POOL_EXHAUSTION,
        enabled=True,
        severity="warning",
        threshold_percent=90,
    )
    db_session.add(rule)
    await db_session.flush()
    event = AlertEvent(
        rule_id=rule.id,
        subject_type="dhcp_pool",
        subject_id=str(uuid.uuid4()),
        subject_display="pool-1",
        message="90% used",
        severity="warning",
        fired_at=datetime.now(UTC),
    )
    db_session.add(event)
    await _disable(db_session, "core.dhcp")
    await db_session.commit()

    await evaluate_all(db_session)
    await db_session.refresh(event)

    assert event.resolved_at is not None, (
        "the open event survived the module being disabled — nothing will " "ever close it now"
    )
