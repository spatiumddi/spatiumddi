"""Account lockout after N failed logins (issue #71).

Pure functions over a ``LockoutPolicy`` snapshot read from
``PlatformSettings``. The auth router calls these on every login
attempt — `is_locked` short-circuits before the password check, and
`register_failure` / `register_success` mutate the user row in place
(commit is the caller's responsibility).

Windowed counter semantics: the threshold counts failures inside the
rolling ``reset_minutes`` window. If the previous failure is older
than that, the counter starts fresh — so 1 fail every 6 minutes never
accumulates into a lockout while 5 fails inside 5 minutes does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from app.models.auth import User
from app.models.settings import PlatformSettings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class LockoutPolicy:
    threshold: int
    duration_minutes: int
    reset_minutes: int

    @property
    def enabled(self) -> bool:
        return self.threshold > 0

    @classmethod
    def from_row(cls, row: PlatformSettings | None) -> LockoutPolicy:
        if row is None:
            return cls(threshold=0, duration_minutes=15, reset_minutes=15)
        return cls(
            threshold=int(row.lockout_threshold),
            duration_minutes=int(row.lockout_duration_minutes),
            reset_minutes=int(row.lockout_reset_minutes),
        )


def is_locked(user: User, now: datetime | None = None) -> bool:
    """True when ``failed_login_locked_until`` is set and still in the
    future. Independent of ``LockoutPolicy.enabled`` so a previously-
    locked user stays locked even if an admin disables the feature
    mid-incident — they just have to wait it out (or be unlocked)."""
    if user.failed_login_locked_until is None:
        return False
    now = now or datetime.now(UTC)
    return user.failed_login_locked_until > now


def register_failure(user: User, policy: LockoutPolicy, now: datetime | None = None) -> bool:
    """Bump the failure counter and lock the account if it crossed the
    threshold. Returns True iff the account just transitioned to locked
    so the caller can write a distinct audit row.

    Caller still commits.
    """
    if not policy.enabled:
        return False
    now = now or datetime.now(UTC)

    # Windowed reset: any failure older than ``reset_minutes`` is
    # considered to have aged out. We can't track every individual
    # failure timestamp without a separate table, so we approximate:
    # if the prior failure is past the window, drop the counter to
    # zero before incrementing.
    if user.last_failed_login_at is not None and (now - user.last_failed_login_at) > timedelta(
        minutes=policy.reset_minutes
    ):
        user.failed_login_count = 0

    user.failed_login_count = (user.failed_login_count or 0) + 1
    user.last_failed_login_at = now

    if user.failed_login_count >= policy.threshold:
        user.failed_login_locked_until = now + timedelta(minutes=policy.duration_minutes)
        logger.warning(
            "account_locked",
            user_id=str(user.id),
            username=user.username,
            until=user.failed_login_locked_until.isoformat(),
            threshold=policy.threshold,
        )
        return True
    return False


def register_success(user: User) -> None:
    """Reset the lockout state on successful login. Caller commits."""
    if user.failed_login_count or user.failed_login_locked_until:
        user.failed_login_count = 0
        user.failed_login_locked_until = None
        user.last_failed_login_at = None


def unlock(user: User) -> bool:
    """Clear the lockout state. Returns True iff anything actually
    changed so the admin endpoint can decide whether to write an
    audit row vs. surface "already unlocked"."""
    if (
        user.failed_login_count == 0
        and user.failed_login_locked_until is None
        and user.last_failed_login_at is None
    ):
        return False
    user.failed_login_count = 0
    user.failed_login_locked_until = None
    user.last_failed_login_at = None
    return True
