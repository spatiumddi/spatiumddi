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
