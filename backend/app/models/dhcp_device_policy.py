"""Fingerprint-driven DHCP policy (issue #700).

Device profiling (#—passive DHCP fingerprinting via the agent's scapy
sniffer + fingerbank enrichment) tells us what a device *is*. Kea client
classes let us treat classes of device differently. Nothing connected the
two: the profile was read-only decoration on the IP row, and client
classes were hand-authored match expressions.

A ``DHCPDevicePolicy`` is the join. The operator picks fingerbank device
*classes* ("Printer", "Internet of Things (IoT)") and an outcome — an
option set, a lease time, and optionally a pool — and SpatiumDDI compiles
that down to a real Kea client-class ``test`` expression over the
option-55 / option-60 signatures that produced those classifications.

**What the compiled expression actually matches, stated plainly.**
Fingerbank's classification is a lookup against their corpus; there is no
"device class == IoT" predicate a DHCP server can evaluate. So the
compiler matches the *signatures we have observed and had classified into
the selected classes* — not the abstract category. Two consequences the UI
repeats rather than hides:

1. A device whose signature we have never seen does not match, however
   obviously it belongs to the category. It is classified on its first
   lease and the policy applies from the next renewal. That is the
   honest v1 boundary, and it is why nothing here should be described
   as instant enforcement.
2. The policy tracks observation. As new signatures land in a selected
   class the expression grows on its own — which is the feature, but it
   also means the rendered config changes without an operator edit.

``class_name`` is deliberately a stored column rather than something
derived from ``name`` at render time. Pools reference a class by name
(``DHCPPool.class_restriction``), so a rename that silently moved the
generated class name would detach every pool pointing at it — the pool
would keep a restriction to a class that no longer exists, which Kea
accepts and which quietly stops the pool serving anyone.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.dhcp import DHCPServerGroup


class DHCPDevicePolicy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One fingerprint-driven policy — group-scoped, compiled into a Kea class."""

    __tablename__ = "dhcp_device_policy"
    __table_args__ = (
        UniqueConstraint("group_id", "name", name="uq_dhcp_device_policy_group_name"),
        # The generated Kea class name has to be unique per group for the
        # same reason ``name`` does: two classes with one name is a config
        # Kea rejects outright, taking every other class down with it.
        UniqueConstraint("group_id", "class_name", name="uq_dhcp_device_policy_group_class_name"),
        Index("ix_dhcp_device_policy_group", "group_id"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server_group.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # The Kea client-class name this policy compiles to. Stable across
    # renames (see the module docstring) and referenced by pools via
    # ``DHCPPool.class_restriction`` to get the issue's "optionally this
    # pool" outcome.
    class_name: Mapped[str] = mapped_column(String(128), nullable=False)

    # Fingerbank device classes the operator selected, e.g.
    # ``["Printer", "Internet of Things (IoT)"]``. Matched against
    # ``DHCPFingerprint.fingerbank_device_class``.
    device_classes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # The outcome. ``options`` mirrors ``DHCPClientClass.options``
    # (``{name: value}``); ``lease_time`` becomes the class's
    # ``valid-lifetime``, which Kea honours per-class — verified against
    # kea-dhcp4 3.0.3, since a silently-ignored lease time would be the
    # difference between a quarantine that expires and one that does not.
    options: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    lease_time: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Operator escape hatch, required by the issue: the compiled
    # expression must be visible AND overridable so nobody debugs a black
    # box against kea-dhcp4.log. When set, this is rendered verbatim and
    # the compiler's output is shown alongside it for comparison only.
    match_override: Mapped[str | None] = mapped_column(Text, nullable=True)

    # A signature seen on devices BOTH inside and outside the selected
    # classes is ambiguous: matching it would apply this policy to devices
    # the operator did not select. Excluded by default; this is the
    # explicit, audited opt-in. See ``services.dhcp.device_policy``.
    include_ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Kea evaluates client-classes in declaration order, so this is
    # behaviour rather than presentation — the same reasoning as
    # ``DHCPPXEArchMatch.priority``.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_compiled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    group: Mapped[DHCPServerGroup] = relationship(
        "DHCPServerGroup", back_populates="device_policies"
    )


__all__ = ["DHCPDevicePolicy"]
