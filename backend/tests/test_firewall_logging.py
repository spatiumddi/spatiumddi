"""Tests for the #404 opt-in firewall drop-logging render injection.

When ``firewall_logging_enabled`` is on (and ``firewall_enabled`` is on), the
rendered nft drop-in body gains a single rate-limited catch-all ``log prefix
"spatium-fw: "`` rule at the very end (after the accept rules), so dropped
packets land in the kernel log for the Firewall → Logs viewer. The flag is a
no-op when enforcement is off (the bundle short-circuits to the disabled
shape).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.appliance.firewall import _FIREWALL_LOG_RULE, firewall_bundle

_COMMON = dict(
    role_assignment={"roles": []},
    cluster_peer_cidrs=[],
    pod_cidrs=[],
    service_cidrs=[],
    cp_member_count=1,
    vip_configured=False,
)


async def test_logging_off_has_no_log_rule(db_session: AsyncSession) -> None:
    bundle = await firewall_bundle(db_session, firewall_enabled=True, **_COMMON)
    assert bundle["enabled"] is True
    assert _FIREWALL_LOG_RULE not in bundle["firewall_conf"]
    assert "spatium-fw:" not in bundle["firewall_conf"]


async def test_logging_on_appends_single_log_rule(db_session: AsyncSession) -> None:
    base = await firewall_bundle(db_session, firewall_enabled=True, **_COMMON)
    logged = await firewall_bundle(
        db_session, firewall_enabled=True, firewall_logging_enabled=True, **_COMMON
    )
    # The rule lands once, at the end (accepts terminate first; the catch-all
    # logs whatever falls through before the base chain's policy drop).
    assert logged["firewall_conf"].count("spatium-fw:") == 1
    assert logged["firewall_conf"].rstrip().endswith(_FIREWALL_LOG_RULE)
    # Body changed → hash shifts → the supervisor re-applies + picks up logging.
    assert logged["config_hash"] != base["config_hash"]
    # Rate limit is present so a scan can't flood the kernel log.
    assert "limit rate" in _FIREWALL_LOG_RULE


async def test_logging_ignored_when_enforcement_off(db_session: AsyncSession) -> None:
    # firewall_enabled off → disabled shape regardless of the logging flag.
    bundle = await firewall_bundle(
        db_session, firewall_enabled=False, firewall_logging_enabled=True, **_COMMON
    )
    assert bundle["enabled"] is False
    assert bundle["firewall_conf"] == ""
    assert bundle["config_hash"] == ""
