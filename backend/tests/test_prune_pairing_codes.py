"""Integration tests for the pairing-code prune sweep (#189).

The original prune task referenced ``PairingCode.used_at`` which does
not exist on the model — claim state is tracked via the ``PairingClaim``
child table (one row per successful supervisor registration). This meant
the "claimed" bucket silently pruned *nothing*, leaving the table
unbounded for claimed codes. Operators who revoked a code expected it
to eventually disappear from the list; instead it persisted forever.

These tests exercise the fixed ``_sweep()`` function against a real
Postgres instance, following the pattern from ``test_reservation_sweep``:

1. Seed rows via ``db_session`` + commit.
2. Call ``_sweep()`` directly — it opens its own ``task_session()``
   which connects to the same test DB (``conftest.py`` rewrites
   ``DATABASE_URL`` before any app import).
3. Verify via ``db_session`` after the sweep.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appliance import Appliance, PairingClaim, PairingCode
from app.tasks.prune_pairing_codes import (
    _GRACE_AFTER_EXPIRY,
    CLAIMED_RETENTION_DAYS,
    EXPIRED_RETENTION_HOURS,
    REVOKED_RETENTION_DAYS,
    _sweep,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _code(
    *,
    persistent: bool = False,
    revoked_at: datetime | None = None,
    expires_at: datetime | None = None,
    enabled: bool = True,
) -> PairingCode:
    """Minimal PairingCode fixture; hashes are deterministic but not meaningful."""
    token = uuid.uuid4().hex
    code_hash = hashlib.sha256(token.encode()).hexdigest()
    return PairingCode(
        id=uuid.uuid4(),
        code_hash=code_hash,
        code_last_two=token[-2:],
        persistent=persistent,
        enabled=enabled,
        revoked_at=revoked_at,
        expires_at=expires_at,
    )


def _appliance() -> Appliance:
    """Minimal Appliance row for use as the FK target of a PairingClaim."""
    raw = uuid.uuid4().bytes  # 16-byte stand-in for DER public key
    fingerprint = hashlib.sha256(raw).hexdigest()
    return Appliance(
        id=uuid.uuid4(),
        hostname=f"appliance-{uuid.uuid4().hex[:8]}",
        public_key_der=raw,
        public_key_fingerprint=fingerprint,
    )


async def _make_claim(
    db: AsyncSession,
    code: PairingCode,
    *,
    claimed_at: datetime,
) -> PairingClaim:
    """Create an Appliance + PairingClaim attached to *code*."""
    app_row = _appliance()
    db.add(app_row)
    await db.flush()
    claim = PairingClaim(
        id=uuid.uuid4(),
        pairing_code_id=code.id,
        appliance_id=app_row.id,
        claimed_at=claimed_at,
    )
    db.add(claim)
    await db.flush()
    return claim


async def _count(db: AsyncSession) -> int:
    return int((await db.execute(select(func.count()).select_from(PairingCode))).scalar_one())


# ---------------------------------------------------------------------------
# Claimed bucket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claimed_code_older_than_retention_is_pruned(db_session: AsyncSession) -> None:
    """A code whose only PairingClaim is older than CLAIMED_RETENTION_DAYS is removed."""
    pc = _code()
    db_session.add(pc)
    await db_session.flush()

    old_at = datetime.now(UTC) - timedelta(days=CLAIMED_RETENTION_DAYS + 1)
    await _make_claim(db_session, pc, claimed_at=old_at)
    await db_session.commit()

    result = await _sweep()

    assert result["claimed_removed"] == 1
    assert result["revoked_removed"] == 0
    assert result["expired_removed"] == 0
    assert await _count(db_session) == 0


@pytest.mark.asyncio
async def test_claimed_code_within_retention_is_kept(db_session: AsyncSession) -> None:
    """A recently claimed code is still inside the retention window and must stay."""
    pc = _code()
    db_session.add(pc)
    await db_session.flush()

    recent_at = datetime.now(UTC) - timedelta(days=CLAIMED_RETENTION_DAYS - 1)
    await _make_claim(db_session, pc, claimed_at=recent_at)
    await db_session.commit()

    result = await _sweep()

    assert result["claimed_removed"] == 0
    assert await _count(db_session) == 1


@pytest.mark.asyncio
async def test_persistent_code_with_all_old_claims_pruned(db_session: AsyncSession) -> None:
    """A persistent code where every PairingClaim is past the cutoff is removed."""
    pc = _code(persistent=True)
    db_session.add(pc)
    await db_session.flush()

    old_at = datetime.now(UTC) - timedelta(days=CLAIMED_RETENTION_DAYS + 5)
    await _make_claim(db_session, pc, claimed_at=old_at)
    await _make_claim(db_session, pc, claimed_at=old_at)
    await db_session.commit()

    result = await _sweep()

    assert result["claimed_removed"] == 1
    assert await _count(db_session) == 0


@pytest.mark.asyncio
async def test_persistent_code_with_one_recent_claim_kept(db_session: AsyncSession) -> None:
    """If ANY claim is within the window the code is kept; most-recent claim governs."""
    pc = _code(persistent=True)
    db_session.add(pc)
    await db_session.flush()

    old_at = datetime.now(UTC) - timedelta(days=CLAIMED_RETENTION_DAYS + 5)
    recent_at = datetime.now(UTC) - timedelta(days=1)
    await _make_claim(db_session, pc, claimed_at=old_at)
    await _make_claim(db_session, pc, claimed_at=recent_at)
    await db_session.commit()

    result = await _sweep()

    assert result["claimed_removed"] == 0
    assert await _count(db_session) == 1


# ---------------------------------------------------------------------------
# Revoked bucket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoked_code_older_than_retention_is_pruned(db_session: AsyncSession) -> None:
    """A code revoked more than REVOKED_RETENTION_DAYS ago is hard-deleted."""
    old_revoke = datetime.now(UTC) - timedelta(days=REVOKED_RETENTION_DAYS + 1)
    pc = _code(revoked_at=old_revoke)
    db_session.add(pc)
    await db_session.commit()

    result = await _sweep()

    assert result["revoked_removed"] == 1
    assert result["claimed_removed"] == 0
    assert result["expired_removed"] == 0
    assert await _count(db_session) == 0


@pytest.mark.asyncio
async def test_revoked_code_within_retention_is_kept(db_session: AsyncSession) -> None:
    """A freshly revoked code is inside the window — still useful for audit correlation."""
    recent_revoke = datetime.now(UTC) - timedelta(days=REVOKED_RETENTION_DAYS - 1)
    pc = _code(revoked_at=recent_revoke)
    db_session.add(pc)
    await db_session.commit()

    result = await _sweep()

    assert result["revoked_removed"] == 0
    assert await _count(db_session) == 1


@pytest.mark.asyncio
async def test_unrevoked_code_not_touched_by_revoked_bucket(db_session: AsyncSession) -> None:
    """A code with revoked_at=None is never matched by the revoked bucket."""
    pc = _code()
    db_session.add(pc)
    await db_session.commit()

    result = await _sweep()

    assert result["revoked_removed"] == 0


# ---------------------------------------------------------------------------
# Expired-without-claim bucket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_unclaimed_code_past_grace_is_pruned(db_session: AsyncSession) -> None:
    """An expired, unclaimed code well past the grace + retention window is pruned."""
    old_expiry = (
        datetime.now(UTC) - _GRACE_AFTER_EXPIRY - timedelta(hours=EXPIRED_RETENTION_HOURS + 1)
    )
    pc = _code(expires_at=old_expiry)
    db_session.add(pc)
    await db_session.commit()

    result = await _sweep()

    assert result["expired_removed"] == 1
    assert result["claimed_removed"] == 0
    assert result["revoked_removed"] == 0
    assert await _count(db_session) == 0


@pytest.mark.asyncio
async def test_expired_code_within_grace_window_is_kept(db_session: AsyncSession) -> None:
    """A barely expired code within the grace window is kept; operator may still use it."""
    grace_expiry = datetime.now(UTC) - timedelta(minutes=5)
    pc = _code(expires_at=grace_expiry)
    db_session.add(pc)
    await db_session.commit()

    result = await _sweep()

    assert result["expired_removed"] == 0
    assert await _count(db_session) == 1


@pytest.mark.asyncio
async def test_expired_but_claimed_code_protected_by_exists_guard(
    db_session: AsyncSession,
) -> None:
    """An expired code with a PairingClaim is NOT pruned by the expired bucket.

    The EXISTS subquery blocks it: the code must age out via the claimed
    bucket instead. With a recent claim it survives both buckets entirely.
    """
    old_expiry = (
        datetime.now(UTC) - _GRACE_AFTER_EXPIRY - timedelta(hours=EXPIRED_RETENTION_HOURS + 1)
    )
    pc = _code(expires_at=old_expiry)
    db_session.add(pc)
    await db_session.flush()

    recent_at = datetime.now(UTC) - timedelta(hours=1)
    await _make_claim(db_session, pc, claimed_at=recent_at)
    await db_session.commit()

    result = await _sweep()

    assert result["expired_removed"] == 0  # blocked by EXISTS guard
    assert result["claimed_removed"] == 0  # claim is too recent
    assert await _count(db_session) == 1


@pytest.mark.asyncio
async def test_no_expiry_code_not_in_expired_bucket(db_session: AsyncSession) -> None:
    """A persistent code with expires_at=None is never touched by the expired bucket."""
    pc = _code(persistent=True, expires_at=None)
    db_session.add(pc)
    await db_session.commit()

    result = await _sweep()

    assert result["expired_removed"] == 0
    assert await _count(db_session) == 1


# ---------------------------------------------------------------------------
# Mixed / boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_handles_empty_table(db_session: AsyncSession) -> None:
    """Sweep on an empty table returns all-zeros and does not crash."""
    result = await _sweep()

    assert result == {"claimed_removed": 0, "revoked_removed": 0, "expired_removed": 0}


@pytest.mark.asyncio
async def test_sweep_is_idempotent(db_session: AsyncSession) -> None:
    """Running the sweep twice removes nothing on the second pass."""
    old_revoke = datetime.now(UTC) - timedelta(days=REVOKED_RETENTION_DAYS + 1)
    pc = _code(revoked_at=old_revoke)
    db_session.add(pc)
    await db_session.commit()

    first = await _sweep()
    second = await _sweep()

    assert first["revoked_removed"] == 1
    assert second == {"claimed_removed": 0, "revoked_removed": 0, "expired_removed": 0}


@pytest.mark.asyncio
async def test_three_buckets_each_remove_one(db_session: AsyncSession) -> None:
    """Three codes in three terminal states — each bucket removes exactly one."""
    # 1. Old claimed code
    pc_claimed = _code()
    db_session.add(pc_claimed)
    await db_session.flush()
    old_claim_at = datetime.now(UTC) - timedelta(days=CLAIMED_RETENTION_DAYS + 1)
    await _make_claim(db_session, pc_claimed, claimed_at=old_claim_at)

    # 2. Old revoked code
    old_revoke = datetime.now(UTC) - timedelta(days=REVOKED_RETENTION_DAYS + 1)
    pc_revoked = _code(revoked_at=old_revoke)
    db_session.add(pc_revoked)

    # 3. Old expired, unclaimed code
    old_expiry = (
        datetime.now(UTC) - _GRACE_AFTER_EXPIRY - timedelta(hours=EXPIRED_RETENTION_HOURS + 1)
    )
    pc_expired = _code(expires_at=old_expiry)
    db_session.add(pc_expired)

    await db_session.commit()

    result = await _sweep()

    assert result["claimed_removed"] == 1
    assert result["revoked_removed"] == 1
    assert result["expired_removed"] == 1
    assert await _count(db_session) == 0


@pytest.mark.asyncio
async def test_active_pending_code_survives_all_buckets(db_session: AsyncSession) -> None:
    """A fresh pending persistent code (no expiry, no claims, not revoked) is untouched."""
    pc = _code(persistent=True, expires_at=None)
    db_session.add(pc)
    await db_session.commit()

    result = await _sweep()

    assert result == {"claimed_removed": 0, "revoked_removed": 0, "expired_removed": 0}
    assert await _count(db_session) == 1


@pytest.mark.asyncio
async def test_revoked_claimed_code_counted_once_by_revoked_bucket(
    db_session: AsyncSession,
) -> None:
    """A code that is both revoked AND has old claims is deleted once.

    The claimed bucket requires ``revoked_at.is_(None)`` so it skips
    revoked codes. The revoked bucket picks it up instead — no double
    count, no crash on second delete attempt.
    """
    old_revoke = datetime.now(UTC) - timedelta(days=REVOKED_RETENTION_DAYS + 1)
    pc = _code(revoked_at=old_revoke)
    db_session.add(pc)
    await db_session.flush()

    old_claim_at = datetime.now(UTC) - timedelta(days=CLAIMED_RETENTION_DAYS + 1)
    await _make_claim(db_session, pc, claimed_at=old_claim_at)
    await db_session.commit()

    result = await _sweep()

    assert result["claimed_removed"] == 0  # skipped — revoked_at is set
    assert result["revoked_removed"] == 1  # caught by revoked bucket
    assert await _count(db_session) == 0
