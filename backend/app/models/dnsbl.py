"""DNSBL / RBL reputation monitoring (issue #528).

SpatiumDDI already knows every public-facing IP it manages — IPAM rows,
NAT/PAT egress addresses, cloud-mirror public IPs, and the subnets an
operator has flagged ``internet_facing`` (#75). This subsystem checks
those IPs against the major DNS blocklists (Spamhaus ZEN, Barracuda,
SpamCop, SORBS, …) on a daily sweep — the classic reversed-octet DNS
lookup (``4.3.2.1.zen.spamhaus.org``) — so a mail-deliverability /
reputation problem surfaces here before users report bounced mail.

Three tables:

* :class:`DNSBLList` — the curated catalog of blocklists, one row per
  list. Seeded as platform rows (``is_builtin=True``) the same way the
  RPZ source catalog + BGP-communities catalog are. Each row carries the
  DNS ``zone_suffix`` (``zen.spamhaus.org``), a per-list ``enabled``
  toggle, a ``return_codes`` map (``{"127.0.0.2": "spam"}``) rendered in
  the setup UI, and ``requires_registration`` + ``qps_note`` so the
  operator knows a list needs a data-feed subscription (Spamhaus for
  high-volume) or has a strict query-rate policy.

* :class:`DNSBLPinnedIP` — operator-pinned IPs to always monitor, on top
  of the auto-derived candidate set. One row per IP. ``ip_address_id`` is
  an optional convenience FK to the IPAM row (``SET NULL`` so deleting
  the IPAM row keeps the pin).

* :class:`DNSBLListing` — the per-IP-per-list result / latch state, one
  row per ``(ip, list)`` pair that has been checked at least once.
  ``listed`` is the current state; ``resolved_at`` is set (and ``listed``
  flipped False) when a later sweep finds the IP delisted. The alert
  evaluator reads *active* rows (``listed IS TRUE``) and mirrors their
  lifecycle into ``AlertEvent`` — exactly the latch pattern the domain /
  circuit / BGP-hijack rules use, so a listing fires once on first
  sighting and auto-resolves on delist.

Only IPv4 is checked in v1 — the DNSBLs are IPv4-centric; IPv6 DNSBL
(nibble-reversed ``ip6.arpa``-style suffixes) is a future enhancement.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Candidate provenance — how an IP ended up in the sweep. Stored on the
# listing row so the UI / alert can tell the operator *why* an IP is
# being monitored.
SOURCE_IPAM = "ipam"  # public IP tracked as an IPAM ip_address row
SOURCE_INTERNET_FACING = "internet_facing"  # IP in an internet_facing subnet
SOURCE_NAT_EGRESS = "nat_egress"  # external/egress IP of a NAT/PAT mapping
SOURCE_PINNED = "pinned"  # operator-pinned via DNSBLPinnedIP

DNSBL_SOURCES = frozenset({SOURCE_IPAM, SOURCE_INTERNET_FACING, SOURCE_NAT_EGRESS, SOURCE_PINNED})


class DNSBLList(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A curated DNS blocklist SpatiumDDI can query (catalog row)."""

    __tablename__ = "dnsbl_list"
    __table_args__ = (
        UniqueConstraint("zone_suffix", name="uq_dnsbl_list_zone_suffix"),
        Index("ix_dnsbl_list_enabled", "enabled"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # The DNS zone the reversed-octet query is appended to, e.g.
    # ``zen.spamhaus.org``. No trailing dot.
    zone_suffix: Mapped[str] = mapped_column(String(255), nullable=False)

    # Coarse bucket for UI grouping — ``combined`` | ``spam`` | ``exploit``
    # | ``policy`` | ``proxy``. Purely informational.
    category: Mapped[str] = mapped_column(
        String(24), nullable=False, default="combined", server_default=sa_text("'combined'")
    )

    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=sa_text("''")
    )
    homepage_url: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Per-list enable. Default OFF for lists that require registration
    # (Spamhaus over a public resolver returns 127.255.255.252 = "query
    # blocked"); the operator opts each list in from the catalog UI.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    # Return-code → meaning map, e.g. {"127.0.0.2": "spam",
    # "127.0.0.4": "exploit"}. Used to render a human reason from the A
    # record a listing returns.
    return_codes: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa_text("'{}'::jsonb")
    )

    # True when the list requires a paid/registered data feed or a
    # rDNS/rsync mirror for anything beyond trivial query volume (e.g.
    # Spamhaus, Barracuda). Surfaced as a warning badge in the setup UI.
    requires_registration: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    # Free-text note on the list's query-rate / registration policy shown
    # in the setup UI (e.g. "Free for < 300k queries/day from a
    # non-public resolver; register for higher volume").
    qps_note: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=sa_text("''")
    )

    # Platform-seeded row (from the catalog) vs operator-authored custom
    # list. Seed rows are refreshed idempotently on boot.
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )


class DNSBLPinnedIP(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An IP the operator has pinned for reputation monitoring."""

    __tablename__ = "dnsbl_pinned_ip"
    __table_args__ = (
        UniqueConstraint("ip", name="uq_dnsbl_pinned_ip"),
        Index("ix_dnsbl_pinned_ip_ip_address_id", "ip_address_id"),
    )

    ip: Mapped[str] = mapped_column(INET, nullable=False)
    note: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=sa_text("''")
    )
    # Optional convenience link to the IPAM row. SET NULL so deleting the
    # IPAM row leaves the pin (the raw ``ip`` stays authoritative).
    ip_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_address.id", ondelete="SET NULL"),
        nullable=True,
    )


class DNSBLListing(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Per-IP-per-list reputation result + latch.

    Exists once a ``(ip, list)`` pair has been checked. ``listed`` is the
    current state; the sweep resolves the row (``listed=False`` +
    ``resolved_at`` set) when the IP delists so the alert auto-resolves.
    """

    __tablename__ = "dnsbl_listing"
    __table_args__ = (
        UniqueConstraint("ip", "list_id", name="uq_dnsbl_listing_ip_list"),
        Index("ix_dnsbl_listing_list_id", "list_id"),
        Index(
            "ix_dnsbl_listing_listed",
            "listed",
            postgresql_where=sa_text("listed IS TRUE"),
        ),
        Index("ix_dnsbl_listing_ip", "ip"),
    )

    ip: Mapped[str] = mapped_column(INET, nullable=False)
    list_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dnsbl_list.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Current listing state. The sweep flips this + resolved_at.
    listed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    # Which candidate source surfaced this IP (see the SOURCE_* consts).
    # Purely informational; refreshed on each sweep.
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SOURCE_IPAM, server_default=sa_text("'ipam'")
    )

    # The 127.0.0.x return codes the A query resolved to (list of str).
    return_codes: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa_text("'[]'::jsonb")
    )
    # The TXT record the list returned (the human reason / delist URL).
    txt_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set when the last check itself errored (transient resolver / SERVFAIL
    # — recorded as data so the sweep never raises). NULL on a clean check.
    check_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    first_listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "DNSBLList",
    "DNSBLPinnedIP",
    "DNSBLListing",
    "SOURCE_IPAM",
    "SOURCE_INTERNET_FACING",
    "SOURCE_NAT_EGRESS",
    "SOURCE_PINNED",
    "DNSBL_SOURCES",
]
