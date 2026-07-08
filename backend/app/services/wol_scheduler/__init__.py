"""Scheduled Wake-on-LAN service package — Phase 1 (issue #586).

Backend service layer for recurring, tag-targeted Wake-on-LAN with a
built-in holiday gate (blackout dates + term range — NO external
iCal/CalDAV, that's Phase 2).  Composed of four pure-ish modules:

* :mod:`.schedule` — DST-safe ``compute_next_run`` + ``is_due`` + cron/tz
  validation.
* :mod:`.gating` — the built-in term-range / blackout-date gate.
* :mod:`.resolver` — ``target_selector`` → deduped, skip-annotated wake
  targets, reusing ``apply_tag_filter`` + the #533 MAC/broadcast helpers.
* :mod:`.dispatch` — the repeat/stagger send loop, reusing the shipped
  ``app.services.wol`` send path verbatim (never re-implements the packet).

All DB access is async; no DNS/DHCP driver logic leaks in here.
"""

from __future__ import annotations

from app.services.wol_scheduler.dispatch import (
    DispatchOutcome,
    dispatch_wol_targets,
)
from app.services.wol_scheduler.gating import (
    SKIP_HOLIDAY,
    SKIP_OFF_TERM,
    evaluate_gate,
    gate_verdict,
    local_fire_date,
)
from app.services.wol_scheduler.resolver import (
    MODE_ADDRESS_TAGS,
    MODE_HOSTS,
    MODE_SUBNET,
    MODE_SUBNET_TAGS,
    SKIP_NO_MAC,
    VALID_MODES,
    InvalidSelector,
    ResolvedTargets,
    SkippedTarget,
    WakeTarget,
    group_by_segment,
    resolve_wol_targets,
)
from app.services.wol_scheduler.schedule import (
    InvalidCronExpression,
    InvalidTimezone,
    compute_next_run,
    compute_next_wol_run,
    is_due,
    validate_cron,
    validate_timezone,
)

__all__ = [
    # schedule
    "compute_next_run",
    "compute_next_wol_run",
    "is_due",
    "validate_cron",
    "validate_timezone",
    "InvalidCronExpression",
    "InvalidTimezone",
    # gating
    "evaluate_gate",
    "gate_verdict",
    "local_fire_date",
    "SKIP_HOLIDAY",
    "SKIP_OFF_TERM",
    # resolver
    "resolve_wol_targets",
    "group_by_segment",
    "ResolvedTargets",
    "WakeTarget",
    "SkippedTarget",
    "InvalidSelector",
    "SKIP_NO_MAC",
    "VALID_MODES",
    "MODE_ADDRESS_TAGS",
    "MODE_SUBNET",
    "MODE_SUBNET_TAGS",
    "MODE_HOSTS",
    # dispatch
    "dispatch_wol_targets",
    "DispatchOutcome",
]
