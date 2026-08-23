"""Unit tests for the Sentinel-aware Redis URL parsing (#272 Phase 3).

Pure-logic coverage of ``is_sentinel_url`` + ``_parse_sentinel_url``;
the actual ``make_async_redis`` / ``make_sync_redis`` construction
needs a live Sentinel so it's exercised in cluster e2e, not here.
"""

from __future__ import annotations

from app.core.redis_client import _parse_sentinel_url, is_sentinel_url


def test_is_sentinel_url() -> None:
    assert is_sentinel_url("sentinel://h:26379/0")
    assert is_sentinel_url("redis+sentinel://h:26379/0")
    assert not is_sentinel_url("redis://h:6379/0")
    assert not is_sentinel_url("rediss://h:6379/0")


def test_parse_sentinel_url_multi_host_with_password() -> None:
    hosts, db, password = _parse_sentinel_url("sentinel://:s3cret@h1:26379,h2:26379,h3:26379/2")
    assert hosts == [("h1", 26379), ("h2", 26379), ("h3", 26379)]
    assert db == 2
    assert password == "s3cret"


def test_parse_sentinel_url_defaults_port_and_db() -> None:
    # Bare host (no port) defaults to 26379; missing db defaults to 0.
    hosts, db, password = _parse_sentinel_url("sentinel://a:26379,b/0")
    assert hosts == [("a", 26379), ("b", 26379)]
    assert db == 0
    assert password is None


def test_parse_sentinel_url_no_db_path() -> None:
    hosts, db, password = _parse_sentinel_url("sentinel://only:26379")
    assert hosts == [("only", 26379)]
    assert db == 0
    assert password is None


def test_sentinel_kwargs_propagates_socket_knobs() -> None:
    # #590 — the sentinel hops must inherit the caller's timeouts: without
    # them, connecting to a sentinel whose pod died with its node (but whose
    # FQDN still resolves for the ~20-40s until Kubernetes marks the node's
    # pods not-ready) hangs for the OS TCP timeout, and the api readiness
    # check rides that hang straight through the kubelet's 1s probe timeout.
    from app.core.redis_client import _sentinel_kwargs

    out = _sentinel_kwargs(
        "pw",
        {
            "socket_connect_timeout": 2,
            "socket_timeout": 2,
            "socket_keepalive": True,
            "db": 0,  # not a socket knob — must not leak
            "decode_responses": True,  # not a socket knob — must not leak
        },
    )
    assert out == {
        "socket_connect_timeout": 2,
        "socket_timeout": 2,
        "socket_keepalive": True,
        "password": "pw",
    }


def test_sentinel_kwargs_without_password_or_knobs() -> None:
    from app.core.redis_client import _sentinel_kwargs

    assert _sentinel_kwargs(None, {}) == {}
    assert _sentinel_kwargs(None, {"socket_timeout": 5}) == {"socket_timeout": 5}


# ── Default connect timeout (#925) ───────────────────────────────────
#
# #590 bounded the sentinel hops but did it at each call site. #925 is the
# call site that missed: the beat heartbeat passed no timeout at all, so
# its connect to a rebooting node's still-resolving sentinel was unbounded
# — measured still blocked at 60 s, against ~28 s to a clean
# MasterNotFoundError once bounded. These pin the default that makes the
# omission unrepresentable.


def test_default_connect_timeout_is_applied_when_caller_passes_nothing() -> None:
    from app.core.redis_client import DEFAULT_CONNECT_TIMEOUT_SECONDS, _with_default_timeouts

    assert _with_default_timeouts({}) == {"socket_connect_timeout": DEFAULT_CONNECT_TIMEOUT_SECONDS}


def test_caller_supplied_connect_timeout_wins() -> None:
    from app.core.redis_client import _with_default_timeouts

    assert _with_default_timeouts({"socket_connect_timeout": 0.5}) == {
        "socket_connect_timeout": 0.5
    }


def test_socket_timeout_is_never_defaulted() -> None:
    """The read timeout must stay opt-in.

    ``core.agent_wake`` parks a pub/sub connection waiting for a message
    that is *supposed* to be slow. A defaulted ``socket_timeout`` would tear
    that read down mid-wait and turn the wake bus into a reconnect loop, so
    the default deliberately bounds the connect only.
    """
    from app.core.redis_client import _with_default_timeouts

    assert "socket_timeout" not in _with_default_timeouts({})
    assert _with_default_timeouts({"socket_timeout": 9})["socket_timeout"] == 9


def test_default_reaches_a_plain_url_client() -> None:
    """End to end through the real constructor, not just the helper."""
    from app.core.redis_client import DEFAULT_CONNECT_TIMEOUT_SECONDS, make_async_redis

    client = make_async_redis("redis://127.0.0.1:6379/0")
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_connect_timeout"] == DEFAULT_CONNECT_TIMEOUT_SECONDS


def test_default_reaches_the_sentinel_hops() -> None:
    """The hops are the half that hung — a default that stops at the master
    connection would leave the actual #925 path unbounded."""
    from app.core.redis_client import (
        DEFAULT_CONNECT_TIMEOUT_SECONDS,
        _sentinel_kwargs,
        _with_default_timeouts,
    )

    hop_kwargs = _sentinel_kwargs(None, _with_default_timeouts({}))
    assert hop_kwargs["socket_connect_timeout"] == DEFAULT_CONNECT_TIMEOUT_SECONDS
