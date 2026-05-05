"""Domain registration tracking — distinct from DNSZone.

A ``Domain`` is the registered name as it lives in the registry / WHOIS
(e.g. ``example.com`` registered with GoDaddy). A ``DNSZone`` is what
SpatiumDDI serves authoritatively. One Domain may have zero, one, or
many DNSZones beneath it; the linkage is intentionally optional and
deferred to a follow-up PR (see issue #87).

This phase ships:

* The DB row.
* A synchronous ``refresh-whois`` action that calls the RDAP client
  in :mod:`app.services.rdap` and writes the normalised response back.
* The standard CRUD shape (list / create / get / update / delete /
  bulk-delete).

The scheduled background refresh task + alert rule types + dashboard
widget are deferred to follow-ups so the cadence + rate-limits can be
dialed in independently of the data-model landing.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Domain(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single registered domain name (apex FQDN).

    ``name`` is stored lowercase + without trailing dot; the API does
    the normalisation on write so callers can paste any common shape.

    ``whois_state`` is a derived label that ``refresh-whois`` recomputes
    on each refresh. Operators read it; the scheduled task (deferred)
    will read it too. See ``derive_whois_state`` in the router for the
    decision rules.
    """

    __tablename__ = "domain"
    __table_args__ = (
        Index("ix_domain_name", "name", unique=True),
        Index("ix_domain_whois_state", "whois_state"),
        Index("ix_domain_expires_at", "expires_at"),
        Index("ix_domain_next_check_at", "next_check_at"),
    )

    # Apex FQDN, lowercased + trailing-dot-stripped on save.
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # ── Registry-side facts (populated by RDAP on refresh) ────────────
    registrar: Mapped[str | None] = mapped_column(String(255), nullable=True)
    registrant_org: Mapped[str | None] = mapped_column(String(255), nullable=True)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Nameserver expectation + observation ──────────────────────────
    # Operator sets ``expected_nameservers``; ``actual_nameservers`` is
    # what RDAP last reported. ``nameserver_drift`` is recomputed on
    # every refresh and is a denormalised flag for cheap "is this
    # drifting?" filtering in list reads.
    expected_nameservers: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    actual_nameservers: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    nameserver_drift: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    dnssec_signed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # ── WHOIS bookkeeping ────────────────────────────────────────────
    # ``whois_data`` carries the raw RDAP response (or None on
    # unreachable). The endpoint surfaces it back to the UI as a
    # collapsible JSON viewer so operators can dig into the original
    # registry payload without leaving SpatiumDDI.
    whois_last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    whois_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # whois_state: ok | drift | expiring | expired | unreachable | unknown
    whois_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )

    # When the scheduled task should next consider this row. NULL means
    # "never refreshed yet — pick up on the next sweep". The task
    # itself doesn't ship in this PR; the column is here so the
    # follow-up can land without a fresh migration.
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Operator metadata ─────────────────────────────────────────────
    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    custom_fields: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    # Logical ownership (issue #91).
    #
    # ``registrar_provider_id`` is the FK successor to the freeform
    # ``registrar`` text column above. The text column stays through
    # this release for back-compat — the issue defers the
    # text-to-FK backfill to a follow-up so existing operator-
    # populated values aren't silently lost on a name mismatch.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customer.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    registrar_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("provider.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


__all__ = ["Domain"]
