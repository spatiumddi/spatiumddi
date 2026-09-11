"""Fold the orchestrator's shard counters into the numbers a report can print.

Pure and dependency-free so the orchestrator (perf/generators/orchestrator/
accounting.py), the report generator (collect.py) and any consumer of a
``orchestrator.shard*.summary.ndjson`` line compute the SAME handshake and DNS
figures from the same counters (#1057).

Two accounting holes made the shard summaries unreliable evidence before this
module existed:

* ``_dns_query`` counted ``dns_ok`` only for NOERROR/NXDOMAIN and
  ``dns_timeout`` only on an exception, so an answer with any other rcode was
  counted as nothing. A run whose 606k queries BIND answered REFUSED read
  "ok 0, timeouts 46" beside a 606k-sample latency histogram.
* ``_on_ack`` counted ``dora_ack`` only while the device was still
  DISCOVERING; an ACK arriving after the device had given up was counted as a
  timeout AND discarded, so the generator's handshake figure could not be
  reconciled with kea's own ACK count.

The counters are cumulative per shard; ``sum_counters`` adds shards, never
windows (a per-window row carries the cumulative value at that window's end,
so summing windows multiplies the truth — see ``dns_timeouts_from_windows``).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: rcodes that count as an answered query for ``dns_ok`` — unchanged meaning:
#: NOERROR is the positive path, NXDOMAIN the deliberate-miss slice (§1.7).
DNS_OK_RCODES = ("NOERROR", "NXDOMAIN")
RCODE_PREFIX = "dns_rcode_"


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def sum_counters(summaries: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Add every integer ``counters`` field across shard summaries.

    Dynamic ``dns_rcode_<NAME>`` keys are summed like any other counter, so a
    rcode seen by one shard only still appears in the fold.
    """
    out: dict[str, int] = {}
    for s in summaries:
        c = s.get("counters") if isinstance(s, dict) else None
        if not isinstance(c, dict):
            continue
        for k, v in c.items():
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                continue
            out[k] = out.get(k, 0) + _int(v)
    return out


def dns_rcodes(counters: dict[str, Any]) -> dict[str, int]:
    """``{"REFUSED": n, "NOERROR": m, ...}`` from the flattened counter keys."""
    return {k[len(RCODE_PREFIX):]: _int(v) for k, v in counters.items()
            if k.startswith(RCODE_PREFIX)}


def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 3) if den > 0 else None


def dns_summary(counters: dict[str, Any]) -> dict[str, Any]:
    """The DNS query stream's outcome ledger from one counter set.

    ``sent`` = ``answered + timeouts + errors`` when every query was accounted
    for; ``unaccounted`` shows any gap (queries still in flight at shutdown, or
    a generator that predates per-rcode accounting, where it equals the
    answers that were counted as nothing).
    """
    sent = _int(counters.get("dns_sent"))
    ok = _int(counters.get("dns_ok"))
    answered = _int(counters.get("dns_answered"))
    timeouts = _int(counters.get("dns_timeout"))
    errors = _int(counters.get("dns_error"))
    rcodes = dns_rcodes(counters)
    if not answered and rcodes:
        answered = sum(rcodes.values())
    not_ok = max(0, answered - ok)
    return {
        "sent": sent,
        "answered": answered,
        "ok": ok,
        "not_ok": not_ok,
        "timeouts": timeouts,
        "errors": errors,
        "unaccounted": max(0, sent - answered - timeouts - errors),
        "rcodes": dict(sorted(rcodes.items())),
        "ok_pct_of_sent": _pct(ok, sent),
        "ok_pct_of_answered": _pct(ok, answered),
        "timeout_pct_of_sent": _pct(timeouts, sent),
    }


def dns_timeouts_from_windows(stats_rows: Iterable[dict[str, Any]]) -> int:
    """Cumulative ``dns_timeout`` across shards from the PERIODIC stat rows.

    Each row carries the shard's cumulative value at the end of that window,
    so the run's total is the last (largest) value per shard, summed over
    shards — never the sum over rows, which counts every earlier window's
    timeouts again on every later row (collect.py b7 did exactly that).
    """
    per_shard: dict[Any, int] = {}
    for r in stats_rows:
        if not isinstance(r, dict):
            continue
        shard = r.get("shard", 0)
        v = _int(r.get("dns_timeout"))
        if v > per_shard.get(shard, 0):
            per_shard[shard] = v
    return sum(per_shard.values())
