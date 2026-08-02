"""The Copilot's ``create_dns_zone`` DNSSEC gate must match the REST gate.

Issue #798. ``_POWERDNS_ONLY_FEATURES`` in ``app.services.ai.operations``
hardcoded ``"powerdns" not in drivers`` and was written when PowerDNS was the
only online signer. BIND9 inline-signing (#49) and Technitium online signing
(#740) widened the REST layer's ``_DRIVER_GATED_OPERATIONS["dnssec_sign"]``;
the Copilot constant was not widened with it, so the tool refused work the
REST API accepted for two releases.

The fix reads the REST gate instead of restating it. These tests exist so the
two cannot silently diverge again: the parametrised case walks *every* driver
the REST layer knows about, so widening (or narrowing) the REST gate without
touching the Copilot is a test failure rather than a shipped inconsistency.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dns.router import _DRIVER_GATED_OPERATIONS
from app.core.security import hash_password
from app.models.auth import User
from app.models.dns import DNSServer, DNSServerGroup
from app.services.ai.operations import (
    _DNS_DRIVER_HINTS,
    CreateDNSZoneArgs,
    get_operation,
)

# Every driver the REST DNSSEC gate has an opinion about — the ones it allows
# plus the ones it deliberately excludes. Both halves matter: the bug was an
# over-tight allow set, and the obvious over-correction is a loose gate that
# lets a zone land on Windows DNS and never get signed.
_SIGNING_DRIVERS = _DRIVER_GATED_OPERATIONS["dnssec_sign"]
_ALL_DRIVERS = sorted(_DNS_DRIVER_HINTS | set(_SIGNING_DRIVERS))


async def _user(db: AsyncSession) -> User:
    suffix = uuid.uuid4().hex[:8]
    u = User(
        username=f"dnssec-gate-{suffix}",
        email=f"dnssec-gate-{suffix}@example.test",
        display_name="DNSSEC Gate",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    u.groups = []  # mark loaded — is_effective_superadmin walks .groups (#351)
    db.add(u)
    await db.commit()
    return u


async def _group_with_driver(db: AsyncSession, driver: str) -> DNSServerGroup:
    grp = DNSServerGroup(name=f"g-{driver}-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    db.add(
        DNSServer(
            group_id=grp.id,
            name=f"s-{uuid.uuid4().hex[:6]}",
            driver=driver,
            host="192.0.2.10",
        )
    )
    await db.flush()
    return grp


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", _ALL_DRIVERS)
async def test_dnssec_preview_agrees_with_rest_gate(db_session: AsyncSession, driver: str) -> None:
    """For every driver: the Copilot preview accepts DNSSEC iff REST would."""
    user = await _user(db_session)
    grp = await _group_with_driver(db_session, driver)
    op = get_operation("create_dns_zone")
    assert op is not None

    preview = await op.preview(
        db_session,
        user,
        CreateDNSZoneArgs(
            name=f"z{uuid.uuid4().hex[:6]}.example.com",
            group_id=str(grp.id),
            dnssec_enabled=True,
        ),
    )

    rest_would_allow = driver in _SIGNING_DRIVERS
    assert preview.ok is rest_would_allow, (
        f"driver {driver!r}: REST gate allows={rest_would_allow} but the "
        f"Copilot preview returned ok={preview.ok} ({preview.detail})"
    )
    if not rest_would_allow:
        # The rejection has to name the drivers that WOULD work — the old one
        # said "PowerDNS" and sent operators to the wrong driver.
        for allowed in _SIGNING_DRIVERS:
            assert allowed in preview.detail


@pytest.mark.asyncio
async def test_dnssec_preview_rejects_mixed_group_with_one_non_signer(
    db_session: AsyncSession,
) -> None:
    """One incapable member disqualifies the whole group.

    SUBSET semantics, matching ``_check_driver_gated_operation``. The
    tempting over-correction after #798 is an intersection test ("some
    server can sign"), which would pass this group, land a zone flagged
    ``dnssec_enabled``, and then have the sign/unsign endpoints 422 it —
    signed in the UI, unsigned on the wire, unfixable through the API.
    """
    user = await _user(db_session)
    grp = await _group_with_driver(db_session, "windows_dns")
    db_session.add(
        DNSServer(
            group_id=grp.id,
            name=f"s-{uuid.uuid4().hex[:6]}",
            driver="bind9",
            host="192.0.2.11",
        )
    )
    await db_session.flush()

    op = get_operation("create_dns_zone")
    assert op is not None
    preview = await op.preview(
        db_session,
        user,
        CreateDNSZoneArgs(
            name=f"z{uuid.uuid4().hex[:6]}.example.com",
            group_id=str(grp.id),
            dnssec_enabled=True,
        ),
    )
    assert not preview.ok
    assert "windows_dns" in preview.detail


@pytest.mark.asyncio
async def test_dnssec_preview_fails_soft_on_an_empty_group(
    db_session: AsyncSession,
) -> None:
    """A group with no servers passes — nobody to disagree yet.

    Also matching ``_check_driver_gated_operation``, which returns early on
    an empty driver set. The REST gate re-runs once a driver is known.
    """
    user = await _user(db_session)
    grp = DNSServerGroup(name=f"g-empty-{uuid.uuid4().hex[:6]}")
    db_session.add(grp)
    await db_session.flush()

    op = get_operation("create_dns_zone")
    assert op is not None
    preview = await op.preview(
        db_session,
        user,
        CreateDNSZoneArgs(
            name=f"z{uuid.uuid4().hex[:6]}.example.com",
            group_id=str(grp.id),
            dnssec_enabled=True,
        ),
    )
    assert preview.ok, preview.detail
    assert "no servers in the group yet" in (preview.preview_text or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", sorted(_SIGNING_DRIVERS))
async def test_preview_text_does_not_promise_signing_the_create_wont_do(
    db_session: AsyncSession, driver: str
) -> None:
    """Say what actually happens, per driver.

    Zone-create records intent. BIND9 converges from that alone (it signs
    inline from the config bundle); PowerDNS and Technitium sign only in
    response to the ``dnssec_sign`` record op, which neither this path nor
    the REST ``create_zone`` enqueues. The preview must not imply otherwise.
    """
    user = await _user(db_session)
    grp = await _group_with_driver(db_session, driver)
    op = get_operation("create_dns_zone")
    assert op is not None
    preview = await op.preview(
        db_session,
        user,
        CreateDNSZoneArgs(
            name=f"z{uuid.uuid4().hex[:6]}.example.com",
            group_id=str(grp.id),
            dnssec_enabled=True,
        ),
    )
    assert preview.ok, preview.detail
    text = preview.preview_text or ""
    if driver == "bind9":
        assert "signs inline" in text
    else:
        assert "flag only" in text
        assert driver in text


@pytest.mark.asyncio
async def test_non_dnssec_zone_is_ungated(db_session: AsyncSession) -> None:
    """The gate must fire only when the feature is actually requested."""
    user = await _user(db_session)
    grp = await _group_with_driver(db_session, "windows_dns")
    op = get_operation("create_dns_zone")
    assert op is not None
    preview = await op.preview(
        db_session,
        user,
        CreateDNSZoneArgs(
            name=f"z{uuid.uuid4().hex[:6]}.example.com",
            group_id=str(grp.id),
            dnssec_enabled=False,
        ),
    )
    assert preview.ok, preview.detail
    assert "DNSSEC" not in (preview.preview_text or "")


def test_tool_schema_descriptions_name_every_signing_driver() -> None:
    """The operator-visible schema text ships to the model — it steers the LLM.

    Three descriptions stated "only PowerDNS supports online signing" as fact
    long after that stopped being true, which pushed the model toward PowerDNS
    even where the guard would have allowed BIND9 or Technitium. Assert the
    prose stays in step with the gate it describes.
    """
    fields = CreateDNSZoneArgs.model_fields
    hint_desc = fields["driver_hint"].description or ""
    dnssec_desc = fields["dnssec_enabled"].description or ""
    op = get_operation("create_dns_zone")
    assert op is not None

    for driver in _SIGNING_DRIVERS:
        assert driver in hint_desc, f"driver_hint description omits {driver!r}"
        assert driver in dnssec_desc, f"dnssec_enabled description omits {driver!r}"
        assert driver in op.description, f"operation description omits {driver!r}"

    # The claim that used to be here, and must not come back.
    for text in (hint_desc, dnssec_desc, op.description):
        assert "only PowerDNS" not in text
