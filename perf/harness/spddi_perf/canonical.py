"""Canonical numbers — the single source of truth (docs/PERFORMANCE_TESTING.md §0.A).

Every rate/curve figure in the suite derives from here so a correction lands in one
place and propagates everywhere. The load-bearing correction baked in: **Kea's
``renew-timer`` is HARDCODED to 900s** (``render_kea.py:641``), independent of lease
time — so the steady renewal floor is ``D_online / 900``, NOT ``D_online / (lease/2)``.

The §1.3 hourly diurnal table is the authoritative reference curve for the 250k
headline run. ``setpoints.py`` samples it (with interpolation) and scales it by the
manifest's peak knobs for non-headline profiles (300k ceiling, smoke, etc.).
"""

from __future__ import annotations

# --- Renewal cadence (VERIFIED, not derived from lease time) ---------------------
T1_RENEW_S = 900       # Kea renew-timer, hardcoded (render_kea.py:641). Confirm Phase 0.
T2_REBIND_S = 1800     # Kea rebind-timer, hardcoded.

# --- Population headline (§0.A) --------------------------------------------------
D_TOTAL_HEADLINE = 250_000     # unique devices seen across 24h
D_TOTAL_STRETCH = 300_000      # ceiling cut
PEAK_ONLINE = 150_000          # peak concurrent active leases (08:00)
ONLINE_FLOOR = 52_000          # overnight trough (03:00-05:00)

# --- DNS workload headline (§1.7) ------------------------------------------------
DNS_QPS_SUSTAINED_PEAK = 18_000     # busy-hour sustained authoritative qps (a stated input)
DNS_QPS_BURST_CEILING = 120_000     # raw-protocol resperf/dnsperf ramp target only

# --- The §1.3 hourly diurnal table (index 0..23 = local hour, headline 250k run) --
# These three arrays are internally consistent and are THE reference curve.
HOURS = tuple(range(24))

# Devices holding an active lease at the END of each hour.
ONLINE_BY_HOUR = (
    60_000, 56_000, 53_000, 52_000, 52_000, 55_000,      # 00..05 overnight/trough
    70_000, 105_000, 145_000, 150_000, 150_000, 149_000,  # 06..11 surge -> plateau
    142_000, 148_000, 150_000, 148_000, 130_000, 110_000,  # 12..17 lunch -> departure
    125_000, 135_000, 130_000, 120_000, 100_000, 80_000,   # 18..23 dorm peak -> wind-down
)

# First-lease DORA per second (= new arrivals this hour / 3600).
DORA_PER_S_BY_HOUR = (
    0.4, 0.3, 0.2, 0.2, 0.2, 0.7,
    3.3, 9.7, 12.5, 5.0, 2.2, 1.9,
    1.7, 3.9, 2.5, 1.9, 1.4, 1.1,
    4.4, 3.9, 2.5, 1.7, 1.1, 0.7,
)

# Sustained DNS queries per second (the curve value, NOT the burst ceiling).
DNS_QPS_BY_HOUR = (
    1_200, 1_120, 1_060, 1_040, 1_040, 1_200,
    2_800, 9_000, 18_000, 16_000, 15_000, 15_000,
    13_000, 14_000, 16_000, 15_000, 12_000, 9_000,
    13_000, 17_000, 15_000, 12_000, 7_000, 4_000,
)

# New arrivals per hour (∑ ≈ 253k ≈ D_TOTAL_HEADLINE). Reference / sum-check only.
ARRIVALS_BY_HOUR = (
    1_500, 1_000, 800, 600, 800, 2_500,
    12_000, 35_000, 45_000, 18_000, 8_000, 7_000,
    6_000, 14_000, 9_000, 7_000, 5_000, 4_000,
    16_000, 14_000, 9_000, 6_000, 4_000, 2_500,
)

assert len(ONLINE_BY_HOUR) == len(DORA_PER_S_BY_HOUR) == len(DNS_QPS_BY_HOUR) == 24
assert len(ARRIVALS_BY_HOUR) == 24


def renew_per_s(online: float) -> float:
    """Steady-state renewals/sec for ``online`` concurrent devices (= online / 900s)."""
    return online / T1_RENEW_S


def lease_events_per_s(dora_per_s: float, online: float) -> float:
    """lease-events/sec that hit the control plane = DORA + renewals (§0.A)."""
    return dora_per_s + renew_per_s(online)


def _interp(table: tuple, hour: float) -> float:
    """Linear interpolation of a 24-entry hourly table at fractional ``hour`` (wraps)."""
    hour = hour % 24.0
    lo = int(hour)
    hi = (lo + 1) % 24
    frac = hour - lo
    return table[lo] * (1.0 - frac) + table[hi] * frac


def reference_point(hour: float) -> dict:
    """Interpolated reference (250k headline) curve at fractional local ``hour`` [0,24).

    Returns absolute headline values; ``setpoints.sample_diurnal`` scales these by
    the manifest's peak knobs for other profiles. ``renew_per_s`` is always derived
    from the (scaled) online count, never stored, so the 900s correction can't drift.
    """
    online = _interp(ONLINE_BY_HOUR, hour)
    dora = _interp(DORA_PER_S_BY_HOUR, hour)
    qps = _interp(DNS_QPS_BY_HOUR, hour)
    return {
        "online": online,
        "dora_per_s": dora,
        "renew_per_s": renew_per_s(online),
        "lease_events_per_s": lease_events_per_s(dora, online),
        "dns_qps": qps,
    }
