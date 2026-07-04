"""Optional RIS Live WebSocket consumer (issue #527).

RIPE RIS Live (https://ris-live.ripe.net/) is a free, server-filterable
firehose of BGP UPDATE messages from the RIS route collectors, delivered
as JSON over a single WebSocket. This module is the REAL-TIME upgrade to
the periodic ``app.tasks.bgp_hijack_poll`` — it subscribes for the
prefixes associated with tracked ASNs and, on a BGP announcement whose
origin AS is unexpected, records a detection immediately instead of
waiting for the next poll.

**This is strictly opt-in and never load-bearing.** A persistent
WebSocket doesn't fit Celery's task model, so this ships as a
standalone async entrypoint you run as its own process (or sidecar):

    python -m app.services.bgp.ris_live

It only runs when ``settings.bgp_ris_live_enabled`` is true (env
``BGP_RIS_LIVE_ENABLED=1``). The beat-driven poll remains the source of
truth and the alert-latch state; this consumer writes into the SAME
``bgp_hijack_detection`` table via the SAME
``app.services.bgp.hijack_monitor`` helpers, so detections it opens are
resolved by the poll's delist sweep exactly like poll-opened rows. If
the ``websockets`` package isn't installed, the entrypoint exits with a
clear message rather than crashing an import.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from app.config import settings
from app.db import task_session
from app.models.bgp_monitor import BGPTrackedPrefix
from app.services.bgp.hijack_monitor import (
    KIND_MORE_SPECIFIC,
    KIND_PREFIX_HIJACK,
    RPKI_VALID,
    _parse_net,
    derive_rpki_status,
    expected_origin_set,
    record_detection,
)

logger = structlog.get_logger(__name__)

# How often we re-read the tracked-prefix set from the DB to (re)build
# the RIS Live subscription filter. New tracked prefixes take effect on
# the next refresh.
_SUBSCRIPTION_REFRESH_SECONDS = 300


async def _load_tracked_index() -> dict[int, list[BGPTrackedPrefix]]:
    """Return enabled tracked prefixes indexed by IP version, so we can
    quickly find which tracked prefix an announced route falls under."""
    async with task_session() as db:
        rows = (
            (await db.execute(select(BGPTrackedPrefix).where(BGPTrackedPrefix.enabled.is_(True))))
            .scalars()
            .all()
        )
    index: dict[int, list[BGPTrackedPrefix]] = {4: [], 6: []}
    for row in rows:
        net = _parse_net(str(row.prefix))
        if net is not None:
            index[net.version].append(row)
    return index


def _match_tracked(
    announced: ipaddress._BaseNetwork,
    index: dict[int, list[BGPTrackedPrefix]],
) -> tuple[BGPTrackedPrefix, str] | None:
    """Find the tracked prefix that ``announced`` falls under and the
    detection kind (exact vs more-specific)."""
    for tracked in index.get(announced.version, []):
        tnet = _parse_net(str(tracked.prefix))
        if tnet is None:
            continue
        if tnet == announced:
            return tracked, KIND_PREFIX_HIJACK
        try:
            covers = tnet.supernet_of(announced)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            covers = False
        if covers and announced.prefixlen > tnet.prefixlen:
            return tracked, KIND_MORE_SPECIFIC
    return None


def _origin_from_path(as_path: list[Any] | None) -> int | None:
    """RIS Live ``path`` is the AS_PATH; the origin is the last element
    (may be an AS_SET list). Return the first concrete int we find."""
    if not as_path:
        return None
    last = as_path[-1]
    if isinstance(last, list):  # AS_SET
        last = last[0] if last else None
    try:
        return int(last)
    except (TypeError, ValueError):
        return None


async def _handle_message(raw: str, index: dict[int, list[BGPTrackedPrefix]]) -> int:
    """Process one RIS Live JSON message. Returns detections opened."""
    try:
        msg = json.loads(raw)
    except ValueError:
        return 0
    if not isinstance(msg, dict) or msg.get("type") != "ris_message":
        return 0
    data = msg.get("data") or {}
    if data.get("type") != "UPDATE":
        return 0
    announcements = data.get("announcements") or []
    origin = _origin_from_path(data.get("path"))
    if origin is None:
        return 0

    opened = 0
    now = datetime.now(UTC)
    async with task_session() as db:
        for ann in announcements:
            for prefix_str in ann.get("prefixes") or []:
                net = _parse_net(prefix_str)
                if net is None:
                    continue
                match = _match_tracked(net, index)
                if match is None:
                    continue
                tracked, kind = match
                if origin in expected_origin_set(tracked):
                    continue
                status = await derive_rpki_status(db, str(net), origin)
                if status == RPKI_VALID:
                    continue
                _row, was_opened = await record_detection(
                    db,
                    tracked=tracked,
                    observed_prefix=str(net),
                    observed_origin=origin,
                    detection_kind=kind,
                    rpki_status=status,
                    now=now,
                    source="ris_live",
                    detail={"peer": data.get("peer"), "path": data.get("path")},
                )
                if was_opened:
                    opened += 1
        if opened:
            await db.commit()
    return opened


async def run_ris_live_consumer() -> None:
    """Connect to RIS Live, subscribe to every tracked prefix, and
    stream UPDATE messages into the detection table. Reconnects on drop.
    """
    try:
        import websockets  # noqa: PLC0415
    except ImportError:
        logger.error(
            "ris_live_websockets_missing",
            hint="pip install websockets to run the RIS Live consumer",
        )
        return

    url = settings.bgp_ris_live_url
    logger.info("ris_live_consumer_starting", url=url)
    backoff = 1.0
    while True:
        try:
            index = await _load_tracked_index()
            all_prefixes = [str(t.prefix) for rows in index.values() for t in rows]
            if not all_prefixes:
                logger.info("ris_live_no_tracked_prefixes")
                await asyncio.sleep(_SUBSCRIPTION_REFRESH_SECONDS)
                continue

            async with websockets.connect(url, ping_interval=30) as ws:
                backoff = 1.0
                # Server-side prefix filter — one subscribe per prefix so
                # RIS only ships us UPDATEs touching a tracked prefix.
                for prefix in all_prefixes:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "ris_subscribe",
                                "data": {"prefix": prefix, "moreSpecific": True},
                            }
                        )
                    )
                logger.info("ris_live_subscribed", prefix_count=len(all_prefixes))

                last_refresh = asyncio.get_running_loop().time()
                async for raw in ws:
                    text = raw.decode() if isinstance(raw, bytes) else raw
                    await _handle_message(text, index)
                    # Periodically rebuild the subscription set.
                    if (
                        asyncio.get_running_loop().time() - last_refresh
                        > _SUBSCRIPTION_REFRESH_SECONDS
                    ):
                        break  # reconnect loop reloads + resubscribes
        except Exception as exc:  # noqa: BLE001 — keep the consumer alive across any drop
            logger.warning("ris_live_consumer_error", error=str(exc), backoff=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def main() -> None:
    if not settings.bgp_ris_live_enabled:
        logger.info(
            "ris_live_disabled",
            hint="set BGP_RIS_LIVE_ENABLED=1 to enable the real-time consumer",
        )
        return
    asyncio.run(run_ris_live_consumer())


if __name__ == "__main__":
    main()
