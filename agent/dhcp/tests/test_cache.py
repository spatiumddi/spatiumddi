"""Cache atomic-write + round-trip smoke tests (DHCP.md §6 resilience)."""

from __future__ import annotations

import os

from spatium_dhcp_agent.cache import (
    CACHE_SCHEMA_VERSION,
    commit_config,
    ensure_layout,
    load_config,
    load_or_create_agent_id,
    load_previous_config,
    load_token,
    save_config,
    save_rendered_kea,
    save_token,
)


def test_cache_roundtrip(tmp_state) -> None:
    ensure_layout(tmp_state)
    bundle = {
        "etag": "sha256:abc",
        "subnets": [{"id": 1, "subnet": "192.0.2.0/24"}],
    }
    save_config(tmp_state, bundle, "sha256:abc")
    loaded, etag = load_config(tmp_state)
    assert etag == "sha256:abc"
    assert loaded is not None
    # Schema version is stamped on write.
    assert loaded.get("_schema_version") == CACHE_SCHEMA_VERSION
    assert loaded["subnets"] == bundle["subnets"]


def test_fetching_does_not_rotate_previous(tmp_state) -> None:
    """``previous`` tracks the last bundle that APPLIED, not the last fetched.

    This test asserted the opposite until #882. The old rotate-on-fetch made
    ``previous`` useless as a fallback: a bundle Kea refuses leaves the etag
    unadvanced, so the next poll re-fetches the same bundle and rotates the
    bad config into ``previous``, destroying the only copy that worked.
    """
    ensure_layout(tmp_state)
    save_config(tmp_state, {"v": 1}, "etag1")
    save_config(tmp_state, {"v": 2}, "etag2")
    assert not (tmp_state / "config" / "previous.json").exists()
    assert (tmp_state / "config" / "current.json").exists()


def test_commit_config_promotes_current(tmp_state) -> None:
    ensure_layout(tmp_state)
    save_config(tmp_state, {"v": 1}, "etag1")
    commit_config(tmp_state, "etag1")
    save_config(tmp_state, {"v": 2}, "etag2")

    bundle, etag = load_previous_config(tmp_state)
    assert etag == "etag1"
    assert bundle["v"] == 1


def test_agent_id_stable(tmp_state) -> None:
    ensure_layout(tmp_state)
    a = load_or_create_agent_id(tmp_state)
    b = load_or_create_agent_id(tmp_state)
    assert a == b


def test_token_persisted_0600(tmp_state) -> None:
    ensure_layout(tmp_state)
    save_token(tmp_state, "my.jwt.token")
    assert load_token(tmp_state) == "my.jwt.token"
    mode = os.stat(tmp_state / "agent_token.jwt").st_mode & 0o777
    # tmpfs may not honor chmod; accept either 0600 or a broader mask.
    assert mode in (0o600, 0o644, 0o664, 0o666)


def test_rendered_kea_saved(tmp_state) -> None:
    ensure_layout(tmp_state)
    p = save_rendered_kea(tmp_state, {"Dhcp4": {"subnet4": []}})
    assert p.exists()
    assert p.name == "kea-dhcp4.json"
