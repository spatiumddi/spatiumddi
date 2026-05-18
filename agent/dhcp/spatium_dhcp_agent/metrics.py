"""Kea packet-counter poller — emits per-bucket deltas upstream.

Reads Kea's ``statistic-get-all`` every 60 s over the local control
socket, converts monotonically-increasing packet counters into
per-bucket deltas (subtracting the previous snapshot), and POSTs one
sample to ``/api/v1/dhcp/agents/metrics``. Counter resets caused by a
Kea restart are detected as ``delta < 0``; in that case we discard
the bucket and seed the next snapshot fresh — better to drop one
bucket than emit a spurious negative-turned-positive spike when the
new counters climb back up.

Kea statistic names we care about. The eight column names on
``dhcp_metric_sample`` were v4-shaped originally and now do double
duty for v6 by mapping each v6 message to the v4 column with the
closest role-equivalent semantics (SOLICIT≈DISCOVER, ADVERTISE≈
OFFER, REPLY≈ACK, INFORMATION-REQUEST≈INFORM, RENEW+REBIND fold
into ``request``). Issue #264 — both stacks share one row per
server so operators running v6 finally get per-bucket numbers
without a schema migration.

    pkt4-discover-received                pkt6-solicit-received        → discover
    pkt4-offer-sent                       pkt6-advertise-sent          → offer
    pkt4-request-received                 pkt6-request-received
                                          pkt6-renew-received
                                          pkt6-rebind-received         → request
    pkt4-ack-sent                         pkt6-reply-sent              → ack
    pkt4-nak-sent                                                      → nak
    pkt4-decline-received                 pkt6-decline-received        → decline
    pkt4-release-received                 pkt6-release-received        → release
    pkt4-inform-received                  pkt6-information-request-received → inform
"""

from __future__ import annotations

import random
import threading
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from .config import AgentConfig
from .kea_ctrl import KeaCtrlError, send_command

log = structlog.get_logger(__name__)

# Map Kea's statistic names to the column names on dhcp_metric_sample.
# Multiple v6 message types fold into a single v4-shaped column when
# their roles align — see the module docstring for the mapping
# rationale (issue #264).
_STAT_MAP = {
    "pkt4-discover-received": "discover",
    "pkt4-offer-sent": "offer",
    "pkt4-request-received": "request",
    "pkt4-ack-sent": "ack",
    "pkt4-nak-sent": "nak",
    "pkt4-decline-received": "decline",
    "pkt4-release-received": "release",
    "pkt4-inform-received": "inform",
    "pkt6-solicit-received": "discover",
    "pkt6-advertise-sent": "offer",
    "pkt6-request-received": "request",
    "pkt6-renew-received": "request",
    "pkt6-rebind-received": "request",
    "pkt6-reply-sent": "ack",
    "pkt6-decline-received": "decline",
    "pkt6-release-received": "release",
    "pkt6-information-request-received": "inform",
}

# Column names — the unique set of values from ``_STAT_MAP``, used by
# ``_compute_delta`` so a multi-stat → single-column mapping doesn't
# re-diff the same column twice.
_METRIC_COLUMNS = sorted(set(_STAT_MAP.values()))


def _extract_counter(series: Any) -> int | None:
    """Pull the most recent numeric value from one ``statistic-get-all`` entry.

    Kea returns a list of ``[value, timestamp]`` pairs, newest first —
    e.g. ``[[125, "2026-04-22 09:00:00.001"], [120, ...]]``. Shape is
    stable across Kea 2.4-2.6. We tolerate empty lists / missing
    values so a fresh daemon that hasn't ticked a counter yet shows
    up as 0 rather than crashing the poller.
    """
    if not isinstance(series, list) or not series:
        return 0
    first = series[0]
    if isinstance(first, list) and first:
        v = first[0]
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
    return None


def _parse_snapshot(resp: dict[str, Any]) -> dict[str, int]:
    """``statistic-get-all`` → flat ``{column: current_counter}`` dict.

    Multiple stat names can map to the same column (e.g. ``pkt6-
    renew-received`` + ``pkt6-rebind-received`` both feed ``request``);
    in that case we sum the counters so the column carries the
    full per-role activity.
    """
    args = resp.get("arguments") or {}
    out: dict[str, int] = {}
    for stat_name, col in _STAT_MAP.items():
        v = _extract_counter(args.get(stat_name))
        if v is not None:
            out[col] = out.get(col, 0) + v
    return out


class MetricsPoller:
    def __init__(self, cfg: AgentConfig, token_ref: list[str]):
        self.cfg = cfg
        self.token_ref = token_ref
        self._stop = threading.Event()
        # Previous snapshot. None on first tick — the first post-boot
        # bucket is absorbed (no baseline to diff against).
        self._prev: dict[str, int] | None = None

    def stop(self) -> None:
        self._stop.set()

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.cfg.control_plane_url,
            verify=self.cfg.httpx_verify(),
            timeout=15.0,
        )

    def _poll_kea(self) -> dict[str, int] | None:
        try:
            resp = send_command(self.cfg.kea_control_socket, "statistic-get-all")
        except KeaCtrlError as e:
            log.debug("metrics_kea_err", error=str(e))
            return None
        except FileNotFoundError:
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("metrics_kea_unexpected", error=str(e))
            return None
        return _parse_snapshot(resp)

    def _compute_delta(self, current: dict[str, int]) -> dict[str, int] | None:
        prev = self._prev
        self._prev = current
        if prev is None:
            return None  # first bucket — no baseline
        delta: dict[str, int] = {}
        reset = False
        for col in _METRIC_COLUMNS:
            d = current.get(col, 0) - prev.get(col, 0)
            if d < 0:
                reset = True
                break
            delta[col] = d
        if reset:
            log.info("metrics_counter_reset")
            return None
        return delta

    def _report(self, bucket_at: datetime, delta: dict[str, int]) -> None:
        try:
            with self._client() as c:
                resp = c.post(
                    "/api/v1/dhcp/agents/metrics",
                    json={"bucket_at": bucket_at.isoformat(), **delta},
                    headers={"Authorization": f"Bearer {self.token_ref[0]}"},
                )
            if resp.status_code not in (200, 204):
                log.warning("metrics_report_failed", status=resp.status_code)
        except httpx.HTTPError as e:
            log.warning("metrics_report_http_error", error=str(e))

    def run(self) -> None:
        while not self._stop.is_set():
            current = self._poll_kea()
            if current is not None:
                delta = self._compute_delta(current)
                if delta is not None:
                    # Bucket timestamp is "now, rounded to the poll
                    # interval" — the server path dedupes on
                    # (server_id, bucket_at) so a retry that lands in
                    # the next interval won't double-count.
                    now = datetime.now(UTC).replace(microsecond=0)
                    bucket = now.replace(second=(now.second // 60) * 60)
                    self._report(bucket, delta)
            # 60s base + small jitter so paired peers don't hit the
            # control plane in lockstep.
            interval = 60.0 + random.uniform(-3, 3)
            self._stop.wait(timeout=max(30.0, interval))
