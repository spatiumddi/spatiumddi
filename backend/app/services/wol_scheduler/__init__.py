"""Scheduled Wake-on-LAN service package (issue #586).

Backend service layer for recurring, tag-targeted Wake-on-LAN with a
built-in holiday gate (blackout dates + term range) plus the Phase-2
external iCal / CalDAV calendar gate.  Composed of the pure-ish modules:

* :mod:`.schedule` — DST-safe ``compute_next_run`` + ``is_due`` + cron/tz
  validation.
* :mod:`.gating` — the built-in term-range / blackout-date gate.
* :mod:`.resolver` — ``target_selector`` → deduped, skip-annotated wake
  targets, reusing ``apply_tag_filter`` + the #533 MAC/broadcast helpers.
* :mod:`.dispatch` — the repeat/stagger send loop, reusing the shipped
  ``app.services.wol`` send path verbatim (never re-implements the packet).
* :mod:`.calendar` — iCal / CalDAV parsing → flattened all-day event spans
  (recurrence expanded over a bounded horizon).
* :mod:`.calendar_sync` — the feed reconciler that set-reconciles a calendar's
  cached ``wol_calendar_event`` spans (blocklist-feed shape).

All DB access is async; no DNS/DHCP driver logic leaks in here.
"""

from __future__ import annotations

from app.services.wol_scheduler.calendar import (
    ParsedEvent,
    fetch_caldav_events,
    fetch_ical_url,
    parse_ical,
)
from app.services.wol_scheduler.calendar_sync import sync_calendar
from app.services.wol_scheduler.dispatch import (
    DispatchOutcome,
    dispatch_wol_targets,
)
from app.services.wol_scheduler.gating import (
    SKIP_CALENDAR_EVENT,
    SKIP_HOLIDAY,
    SKIP_NO_CALENDAR_EVENT,
    SKIP_OFF_TERM,
    evaluate_gate,
    gate_verdict,
    load_gate_calendar_events,
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
from app.services.wol_scheduler.verify import (
    ACTIVE_METHODS,
    PASSIVE_METHODS,
    VERIFY_METHOD_AUTO,
    VERIFY_METHOD_PING,
    VERIFY_METHOD_SEEN,
    VERIFY_METHOD_TCP,
    VERIFY_METHODS,
    auto_stagger_ms,
    probe_liveness,
    probe_seen,
    verify_run_targets,
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
    "load_gate_calendar_events",
    "local_fire_date",
    "SKIP_HOLIDAY",
    "SKIP_OFF_TERM",
    "SKIP_CALENDAR_EVENT",
    "SKIP_NO_CALENDAR_EVENT",
    # calendar
    "parse_ical",
    "fetch_ical_url",
    "fetch_caldav_events",
    "ParsedEvent",
    "sync_calendar",
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
    # verify (Phase 3) + multi-source liveness (#596)
    "probe_liveness",
    "probe_seen",
    "verify_run_targets",
    "auto_stagger_ms",
    "ACTIVE_METHODS",
    "PASSIVE_METHODS",
    "VERIFY_METHODS",
    "VERIFY_METHOD_AUTO",
    "VERIFY_METHOD_PING",
    "VERIFY_METHOD_SEEN",
    "VERIFY_METHOD_TCP",
]
