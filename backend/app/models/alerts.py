"""Alerts — rule-based notifications on metrics we already collect.

Two tables:

* ``AlertRule`` — the operator-authored definition. Each row pins a
  rule type (``subnet_utilization`` / ``server_unreachable``) plus its
  params and which delivery channels to hit on firing.

* ``AlertEvent`` — one row per firing. ``resolved_at`` is NULL while
  the underlying condition still matches; the evaluator flips it to
  the current timestamp when the condition clears. This is the
  "open vs resolved" view the UI renders and the syslog/webhook hooks
  consume.

Delivery reuses the platform-level syslog + webhook targets configured
for audit forwarding (``services/audit_forward._send_syslog`` /
``_send_webhook``). A per-rule delivery override would be nice but adds
a config surface we don't need yet — every op shop I've seen feeds
alerts into the same SIEM that audit goes to.

De-duplication: the evaluator looks for an existing *open* AlertEvent
for ``(rule_id, subject_type, subject_id)`` before opening a new one.
So a subnet hovering at 96% utilisation doesn't fire every minute —
exactly one open event until it drops back below threshold.
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
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AlertRule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Operator-authored alert definition."""

    __tablename__ = "alert_rule"

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )

    # Rule type discriminator. Valid values today:
    #   "subnet_utilization"   — threshold_percent vs Subnet.utilization_percent.
    #                            Honours PlatformSettings.utilization_max_prefix_*
    #                            so /30, /127 etc. can't trip the alarm.
    #   "server_unreachable"   — server_type in {"dns","dhcp","any"}; fires on
    #                            any server row whose `status` is "unreachable"
    #                            or "error" at eval time.
    rule_type: Mapped[str] = mapped_column(String(40), nullable=False)

    # Subnet utilization params.
    threshold_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Server-unreachable params.
    server_type: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # Severity (info | warning | critical). Maps to a syslog severity on
    # delivery and drives UI colour.
    severity: Mapped[str] = mapped_column(
        String(10), nullable=False, default="warning", server_default=sa_text("'warning'")
    )

    # Delivery channel toggles. Platform-level audit-forward targets are
    # reused for the actual connection.
    notify_syslog: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    notify_webhook: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )

    __table_args__ = (Index("ix_alert_rule_rule_type_enabled", "rule_type", "enabled"),)


class AlertEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One firing of an ``AlertRule`` against one subject.

    A row's lifecycle:
      * Created when the evaluator first sees the condition match for a
        subject with no existing open event → ``resolved_at`` NULL.
      * Stays open as long as the condition still matches on each eval.
      * Closed by the next eval pass that finds the condition cleared
        — the same pass never both opens and closes an event; there's
        always at least one "still-firing" tick in between.
    """

    __tablename__ = "alert_event"

    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("alert_rule.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # What fired. `subject_type` mirrors the rule type family:
    #   "subnet"  — subject_id is a Subnet UUID, display is "<CIDR> — <name>"
    #   "server"  — subject_id is a DNS/DHCPServer UUID, display is the name
    subject_type: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_display: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="warning")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Delivery receipts. Set once by the first evaluator pass that
    # opened the event; subsequent "still firing" ticks don't re-deliver.
    delivered_syslog: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    delivered_webhook: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    __table_args__ = (
        Index(
            "ix_alert_event_rule_subject_open",
            "rule_id",
            "subject_type",
            "subject_id",
            postgresql_where=sa_text("resolved_at IS NULL"),
        ),
        Index("ix_alert_event_fired_at", "fired_at"),
    )
