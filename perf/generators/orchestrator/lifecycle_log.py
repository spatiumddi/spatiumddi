"""Lifecycle NDJSON writer + a latency accumulator (docs §3.5 / §4.8 / §3.4).

Two helpers the orchestrator leans on:

* :class:`LifecycleLog` — appends one device-state-transition record per event to
  ``rp.lifecycle`` (``generators/lifecycle.ndjson``). One line per FSM transition
  (arrival / DORA-ack / renew-ack / depart / release / lapse / re-arrival) so the
  report can reconstruct the per-device timeline and the §8.2.4 lease ledger.

* :class:`LatencyAccumulator` — an HdrHistogram-backed accumulator (DHCP DORA-ack,
  DHCP renew-ack, DNS resolve, both propagation legs) that flushes percentiles on a
  cadence and dumps a ``.hdr`` log at the end. HdrHistogram is the off-box runtime
  dependency (``perf/requirements.txt``); when it is absent we fall back to a bounded
  reservoir + nearest-rank percentiles so the module stays import-clean + runnable in
  a bare env (logged once, lower fidelity — the report flags it).

Both are process-local; each shard owns its own instances and writes its own
per-shard generator file. The report aggregates across shards off-box.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from typing import Any

from spddi_perf.logging_util import append_ndjson, get_logger, utc_now_iso
from spddi_perf.runpaths import RunPaths

_log = get_logger("spddi_perf.orchestrator.lifecycle")

# HdrHistogram config: 1µs .. 600s, 3 significant digits. Latencies recorded in µs.
_HDR_LOWEST_US = 1
_HDR_HIGHEST_US = 600 * 1_000_000
_HDR_SIGFIG = 3

try:  # off-box runtime dep (perf/requirements.txt: hdrhistogram>=0.10)
    from hdrh.histogram import HdrHistogram  # type: ignore

    _HAVE_HDR = True
except Exception:  # pragma: no cover - exercised only in a bare env
    HdrHistogram = None  # type: ignore[assignment,misc]
    _HAVE_HDR = False
    _log.warning(
        "hdrhistogram not importable — falling back to a bounded reservoir; "
        "percentile fidelity is reduced (install perf/requirements.txt off-box)",
        extra={"fields": {"event": "hdr_fallback"}},
    )


class _Reservoir:
    """Tiny nearest-rank percentile fallback when HdrHistogram is unavailable."""

    __slots__ = ("_buf", "_count", "_max")

    def __init__(self, cap: int = 100_000) -> None:
        self._buf: deque[float] = deque(maxlen=cap)
        self._count = 0
        self._max = 0.0

    def record_value(self, v: float) -> None:
        self._buf.append(float(v))
        self._count += 1
        if v > self._max:
            self._max = float(v)

    def get_total_count(self) -> int:
        return self._count

    def get_max_value(self) -> float:
        return self._max

    def get_value_at_percentile(self, pct: float) -> float:
        if not self._buf:
            return 0.0
        ordered = sorted(self._buf)
        rank = max(0, min(len(ordered) - 1, math.ceil(pct / 100.0 * len(ordered)) - 1))
        return ordered[rank]

    def reset(self) -> None:
        self._buf.clear()
        self._count = 0
        self._max = 0.0


class LatencyAccumulator:
    """Thread-safe (records may come off the recv coroutine) latency accumulator.

    Holds a *cumulative* histogram (full-run percentiles + the final ``.hdr`` dump)
    and a *window* histogram reset on each :meth:`window_percentiles` call so the
    per-shard NDJSON shows the interval percentiles, not a smeared lifetime curve.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        if _HAVE_HDR:
            self._cumulative: Any = HdrHistogram(_HDR_LOWEST_US, _HDR_HIGHEST_US, _HDR_SIGFIG)
            self._window: Any = HdrHistogram(_HDR_LOWEST_US, _HDR_HIGHEST_US, _HDR_SIGFIG)
        else:
            self._cumulative = _Reservoir()
            self._window = _Reservoir()

    def record_ms(self, ms: float) -> None:
        """Record one latency sample in milliseconds (stored internally as µs)."""
        if ms < 0:
            return
        us = int(round(ms * 1000.0))
        if us < _HDR_LOWEST_US:
            us = _HDR_LOWEST_US
        elif us > _HDR_HIGHEST_US:
            us = _HDR_HIGHEST_US
        with self._lock:
            self._cumulative.record_value(us)
            self._window.record_value(us)

    def _pct_ms(self, hist: Any, pct: float) -> float:
        return round(hist.get_value_at_percentile(pct) / 1000.0, 3)

    def window_percentiles(self, *, reset: bool = True) -> dict[str, float]:
        """Return p50/p95/p99 (+count) for the current window, then reset it."""
        with self._lock:
            n = self._window.get_total_count()
            out = {
                "count": int(n),
                "p50": self._pct_ms(self._window, 50.0) if n else 0.0,
                "p95": self._pct_ms(self._window, 95.0) if n else 0.0,
                "p99": self._pct_ms(self._window, 99.0) if n else 0.0,
            }
            if reset:
                if _HAVE_HDR:
                    self._window.reset()
                else:
                    self._window.reset()
            return out

    def cumulative_summary(self) -> dict[str, float]:
        with self._lock:
            n = self._cumulative.get_total_count()
            return {
                "count": int(n),
                "p50": self._pct_ms(self._cumulative, 50.0) if n else 0.0,
                "p95": self._pct_ms(self._cumulative, 95.0) if n else 0.0,
                "p99": self._pct_ms(self._cumulative, 99.0) if n else 0.0,
                "p999": self._pct_ms(self._cumulative, 99.9) if n else 0.0,
                "max": round(self._cumulative.get_max_value() / 1000.0, 3) if n else 0.0,
            }

    def dump_hdr(self, path: str) -> bool:
        """Dump the cumulative histogram to ``path`` as an .hdr log. False if no Hdr."""
        if not _HAVE_HDR:
            return False
        with self._lock:
            try:
                # HdrHistogram exposes a compressed base64 encoding usable by the
                # standard hdr log viewers / off-box aggregation.
                encoded = self._cumulative.encode()
                with open(path, "wb") as f:
                    f.write(encoded)
                return True
            except Exception as exc:  # pragma: no cover - best-effort dump
                _log.warning(
                    "hdr dump failed for %s: %s",
                    self.name,
                    exc,
                    extra={"fields": {"event": "hdr_dump_failed", "metric": self.name}},
                )
                return False


class LifecycleLog:
    """Append-only NDJSON of per-device FSM transitions (-> ``rp.lifecycle``)."""

    def __init__(self, rp: RunPaths, shard: int) -> None:
        self._path = rp.lifecycle
        self._shard = shard
        self._lock = threading.Lock()

    def emit(self, *, mac: str, index: int, event: str, **fields: Any) -> None:
        """Record one device transition.

        ``event`` is one of: arrival / discover / dora_ack / renew / renew_ack /
        rebind / rebind_ack / depart / release / lapse / rearrival / nak / timeout.
        """
        rec = {
            "ts": utc_now_iso(),
            "shard": self._shard,
            "mac": mac,
            "index": index,
            "event": event,
            **fields,
        }
        with self._lock:
            append_ndjson(self._path, rec)
