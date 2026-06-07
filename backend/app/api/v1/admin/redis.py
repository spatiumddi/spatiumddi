"""Redis insights — read-only diagnostic surface for the admin UI (#358).

Mirrors ``admin/postgres.py``: a small, superadmin-gated, degrade-friendly
read surface over the Redis we already run (Celery broker + Sentinel HA +,
since #358, the agent config-wake bus). Surfaces ``INFO`` (memory / clients
/ throughput / keyspace / replication) plus the wake-bus counters +
live channel subscriber counts from ``agent_wake.get_wake_metrics`` — so
the Platform Insights Redis tab doubles as live visibility into the
config-wake fan-out without standing up a separate exporter.

Every endpoint returns ``available: false`` + a hint on any Redis error
rather than 500ing (matches the Postgres surface), so a Redis blip never
takes the dashboard down.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import SuperAdmin
from app.config import settings
from app.core.agent_wake import get_wake_metrics
from app.core.redis_client import is_sentinel_url, make_async_redis

logger = structlog.get_logger(__name__)
router = APIRouter()

_CONNECT_TIMEOUT = 2.0


# ── Response shapes ───────────────────────────────────────────────────────


class ReplicaRow(BaseModel):
    ip: str | None = None
    port: int | None = None
    state: str | None = None


class RedisOverview(BaseModel):
    available: bool
    hint: str | None = None
    sentinel: bool = False
    redis_version: str | None = None
    role: str | None = None  # master / slave
    uptime_seconds: int | None = None
    connected_clients: int | None = None
    used_memory_bytes: int | None = None
    used_memory_peak_bytes: int | None = None
    mem_fragmentation_ratio: float | None = None
    maxmemory_bytes: int | None = None
    instantaneous_ops_per_sec: int | None = None
    keyspace_hits: int | None = None
    keyspace_misses: int | None = None
    total_commands_processed: int | None = None
    connected_replicas: int | None = None
    replicas: list[ReplicaRow] = []


class KeyspaceDb(BaseModel):
    db: str
    keys: int
    expires: int


class KeyspaceResponse(BaseModel):
    available: bool
    hint: str | None = None
    dbs: list[KeyspaceDb] = []


class WakeChannel(BaseModel):
    channel: str
    subscribers: int


class WakeBusResponse(BaseModel):
    available: bool
    hint: str | None = None
    published_by_class: dict[str, int] = {}
    active_channels: list[WakeChannel] = []
    total_subscribers: int = 0


# ── Helpers ───────────────────────────────────────────────────────────────


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("/redis/overview", response_model=RedisOverview)
async def redis_overview(_: SuperAdmin) -> RedisOverview:
    """One-shot rollup from Redis ``INFO``: version, role, memory,
    clients, throughput, replication. Degrade-friendly."""
    client = None
    try:
        client = make_async_redis(settings.redis_url, socket_connect_timeout=_CONNECT_TIMEOUT)
        info: dict[str, Any] = await client.info()
        replicas: list[ReplicaRow] = []
        connected_replicas = _to_int(info.get("connected_slaves"))
        for i in range(connected_replicas or 0):
            slave = info.get(f"slave{i}")
            if isinstance(slave, dict):
                replicas.append(
                    ReplicaRow(
                        ip=slave.get("ip"),
                        port=_to_int(slave.get("port")),
                        state=slave.get("state"),
                    )
                )
        return RedisOverview(
            available=True,
            sentinel=is_sentinel_url(settings.redis_url),
            redis_version=info.get("redis_version"),
            role=info.get("role"),
            uptime_seconds=_to_int(info.get("uptime_in_seconds")),
            connected_clients=_to_int(info.get("connected_clients")),
            used_memory_bytes=_to_int(info.get("used_memory")),
            used_memory_peak_bytes=_to_int(info.get("used_memory_peak")),
            mem_fragmentation_ratio=_to_float(info.get("mem_fragmentation_ratio")),
            maxmemory_bytes=_to_int(info.get("maxmemory")),
            instantaneous_ops_per_sec=_to_int(info.get("instantaneous_ops_per_sec")),
            keyspace_hits=_to_int(info.get("keyspace_hits")),
            keyspace_misses=_to_int(info.get("keyspace_misses")),
            total_commands_processed=_to_int(info.get("total_commands_processed")),
            connected_replicas=connected_replicas,
            replicas=replicas,
        )
    except Exception as exc:  # noqa: BLE001 — degrade, never 500 the dashboard
        logger.warning("redis_overview_failed", error=str(exc))
        return RedisOverview(available=False, hint=str(exc))
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass


@router.get("/redis/keyspace", response_model=KeyspaceResponse)
async def redis_keyspace(_: SuperAdmin) -> KeyspaceResponse:
    """Per-db key + volatile-key counts from ``INFO keyspace``."""
    client = None
    try:
        client = make_async_redis(settings.redis_url, socket_connect_timeout=_CONNECT_TIMEOUT)
        info: dict[str, Any] = await client.info("keyspace")
        dbs = [
            KeyspaceDb(
                db=str(name),
                keys=_to_int(stats.get("keys")) or 0,
                expires=_to_int(stats.get("expires")) or 0,
            )
            for name, stats in info.items()
            if isinstance(stats, dict)
        ]
        return KeyspaceResponse(available=True, dbs=dbs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis_keyspace_failed", error=str(exc))
        return KeyspaceResponse(available=False, hint=str(exc))
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass


@router.get("/redis/wake-bus", response_model=WakeBusResponse)
async def redis_wake_bus(_: SuperAdmin) -> WakeBusResponse:
    """Live agent config-wake bus (#358): publishes-by-class + the
    currently-subscribed wake channels with subscriber counts. A healthy
    fleet shows one subscriber per parked agent long-poll per channel."""
    metrics = await get_wake_metrics()
    if not metrics.get("available"):
        return WakeBusResponse(available=False, hint=metrics.get("hint"))
    return WakeBusResponse(
        available=True,
        published_by_class=metrics.get("published_by_class", {}),
        active_channels=[
            WakeChannel(channel=c["channel"], subscribers=c["subscribers"])
            for c in metrics.get("active_channels", [])
        ],
        total_subscribers=metrics.get("total_subscribers", 0),
    )
