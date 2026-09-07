#!/usr/bin/env python3
"""Merge per-shard pytest-split duration files into one, and report balance (#1019).

Each ``Backend — Tests`` shard in ``.github/workflows/ci.yml`` runs pytest with
``--store-durations --clean-durations``, which makes pytest-split write a
``.test_durations`` containing ONLY the tests that shard executed. The
aggregator job hands every shard's file to this script, which:

1. unions them into one complete durations file — the artifact
   ``make test-durations`` downloads and commits as ``backend/.test_durations``;
2. prints how long each shard's slice actually took, and emits a GitHub
   ``::warning::`` annotation when the slowest shard is far above the mean —
   the signal that the committed file has drifted and needs refreshing.

The union is only a complete picture when EVERY shard's file is present; the
workflow gates the call on all shards having succeeded. A test present in the
committed file but in no shard file was deleted or renamed, so it is dropped —
that is how stale entries leave the file.

stdlib-only: it runs on a bare runner with nothing installed.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

# Slowest shard more than this multiple of the mean is worth a nudge. The
# observed pre-#1019 imbalance was 2.6x; a duration-balanced split lands well
# under 1.2x, so this only fires on real drift.
IMBALANCE_WARN_RATIO = 1.35

# A per-test change smaller than this is noise between runners, not drift.
CHANGED_REL_THRESHOLD = 0.25


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


def merge(shards: list[dict[str, float]]) -> dict[str, float]:
    """Union the shard maps. A test id in two shards is a misconfigured split; keep the max."""
    merged: dict[str, float] = {}
    for shard in shards:
        for key, value in shard.items():
            if key in merged:
                print(
                    f"::warning::test {key} appears in more than one shard file",
                    file=sys.stderr,
                )
                merged[key] = max(merged[key], value)
            else:
                merged[key] = value
    return merged


def balance_report(shard_totals: list[tuple[str, float]]) -> tuple[str, bool]:
    """Human-readable per-shard totals plus whether the spread crosses the warning line.

    Totals are summed per-test seconds, not wall clock (xdist runs ~4 at a
    time), but the ratio between shards is what balancing is about and that
    survives the division.
    """
    lines = ["Measured per-shard test time (sum of per-test seconds):"]
    totals = [total for _, total in shard_totals]
    mean = statistics.fmean(totals) if totals else 0.0
    for name, total in sorted(shard_totals, key=lambda item: item[1], reverse=True):
        lines.append(f"  {name:<28} {total:8.1f}s  ({total / mean:4.2f}x mean)" if mean else name)
    slowest = max(totals) if totals else 0.0
    ratio = slowest / mean if mean else 0.0
    lines.append(f"  mean {mean:.1f}s, slowest {slowest:.1f}s, ratio {ratio:.2f}x")
    return "\n".join(lines), ratio > IMBALANCE_WARN_RATIO


def drift_report(committed: dict[str, float], merged: dict[str, float]) -> str:
    """How far the committed file is from what was just measured."""
    added = merged.keys() - committed.keys()
    removed = committed.keys() - merged.keys()
    changed = 0
    for key in merged.keys() & committed.keys():
        before, after = committed[key], merged[key]
        base = max(before, 1e-3)
        if abs(after - before) / base > CHANGED_REL_THRESHOLD:
            changed += 1
    return (
        f"Against the committed file: {len(added)} tests added, {len(removed)} removed, "
        f"{changed} changed by more than {int(CHANGED_REL_THRESHOLD * 100)}%"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("shard_files", nargs="+", type=Path, help="one .test_durations per shard")
    parser.add_argument("--out", required=True, type=Path, help="where to write the merged file")
    parser.add_argument(
        "--committed",
        type=Path,
        help="the .test_durations the shards were split with, for the drift report",
    )
    args = parser.parse_args(argv)

    try:
        shards = [load_durations(path) for path in args.shard_files]
    except MergeError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    merged = merge(shards)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Same shape pytest-split writes (sort_keys + indent=4) so a refresh diffs cleanly.
    args.out.write_text(json.dumps(merged, sort_keys=True, indent=4) + "\n")
    print(f"merged {len(shards)} shard files → {args.out} ({len(merged)} tests)")

    totals = [
        (path.parent.name or path.name, sum(shard.values()))
        for path, shard in zip(args.shard_files, shards, strict=True)
    ]
    report, imbalanced = balance_report(totals)
    print(report)
    if imbalanced:
        print(
            "::warning::backend shards are imbalanced (slowest shard > "
            f"{IMBALANCE_WARN_RATIO}x the mean). Refresh backend/.test_durations with "
            "`make test-durations` and commit it."
        )

    if args.committed is not None and args.committed.exists():
        try:
            committed = load_durations(args.committed)
        except MergeError as exc:
            print(f"::warning::could not read committed durations: {exc}", file=sys.stderr)
        else:
            print(drift_report(committed, merged))
    return 0


if __name__ == "__main__":
    sys.exit(main())
