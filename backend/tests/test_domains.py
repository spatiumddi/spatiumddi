"""Unit tests for the ``derive_whois_state`` decision tree + the RDAP
parser helpers.

The full HTTP CRUD path is covered indirectly by the existing FastAPI
integration test fixture set; this module focuses on the pure
business-logic helpers so the bucket assignment is locked down.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.api.v1.domains.router import derive_whois_state
from app.services.rdap import (
    _extract_dnssec_signed,
    _extract_event_dates,
    _extract_nameservers,
    _extract_registrant_org,
    _extract_registrar,
    _parse_rdap_datetime,
)


# ── derive_whois_state ─────────────────────────────────────────────────


def test_unreachable_when_rdap_returned_none() -> None:
    """RDAP failure short-circuits the entire decision tree."""
    assert (
        derive_whois_state(
            rdap_returned_data=False,
            expires_at=datetime.now(UTC) + timedelta(days=365),
            expected_nameservers=[],
            actual_nameservers=[],
        )
        == "unreachable"
    )


def test_expired_when_expiry_in_past() -> None:
    assert (
        derive_whois_state(
            rdap_returned_data=True,
            expires_at=datetime.now(UTC) - timedelta(days=1),
            expected_nameservers=[],
            actual_nameservers=[],
        )
        == "expired"
    )


def test_expiring_when_within_30_days() -> None:
    """Anything inside the 30-day window collapses to ``expiring`` —
    the alert evaluator (deferred) carries the soft / warn / critical
    sub-buckets.
    """
    assert (
        derive_whois_state(
            rdap_returned_data=True,
            expires_at=datetime.now(UTC) + timedelta(days=15),
            expected_nameservers=[],
            actual_nameservers=[],
        )
        == "expiring"
    )


def test_drift_only_when_expected_pinned() -> None:
    """No expected NS = drift detection disabled; row stays ``ok``."""
    assert (
        derive_whois_state(
            rdap_returned_data=True,
            expires_at=datetime.now(UTC) + timedelta(days=365),
            expected_nameservers=[],
            actual_nameservers=["ns1.example.com", "ns2.example.com"],
        )
        == "ok"
    )


def test_drift_when_expected_differs_from_actual() -> None:
    assert (
        derive_whois_state(
            rdap_returned_data=True,
            expires_at=datetime.now(UTC) + timedelta(days=365),
            expected_nameservers=["ns1.example.com", "ns2.example.com"],
            actual_nameservers=["ns3.example.com", "ns4.example.com"],
        )
        == "drift"
    )


def test_drift_normalises_case_and_trailing_dot() -> None:
    """``Ns1.example.com.`` and ``ns1.example.com`` should compare equal."""
    state = derive_whois_state(
        rdap_returned_data=True,
        expires_at=datetime.now(UTC) + timedelta(days=365),
        expected_nameservers=["NS1.Example.COM.", "ns2.example.com"],
        actual_nameservers=["ns1.example.com", "ns2.example.com."],
    )
    assert state == "ok"


def test_expiry_takes_precedence_over_drift() -> None:
    """Operator gets the more urgent label first."""
    state = derive_whois_state(
        rdap_returned_data=True,
        expires_at=datetime.now(UTC) + timedelta(days=10),
        expected_nameservers=["ns1.example.com"],
        actual_nameservers=["ns2.example.com"],
    )
    assert state == "expiring"


def test_no_expiry_no_drift_returns_ok() -> None:
    """Domain without an expiry datetime + matching NS = ``ok``."""
    state = derive_whois_state(
        rdap_returned_data=True,
        expires_at=None,
        expected_nameservers=["ns1.example.com"],
        actual_nameservers=["ns1.example.com"],
    )
    assert state == "ok"


# ── RDAP parsing helpers ──────────────────────────────────────────────


def test_parse_rdap_datetime_handles_zulu() -> None:
    parsed = _parse_rdap_datetime("2026-04-30T12:00:00Z")
    assert parsed is not None
    assert parsed.year == 2026
    assert parsed.tzinfo is not None


def test_parse_rdap_datetime_handles_microseconds() -> None:
    parsed = _parse_rdap_datetime("2026-04-30T12:00:00.123456Z")
    assert parsed is not None
    assert parsed.microsecond == 123456


def test_parse_rdap_datetime_returns_none_on_garbage() -> None:
    assert _parse_rdap_datetime("not a date") is None
    assert _parse_rdap_datetime("") is None
    assert _parse_rdap_datetime(None) is None  # type: ignore[arg-type]


def test_extract_event_dates_picks_registration_and_expiration() -> None:
    events = [
        {"eventAction": "registration", "eventDate": "2020-01-01T00:00:00Z"},
        {"eventAction": "expiration", "eventDate": "2027-01-01T00:00:00Z"},
        {"eventAction": "last changed", "eventDate": "2025-06-15T00:00:00Z"},
    ]
    dates = _extract_event_dates(events)
    assert dates["registered_at"] is not None
    assert dates["registered_at"].year == 2020
    assert dates["expires_at"] is not None
    assert dates["expires_at"].year == 2027
    assert dates["last_renewed_at"] is not None
    assert dates["last_renewed_at"].year == 2025


def test_extract_event_dates_handles_missing_array() -> None:
    assert _extract_event_dates(None) == {
        "registered_at": None,
        "expires_at": None,
        "last_renewed_at": None,
    }


def test_extract_registrar_pulls_fn_from_vcard() -> None:
    entities = [
        {
            "roles": ["registrar"],
            "vcardArray": [
                "vcard",
                [
                    ["version", {}, "text", "4.0"],
                    ["fn", {}, "text", "GoDaddy.com, LLC"],
                ],
            ],
        }
    ]
    assert _extract_registrar(entities) == "GoDaddy.com, LLC"


def test_extract_registrant_org_prefers_org_over_fn() -> None:
    entities = [
        {
            "roles": ["registrant"],
            "vcardArray": [
                "vcard",
                [
                    ["fn", {}, "text", "John Doe"],
                    ["org", {}, "text", "Example Corp"],
                ],
            ],
        }
    ]
    assert _extract_registrant_org(entities) == "Example Corp"


def test_extract_nameservers_lowercases_sorts_dedupes() -> None:
    payload = {
        "nameservers": [
            {"ldhName": "NS2.example.com."},
            {"ldhName": "ns1.example.com"},
            {"ldhName": "NS1.example.com"},  # dup with case difference
        ]
    }
    assert _extract_nameservers(payload) == [
        "ns1.example.com",
        "ns2.example.com",
    ]


def test_extract_dnssec_signed_handles_explicit_flag() -> None:
    assert _extract_dnssec_signed({"secureDNS": {"delegationSigned": True}}) is True
    assert _extract_dnssec_signed({"secureDNS": {"delegationSigned": False}}) is False
    assert _extract_dnssec_signed({}) is False


def test_extract_dnssec_signed_falls_back_to_ds_data() -> None:
    payload = {"secureDNS": {"dsData": [{"keyTag": 12345}]}}
    assert _extract_dnssec_signed(payload) is True
