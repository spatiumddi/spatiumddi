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
    ("module_id", "path", "env", "header", "driver"),
    [
        (
            "core.dhcp",
            "/api/v1/dhcp/agents/register",
            "DHCP_AGENT_KEY",
            "X-DHCP-Agent-Key",
            "kea",
        ),
        (
            "core.dns",
            "/api/v1/dns/agents/register",
            "DNS_AGENT_KEY",
            "X-DNS-Agent-Key",
            "bind9",
        ),
    ],
)
async def test_registration_is_declined_403_not_404_when_the_subsystem_is_off(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    module_id: str,
    path: str,
    env: str,
    header: str,
    driver: str,
) -> None:
    """Register is the one agent call that CREATES a server row.

    Left open, it undoes the refuse-while-populated guard: empty the
    subsystem, disable it, and a still-running agent re-registers seconds
    later — a live server stranded behind a 404 surface. It must decline,
    and it must decline with 403, because a 404 is exactly the signal that
    makes an agent throw its JWT away and re-bootstrap from its PSK.

    The PSK dependency runs BEFORE the handler body, so this has to get
    PAST it to reach the module guard at all: the bootstrap key is set and
    sent, and the body is valid. The first version of this test asserted
    only ``status in (401, 403)`` and sent neither — it passed locally off
    the 401 from an unconfigured key and passed nothing of the guard, then
    failed in CI where the key is absent entirely and the answer is 503.
    """
    monkeypatch.setenv(env, "test-bootstrap-key")
    await _disable(db_session, module_id)
    await db_session.commit()

    resp = await client.post(
        path,
        json={
            "hostname": f"agent-{uuid.uuid4().hex[:8]}",
            "driver": driver,
            "fingerprint": uuid.uuid4().hex,
        },
        headers={header: "test-bootstrap-key"},
    )
    assert resp.status_code == 403, resp.text
    assert "disabled" in resp.json()["detail"].lower()


@pytest.mark.parametrize(
    ("module_id", "path", "env", "header", "driver"),
    [
        (
            "core.dhcp",
            "/api/v1/dhcp/agents/register",
            "DHCP_AGENT_KEY",
            "X-DHCP-Agent-Key",
            "kea",
        ),
        (
            "core.dns",
            "/api/v1/dns/agents/register",
            "DNS_AGENT_KEY",
            "X-DNS-Agent-Key",
            "bind9",
        ),
    ],
)
async def test_registration_succeeds_while_the_subsystem_is_on(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    module_id: str,
    path: str,
    env: str,
    header: str,
    driver: str,
) -> None:
    """Negative control: the same request with the module ON is accepted.

    Without this the 403 above could come from anything — a rejected body,
    a changed header name — and still look like the guard working.
    """
    monkeypatch.setenv(env, "test-bootstrap-key")
    await db_session.commit()

    resp = await client.post(
        path,
        json={
            "hostname": f"agent-{uuid.uuid4().hex[:8]}",
            "driver": driver,
            "fingerprint": uuid.uuid4().hex,
        },
        headers={header: "test-bootstrap-key"},
    )
    assert resp.status_code < 400, resp.text


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
