"""Appliance-host packet-capture dispatch (#59 Phase 2).

The appliance vantage runs tcpdump on the appliance **host** (real NICs),
not in a container — so the capture rides the supervisor channel. Unlike
:mod:`app.services.appliance.agent_cmd` (an in-memory per-replica queue
for short request/response nettools), the authority here is the
``packet_capture`` **DB row**: a minutes-long job that outlives any single
poll and is replica-agnostic — *any* api replica can serve the
supervisor's poll because the claim is a guarded UPDATE against the shared
Postgres, not an in-memory queue (which would strand a capture enqueued on
a different replica — the #430-class delivery gap we explicitly avoid).

Flow:
  * supervisor long-polls ``/supervisor/pcap/poll`` → :func:`claim_next`
    atomically claims the oldest ``queued`` appliance-vantage row for the
    caller's appliance and returns a structured command (never a shell
    string);
  * supervisor POSTs progress → :func:`record_progress` (returns the
    cancel flag so the host runner can stop);
  * supervisor streams the finished ``.pcap`` to ``/supervisor/pcap/upload``
    → :func:`finalize_capture` stamps the terminal state.

A backstop reaper (``app.tasks.pcap.prune_captures``) fails any row stuck
non-terminal past its deadline, so a lost supervisor never freezes a row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pcap import PacketCapture


async def claim_next(
    db: AsyncSession, appliance_id: uuid.UUID
) -> dict[str, Any] | None:
    """Atomically claim the oldest queued appliance-vantage capture.

    Returns a structured command dict (NEVER a shell string / pre-joined
    argv — the supervisor + host runner rebuild the argv from these
    validated fields) or ``None`` when nothing is queued. The guarded
    UPDATE (``WHERE status='queued'``) makes the claim safe across
    replicas: a second claimant's rowcount is 0 and it returns None.
    """
    row = (
        await db.execute(
            select(PacketCapture)
            .where(
                PacketCapture.status == "queued",
                PacketCapture.vantage_kind == "appliance",
                PacketCapture.appliance_id == appliance_id,
            )
            .order_by(PacketCapture.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    res = await db.execute(
        update(PacketCapture)
        .where(PacketCapture.id == row.id, PacketCapture.status == "queued")
        .values(status="running", started_at=datetime.now(UTC))
    )
    if (res.rowcount or 0) == 0:
        # Lost the race to another replica's poll — leave it for them.
        await db.rollback()
        return None
    await db.commit()
    return {
        "capture_id": str(row.id),
        "interface": row.interface,
        "bpf_filter": row.bpf_filter,
        "snaplen": row.snaplen,
        "promiscuous": row.promiscuous,
        "max_packets": row.max_packets,
        "max_duration_s": row.max_duration_s,
        "max_bytes": row.max_bytes,
    }


async def record_progress(
    db: AsyncSession,
    capture_id: uuid.UUID,
    *,
    packets: int | None,
    bytes_captured: int | None,
    elapsed_s: float | None,
) -> bool:
    """Update live progress; return True when the operator has cancelled.

    The cancel flag rides the progress response so the host runner stops
    on its next tick (backstopped by its own hard wall-clock kill)."""
    row = await db.get(PacketCapture, capture_id)
    if row is None:
        return True  # gone → tell the runner to stop
    if row.status == "cancelled":
        return True
    if bytes_captured is not None:
        row.bytes_captured = bytes_captured
    if packets is not None:
        row.packets_captured = packets
    if elapsed_s is not None:
        row.duration_seconds = elapsed_s
    await db.commit()
    return False


async def finalize_capture(
    db: AsyncSession,
    capture_id: uuid.UUID,
    *,
    pcap_path: str | None,
    pcap_size_bytes: int | None,
    pcap_sha256: str | None,
    packet_count: int | None,
    metadata: dict[str, Any] | None,
    error: str | None,
) -> str:
    """Stamp the terminal state after the supervisor finishes.

    Returns the final status. Cancel is terminal + idempotent: if the row
    was cancelled while the capture was finishing, the uploaded bytes are
    discarded by the caller and the row stays ``cancelled`` (a
    late-completing capture never overwrites a cancel)."""
    row = await db.get(PacketCapture, capture_id)
    if row is None:
        return "missing"
    row.finished_at = datetime.now(UTC)
    if row.status == "cancelled":
        # Caller unlinks the bytes; cancel wins.
        return "cancelled"
    if error:
        row.status = "failed"
        row.error_message = error[:500]
        await db.commit()
        return "failed"
    row.status = "completed"
    row.pcap_path = pcap_path
    row.pcap_size_bytes = pcap_size_bytes
    row.pcap_sha256 = pcap_sha256
    if packet_count is not None:
        row.packets_captured = packet_count
    if pcap_size_bytes is not None:
        row.bytes_captured = pcap_size_bytes
    row.metadata_json = metadata
    await db.commit()
    return "completed"


__all__ = ["claim_next", "record_progress", "finalize_capture"]
