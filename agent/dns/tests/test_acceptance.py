"""Acceptance test stubs for the 5 criteria in docs/deployment/DNS_AGENT.md §9.

Tests that require a live kind cluster / docker daemon are marked
``@pytest.mark.e2e`` and skipped by default. Run them explicitly with::

    pytest agent/dns/tests -m e2e
"""

from __future__ import annotations

import pytest

from spatium_dns_agent.cache import (
    ensure_layout,
    load_config,
    load_or_create_agent_id,
    load_token,
    save_config,
    save_token,
)


# ── Unit-ish (no external deps) ────────────────────────────────────────────────


@pytest.fixture
def tmp_state(tmp_path):
    return tmp_path


def test_cache_roundtrip(tmp_state) -> None:
    """#3 (CP-outage resilience) smoke: bundle round-trips through disk cache."""
    ensure_layout(tmp_state)
    bundle = {"etag": "sha256:abc", "zones": [{"name": "example.com.", "type": "primary"}]}
    save_config(tmp_state, bundle, "sha256:abc")
    loaded, etag = load_config(tmp_state)
    assert etag == "sha256:abc"
    assert loaded == bundle


def test_agent_id_stable(tmp_state) -> None:
    ensure_layout(tmp_state)
    a = load_or_create_agent_id(tmp_state)
    b = load_or_create_agent_id(tmp_state)
    assert a == b


def test_token_persisted_0600(tmp_state) -> None:
    import os
    ensure_layout(tmp_state)
    save_token(tmp_state, "my.jwt.token")
    assert load_token(tmp_state) == "my.jwt.token"
    mode = os.stat(tmp_state / "agent_token.jwt").st_mode & 0o777
    assert mode == 0o600


# ── E2E (require live infrastructure) ─────────────────────────────────────────


@pytest.mark.e2e
def test_autoregister_within_10s() -> None:
    """#1: docker compose --profile dns up results in agent registered <10s.

    Requires: a running control plane, DNS_AGENT_KEY set on both sides,
    the `dns-bind9-dev` compose service running.
    """
    pytest.skip("e2e — run with docker compose up and explicit `-m e2e`")


@pytest.mark.e2e
def test_record_resolves_via_dig_within_2s() -> None:
    """#2: creating an A record via the UI → dig sees it within 2s."""
    pytest.skip("e2e — requires live BIND9 container + control plane")


@pytest.mark.e2e
def test_resilient_to_cp_outage() -> None:
    """#3: killing api+worker does not interrupt DNS resolution."""
    pytest.skip("e2e — requires docker compose stop api worker")


@pytest.mark.e2e
def test_trivy_clean() -> None:
    """#4: image passes trivy with no high/critical CVEs."""
    pytest.skip("e2e — delegated to CI `.github/workflows/build-dns-images.yml`")


@pytest.mark.e2e
def test_helm_chart_primary_secondary_axfr() -> None:
    """#5: Helm chart deploys ns1+ns2 in kind; AXFR observed in logs."""
    pytest.skip("e2e — requires kind cluster + helm install")
