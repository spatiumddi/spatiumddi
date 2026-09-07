#!/usr/bin/env python3
"""Merge per-shard pytest-split duration files into one, and report balance (#1019).

Each ``Backend — Tests`` shard in ``.github/workflows/ci.yml`` runs pytest with
``--store-durations --clean-durations``, which makes pytest-split write a
``.test_durations`` containing ONLY the tests that shard executed. The
aggregator hands every shard's file plus the committed file to this script,
which:

1. **normalizes each shard by its runner's speed.** Hosted runners vary ~2x
   run to run — one shard's trivial tests measured 1.5 s each on one run and
   10 s on the next — so a raw measurement bakes one runner's bad afternoon
   into the file and mis-weights every test that shard happened to hold. The
   committed file predicts what each shard's slice *should* have cost; the
   ratio measured/predicted is that runner's speed factor, and every test in
   the shard is divided by it. Relative cost is all a balancer needs, and
   relative cost is what survives the division.
2. **blends** the normalized measurement 50/50 with the committed value, so a
   noisy single run cannot swing a test's weight and a genuine change still
   lands within a couple of refreshes. Tests new to the file take the
   normalized measurement as-is; tests in the committed file that no shard
   ran were deleted or renamed and are dropped.
3. **reports drift**, and emits a GitHub ``::warning::`` only when the
   committed file has stopped describing relative costs — the sum of
   per-test normalized differences exceeds ``DRIFT_WARN_FRACTION`` of the
   suite, or unknown tests are a large share — never for runner variance,
   which no refresh can fix. The raw per-shard wall time is printed too, with
   each runner's speed factor, because that is where the clock went.

The union is only a complete picture when EVERY shard's file is present; the
workflow gates the call on all shards having succeeded.

stdlib-only: it runs on a bare runner with nothing installed.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

# Refresh when the committed file mis-describes this much of the suite's
# (normalized) cost. Two clean consecutive CI runs disagree by ~3 %; a batch
# of new slow tests or a fixture that got heavier shows up well above 15 %.
DRIFT_WARN_FRACTION = 0.15
# ... or when this share of the measured cost belongs to tests the committed
# file has never seen (they were split as "average", which is a guess).
UNKNOWN_WARN_FRACTION = 0.10
# Below this share of a shard's tests being known, the speed factor is a
# guess too — use 1.0 and say so.
MIN_KNOWN_SHARE = 0.5
# Blend weight of the new normalized measurement against the committed value.
BLEND = 0.5


class MergeError(Exception):
    """Input that cannot be merged — fail loudly rather than publish a partial file."""


def load_durations(path: Path) -> dict[str, float]:
    """Read one pytest-split durations file; refuse anything that is not a str->float map."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise MergeError(f"{path}: not readable as JSON ({exc})") from exc
    if not isinstance(raw, dict) or not raw:
        raise MergeError(f"{path}: expected a non-empty JSON object of test id -> seconds")
    out: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, (int, float)) or value < 0:
            raise MergeError(f"{path}: bad entry {key!r}: {value!r}")
        out[key] = float(value)
    return out


def speed_factor(shard: dict[str, float], committed: dict[str, float]) -> float | None:
    """measured / predicted over the tests the committed file knows; None if too few are known."""
    known = [k for k in shard if k in committed]
    if not shard or len(known) / len(shard) < MIN_KNOWN_SHARE:
        return None
    predicted = sum(committed[k] for k in known)
    measured = sum(shard[k] for k in known)
    if predicted <= 0 or measured <= 0:
        return None
    return measured / predicted


