"""BGP prefix-hijack monitoring (issue #527).

Two tables extend the ASN subsystem with route-origin monitoring:

* ``bgp_tracked_prefix`` — the set of prefixes SpatiumDDI watches on the
  public routing table, one row per ``(asn, prefix)``. The periodic
  poll task (``app.tasks.bgp_hijack_poll``) auto-populates rows from the
  tracked ASN's RPKI ROAs + RIPEstat announced-prefixes; operators can
  also add rows by hand (``source="manual"``). ``expected_origin_asn``
  is denormalised from the parent AS's ``number`` so the poll's
  origin-mismatch compare doesn't re-join on every prefix.
  ``allowed_origins`` is the operator-curated allowlist of *additional*
  origin ASNs that are legitimately allowed to announce the prefix
  (intentional multi-origin / anycast / DDoS-scrubbing providers) —
  the "acknowledge an expected additional origin" write appends here.

* ``bgp_hijack_detection`` — one row per observed hijack. This table IS
  the latch/dedup state: the poll opens a row (``resolved_at`` NULL) the
  first time it sees an unexpected origin announcing a tracked prefix,
  bumps ``last_seen_at`` while the announcement persists, and resolves
  the row (``resolved_at`` set) once the announcement has been absent
  for the delisting window. The alert evaluator reads *active* rows and
  mirrors their lifecycle into ``AlertEvent`` — exactly the pattern the
  RPKI-ROA state ladder + the domain latch rules use.

``detection_kind`` distinguishes an exact-prefix hijack
(``prefix_hijack`` — someone else announcing our exact CIDR) from a
sub-prefix hijack (``more_specific`` — an unexpected origin announcing a
more-specific slice of our prefix, which wins BGP best-path by longest
match). ``rpki_status`` reuses the ROA data already pulled by
``app.tasks.rpki_roa_refresh``: ``invalid`` when a ROA covers the prefix
but doesn't authorise the observed origin (highest confidence it's a
hijack), ``unknown`` when no ROA covers the prefix at all. Severity is
escalated to ``critical`` for ``invalid`` vs ``warning`` for
``unknown``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import CIDR, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BGPTrackedPrefix(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A prefix SpatiumDDI monitors on the public routing table."""

    __tablename__ = "bgp_tracked_prefix"
    __table_args__ = (
        UniqueConstraint("asn_id", "prefix", name="uq_bgp_tracked_prefix"),
        Index("ix_bgp_tracked_prefix_asn", "asn_id"),
        Index("ix_bgp_tracked_prefix_enabled", "enabled"),
    )

    asn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("asn.id", ondelete="CASCADE"),
        nullable=False,
    )
    prefix: Mapped[str] = mapped_column(CIDR, nullable=False)

    # Denormalised from ``asn.number`` — the origin AS we EXPECT to see
    # announcing this prefix. Refreshed by the poll on every pass so an
    # AS-number correction on the parent row propagates.
    expected_origin_asn: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # ``roa`` | ``announced`` | ``both`` | ``manual``. Manual rows are
    # never auto-pruned; auto rows (roa / announced / both) are
    # reconciled against the live sources each pass.
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="roa", server_default=sa_text("'roa'")
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )

    # Operator-curated list of ADDITIONAL origin ASNs that are allowed to
    # announce this prefix without tripping a detection (intentional
    # multi-origin, anycast, scrubbing providers). Stored as a JSON list
    # of ints. The "allowlist an expected additional origin" write
    # appends here.
    allowed_origins: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa_text("'[]'::jsonb")
    )

    # Snapshot of the origins observed on the last successful poll —
    # purely informational for the UI (does not gate detection).
    last_seen_origins: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Per-row gate: the poll only re-evaluates a prefix whose
    # ``next_check_at`` is NULL or elapsed, then bumps it forward by the
    # configured interval — mirrors ``asn.next_check_at`` /
    # ``asn_rpki_roa.next_check_at``.
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BGPHijackDetection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One observed prefix-hijack detection — open until the announcement
    delists, then auto-resolved."""

    __tablename__ = "bgp_hijack_detection"
    __table_args__ = (
        Index("ix_bgp_hijack_detection_asn", "asn_id"),
        Index(
            "ix_bgp_hijack_detection_open",
            "asn_id",
            "observed_prefix",
            "observed_origin_asn",
            "detection_kind",
            postgresql_where=sa_text("resolved_at IS NULL"),
        ),
        Index("ix_bgp_hijack_detection_resolved", "resolved_at"),
    )

    # ``ON DELETE SET NULL`` (NOT cascade): the poll prunes a tracked
    # prefix (``db.delete``) the moment it disappears from RIPEstat /
    # ROA sources — which is EXACTLY what happens to a victim prefix
    # while it's being hijacked (the legitimate announcement drops out
    # of the routing table). A cascade would then delete every open
    # detection + orphan its ``AlertEvent``, clearing the alarm at the
    # worst moment. The detection carries ``tracked_prefix`` (the CIDR
    # string) + ``asn_id`` independently, so it stays open/latched and
    # keeps alerting even after the FK goes NULL.
    tracked_prefix_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bgp_tracked_prefix.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    asn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("asn.id", ondelete="CASCADE"),
        nullable=False,
    )

    tracked_prefix: Mapped[str] = mapped_column(CIDR, nullable=False)
    observed_prefix: Mapped[str] = mapped_column(CIDR, nullable=False)
    expected_origin_asn: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_origin_asn: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # ``prefix_hijack`` (exact-prefix) | ``more_specific`` (sub-prefix).
    detection_kind: Mapped[str] = mapped_column(String(24), nullable=False)

    # ``invalid`` (ROA covers the prefix but not the observed origin) |
    # ``unknown`` (no ROA covers the prefix). ``valid`` announcements are
    # never persisted — they're legitimate multi-origin, not hijacks.
    rpki_status: Mapped[str] = mapped_column(String(12), nullable=False, default="unknown")

    # ``info`` | ``warning`` | ``critical``. Derived from ``rpki_status``
    # by the poll; the alert evaluator uses it as the per-detection
    # severity override.
    severity: Mapped[str] = mapped_column(
        String(10), nullable=False, default="warning", server_default=sa_text("'warning'")
    )

    # ``ripestat_poll`` (beat task) | ``ris_live`` (optional WS consumer).
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="ripestat_poll",
        server_default=sa_text("'ripestat_poll'"),
    )

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Operator acknowledged the detection — suppresses the alert (the
    # matcher skips acknowledged rows) without waiting for the delist
    # window. Distinct from ``allowed_origins`` on the tracked prefix,
    # which suppresses FUTURE detections for that origin too.
    acknowledged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    # Free-form context — the holder org of the observed origin, the RIS
    # peer count, the AS path, etc. Purely informational.
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=sa_text("''")
    )


__all__ = ["BGPTrackedPrefix", "BGPHijackDetection"]
