"""Unit tests for the Redis pub/sub agent wake helper (#358).

Covers the channel grammar, the fire-and-forget publish, the
request-scoped collector, and — most importantly — the #5
fallback-safety contract: any Redis failure degrades to a sleep +
``UNAVAILABLE`` rather than raising, so a Redis-down deployment behaves
exactly like the pre-#358 fixed-tick poll.

No live Redis: ``make_async_redis`` is monkeypatched to a small fake,
matching the convention in ``test_redis_client.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import app.core.agent_wake as aw
from app.core.agent_wake import (
    HOSTCONFIG_ALL,
    WakeResult,
    collect_wake,
    collecting_wakes,
    dhcp_wake_channels,
    dns_group_channel,
    dns_wake_channels,
    get_wake_metrics,
    publish_wake,
    wake_subscription,
)

# ── Fakes ──────────────────────────────────────────────────────────────────────


class FakePubSub:
    def __init__(
        self, messages: list[dict[str, Any]] | None = None, fail_get: bool = False
    ) -> None:
        self._messages = list(messages or [])
        self._fail_get = fail_get
        self.subscribed: list[str] = []
        self.unsubscribed = False
        self.closed = False

    async def subscribe(self, *channels: str) -> None:
        self.subscribed.extend(channels)

    async def get_message(
        self, ignore_subscribe_messages: bool = True, timeout: float | None = None
    ) -> Any:
        if self._fail_get:
            raise RuntimeError("connection reset")
        if self._messages:
            return self._messages.pop(0)
        return None  # no message within the tick

    async def unsubscribe(self, *a: Any) -> None:
        self.unsubscribed = True

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    def __init__(
        self,
        pubsub: FakePubSub | None = None,
        fail_publish: bool = False,
        keys: dict[str, int] | None = None,
        channels: list[str] | None = None,
        numsub: list[tuple[str, int]] | None = None,
    ) -> None:
        self._pubsub = pubsub or FakePubSub()
        self._fail_publish = fail_publish
        self._keys = keys or {}
        self._channels = channels or []
        self._numsub = numsub or []
        self.published: list[tuple[str, str]] = []
        self.incrs: list[str] = []
        self.closed = False

    def pubsub(self) -> FakePubSub:
        return self._pubsub

    async def publish(self, channel: str, message: str) -> None:
        if self._fail_publish:
            raise RuntimeError("redis down")
        self.published.append((channel, message))

    async def incr(self, key: str) -> None:
        self.incrs.append(key)

    async def scan_iter(self, match: str | None = None) -> Any:
        for k in self._keys:
            yield k

    async def get(self, key: str) -> Any:
        return self._keys.get(key)

    async def pubsub_channels(self, pattern: str) -> list[str]:
        return list(self._channels)

    async def pubsub_numsub(self, *channels: str) -> list[tuple[str, int]]:
        return list(self._numsub)

    async def aclose(self) -> None:
        self.closed = True


def _patch_redis(
    monkeypatch: Any, client: FakeRedis | None, *, raise_on_connect: bool = False
) -> None:
    def factory(url: str, **kwargs: Any) -> FakeRedis:
        if raise_on_connect:
            raise RuntimeError("cannot connect")
        assert client is not None
        return client

    monkeypatch.setattr(aw, "make_async_redis", factory)


# ── Channel grammar ─────────────────────────────────────────────────────────────


def test_channel_builders_exact_strings() -> None:
    gid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    sid = uuid.UUID("22222222-2222-2222-2222-222222222222")
    assert dns_group_channel(gid) == f"spatium:wake:dns:group:{gid}"
    assert HOSTCONFIG_ALL == "spatium:wake:hostconfig:all"

    dns_srv = SimpleNamespace(group_id=gid, id=sid)
    assert dns_wake_channels(dns_srv) == [
        f"spatium:wake:dns:group:{gid}",
        f"spatium:wake:dns:server:{sid}",
    ]


def test_dhcp_channels_omit_group_when_ungrouped() -> None:
    sid = uuid.UUID("33333333-3333-3333-3333-333333333333")
    gid = uuid.UUID("44444444-4444-4444-4444-444444444444")

    grouped = dhcp_wake_channels(SimpleNamespace(id=sid, server_group_id=gid))
    assert grouped == [
        f"spatium:wake:dhcp:server:{sid}",
        f"spatium:wake:dhcp:group:{gid}",
        HOSTCONFIG_ALL,
    ]

    ungrouped = dhcp_wake_channels(SimpleNamespace(id=sid, server_group_id=None))
    assert ungrouped == [f"spatium:wake:dhcp:server:{sid}", HOSTCONFIG_ALL]


# ── publish_wake ─────────────────────────────────────────────────────────────────


async def test_publish_wake_dedupes_and_increments_metric(monkeypatch: Any) -> None:
    client = FakeRedis()
    _patch_redis(monkeypatch, client)
    await publish_wake("spatium:wake:dns:group:a", "spatium:wake:dns:group:a", HOSTCONFIG_ALL)
    assert client.published == [
        ("spatium:wake:dns:group:a", "1"),
        ("spatium:wake:hostconfig:all", "1"),
    ]
    # one metric incr per (deduped) channel, keyed by class
    assert client.incrs == [
        "spatium:wake:metrics:published:dns",
        "spatium:wake:metrics:published:hostconfig",
    ]
    assert client.closed is True


async def test_publish_wake_swallows_connect_error(monkeypatch: Any) -> None:
    _patch_redis(monkeypatch, None, raise_on_connect=True)
    # Must NOT raise — a Redis outage can't be allowed to 500 a CRUD write.
    await publish_wake("spatium:wake:dns:group:a")


async def test_publish_wake_swallows_publish_error(monkeypatch: Any) -> None:
    client = FakeRedis(fail_publish=True)
    _patch_redis(monkeypatch, client)
    await publish_wake("spatium:wake:dns:group:a")
    assert client.closed is True  # still tore the connection down


async def test_publish_wake_empty_is_noop(monkeypatch: Any) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("make_async_redis must not be called for an empty publish")

    monkeypatch.setattr(aw, "make_async_redis", boom)
    await publish_wake()  # no channels → no connection at all


# ── collector ────────────────────────────────────────────────────────────────────


async def test_collecting_wakes_publishes_once_deduped(monkeypatch: Any) -> None:
    captured: list[tuple[str, ...]] = []

    async def fake_publish(*channels: str) -> None:
        captured.append(channels)

    monkeypatch.setattr(aw, "publish_wake", fake_publish)
    async with collecting_wakes():
        collect_wake("spatium:wake:dns:group:a")
        collect_wake("spatium:wake:dns:group:a")  # dup
        collect_wake("spatium:wake:dns:group:b")
    assert len(captured) == 1
    assert set(captured[0]) == {"spatium:wake:dns:group:a", "spatium:wake:dns:group:b"}


async def test_collecting_wakes_no_publish_when_empty(monkeypatch: Any) -> None:
    called = False

    async def fake_publish(*channels: str) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(aw, "publish_wake", fake_publish)
    async with collecting_wakes():
        pass
    assert called is False


async def test_collecting_wakes_drops_batch_on_exception(monkeypatch: Any) -> None:
    called = False

    async def fake_publish(*channels: str) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(aw, "publish_wake", fake_publish)
    try:
        async with collecting_wakes():
            collect_wake("spatium:wake:dns:group:a")
            raise ValueError("rolled back")
    except ValueError:
        pass
    # The mutation rolled back, so there's nothing to wake.
    assert called is False


def test_collect_wake_is_noop_without_active_collector() -> None:
    # Outside collecting_wakes the collector is None — single-op callers
    # publish inline instead, so this must be a harmless no-op.
    collect_wake("spatium:wake:dns:group:a")


# ── wait / subscription (the #5 fallback contract) ───────────────────────────────


async def test_wait_returns_wake_on_message(monkeypatch: Any) -> None:
    pubsub = FakePubSub(messages=[{"type": "message", "data": b"1"}])
    _patch_redis(monkeypatch, FakeRedis(pubsub=pubsub))
    async with wake_subscription(["spatium:wake:dns:group:a"]) as wake:
        assert pubsub.subscribed == ["spatium:wake:dns:group:a"]
        assert await wake.wait(0.5) == WakeResult.WAKE
    # torn down on context exit
    assert pubsub.unsubscribed is True
    assert pubsub.closed is True


async def test_wait_returns_timeout_when_no_message(monkeypatch: Any) -> None:
    _patch_redis(monkeypatch, FakeRedis(pubsub=FakePubSub(messages=[])))
    async with wake_subscription(["spatium:wake:dhcp:server:a"]) as wake:
        assert await wake.wait(0.05) == WakeResult.TIMEOUT


async def test_subscribe_failure_degrades_to_unavailable(monkeypatch: Any) -> None:
    _patch_redis(monkeypatch, None, raise_on_connect=True)
    # Redis down: the context still yields a (degraded) subscription so the
    # caller's loop is identical; wait sleeps the (bounded) fallback + UNAVAILABLE.
    async with wake_subscription(["spatium:wake:dns:group:a"]) as wake:
        assert wake.degraded is True
        assert await wake.wait(0.01) == WakeResult.UNAVAILABLE


async def test_get_message_failure_degrades_mid_wait(monkeypatch: Any) -> None:
    pubsub = FakePubSub(fail_get=True)
    _patch_redis(monkeypatch, FakeRedis(pubsub=pubsub))
    async with wake_subscription(["spatium:wake:dns:group:a"]) as wake:
        assert wake.degraded is False
        assert await wake.wait(0.01) == WakeResult.UNAVAILABLE
        assert wake.degraded is True  # stays degraded for the rest of the request


# ── metrics ──────────────────────────────────────────────────────────────────────


async def test_get_wake_metrics_success(monkeypatch: Any) -> None:
    client = FakeRedis(
        keys={"spatium:wake:metrics:published:dns": 7, "spatium:wake:metrics:published:dhcp": 3},
        channels=["spatium:wake:dns:group:a", "spatium:wake:dhcp:server:b"],
        numsub=[("spatium:wake:dns:group:a", 2), ("spatium:wake:dhcp:server:b", 1)],
    )
    _patch_redis(monkeypatch, client)
    metrics = await get_wake_metrics()
    assert metrics["available"] is True
    assert metrics["published_by_class"] == {"dns": 7, "dhcp": 3}
    assert metrics["total_subscribers"] == 3
    assert {c["channel"] for c in metrics["active_channels"]} == {
        "spatium:wake:dns:group:a",
        "spatium:wake:dhcp:server:b",
    }


async def test_get_wake_metrics_degrades_on_error(monkeypatch: Any) -> None:
    _patch_redis(monkeypatch, None, raise_on_connect=True)
    metrics = await get_wake_metrics()
    assert metrics["available"] is False
    assert "hint" in metrics
