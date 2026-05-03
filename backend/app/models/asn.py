"""ASN management — first-class entity for the autonomous systems
carrying our IP space.

Phase 1 ships:

* ``asn`` — the AS row itself. ``number`` is the natural-key 32-bit
  AS number; ``kind`` (public / private) and ``registry`` (RIR) are
  auto-derived from RFC 6996 + RFC 7300 ranges + the IANA ASN
  delegation snapshot at
  ``backend/app/data/asn_registry_delegations.json``. WHOIS/RDAP
  state columns (``whois_*``) are present but un-populated until the
  refresh job lands in a follow-up issue.

* ``asn_rpki_roa`` — RPKI Route Origin Authorization records that
  the AS is authorised to originate. Each row carries the prefix +
  ``max_length`` + validity window + trust anchor + computed state
  (valid / expiring_soon / expired / not_found). Pull job from RIPE
  / Cloudflare / Routinator lands in its own follow-up issue —
  Phase 1 ships only the schema so the FK + UI can wire to a real
  table.

The four `BGP-relationship FKs` (``IPSpace.asn_id``, ``IPBlock.asn_id``,
``Router.local_asn_id``, ``VRF.asn_id``) land when those tables get
touched in their own waves; nothing on this side blocks them.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CIDR, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ASN(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An autonomous-system number tracked by SpatiumDDI.

    ``number`` carries the full 32-bit AS-number range (1..4_294_967_295)
    so it must be stored as ``BigInteger`` rather than ``Integer`` —
    Postgres ``integer`` tops out at 2_147_483_647, which would silently
    truncate any 32-bit private AS (``4_200_000_000+``). We never store
    ``0`` (RFC 7607 reserved), enforced via the create endpoint's
    Pydantic validator rather than a CHECK constraint so legacy rows
    can be migrated in if needed.

    ``kind`` and ``registry`` are denormalised onto the row so list
    queries can filter on them without re-deriving on every read.
    The CRUD layer recomputes both on every write — operators can't
    set them by hand. The RDAP refresh job (follow-up) overwrites
    ``holder_org``, ``whois_data``, ``whois_state`` and
    ``whois_last_checked_at`` based on real WHOIS/RDAP responses.
    """

    __tablename__ = "asn"
    __table_args__ = (
        UniqueConstraint("number", name="uq_asn_number"),
        Index("ix_asn_kind", "kind"),
        Index("ix_asn_registry", "registry"),
        Index("ix_asn_whois_state", "whois_state"),
        Index("ix_asn_holder_org", "holder_org"),
    )

    number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # ``public`` (delegated by an RIR) | ``private`` (RFC 6996 / 7300).
    # Auto-derived from ``number`` on create; never operator-settable.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="public")

    # Pulled from RDAP / WHOIS by the refresh job. NULL until that job
    # has run for the row, or for private ranges where it never runs.
    holder_org: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # ``arin`` | ``ripe`` | ``apnic`` | ``lacnic`` | ``afrinic`` |
    # ``unknown``. Auto-derived from the IANA delegation snapshot;
    # ``unknown`` is the safe default for private ranges and for
    # public numbers the snapshot doesn't cover yet.
    #
    # ``Mapped[]`` annotation collides with SQLAlchemy's declarative
    # class-level ``registry`` attribute on ``DeclarativeBase``; the
    # ``# type: ignore[misc]`` silences mypy's override warning. The
    # ORM still maps the column correctly because ``mapped_column``
    # establishes the column descriptor at class-construction time.
    registry: Mapped[str] = mapped_column(  # type: ignore[misc]
        String(16), nullable=False, default="unknown"
    )

    # WHOIS / RDAP state. ``state`` mirrors the alert-rule ladder so
    # the UI badge logic and the alert evaluator can share one source
    # of truth: ``ok`` = matches last snapshot, ``drift`` = holder
    # changed since last check, ``unreachable`` = consecutive WHOIS
    # failures, ``n/a`` = private range (refresh skipped).
    whois_last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    whois_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    whois_state: Mapped[str] = mapped_column(String(16), nullable=False, default="n/a")

    # Operator-driven metadata; same shape as everywhere else in IPAM
    # / DHCP / DNS so cross-resource bulk-edits and CSV exports compose.
    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    custom_fields: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    # CASCADE delete — RPKI ROAs are orphans without their AS.
    rpki_roas: Mapped[list[ASNRpkiRoa]] = relationship(
        "ASNRpkiRoa",
        back_populates="asn",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ASNRpkiRoa(UUIDPrimaryKeyMixin, Base):
    """RPKI Route Origin Authorization (ROA) — `(asn, prefix, max_length)`
    triple authorising the AS to originate the prefix on the public
    routing table, plus the validity window from the trust anchor.

    Phase 1 ships the schema only; the pull job that populates rows
    from RIPE NCC's RPKI Validator JSON / Cloudflare's
    ``rpki.cloudflare.com`` mirror / Routinator output is a follow-up.

    ``valid_to`` is the actual "expiring" surface — the alert rules
    (``rpki_roa_expiring`` / ``rpki_roa_expired``) key off it. ROAs
    have hard expiry windows per RFC 6488; AS numbers themselves do
    not, which is why this is a separate table rather than a column
    on ``asn``.

    ``state`` is denormalised the same way ``asn.whois_state`` is so
    the dashboard and list filters don't have to recompute on every
    read. The pull job writes it; ``valid`` until <30 days, then
    ``expiring_soon``, then ``expired`` when ``valid_to <= now()``.
    ``not_found`` is set when the trust anchor stops emitting the ROA
    (the AS lost authorisation) without us having seen it expire.
    """

    __tablename__ = "asn_rpki_roa"
    __table_args__ = (
        # An AS can have the same ``(prefix, max_length)`` ROA from
        # different trust anchors (e.g. a transit-AS gets a ROA from
        # both ARIN and APNIC); we keep one row per anchor so the
        # pull job can distinguish them and the UI can show which
        # anchor went stale.
        UniqueConstraint("asn_id", "prefix", "max_length", "trust_anchor", name="uq_asn_rpki_roa"),
        Index("ix_asn_rpki_roa_asn", "asn_id"),
        Index("ix_asn_rpki_roa_state", "state"),
        Index("ix_asn_rpki_roa_valid_to", "valid_to"),
    )

    asn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("asn.id", ondelete="CASCADE"),
        nullable=False,
    )
    prefix: Mapped[str] = mapped_column(CIDR, nullable=False)
    max_length: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # ``arin`` | ``ripe`` | ``apnic`` | ``lacnic`` | ``afrinic``
    trust_anchor: Mapped[str] = mapped_column(String(16), nullable=False)

    # ``valid`` | ``expiring_soon`` | ``expired`` | ``not_found``
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="valid")

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    asn: Mapped[ASN] = relationship("ASN", back_populates="rpki_roas")


__all__ = ["ASN", "ASNRpkiRoa"]
