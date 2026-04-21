"""Background watcher that keeps Kea HA peer URLs in sync with DNS.

Kea's HA hook parses peer URLs via Boost asio, which only accepts IP
literals — hostnames aren't resolved by Kea itself. The agent does
one-time resolution inside ``render_kea._resolve_peer_url`` at render
time, which is fine until a peer container/pod gets a new IP
(``docker compose --force-recreate``, any k8s restart, bridge-IP
churn). After that, Kea's config points at a stale IP and the HA hook
silently drifts to ``communications-interrupted`` → ``partner-down``.

This watcher closes the loop: every ``CHECK_INTERVAL`` seconds it
re-resolves the hostnames from the last-seen bundle's failover block
and, if any peer's IP has changed, fires ``apply_bundle`` to re-render
and reload Kea with fresh URLs.

Resolution failures are treated as transient — we keep the cached IP
and try again next tick. That avoids thrashing during a brief DNS
outage.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import structlog

log = structlog.get_logger(__name__)

# 30s balances responsiveness vs noise. Peer restarts typically settle
# in <5s on compose and <10s on k8s, so we'll pick up new IPs within
# the first half-minute.
CHECK_INTERVAL = 30.0


class PeerResolveWatcher:
    """Re-resolves HA peer hostnames and triggers reload on IP change.

    ``apply_fn`` is the agent's ``SyncLoop._apply_bundle`` — called as
    ``apply_fn(bundle, reload_kea=True)``. We deliberately reuse the
    same render+reload pipeline so the new render goes through the
    full audit path (``save_rendered_kea`` etc.) rather than mutating
    the live config in-place.
    """

    def __init__(
        self,
        apply_fn: Callable[..., None],
        *,
        check_interval: float = CHECK_INTERVAL,
    ):
        self._apply_fn = apply_fn
        self._check_interval = check_interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._bundle: dict[str, Any] | None = None
        # Maps hostname → last resolved IP. We only reload when the
        # resolution changes, not on every tick.
        self._resolved: dict[str, str] = {}

    def set_bundle(self, bundle: dict[str, Any]) -> None:
        """Called by the sync loop after each successful bundle apply.

        Seeds the initial hostname→IP map so the first watcher tick
        doesn't spuriously fire "IP changed" on startup.
        """
        with self._lock:
            self._bundle = bundle
            hosts = self._peer_hosts(bundle)
            for host in hosts:
                try:
                    ipaddress.ip_address(host)
                    continue  # already an IP literal — nothing to watch
                except ValueError:
                    pass
                try:
                    self._resolved[host] = socket.gethostbyname(host)
                except OSError:
                    # Transient — next tick will try again
                    continue
            # Purge stale hostnames (e.g. a peer was removed from the
            # group) so we don't reload on a phantom DNS change.
            self._resolved = {h: ip for h, ip in self._resolved.items() if h in hosts}

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self._check_interval):
            self._tick_once()

    def _tick_once(self) -> None:
        with self._lock:
            bundle = self._bundle
        if bundle is None:
            return
        hosts = self._peer_hosts(bundle)
        if not hosts:
            return
        changed: list[tuple[str, str, str]] = []
        for host in hosts:
            try:
                ipaddress.ip_address(host)
                continue  # hostname is already a literal
            except ValueError:
                pass
            try:
                new_ip = socket.gethostbyname(host)
            except OSError as e:
                log.debug("ha_peer_resolve_transient_fail", host=host, error=str(e))
                continue
            old_ip = self._resolved.get(host)
            if old_ip is None:
                self._resolved[host] = new_ip
                continue
            if new_ip != old_ip:
                changed.append((host, old_ip, new_ip))
                self._resolved[host] = new_ip
        if not changed:
            return
        for host, old_ip, new_ip in changed:
            log.info("ha_peer_ip_changed", host=host, old=old_ip, new=new_ip)
        try:
            self._apply_fn(bundle, reload_kea=True)
            log.info("ha_peer_reresolve_reloaded", changes=len(changed))
        except Exception:  # noqa: BLE001 — don't let one failed reload kill the watcher
            log.exception("ha_peer_reresolve_reload_failed")

    @staticmethod
    def _peer_hosts(bundle: dict[str, Any]) -> list[str]:
        """Extract peer hostnames from the bundle's failover block."""
        inner = (
            bundle.get("bundle")
            if isinstance(bundle.get("bundle"), dict)
            else bundle
        )
        failover = inner.get("failover") or {}
        peers = failover.get("peers") or []
        hosts: list[str] = []
        for p in peers:
            try:
                host = urlparse(p.get("url", "")).hostname
            except Exception:  # noqa: BLE001 — malformed URL, skip quietly
                host = None
            if host:
                hosts.append(host)
        return hosts


__all__ = ["PeerResolveWatcher", "CHECK_INTERVAL"]
