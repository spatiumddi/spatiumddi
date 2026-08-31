"""ROA state derivation — source-awareness of the validity window (#942).

The bug this pins: ``expiring_soon`` was derived from ``valid_to`` with
a 30-day threshold regardless of what the source's validity field
actually means. Cloudflare's ``rpki.json`` — the DEFAULT source — ships
``expires``, which is the validity of the VRP cache entry rather than of
the ROA. Measured against the live global dump on 2026-08-31: of 997,298
ROAs, 100% expired within 7 days and 79% within 2, because RPKI CAs
re-sign the objects in the validation chain on a rolling cycle of hours.

So the threshold matched every ROA, permanently. That is not a noisy
signal — it is a constant, and it fed the ``rpki_roa_expiring`` alert
rule one event per ROA (928 open events on a small dev estate).

These are pure-function tests on purpose: the failure was silent and
total, and a unit test on the ladder is the cheapest thing that catches
the next source being added without measuring its distribution first.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.tasks.rpki_roa_refresh import _LIFETIME_VALIDITY_SOURCES, _derive_roa_state

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("source", ["cloudflare", "ripe"])
def test_unknown_window_is_valid(source: str) -> None:
    """A mirror that exposes no window must not produce expiry events."""
    assert _derive_roa_state(None, NOW, source) == "valid"


@pytest.mark.parametrize(
    "valid_to",
    [
        NOW + timedelta(hours=6),
        NOW + timedelta(days=1),
        NOW + timedelta(days=2),
        NOW + timedelta(days=6),
    ],
)
def test_cloudflare_short_cycle_expiry_is_not_expiring_soon(valid_to: datetime) -> None:
    """The whole Cloudflare dump sits in this range at any moment.

    Reading it as a lifetime made the KPI equal to the total ROA count
    and stormed the alert rule. These windows are the *normal* steady
    state for that source, not a deadline.
    """
    assert _derive_roa_state(valid_to, NOW, "cloudflare") == "valid"


def test_cloudflare_past_window_is_not_expired_either() -> None:
    """A lapsed cache entry is not a lapsed ROA.

    Presence in the dump is the signal for this source — a ROA the AS
    holder actually pulled disappears and the reconcile DELETEs it. A
    ``valid_to`` in the past only means our snapshot went stale, and
    reading it as ``expired`` would turn a dead refresh task into a
    fleet-wide "every ROA expired" alert storm.
    """
    assert _derive_roa_state(NOW - timedelta(days=3), NOW, "cloudflare") == "valid"


def test_ripe_lifetime_window_keeps_the_full_ladder() -> None:
    """RIPE's ``notAfter`` is a genuine EE-certificate lifetime."""
    assert _derive_roa_state(NOW - timedelta(days=1), NOW, "ripe") == "expired"
    assert _derive_roa_state(NOW + timedelta(days=10), NOW, "ripe") == "expiring_soon"
    assert _derive_roa_state(NOW + timedelta(days=29), NOW, "ripe") == "expiring_soon"
    assert _derive_roa_state(NOW + timedelta(days=200), NOW, "ripe") == "valid"


def test_ripe_boundary_is_inclusive_at_30_days() -> None:
    assert _derive_roa_state(NOW + timedelta(days=30), NOW, "ripe") == "expiring_soon"
    assert _derive_roa_state(NOW + timedelta(days=30, seconds=1), NOW, "ripe") == "valid"


def test_unknown_source_fails_closed_to_valid() -> None:
    """An unrecognised source must not manufacture expiry events.

    ``refresh_due_roas`` already coerces the setting to a known value,
    so this is defence in depth for a future source added to the
    settings enum but not measured and listed here.
    """
    assert _derive_roa_state(NOW + timedelta(hours=1), NOW, "routinator") == "valid"


def test_cloudflare_is_not_a_lifetime_source() -> None:
    """Pins the decision itself, so re-adding it is a deliberate act.

    Anything listed here must have had its expiry distribution measured
    against the live source first — see the module docstring.
    """
    assert "cloudflare" not in _LIFETIME_VALIDITY_SOURCES
    assert "ripe" in _LIFETIME_VALIDITY_SOURCES
