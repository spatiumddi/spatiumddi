"""Acceptance / e2e stubs — mirror ``agent/dns/tests/test_acceptance.py``.

Tests that require a live kind cluster / docker daemon are marked
``@pytest.mark.e2e`` and skipped by default. Run them explicitly with::

    pytest agent/dhcp/tests -m e2e
"""

from __future__ import annotations

import pytest


@pytest.mark.e2e
def test_autoregister_within_10s() -> None:
    """`docker compose --profile dhcp up` → agent registered in <10s."""
    pytest.skip("e2e — run with docker compose up and explicit `-m e2e`")


@pytest.mark.e2e
def test_lease_flows_back_to_control_plane() -> None:
    """DORA handshake from a client → lease event posted to control plane."""
    pytest.skip("e2e — requires live Kea + DHCP client + control plane")


@pytest.mark.e2e
def test_resilient_to_cp_outage() -> None:
    """Killing api+worker does not interrupt DHCP service."""
    pytest.skip("e2e — requires docker compose stop api worker")


@pytest.mark.e2e
def test_trivy_clean() -> None:
    """Image passes trivy with no high/critical CVEs."""
    pytest.skip("e2e — delegated to CI `.github/workflows/build-dhcp-images.yml`")


@pytest.mark.e2e
def test_helm_chart_primary_secondary() -> None:
    """Helm chart deploys primary+secondary in kind; both serve leases."""
    pytest.skip("e2e — requires kind cluster + helm install")
