"""Pydantic schemas for the Scheduled Wake-on-LAN REST API — Phase 1
(issue #586).

Mounted at ``/api/v1/wake-scheduler`` (module ``tools.wake_scheduler``).
The wire contract is the ``wake-scheduler`` prefix even though the Python
package is ``wol_schedules`` — the frontend api client, the MCP tools, and
the runner docstrings all reference ``/wake-scheduler``.

**Phase 1 only** — the holiday gate is the built-in ``blackout_dates`` +
``active_from`` / ``active_until`` + ``timezone``. There is NO external
iCal / CalDAV calendar (that is Phase 2 and deliberately absent).

Validation reuses the shipped service helpers verbatim so the API and the
beat runner agree on what a valid schedule is:

* ``validate_cron`` / ``validate_timezone`` (``app.services.wol_scheduler.schedule``)
* ``VALID_MODES`` (``app.services.wol_scheduler.resolver``)
* ``NetToolTarget`` (``app.services.nettools.schemas``) for the vantage.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.nettools.schemas import NetToolTarget
from app.services.wol_scheduler.schedule import (
    InvalidCronExpression,
    InvalidTimezone,
    validate_cron,
    validate_timezone,
)

# Selector modes (mirrors ``app.services.wol_scheduler.resolver.VALID_MODES``).
SelectorMode = Literal["address_tags", "subnet", "subnet_tags", "hosts"]

# Vantage kinds Phase-1 WoL can actually send from (a magic packet only
# originates from the control-plane server or a Fleet appliance NIC).
_ALLOWED_VANTAGE_KINDS = frozenset({"server", "appliance"})


# ── Sub-shapes ───────────────────────────────────────────────────────


class TargetSelectorIn(BaseModel):
    """The stored ``target_selector`` JSONB, validated on the way in.

    ``tags`` are the ``?tag=key`` / ``?tag=key:value`` grammar (ANDed) for
    the tag modes; ``subnet_ids`` / ``address_ids`` are the explicit-id
    modes. Only the list relevant to ``mode`` is consulted by the resolver,
    but all four fields are carried so the operator can flip modes without
    losing the other selections.
    """

    mode: SelectorMode
    tags: list[str] = Field(default_factory=list)
    subnet_ids: list[uuid.UUID] = Field(default_factory=list)
    address_ids: list[uuid.UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_tags_for_tag_modes(self) -> TargetSelectorIn:
        """Reject an empty / whitespace-only ``tags`` list for the tag modes.

        Without this a ``{mode:'address_tags', tags:[]}`` selector would
        resolve to *every* unicast host in scope (``apply_tag_filter`` returns
        the statement unchanged for an empty tag list) — a platform-wide mass
        wake. The resolver has a matching defensive match-nothing guard for any
        row already stored before this validator existed.
        """
        if self.mode in ("address_tags", "subnet_tags"):
            cleaned = [t for t in self.tags if t and t.strip()]
            if not cleaned:
                raise ValueError(f"target_selector.tags must be non-empty for mode {self.mode!r}")
        return self

    def to_jsonb(self) -> dict[str, Any]:
        """Serialise to the resolver's stored shape (UUIDs → str)."""
        return {
            "mode": self.mode,
            "tags": list(self.tags),
            "subnet_ids": [str(s) for s in self.subnet_ids],
            "address_ids": [str(a) for a in self.address_ids],
        }


def _validate_vantage(v: NetToolTarget | None) -> NetToolTarget | None:
    """Reject a vantage kind WoL can't send from (Phase 1: server/appliance)."""
    if v is None:
        return v
    if v.kind not in _ALLOWED_VANTAGE_KINDS:
        raise ValueError(
            f"vantage.kind must be one of {sorted(_ALLOWED_VANTAGE_KINDS)}, got {v.kind!r}"
        )
    if v.kind == "appliance" and v.id is None:
        raise ValueError("vantage.id is required when vantage.kind is 'appliance'")
    return v


def _validate_blackouts(value: list[str] | None) -> list[str] | None:
    """Ensure every blackout entry is an ISO ``YYYY-MM-DD`` string."""
    if value is None:
        return value
    out: list[str] = []
    for raw in value:
        try:
            out.append(date.fromisoformat(str(raw).strip()).isoformat())
        except ValueError as exc:
            raise ValueError(f"blackout date {raw!r} is not an ISO YYYY-MM-DD date") from exc
    return out


# ── Create / Update ──────────────────────────────────────────────────


