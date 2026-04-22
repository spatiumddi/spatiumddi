"""ACME (RFC 8555) DNS-01 provider account model.

SpatiumDDI exposes an `acme-dns`-compatible HTTP surface under
`/api/v1/acme/` for external clients (certbot / lego / acme.sh) that
need to prove control of a FQDN hosted in a SpatiumDDI-managed zone.
Each row is one issued credential, scoped to a single subdomain of a
specific zone — the delegation pattern is the standard acme-dns one:
the operator CNAMEs ``_acme-challenge.<their-domain>`` to
``<subdomain>.<our-acme-zone>`` and gives SpatiumDDI authority over
the small subzone only.

Permissions note: ACME is a **separate auth path** from `APIToken`.
Clients speak the acme-dns protocol (`X-Api-User` / `X-Api-Key`), not
bearer JWTs, so the credential surface doesn't pollute the main API
RBAC path. Management of ACME accounts (create / list / revoke) IS
gated through the normal permission system — resource type
``acme_account``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ACMEAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One acme-dns-compatible credential.

    Each account is bound to a specific ``DNSZone`` (the parent
    delegation target) and a unique ``subdomain`` within that zone.
    Clients authenticate with ``username`` + the plaintext password we
    generated at registration — we only store the bcrypt hash. A
    leaked credential can only write TXT records at
    ``<subdomain>.<zone>``; it cannot touch any other part of the
    zone.
    """

    __tablename__ = "acme_account"

    # acme-dns protocol fields — stable identifiers returned to the
    # client at registration, used to authenticate subsequent /update
    # calls. ``username`` doubles as the lookup key on auth.
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # The subdomain the account is permitted to write TXT records at.
    # Canonical acme-dns shape: a UUID. Unique across the whole system
    # (not just within a zone) so accidental collisions can't grant one
    # account write access under another's delegation.
    subdomain: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    # The parent zone the subdomain lives under. The zone is operator-
    # provisioned ahead of time (it's a regular SpatiumDDI zone, just
    # intended to carry ACME TXT records — typically something like
    # ``acme.example.com.``).
    zone_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_zone.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Optional IP allowlist (CIDRs). Empty / NULL = no source
    # restriction. Checked against the HTTP client address on every
    # /update call.
    allowed_source_cidrs: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    # Human-readable label for the admin UI. Not part of the protocol.
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Eagerly-accessible zone for the /update path (need zone.name +
    # zone.group_id to route the TXT write). Not a back_populates
    # relationship — DNSZone doesn't know about ACME accounts.
    zone: Mapped[DNSZone] = relationship("DNSZone")  # type: ignore[name-defined]  # noqa: F821


__all__ = ["ACMEAccount"]
