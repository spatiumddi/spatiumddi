"""Redis connection helper with Sentinel support (#272 Phase 3).

The app talks to Redis through ``redis.asyncio``. In a single-node
deployment ``redis_url`` is a plain ``redis://host:6379/0`` and
``aioredis.from_url`` handles it directly.

In an HA deployment (control-plane cluster, #272) Redis runs behind
Sentinel: the master can be any of N pods and moves on failover, so a
static Service can't track it. ``aioredis.from_url`` does NOT parse
``sentinel://`` URLs, so this helper detects that scheme, queries the
Sentinels for the current master, and returns a master-bound client.

Sentinel URL shape (emitted by the umbrella chart when
``redis.kind=sentinel``):

    sentinel://[:<password>@]host1:26379,host2:26379,host3:26379/<db>

The master name comes from ``settings.redis_sentinel_master``
(default ``mymaster``). The Sentinel auth password comes from
``settings.redis_sentinel_password`` when set, else the password
embedded in the URL.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import redis as redis_sync
import redis.asyncio as aioredis
from redis.asyncio.sentinel import Sentinel
from redis.sentinel import Sentinel as SentinelSync

from app.config import settings

_SENTINEL_SCHEMES = ("sentinel://", "redis+sentinel://")


def is_sentinel_url(url: str) -> bool:
    return url.startswith(_SENTINEL_SCHEMES)


def _parse_sentinel_url(url: str) -> tuple[list[tuple[str, int]], int, str | None]:
    """Return (sentinel_hosts, db, password) from a sentinel:// URL.

    netloc may carry a leading ``[:password@]`` plus a comma-separated
    host:port list. Path is the redis db index.
    """
    parsed = urlparse(url)
    password = parsed.password
    # ``parsed.netloc`` includes any ``user:pass@`` prefix; strip it to
    # get the host list (urlparse only exposes the FIRST host via
    # parsed.hostname, so we parse the raw netloc ourselves).
    hostpart = parsed.netloc.rsplit("@", 1)[-1]
    hosts: list[tuple[str, int]] = []
    for hp in hostpart.split(","):
        hp = hp.strip()
        if not hp:
            continue
        host, _, port = hp.partition(":")
        hosts.append((host, int(port) if port else 26379))
    db = 0
    path = parsed.path.lstrip("/")
    if path:
        db = int(path)
    return hosts, db, password


def make_async_redis(url: str, **kwargs: Any) -> aioredis.Redis:
    """Return an async Redis client for ``url``.

    Plain ``redis://`` / ``rediss://`` URLs go straight through
    ``from_url``. ``sentinel://`` URLs resolve the current master via
    Sentinel and return a master-bound client that re-resolves on
    reconnect (so it follows failover).
    """
    if not is_sentinel_url(url):
        return aioredis.from_url(url, **kwargs)

    hosts, db, url_password = _parse_sentinel_url(url)
    sentinel_password = settings.redis_sentinel_password or url_password
    sentinel = Sentinel(
        hosts,
        sentinel_kwargs=_sentinel_kwargs(sentinel_password, kwargs),
        password=url_password,
        **kwargs,
    )
    return sentinel.master_for(
        settings.redis_sentinel_master,
        db=db,
        password=url_password,
    )


def _sentinel_kwargs(password: str | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Connection kwargs for the SENTINEL hops, not just the master hop.

    #590 — ``**kwargs`` (socket timeouts et al.) only reaches the
    master-bound connection pool; the Sentinel objects that
    ``discover_master`` iterates get ``sentinel_kwargs``, which carried
    ONLY the password. So a caller's ``socket_connect_timeout=2`` did
    not bound the sentinel hops at all: connecting to a sentinel whose
    pod died with its node — but whose headless FQDN still resolves for
    the ~20-40 s until Kubernetes marks the node's pods not-ready —
    hangs for the OS TCP connect timeout (minutes, SYNs into a black
    hole). The api's readiness check walks exactly that path on every
    probe, so ``/health/ready`` blew straight through the kubelet's 1 s
    probe timeout and the api sat NotReady cluster-wide during exactly
    the window a node loss must be survivable (observed live 2026-07-12,
    kill_leader drill on a nested 3-node rig). Propagate the socket
    knobs so a dead sentinel costs its timeout, not minutes."""
    out: dict[str, Any] = {
        k: v for k, v in kwargs.items()
        if k in ("socket_connect_timeout", "socket_timeout", "socket_keepalive")
    }
    if password:
        out["password"] = password
    return out


def make_sync_redis(url: str, **kwargs: Any) -> redis_sync.Redis:
    """Synchronous counterpart of ``make_async_redis`` — for the Celery
    tasks (e.g. the beat heartbeat) that run outside the asyncio loop.
    """
    if not is_sentinel_url(url):
        return redis_sync.from_url(url, **kwargs)

    hosts, db, url_password = _parse_sentinel_url(url)
    sentinel_password = settings.redis_sentinel_password or url_password
    sentinel = SentinelSync(
        hosts,
        sentinel_kwargs=_sentinel_kwargs(sentinel_password, kwargs),
        password=url_password,
        **kwargs,
    )
    return sentinel.master_for(
        settings.redis_sentinel_master,
        db=db,
        password=url_password,
    )
