"""The seeder refuses to skip its load onto someone else's dataset (#981).

Fixing the zone lookup made ``forward_zone_was_created`` reachable for
the first time, and the skip it gates was keyed on the forward zone's
NAME — which every shipped manifest shares. So the fix opened a silent
failure: run ``smoke.yaml`` then ``300k-ceiling.yaml`` against one reused
group and the 300k load is skipped, the fresh reverse zones stay empty,
every PTR NXDOMAINs, and the run exits 0 reporting a 300k-ceiling result
measured on smoke's 10k dataset. Before, that combination aborted loudly
(in the broken lookup) — so this guard is what keeps the fix from being a
downgrade.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spddi_perf.seed_reuse import (  # noqa: E402
    EMPTY_REVERSE,
    LARGER,
    OK,
    SHORT,
    reused_dataset_verdict,
)


def verdict(present: int, planned: int, fresh: list[str] | None = None) -> str:
    return reused_dataset_verdict(
        present=present, planned=planned, fresh_reverse_zones=fresh or []
    )[0]


def test_same_manifest_rerun_is_ok() -> None:
    assert verdict(present=10_000, planned=10_000) == OK


def test_smoke_then_300k_is_short() -> None:
    """The scenario in the docstring, in numbers."""
    assert verdict(present=10_000, planned=300_000) == SHORT


def test_soa_ns_and_leftover_mutations_are_not_short() -> None:
    """A zone carries its own SOA/NS, and a prior run's perf-op-* records."""
    assert verdict(present=10_042, planned=10_000) == LARGER


def test_larger_dataset_is_allowed_but_reported() -> None:
    v, detail = reused_dataset_verdict(present=300_000, planned=10_000, fresh_reverse_zones=[])
    assert v == LARGER
    assert "300000" in detail and "10000" in detail


def test_a_reverse_zone_created_empty_this_run_is_refused() -> None:
    """Independent of the forward count — a different reverse_zone_shape."""
    assert verdict(present=10_000, planned=10_000, fresh=["7.10.in-addr.arpa"]) == EMPTY_REVERSE


def test_empty_reverse_outranks_short_and_the_detail_names_both() -> None:
    v, detail = reused_dataset_verdict(
        present=10, planned=300, fresh_reverse_zones=["7.10.in-addr.arpa"]
    )
    assert v == EMPTY_REVERSE
    assert "300" in detail and "7.10.in-addr.arpa" in detail


def test_an_empty_reused_zone_is_short_not_ok() -> None:
    """Zero records with a plan is the worst case, not a no-op."""
    assert verdict(present=0, planned=10_000) == SHORT


def test_a_manifest_that_plans_nothing_is_ok() -> None:
    assert verdict(present=0, planned=0) == OK


def test_ok_carries_no_detail() -> None:
    assert reused_dataset_verdict(present=5, planned=5, fresh_reverse_zones=[])[1] == ""
