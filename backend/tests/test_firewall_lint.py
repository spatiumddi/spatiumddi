"""firewall_extra allowlist-grammar lint (#285 Phase 3d).

Pure-function tests for the linter's error/warning classification, plus a
write-path test proving the role-assignment PUT hard-422s on a dangerous
firewall_extra but lets a soft-warning one through (nft -c -f is the final
authority; grammar nits are advisory).
"""

from __future__ import annotations

import hashlib
import os
import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.appliance import APPLIANCE_STATE_APPROVED, Appliance
from app.models.auth import User
from app.services.appliance.firewall_lint import errors, lint_firewall_extra, warnings


def _sev(text: str) -> tuple[set[str], set[str]]:
    f = lint_firewall_extra(text)
    return (
        {x.message for x in errors(f)},
        {x.message for x in warnings(f)},
    )


def test_clean_rule_no_findings() -> None:
    f = lint_firewall_extra('ip saddr { 10.0.0.0/8 } tcp dport 9090 accept comment "metrics"')
    assert f == []


def test_blank_and_comment_lines_skipped() -> None:
    assert lint_firewall_extra("\n  \n# a comment\n") == []


def test_drop_22_is_error() -> None:
    errs, _ = _sev("ip saddr { 10.0.0.0/8 } tcp dport 22 drop")
    assert any("port 22" in m for m in errs)


def test_drop_other_port_ok() -> None:
    errs, _ = _sev("ip saddr { 10.0.0.0/8 } tcp dport 23 drop")
    assert errs == set()


def test_forbidden_char_is_error() -> None:
    for ch in (";", "`", "$", "|", "&", "\\"):
        errs, _ = _sev(f"ip saddr {{ 10.0.0.0/8 }} tcp dport 80 accept {ch} echo hi")
        assert any("forbidden character" in m for m in errs), ch


def test_unbalanced_braces_is_error() -> None:
    errs, _ = _sev("ip saddr { 10.0.0.0/8 tcp dport 80 accept")
    assert any("unbalanced braces" in m for m in errs)


def test_missing_saddr_is_warning() -> None:
    errs, warns = _sev("tcp dport 80 accept")
    assert errs == set()
    assert any("scope a source" in m for m in warns)


def test_missing_action_is_warning() -> None:
    _, warns = _sev("ip saddr { 10.0.0.0/8 } tcp dport 80")
    assert any("no accept/drop action" in m for m in warns)


def test_dport_on_icmp_is_warning() -> None:
    _, warns = _sev("ip saddr { 10.0.0.0/8 } icmp dport 80 accept")
    assert any("icmp" in m for m in warns)


def test_v6_in_ip_saddr_is_warning() -> None:
    _, warns = _sev("ip saddr { 2001:db8::/64 } tcp dport 80 accept")
    assert any("v6 CIDR" in m for m in warns)


def test_invalid_cidr_is_warning() -> None:
    _, warns = _sev("ip saddr { 10.0.0.0/99 } tcp dport 80 accept")
    assert any("invalid CIDR" in m for m in warns)


def test_unquoted_comment_is_warning() -> None:
    _, warns = _sev("ip saddr { 10.0.0.0/8 } tcp dport 80 accept comment bare")
    assert any("double-quoted" in m for m in warns)


def test_quoted_comment_with_parens_ok() -> None:
    # Parens live inside the quoted comment value → not metachar-scanned.
    f = lint_firewall_extra('ip saddr { 10.0.0.0/8 } tcp dport 80 accept comment "web (prod)"')
    assert errors(f) == []


# ── Write-path (delta-only 422) ──────────────────────────────────────


async def _approved_appliance(db: AsyncSession) -> Appliance:
    der = os.urandom(32)
    a = Appliance(
        id=uuid.uuid4(),
        hostname=f"n-{uuid.uuid4().hex[:6]}",
        public_key_der=der,
        public_key_fingerprint=hashlib.sha256(der).hexdigest(),
        state=APPLIANCE_STATE_APPROVED,
        deployment_kind="appliance",
        appliance_variant="application",
    )
    db.add(a)
    await db.flush()
    return a


async def _superadmin(db: AsyncSession) -> dict:
    u = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@x.com",
        display_name="T",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(u)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(str(u.id))}"}


async def test_write_path_rejects_dangerous_extra(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _superadmin(db_session)
    a = await _approved_appliance(db_session)
    await db_session.commit()
    r = await client.put(
        f"/api/v1/appliance/appliances/{a.id}/roles",
        headers=h,
        json={"firewall_extra": "ip saddr { 10.0.0.0/8 } tcp dport 22 drop"},
    )
    assert r.status_code == 422
    assert "firewall_extra rejected" in r.json()["detail"]


async def test_write_path_allows_soft_warning_extra(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # Missing-saddr is advisory only — nft -c -f is the final authority, so a
    # plausibly-valid extra the grammar merely dislikes still saves.
    h = await _superadmin(db_session)
    a = await _approved_appliance(db_session)
    await db_session.commit()
    r = await client.put(
        f"/api/v1/appliance/appliances/{a.id}/roles",
        headers=h,
        json={"firewall_extra": 'tcp dport 9090 accept comment "metrics"'},
    )
    assert r.status_code == 200, r.text
    await db_session.refresh(a)
    assert a.firewall_extra == 'tcp dport 9090 accept comment "metrics"'