class WakeScheduleCreate(BaseModel):
    """Operator request to create a Wake-on-LAN schedule."""

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    enabled: bool = True

    target_selector: TargetSelectorIn

    # NULL / empty cron == manual-only (never swept by the beat task).
    schedule_cron: str | None = Field(default=None, max_length=128)
    timezone: str = Field(default="UTC", max_length=64)

    # Built-in holiday gate (Phase 1 — no external calendar).
    blackout_dates: list[str] | None = None
    active_from: date | None = None
    active_until: date | None = None

    # Send options honoured by the #533 send path.
    vantage: NetToolTarget | None = None
    repeat_count: int = Field(default=2, ge=1, le=10)
    repeat_interval_ms: int = Field(default=100, ge=0, le=10_000)
    stagger_ms: int = Field(default=0, ge=0, le=60_000)
    port: int = Field(default=9, ge=1, le=65535)

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        try:
            validate_timezone(v)
        except InvalidTimezone as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("schedule_cron")
    @classmethod
    def _check_cron(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        try:
            validate_cron(v)
        except InvalidCronExpression as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("blackout_dates")
    @classmethod
    def _check_blackouts(cls, v: list[str] | None) -> list[str] | None:
        return _validate_blackouts(v)

    @field_validator("vantage")
    @classmethod
    def _check_vantage(cls, v: NetToolTarget | None) -> NetToolTarget | None:
        return _validate_vantage(v)

    @model_validator(mode="after")
    def _check_term_range(self) -> WakeScheduleCreate:
        if (
            self.active_from is not None
            and self.active_until is not None
            and self.active_from > self.active_until
        ):
            raise ValueError("active_from must be on or before active_until")
        return self


class WakeScheduleUpdate(BaseModel):
    """PATCH body — every field optional. ``model_dump(exclude_unset=True)``
    distinguishes "leave unchanged" from an explicit ``null`` on the nullable
    columns (``description`` / ``schedule_cron`` / ``blackout_dates`` /
    ``active_from`` / ``active_until``)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    enabled: bool | None = None

    target_selector: TargetSelectorIn | None = None

    schedule_cron: str | None = Field(default=None, max_length=128)
    timezone: str | None = Field(default=None, max_length=64)

    blackout_dates: list[str] | None = None
    active_from: date | None = None
    active_until: date | None = None

    vantage: NetToolTarget | None = None
    repeat_count: int | None = Field(default=None, ge=1, le=10)
    repeat_interval_ms: int | None = Field(default=None, ge=0, le=10_000)
    stagger_ms: int | None = Field(default=None, ge=0, le=60_000)
    port: int | None = Field(default=None, ge=1, le=65535)

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            validate_timezone(v)
        except InvalidTimezone as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("schedule_cron")
    @classmethod
    def _check_cron(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return v
        try:
            validate_cron(v)
        except InvalidCronExpression as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("blackout_dates")
    @classmethod
    def _check_blackouts(cls, v: list[str] | None) -> list[str] | None:
        return _validate_blackouts(v)

    @field_validator("vantage")
    @classmethod
    def _check_vantage(cls, v: NetToolTarget | None) -> NetToolTarget | None:
        return _validate_vantage(v)


# ── Read ─────────────────────────────────────────────────────────────


class WakeScheduleRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    enabled: bool
    target_selector: dict[str, Any]
    schedule_cron: str | None
    timezone: str
    blackout_dates: list[str] | None
    active_from: date | None
    active_until: date | None
    vantage: dict[str, Any]
    repeat_count: int
    repeat_interval_ms: int
    stagger_ms: int
    port: int
    last_run_at: datetime | None
    last_run_status: str | None
    last_run_skip_reason: str | None
    last_target_count: int | None
    next_run_at: datetime | None
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    modified_at: datetime


class WakeTargetRead(BaseModel):
    """A host that WOULD be sent a magic packet (preview) — resolver
    ``WakeTarget`` flattened for the wire."""

    ip_address_id: uuid.UUID | None
    address: str | None
    mac: str
    subnet_id: uuid.UUID | None
    broadcast: str
    mac_source: str
    hostname: str | None = None


class SkippedTargetRead(BaseModel):
    """A matched input that would NOT be sent, with a reason (preview)."""

    reason: str
    ip_address_id: uuid.UUID | None = None
    address: str | None = None
    subnet_id: uuid.UUID | None = None


class TargetPreviewRequest(BaseModel):
    """Body for the unsaved ``POST /preview-targets`` — resolves a selector
    the operator is still editing (the create modal's live match count)."""

    target_selector: TargetSelectorIn


class TargetPreviewRead(BaseModel):
    """Resolver output for a preview: bucket counts + a capped sample of each
    bucket + (for a saved schedule) the next fire time and the built-in gate
    verdict at that fire."""

    matched_count: int
    wake_count: int
    skipped_count: int
    # Per-host skips whose reason is ``no_mac`` — the most common miss, worth
    # surfacing on its own so the operator sees "N hosts have no known MAC".
    mac_less_count: int
    sample: list[WakeTargetRead]
    skipped_sample: list[SkippedTargetRead]
    # Only populated for a saved-schedule preview (unsaved has no cron/gate).
    next_run_at: datetime | None = None
    gate_verdict: str | None = None


class WakeRunRead(BaseModel):
    id: uuid.UUID
    schedule_id: uuid.UUID | None
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    skip_reason: str | None
    target_count: int
    sent_count: int
    skipped_count: int
    failed_count: int
    triggered_by_user_id: uuid.UUID | None
    error: str | None
    created_at: datetime


class WakeRunTargetRead(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    ip_address_id: uuid.UUID | None
    address: str | None
    mac: str | None
    subnet_id: uuid.UUID | None
    broadcast: str | None
    vantage: dict[str, Any] | None
    mac_source: str | None
    sent: bool
    skip_reason: str | None
    error: str | None
    created_at: datetime


class WakeRunDetailRead(WakeRunRead):
    """A run plus its per-host ``wol_run_target`` outcomes."""

    targets: list[WakeRunTargetRead]


__all__ = [
    "SelectorMode",
    "TargetSelectorIn",
    "WakeScheduleCreate",
    "WakeScheduleUpdate",
    "WakeScheduleRead",
    "WakeTargetRead",
    "SkippedTargetRead",
    "TargetPreviewRequest",
    "TargetPreviewRead",
    "WakeRunRead",
    "WakeRunTargetRead",
    "WakeRunDetailRead",
]