def merge(
    shards: list[dict[str, float]], committed: dict[str, float]
) -> tuple[dict[str, float], list[float | None]]:
    """Normalize each shard by its speed factor, blend with the committed value, union.

    Returns the merged map and the per-shard factors (None where unknown → 1.0
    was used). A test id in two shards is a misconfigured split; keep the max.
    """
    merged: dict[str, float] = {}
    factors: list[float | None] = []
    for shard in shards:
        factor = speed_factor(shard, committed)
        factors.append(factor)
        divisor = factor if factor else 1.0
        for key, raw in shard.items():
            normalized = raw / divisor
            value = (
                BLEND * normalized + (1 - BLEND) * committed[key]
                if key in committed
                else normalized
            )
            if key in merged:
                print(
                    f"::warning::test {key} appears in more than one shard file",
                    file=sys.stderr,
                )
                merged[key] = max(merged[key], value)
            else:
                merged[key] = value
    return merged, factors


def wall_report(
    names: list[str], shards: list[dict[str, float]], factors: list[float | None]
) -> str:
    """Where the clock went: raw per-shard totals and each runner's speed factor."""
    totals = [sum(s.values()) for s in shards]
    mean = statistics.fmean(totals) if totals else 0.0
    lines = ["Measured per-shard test time (sum of per-test seconds, raw):"]
    for name, total, factor in sorted(
        zip(names, totals, factors, strict=True), key=lambda t: -t[1]
    ):
        speed = f"runner {factor:4.2f}x expected" if factor else "runner speed unknown"
        share = f"({total / mean:4.2f}x mean)" if mean else ""
        lines.append(f"  {name:<28} {total:8.1f}s  {share}  {speed}")
    if mean:
        lines.append(
            f"  mean {mean:.1f}s, slowest {max(totals):.1f}s, ratio {max(totals) / mean:.2f}x — "
            "spread here is runner variance once normalized; see drift below"
        )
    return "\n".join(lines)


def drift(
    committed: dict[str, float], shards: list[dict[str, float]], factors: list[float | None]
) -> tuple[float, float, int, int]:
    """(drift fraction, unknown fraction, tests added, tests removed) on normalized data."""
    total = 0.0
    moved = 0.0
    unknown = 0.0
    seen: set[str] = set()
    for shard, factor in zip(shards, factors, strict=True):
        divisor = factor if factor else 1.0
        for key, raw in shard.items():
            seen.add(key)
            normalized = raw / divisor
            total += normalized
            if key in committed:
                moved += abs(normalized - committed[key])
            else:
                unknown += normalized
    removed = len(committed.keys() - seen)
    added = len(seen - committed.keys())
    if total <= 0:
        return 0.0, 0.0, added, removed
    return moved / total, unknown / total, added, removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("shard_files", nargs="+", type=Path, help="one .test_durations per shard")
    parser.add_argument("--out", required=True, type=Path, help="where to write the merged file")
    parser.add_argument(
        "--committed",
        type=Path,
        help="the .test_durations the shards were split with (normalization + drift baseline)",
    )
    args = parser.parse_args(argv)

    try:
        shards = [load_durations(path) for path in args.shard_files]
        committed: dict[str, float] = {}
        if args.committed is not None and args.committed.exists():
            committed = load_durations(args.committed)
    except MergeError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    merged, factors = merge(shards, committed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Same shape pytest-split writes (sort_keys + indent=4) so a refresh diffs cleanly.
    args.out.write_text(json.dumps(merged, sort_keys=True, indent=4) + "\n")
    print(f"merged {len(shards)} shard files → {args.out} ({len(merged)} tests)")

    names = [path.parent.name or path.name for path in args.shard_files]
    print(wall_report(names, shards, factors))

    if not committed:
        print("No committed durations file: shards taken as measured (no normalization).")
        return 0

    drift_frac, unknown_frac, added, removed = drift(committed, shards, factors)
    print(
        f"Against the committed file (normalized): {added} tests added, {removed} removed; "
        f"{drift_frac:.1%} of the suite's cost moved, {unknown_frac:.1%} belongs to unknown tests"
    )
    if drift_frac > DRIFT_WARN_FRACTION or unknown_frac > UNKNOWN_WARN_FRACTION:
        print(
            "::warning::backend/.test_durations no longer describes the suite's relative "
            f"costs ({drift_frac:.0%} moved, {unknown_frac:.0%} unknown). Refresh it with "
            "`make test-durations` and commit it."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
