"""PowerDNS agent driver — STUB.

The parallel driver-abstraction agent will flesh this out. For now it runs,
reports health, and refuses to apply record ops.
"""

from __future__ import annotations

from typing import Any

import structlog

from .base import DriverBase

log = structlog.get_logger(__name__)


class PowerDNSDriver(DriverBase):
    def render(self, bundle: dict[str, Any]) -> None:
        log.info("powerdns_render_stub", zones=len(bundle.get("zones", [])))

    def validate(self) -> None:
        return

    def swap_and_reload(self) -> None:
        return

    def apply_record_op(self, op: dict[str, Any]) -> None:
        raise NotImplementedError("PowerDNS agent driver is a stub in Phase 2")

    def start_daemon(self) -> None:
        log.info("powerdns_start_stub")

    def daemon_running(self) -> bool:
        return True
