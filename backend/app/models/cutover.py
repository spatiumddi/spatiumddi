"""Windows → SpatiumDDI cutover plans (issue #756).

The one-shot importers (#128 / #129) get an operator's Windows DNS / DHCP
estate *into* SpatiumDDI. Nothing took them the rest of the way: proving the
imported rows still match what Windows is serving, running both sides in
parallel, performing the switch per zone / per scope, and knowing what is safe
to turn off on the Windows side afterwards. These four tables are the state
behind that workflow.

A :class:`CutoverPlan` is operator scratch space — a named migration with a
source Windows server (DNS and/or DHCP), a target SpatiumDDI server group, and
a set of :class:`CutoverItem` rows, one per zone or scope being migrated. The
item carries everything that has to survive a page reload *and* a rollback:
the last parity report, the last shadow-query comparison, the pre-flight TTL
snapshot (so a rollback restores the exact values that were there before), the
lease-handover result, and the current blocker set.

:class:`CutoverChecklistItem` is the Phase 4 decommission list, seeded per plan
from a static catalog so an operator's ticks and notes persist. Every state
transition also appends a :class:`CutoverEvent`, giving the plan an ordered
timeline that outlives the items themselves.

No ``SoftDeleteMixin`` anywhere here on purpose: a plan is a working document,
not an authoritative record of network state. Deleting one throws away
bookkeeping, never configuration — the zones, scopes, and reservations it
points at are owned by their own tables and are untouched by the cascade.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# ── Enumerated string values ───────────────────────────────────────────
# Kept as plain strings (matching every other model in the codebase) rather
# than native PG enums, so adding a stage never needs a migration.

#: ``CutoverPlan.status``.
PLAN_STATUSES: frozenset[str] = frozenset(
    {
        "draft",  # being assembled; nothing verified yet
        "verifying",  # Phase 1 — parity checks running / reviewed
        "parallel",  # Phase 2 — both sides up, managed side unreachable by clients
        "cutting_over",  # Phase 3 — at least one item switched
        "completed",  # every item cut over
        "rolled_back",  # the switch was reversed
        "abandoned",  # operator gave up; kept for the audit trail
    }
)

#: ``CutoverItem.kind``.
ITEM_KINDS: frozenset[str] = frozenset({"dns_zone", "dhcp_scope"})

#: ``CutoverItem.stage``.
ITEM_STAGES: frozenset[str] = frozenset(
    {
        "pending",  # added to the plan, nothing run
        "parity_checked",  # a parity report exists for it
        "preflight_done",  # TTLs lowered (DNS) / leases handed over (DHCP)
        "cut_over",  # the switch happened
        "rolled_back",  # the switch was reversed
    }
)

#: ``CutoverEvent.kind`` — the timeline's event vocabulary.
EVENT_KINDS: frozenset[str] = frozenset(
    {
        "plan",
        "parity",
        "shadow",
        "preflight",
        "lease_handover",
        "cutover",
        "rollback",
        "checklist",
        "note",
    }
)


class CutoverPlan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One named Windows → SpatiumDDI migration."""

    __tablename__ = "cutover_plan"

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default="draft"
    )

    # Where we're migrating FROM. Both nullable because a plan may cover DNS
    # only, DHCP only, or both. SET NULL rather than CASCADE: deleting the
    # Windows server row (a plausible *last* decommission step) must not
    # silently destroy the record of the migration that retired it.
    source_dns_server_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_dhcp_server_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Where we're migrating TO. Groups, not servers — a group is the unit of
    # configuration on both the DNS and DHCP sides.
    target_dns_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server_group.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    target_dhcp_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server_group.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list[CutoverItem]] = relationship(
        "CutoverItem",
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="CutoverItem.source_ref",
    )
    checklist: Mapped[list[CutoverChecklistItem]] = relationship(
        "CutoverChecklistItem",
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="CutoverChecklistItem.sort_order",
    )


class CutoverItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One zone or one scope being migrated, and everything known about it.

    ``source_ref`` is how the object is named on the *Windows* side — a zone
    name without its trailing dot, or a DHCP scope id (its network address).
    It is the stable identity: ``zone_id`` / ``scope_id`` are nullable and
    ``SET NULL``, so deleting the SpatiumDDI-side row during a messy migration
    leaves the item (and its history) intact rather than vanishing mid-cutover.
    """

    __tablename__ = "cutover_item"
    __table_args__ = (
        UniqueConstraint("plan_id", "kind", "source_ref", name="uq_cutover_item_plan_kind_ref"),
        Index("ix_cutover_item_plan", "plan_id"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cutover_plan.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)

    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_zone.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    scope_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_scope.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    stage: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )

    # Latest readiness verdict — a list of ``{code, severity, message}`` dicts.
    # Recomputed on every parity run and immediately before a cutover; the
    # cutover endpoint refuses while any entry has ``severity == "block"``.
    blockers: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa_text("'[]'::jsonb")
    )

    last_parity: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_parity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_shadow: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_shadow_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Pre-flight TTL snapshot (DNS items). Written once, on the first apply,
    # and never overwritten by a second apply — it holds the *pristine*
    # pre-migration values, which is the whole point of keeping it.
    ttl_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ttl_lowered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Lease-handover result (DHCP items): counts + timestamp.
    lease_handover: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    cut_over_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    plan: Mapped[CutoverPlan] = relationship("CutoverPlan", back_populates="items")


class CutoverChecklistItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One line of the Phase 4 decommission checklist, per plan.

    Rows are seeded from the static catalog in
    :mod:`app.services.cutover.checklist` the first time a plan's checklist is
    read. The catalog can grow between releases; seeding is an idempotent
    upsert on ``(plan_id, key)``, so a plan created before a new item existed
    picks it up on the next read without losing any existing ticks.
    """

    __tablename__ = "cutover_checklist_item"
    __table_args__ = (
        UniqueConstraint("plan_id", "key", name="uq_cutover_checklist_plan_key"),
        Index("ix_cutover_checklist_plan", "plan_id"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cutover_plan.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="general")
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Which half of the migration the item belongs to: dns_zone | dhcp_scope |
    # general. Drives whether the UI shows it at all — a DNS-only plan has no
    # business being nagged about deauthorizing a DHCP server in AD.
    applies_to: Mapped[str] = mapped_column(
        String(12), nullable=False, default="general", server_default="general"
    )

    is_done: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    done_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0")
    )

    plan: Mapped[CutoverPlan] = relationship("CutoverPlan", back_populates="checklist")


class CutoverEvent(UUIDPrimaryKeyMixin, Base):
    """Append-only timeline entry for a plan.

    Deliberately not a ``TimestampMixin`` model: an event has one time, the
    moment it happened, and a ``modified_at`` on an append-only row would be a
    lie. ``item_id`` is ``SET NULL`` so removing an item from a plan doesn't
    erase the record of what was already done to it.
    """

    __tablename__ = "cutover_event"
    __table_args__ = (Index("ix_cutover_event_plan_at", "plan_id", "at"),)

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cutover_plan.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cutover_item.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    summary: Mapped[str] = mapped_column(String(512), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    user_display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
