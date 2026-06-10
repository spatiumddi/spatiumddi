"""Unit tests for the time-bound-grant union in ``user_has_permission`` (#65).

These exercise the pure helper with grants stashed on
``User._active_time_bound_grants`` (the same attribute the auth dependency
populates per request). The static role grants are intentionally empty so we
prove the grant path is what admits the access.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import user_has_permission
from app.models.auth import Group, User
from app.models.time_bound_grant import TimeBoundGrant
from app.services.time_bound_grants import load_active_grants_for_groups


def _user() -> User:
    u = User(
        username=f"u{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@t.io",
        display_name="Test",
        hashed_password="x",
        is_superadmin=False,
        is_active=True,
    )
    u.groups = []  # no static role grants
    u._active_time_bound_grants = []
    return u


def _grant(
    *,
    action: str = "write",
    resource_type: str = "subnet",
    resource_id: str | None = None,
    expires_in: timedelta = timedelta(hours=1),
    revoked: bool = False,
) -> TimeBoundGrant:
    g = TimeBoundGrant(
        group_id=uuid.uuid4(),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        expires_at=datetime.now(UTC) + expires_in,
        revoked_at=datetime.now(UTC) if revoked else None,
        reason="test",
    )
    return g


def _bare_user() -> User:
    """A User built outside the auth dependency — no ``_active_time_bound_grants``
    assignment at all, so it falls back to the class-level sentinel."""
    return User(
        username=f"u{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@t.io",
        display_name="Bare",
        hashed_password="x",
        is_superadmin=False,
        is_active=True,
    )


def test_users_do_not_share_grant_state() -> None:
    """Two distinct User instances must not share grant state.

    The class-level ``_active_time_bound_grants`` default is the immutable
    ``None`` sentinel (NOT a shared ``[]`` literal), so a User built without
    a per-request assignment reads as "no grants" and is never polluted by
    another User's grants — even if that other User's list is mutated in
    place. Regression guard for the mutable-class-default footgun.
    """
    u1 = _bare_user()
    u1.groups = []
    u2 = _bare_user()
    u2.groups = []

    # Neither got a deps-style assignment: both read as empty / denied.
    assert user_has_permission(u1, "write", "subnet") is False
    assert user_has_permission(u2, "write", "subnet") is False

    # Give u1 a real list and mutate it in place (the leak vector). u2 must
    # stay empty — it does not share u1's list.
    u1._active_time_bound_grants = []
    u1._active_time_bound_grants.append(_grant(action="write", resource_type="subnet"))
    assert user_has_permission(u1, "write", "subnet") is True
    assert user_has_permission(u2, "write", "subnet") is False

    # A third bare User constructed after u1's mutation is also unaffected.
    u3 = _bare_user()
    u3.groups = []
    assert user_has_permission(u3, "write", "subnet") is False


def test_live_grant_admits_action_with_no_role() -> None:
    """A user with no role grant gets access purely from a live grant."""
    u = _user()
    assert user_has_permission(u, "write", "subnet") is False  # baseline: denied
    u._active_time_bound_grants = [_grant(action="write", resource_type="subnet")]
    assert user_has_permission(u, "write", "subnet") is True


async def test_expired_grant_denied_even_pre_sweep(db_session: AsyncSession) -> None:
    """An expired grant (expires_at in the past) is excluded by the
    request-time load filter (``expires_at > now()``) — so expiry is honoured
    even before the 60 s sweep flips ``revoked_at``. This drives the real DB
    load helper, not a hand-built list."""
    group = Group(name=f"g{uuid.uuid4().hex[:6]}", description="")
    db_session.add(group)
    await db_session.flush()
    db_session.add(
        TimeBoundGrant(
            group_id=group.id,
            action="write",
            resource_type="subnet",
            resource_id=None,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),  # already expired
            revoked_at=None,  # NOT yet swept
            reason="expired",
        )
    )
    await db_session.flush()

    loaded = await load_active_grants_for_groups(db_session, [group.id])
    assert loaded == []  # expired grant filtered out at load time

    u = _user()
    u._active_time_bound_grants = loaded
    assert user_has_permission(u, "write", "subnet") is False


async def test_revoked_grant_excluded_by_load_filter(db_session: AsyncSession) -> None:
    """A revoked (``revoked_at`` set) grant is excluded by the load filter →
    denied, even though ``expires_at`` is still in the future."""
    group = Group(name=f"g{uuid.uuid4().hex[:6]}", description="")
    db_session.add(group)
    await db_session.flush()
    db_session.add(
        TimeBoundGrant(
            group_id=group.id,
            action="write",
            resource_type="subnet",
            resource_id=None,
            expires_at=datetime.now(UTC) + timedelta(hours=1),  # still in the future
            revoked_at=datetime.now(UTC),  # but revoked
            reason="revoked",
        )
    )
    await db_session.flush()

    loaded = await load_active_grants_for_groups(db_session, [group.id])
    assert loaded == []

    u = _user()
    u._active_time_bound_grants = loaded
    assert user_has_permission(u, "write", "subnet") is False


async def test_live_grant_loaded_and_admits(db_session: AsyncSession) -> None:
    """A live grant (not revoked, not expired) is loaded by the filter and
    admits the action end-to-end."""
    group = Group(name=f"g{uuid.uuid4().hex[:6]}", description="")
    db_session.add(group)
    await db_session.flush()
    db_session.add(
        TimeBoundGrant(
            group_id=group.id,
            action="write",
            resource_type="subnet",
            resource_id=None,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            revoked_at=None,
            reason="live",
        )
    )
    await db_session.flush()

    loaded = await load_active_grants_for_groups(db_session, [group.id])
    assert len(loaded) == 1

    u = _user()
    u._active_time_bound_grants = loaded
    assert user_has_permission(u, "write", "subnet") is True


def test_scoped_grant_does_not_satisfy_unscoped_check() -> None:
    """A resource_id-scoped grant must not satisfy a whole-type check."""
    u = _user()
    rid = str(uuid.uuid4())
    u._active_time_bound_grants = [_grant(action="write", resource_type="subnet", resource_id=rid)]
    # Unscoped check (resource_id=None) must NOT be satisfied by a scoped grant.
    assert user_has_permission(u, "write", "subnet") is False
    # The scoped check for the exact id IS satisfied.
    assert user_has_permission(u, "write", "subnet", rid) is True
    # A different id is NOT.
    assert user_has_permission(u, "write", "subnet", str(uuid.uuid4())) is False


def test_admin_grant_implies_read_write_delete() -> None:
    """An ``admin`` grant covers read / write / delete on the same type."""
    u = _user()
    u._active_time_bound_grants = [_grant(action="admin", resource_type="subnet")]
    assert user_has_permission(u, "read", "subnet") is True
    assert user_has_permission(u, "write", "subnet") is True
    assert user_has_permission(u, "delete", "subnet") is True
    assert user_has_permission(u, "admin", "subnet") is True
    # Does not bleed into a different resource type.
    assert user_has_permission(u, "read", "dns_zone") is False


def test_inactive_user_never_admitted_by_grant() -> None:
    """An inactive user is denied even with a live grant."""
    u = _user()
    u.is_active = False
    u._active_time_bound_grants = [_grant(action="write", resource_type="subnet")]
    assert user_has_permission(u, "write", "subnet") is False
