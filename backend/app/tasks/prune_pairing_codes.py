"""Sweep stale pairing codes (#169).

Pairing codes are short-lived (default 15 min expiry, max 60 min) and
single-use. Once a row is past its grace window — claimed,
revoked, or just expired long enough that no operator is still
expecting to use it — there's no operational reason to keep it
around. We sweep them so the table stays small and the
``GET /api/v1/appliance/pairing-codes`` list stays scannable.

Three independent retention buckets:

* **Claimed** rows prune after 30 days. Useful for "who paired which
  agent and when" forensics during onboarding, but past a month the
  audit log carries the same signal.
* **Revoked** rows prune after 7 days. Operator-driven cancellation
  is a transient state; once it's a week old it's just noise.
* **Expired without claim** rows prune after 24 h past their grace
  window. Most expiries are operator-noise ("generated, walked
  away, generated again later"); short retention keeps the list
  clean.

Runs every 30 minutes so a freshly-expired code drops off the
operator's view in a predictable window without flooding the beat
schedule.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete

from app.celery_app import celery_app
from app.db import task_session
from app.models.appliance import PairingCode

logger = structlog.get_logger(__name__)

CLAIMED_RETENTION_DAYS = 30
REVOKED_RETENTION_DAYS = 7
EXPIRED_RETENTION_HOURS = 24
# Grace window must match the consume endpoint's tolerance so a row
# that's "expired but still in grace" doesn't get prematurely swept.
_GRACE_AFTER_EXPIRY = timedelta(minutes=30)


async def _sweep() -> dict[str, int]:
    async with task_session() as db:
        now = datetime.now(UTC)
        claimed_cutoff = now - timedelta(days=CLAIMED_RETENTION_DAYS)
        revoked_cutoff = now - timedelta(days=REVOKED_RETENTION_DAYS)
        expired_cutoff = now - _GRACE_AFTER_EXPIRY - timedelta(hours=EXPIRED_RETENTION_HOURS)

        claimed_del = await db.execute(
            delete(PairingCode).where(
                PairingCode.used_at.isnot(None),
                PairingCode.used_at < claimed_cutoff,
            )
        )
        revoked_del = await db.execute(
            delete(PairingCode).where(
                PairingCode.used_at.is_(None),
                PairingCode.revoked_at.isnot(None),
                PairingCode.revoked_at < revoked_cutoff,
            )
        )
        expired_del = await db.execute(
            delete(PairingCode).where(
                PairingCode.used_at.is_(None),
                PairingCode.revoked_at.is_(None),
                PairingCode.expires_at < expired_cutoff,
            )
        )
        await db.commit()
        return {
            "claimed_removed": claimed_del.rowcount or 0,
            "revoked_removed": revoked_del.rowcount or 0,
            "expired_removed": expired_del.rowcount or 0,
        }


@celery_app.task(name="app.tasks.prune_pairing_codes.prune_pairing_codes")
def prune_pairing_codes() -> dict[str, int]:
    result = asyncio.run(_sweep())
    logger.info("pairing_codes_pruned", **result)
    return result
