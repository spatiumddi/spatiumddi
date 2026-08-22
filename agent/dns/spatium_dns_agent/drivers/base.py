"""Agent-side driver base class.

Mirrors the control-plane DNSDriverBase but on the container side — the agent
asks its driver to render configs, reload the daemon, and apply RFC 2136 /
RFC 2136 record ops over loopback.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..config_apply import (
    PHASE_RELOAD,
    PHASE_RENDER,
    PHASE_VALIDATE,
    ConfigApplyError,
)

# Record-op kinds that describe an RRset, and so can carry the complete
# desired ``record["rrset"]`` the control plane stamps on them (#773).
# Everything else on the queue is zone-level (the ``dnssec_*`` ops) and
# branches off before any of the record machinery runs.
RRSET_OP_KINDS = frozenset({"create", "update", "delete"})


class DriverBase(ABC):
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir

    @abstractmethod
    def render(self, bundle: dict[str, Any]) -> None:
        """Render config files (atomic to ``rendered.new``)."""

    @abstractmethod
    def validate(self) -> None:
        """Validate the rendered config. Raise on failure."""

    @abstractmethod
    def swap_and_reload(self) -> None:
        """Rename rendered.new → rendered, signal the daemon to reload."""

    @abstractmethod
    def apply_record_op(self, op: dict[str, Any]) -> dict[str, Any] | None:
        """Apply a RecordOp via loopback nsupdate via RFC 2136.

        Returns an optional dict carrying driver-specific result
        details — e.g. the PowerDNS driver returns DNSSEC state
        flags that ``sync.py`` propagates back to the control plane
        in the pending-op ACK. BIND9's driver returns ``None``
        (no extra signal to surface). Issue #251 — pre-fix the ABC
        declared ``-> None`` while powerdns.py already returned a
        dict; sync.py relied on the dict shape so the contract was
        already widened in practice.
        """

    @abstractmethod
    def start_daemon(self) -> None:
        """Spawn the DNS daemon. Called once at startup."""

    @abstractmethod
    def daemon_running(self) -> bool:
        ...

    def daemon_version(self) -> str | None:
        """Version of the DNS daemon binary, e.g. ``"5.0.5"`` / ``"9.20.26"``.

        Reported on each heartbeat and persisted to ``dns_server.daemon_version``
        so the control plane can reason about what the fleet is actually
        running — the rolling-upgrade preflight needs it to tell an operator
        that crossing PowerDNS 4.x → 5.x performs a one-way LMDB schema
        migration (#638).

        ``None`` means "could not determine", which the control plane must
        treat as UNKNOWN — never as a specific version. Not abstract: a driver
        that has no cheap version probe simply doesn't report one.
        """
        return None

    def apply_config(self, bundle: dict[str, Any]) -> None:
        """Default orchestration: render → validate → swap+reload.

        Every failure is re-raised as :class:`ConfigApplyError` carrying the
        phase it happened in. The caller needs that to choose a recovery: a
        render or validate failure never reached the daemon, so nothing has
        to be undone beyond the staging tree, whereas a swap/reload failure
        means the live config directory has already been replaced and the
        previous bundle has to be re-rendered to get back to a known state
        (#882).
        """
        for phase, step in (
            (PHASE_RENDER, lambda: self.render(bundle)),
            (PHASE_VALIDATE, self.validate),
            (PHASE_RELOAD, self.swap_and_reload),
        ):
            try:
                step()
            except Exception as e:
                raise ConfigApplyError(phase, e) from e
