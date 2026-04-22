"""Kea packet-counter poller — emits per-bucket deltas upstream.

Reads Kea's ``statistic-get-all`` every 60 s over the local control
socket, converts monotonically-increasing packet counters into
per-bucket deltas (subtracting the previous snapshot), and POSTs one
sample to ``/api/v1/dhcp/agents/metrics``. Counter resets caused by a
Kea restart are detected as ``delta < 0``; in that case we discard
the bucket and seed the next snapshot fresh — better to drop one
bucket than emit a spurious negative-turned-positive spike when the
new counters climb back up.

Kea statistic names we care about (v4-only for MVP — DHCPv6 stats
have the same shape under ``pkt6-*`` names and can be added with a
one-line map once v6 scopes are in wide use):

    pkt4-discover-received    → discover
    pkt4-offer-sent           → offer
    pkt4-request-received     → request
    pkt4-ack-sent             → ack
    pkt4-nak-sent             → nak
    pkt4-decline-received     → decline
    pkt4-release-received     → release
    pkt4-inform-received      → inform
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
_STAT_MAP = {
    "pkt4-discover-received": "discover",
    "pkt4-offer-sent": "offer",
    "pkt4-request-received": "request",
    "pkt4-ack-sent": "ack",
    "pkt4-nak-sent": "nak",
    "pkt4-decline-received": "decline",
    "pkt4-release-received": "release",
    "pkt4-inform-received": "inform",
}


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
    """``statistic-get-all`` → flat ``{column: current_counter}`` dict."""
    args = resp.get("arguments") or {}
    out: dict[str, int] = {}
    for stat_name, col in _STAT_MAP.items():
        v = _extract_counter(args.get(stat_name))
        if v is not None:
            out[col] = v
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
        verify: bool | str = True
        if self.cfg.insecure_skip_tls_verify:
            verify = False
        elif self.cfg.tls_ca_path:
            verify = self.cfg.tls_ca_path
        return httpx.Client(base_url=self.cfg.control_plane_url, verify=verify, timeout=15.0)

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
        for col in _STAT_MAP.values():
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
