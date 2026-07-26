"""Operator triage decisions on DNS threat findings (issue #699).

A finding is a *score*, not a verdict. Plenty of hosts legitimately look
like tunnels — a backup agent chunking payload into subdomains, a
security scanner, a bespoke internal service — and once an operator has
looked at one and concluded it's fine, it should stop shouting.

This is a **mute on the client**, deliberately not a delete on the
window rows, for two reasons:

* **Deleting wouldn't stick.** The aggregator recomputes the current and
  previous hour every 15 minutes and upserts, and the backfill refills
  any bucket left with no rows. A "cleared" recent finding would
  reappear within the quarter hour while an older one stayed gone —
  same button, different behaviour depending on age.
* **Deleting destroys evidence.** These rows describe what may be an
  exfiltration event. "We reviewed it and it's the backup script" is a
  fact worth keeping, and keeping it *next to* the evidence is what
  makes it auditable later.

Mirrors the acknowledge/suppress shape already used for
``internal_error``: a mute either expires (``muted_until``) or is
permanent (NULL), and carries the operator and their reason so the next
person to look doesn't re-investigate the same host.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DNSThreatMute(Base):
    """One operator decision to stop surfacing a client's findings."""

    __tablename__ = "dns_threat_mute"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # One active mute per client. Re-muting an already-muted client
    # updates the row (new expiry / reason) rather than stacking.
    client_ip: Mapped[str] = mapped_column(INET, nullable=False, unique=True)

    # NULL = muted indefinitely. A dated mute is the safer default in
    # the UI: "this is fine for now" ages out and gets re-examined,
    # rather than a one-click decision silently outliving the person
    # who made it.
    muted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Free text, required by the API — an unexplained mute is how a real
    # incident gets buried by someone tidying a dashboard.
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    muted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Denormalised so the row still says who muted it after the account
    # is deleted — the FK above goes NULL, the name shouldn't.
    muted_by_display: Mapped[str] = mapped_column(String(120), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    modified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def is_active(self, now: datetime) -> bool:
        """True when this mute still applies.

        An expired mute is kept rather than deleted: "we muted this host
        for 30 days in March" is part of the audit trail, and the row is
        what lets the UI say *why* a client stopped being muted.
        """
        return self.muted_until is None or self.muted_until > now
