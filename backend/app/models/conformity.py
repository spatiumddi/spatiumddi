"""Conformity evaluations — periodic policy checks against PCI / HIPAA /
internet-facing classifications, with audit-grade reporting (issue #106).

Two tables:

* ``ConformityPolicy`` — declarative check definition. Operator-authored
  for custom rules; ``is_builtin=True`` marks the seeded library (PCI /
  HIPAA / internet-facing starter set). Each row pins a ``check_kind``
  that names a Python evaluator function in
  ``app.services.conformity.checks``.

* ``ConformityResult`` — append-only history. One row per
  ``(policy, target resource)`` per evaluation pass. ``status`` is one
  of ``pass`` / ``fail`` / ``warn`` / ``not_applicable``. Indexed on
  ``(policy_id, evaluated_at desc)`` and
  ``(resource_kind, resource_id, evaluated_at desc)`` so two natural
  drilldown queries — "every result for this policy" and "every
  policy that touched this resource" — both hit an index.

The companion alert rule type in #105 fires *reactively* on
mutations; conformity evaluations run *proactively* on a schedule
and produce auditor-acceptable PDF artifacts. The two sit on the
same classification taxonomy (``pci_scope`` / ``hipaa_scope`` /
``internet_facing`` from #75) and use the same delivery channels
when a previously-conformant resource starts failing.
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ConformityPolicy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Declarative conformity check.

    The ``target_filter`` JSONB encodes a predicate over the target
    rows + their inheritance-aware classification — operators can
    target "every PCI subnet" without naming individual rows. Today
    classification flags only exist on Subnet, but the predicate
    grammar leaves room for inheritance once IPBlock / IPSpace
    classification lands.
    """

    __tablename__ = "conformity_policy"

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Free-form bucket: ``PCI-DSS 4.0`` | ``HIPAA`` | ``NIST 800-53`` |
    # ``SOC2`` | ``custom`` — drives PDF section grouping. Not a hard
    # enum because operators may name their own.
    framework: Mapped[str] = mapped_column(String(40), nullable=False, default="custom")
    # Optional control id within the framework (e.g. PCI-DSS 1.2.1).
    reference: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # ``info`` | ``warning`` | ``critical``. Sets the PDF colour and
    # the alert severity when this policy fails into the alert
    # framework.
    severity: Mapped[str] = mapped_column(
        String(10), nullable=False, default="warning", server_default=sa_text("'warning'")
    )

    # ``subnet`` | ``ip_address`` | ``dns_zone`` | ``dhcp_scope`` |
    # ``platform``. ``platform`` runs the check exactly once per pass
    # against the platform itself (no per-resource fanout) — useful
    # for "audit retention ≥ 90 d" style global policies.
    target_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    # Predicate against target row + inherited classification.
    # Recognised keys today:
    #   classification: "pci_scope" | "hipaa_scope" | "internet_facing"
    target_filter: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Named evaluator in ``app.services.conformity.checks``. Unknown
    # check_kind → result.status = "not_applicable" with a diagnostic
    # message naming the missing kind, so a stale policy doesn't
    # crash the whole pass.
    check_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    check_args: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )

    # Hours between evaluations. 0 = "on-demand only" (the beat task
    # skips them; operator triggers via the per-policy "Re-evaluate
    # now" endpoint).
    eval_interval_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=24, server_default=sa_text("24")
    )
    last_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Wire to an alert rule so a pass→fail flip emits an event into
    # the existing alert-event surface. NULL → no alert fanout.
    fail_alert_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("alert_rule.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "ix_conformity_policy_framework_enabled",
            "framework",
            "enabled",
        ),
    )


class ConformityResult(UUIDPrimaryKeyMixin, Base):
    """Append-only history. One row per (policy, resource) per pass.

    No ``modified_at`` — these rows never mutate. The unique
    constraint enforces "one row per policy/resource/pass" so the
    evaluator's per-pass UPSERT can't accidentally double-write.
    """

    __tablename__ = "conformity_result"

    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conformity_policy.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ``platform`` policies write resource_id=``"platform"`` and
    # resource_kind=``"platform"`` for the singleton row.
    resource_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_display: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # ``pass`` | ``fail`` | ``warn`` | ``not_applicable``. ``warn``
    # is reserved for soft policy violations the operator may
    # tolerate; today every check uses ``pass`` / ``fail`` /
    # ``not_applicable``.
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    diagnostic: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index(
            "ix_conformity_result_policy_evaluated",
            "policy_id",
            "evaluated_at",
        ),
        Index(
            "ix_conformity_result_resource_evaluated",
            "resource_kind",
            "resource_id",
            "evaluated_at",
        ),
    )
