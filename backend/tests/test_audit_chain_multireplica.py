"""Audit-chain integrity under multiple api replicas (#272 Phase 4).

The tamper-evidence chain (#73) seeds each row's ``prev_hash`` from
the latest *persisted* row via a ``before_flush`` listener
(``app.services.audit_chain.compute_audit_hashes``). Under multi-replica
HA (#272) several api Pods write audit rows against the same database
concurrently. The listener guards the "read latest hash → hash my row →
write" sequence with a Postgres transaction-scoped advisory lock
(``pg_advisory_xact_lock``) so two transactions can't both read the same
predecessor and fork the chain.

These tests prove the cross-session contract holds: rows inserted
through *independent* sessions (standing in for separate api replicas,
each with its own DB connection + transaction) still produce one
unbroken chain, and ``verify_chain`` confirms it.

True wall-clock concurrency isn't reproducible in a single-process
test, but the advisory lock serialises the critical section so the
real-world behaviour reduces to exactly this interleaving of committed
transactions — which is what we assert.
"""

from __future__ import annotations

import uuid

import pytest

from app.db import AsyncSessionLocal
from app.models.audit import AuditLog
from app.services.audit_chain import verify_chain

pytestmark = pytest.mark.asyncio


def _make_row(n: int) -> AuditLog:
    return AuditLog(
        user_display_name=f"replica-writer-{n}",
        action="update",
        resource_type="subnet",
        resource_id=str(uuid.uuid4()),
        resource_display=f"10.0.{n}.0/24",
    )


async def _insert_one(n: int) -> AuditLog:
    """Insert + commit a single audit row in its OWN session/transaction,
    mimicking one api replica handling one request.
    """
    async with AsyncSessionLocal() as session:
        row = _make_row(n)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def test_interleaved_sessions_form_unbroken_chain() -> None:
    # Six writes across six independent sessions, alternating "replicas".
    rows = [await _insert_one(n) for n in range(6)]

    # Every row got hashed.
    assert all(r.row_hash for r in rows)
    # First row anchors the chain (no predecessor).
    assert rows[0].prev_hash is None
    # Each subsequent row's prev_hash links to the prior row's row_hash —
    # proving the listener read the latest *persisted* row even though a
    # different session wrote it.
    for prev, cur in zip(rows, rows[1:]):
        assert cur.prev_hash == prev.row_hash
    # seq is monotonic in chain order.
    assert [r.seq for r in rows] == sorted(r.seq for r in rows)

    async with AsyncSessionLocal() as session:
        result = await verify_chain(session)
    assert result.ok, result.breaks
    assert result.rows_checked == 6


async def test_multiple_rows_in_one_flush_chain_together() -> None:
    # A single request that emits several audit rows in one flush must
    # also chain internally (the listener walks session.new in order).
    async with AsyncSessionLocal() as session:
        batch = [_make_row(n) for n in range(3)]
        session.add_all(batch)
        await session.commit()

    # Independent row appended afterwards by "another replica".
    tail = await _insert_one(99)

    async with AsyncSessionLocal() as session:
        result = await verify_chain(session)
    assert result.ok, result.breaks
    assert result.rows_checked == 4
    # The standalone tail links onto the last row of the batch.
    assert tail.prev_hash is not None
