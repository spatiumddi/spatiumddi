"""Is a reused DNS group's dataset the one this manifest asked for? (#981)

The seeder skips its bulk record load when the forward zone already
exists, so a re-run against a pre-seeded group (``SPDDI_PERF_DNS_GROUP_ID``)
does not re-POST records that are already there and blow the loader's 1%
failure cap.

That skip was gated on the forward zone's *existence* alone, which says
nothing about its size — and every shipped manifest names the same
forward zone. Until the #981 zone lookup was fixed the gate was
unreachable (the lookup raised first), so the mismatch never had a chance
to bite. With the lookup fixed it can, and it fails silently: run
``smoke.yaml`` then ``300k-ceiling.yaml`` against one group and the 300k
load is skipped, the fresh reverse zones stay empty, every PTR NXDOMAINs,
and the run exits 0 reporting a 300k-ceiling result measured on smoke's
10k dataset.

The decision lives here, pure and dependency-free, so it can be tested
without the seeder's ``httpx`` import; the seeder does the counting, the
logging and the raising.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Reuse is fine — skip the load.
OK = "ok"
#: Reuse is fine but the zone is bigger than the manifest declares.
LARGER = "larger"
#: Reuse would measure against a dataset that is missing records.
SHORT = "short"
#: The forward zone was reused while a reverse zone was created empty.
EMPTY_REVERSE = "empty_reverse"


def reused_dataset_verdict(
    *,
    present: int,
    planned: int,
    fresh_reverse_zones: Iterable[str],
) -> tuple[str, str]:
    """Classify a reused group, returning ``(verdict, detail)``.

    ``present`` is how many records the reused forward zone holds and
    ``planned`` how many this manifest would load into it.
    ``fresh_reverse_zones`` are reverse zones this run created (i.e. that
    the skipped load would leave empty).

    * ``SHORT`` — fewer records than planned. Every query for a name that
      was never loaded is an NXDOMAIN, so the DNS test measures the
      negative path and the report presents it as the positive one.
    * ``EMPTY_REVERSE`` — the forward zone was reused but a reverse zone
      was created empty this run (a manifest with a different
      ``reverse_zone_shape``); skipping leaves it that way and every PTR
      NXDOMAINs. Checked even when the forward count is fine, because the
      two are independent.
    * ``LARGER`` — more records than planned. Not an error: every planned
      name still resolves, so the run is valid, just measured against a
      larger authoritative set than the manifest declares. Some excess is
      normal — the zone's own SOA/NS rows, and any ``perf-op-*`` records a
      previous run's mutation stream left behind.
    * ``OK`` — exactly as planned.

    ``EMPTY_REVERSE`` outranks ``SHORT``: it is the more specific finding
    and the detail names both.
    """
    fresh = sorted(fresh_reverse_zones)
    if present < planned:
        detail = f"holds {present} record(s) but this manifest plans {planned}"
        if fresh:
            return EMPTY_REVERSE, (
                f"{detail}; reverse zone(s) {fresh} were also created empty this run"
            )
        return SHORT, detail
    if fresh:
        return EMPTY_REVERSE, (
            f"reverse zone(s) {fresh} were created empty this run and the skipped load "
            f"would leave them that way (every PTR would NXDOMAIN)"
        )
    if present > planned:
        return LARGER, f"holds {present} record(s); this manifest plans {planned}"
    return OK, ""
