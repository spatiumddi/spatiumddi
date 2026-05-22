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
