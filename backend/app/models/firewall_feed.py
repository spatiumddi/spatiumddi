"""SpatiumDDI-hosted firewall block-list feed (#606 — the "feed inversion").

The cross-cutting building block that lets feed-polling firewalls enforce
SpatiumDDI's block set with **no write credentials on the firewall**: instead
of SpatiumDDI pushing to the device (the OPNsense/UniFi/PAN-OS #601 model), the
device polls a token-scoped URL SpatiumDDI serves and applies whatever it
returns.

* **Fortinet FortiGate** — *External Threat Feed* (External Connector) points
  at ``GET /api/v1/firewall-feeds/{id}/blocklist.txt?token=…``.
* **Cisco FTD/FMC** — *Security Intelligence* network feed (Phase 2).
* **Check Point** — IOC / custom-intelligence feed (Phase 2).

The feed renders the same ``NetworkBlock`` desired-state set the #601 push
reconcilers converge (fed by #370 rogue-DHCP, #459 new-device, and manual
entries) — one IP / CIDR per line, plain text. The block set is the single
source of truth; the feed is just a projection of it.

Auth is a per-feed unguessable token (Fernet-encrypted at rest, revealed to
the operator through a password-confirm flow so they can paste the full URL
into the firewall). The polling firewall presents it as ``?token=`` (query)
or ``Authorization: Bearer`` (header).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, LargeBinary, String, Text
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Feed value kinds. ``ip`` renders IP/CIDR ``NetworkBlock`` rows (the FortiGate
# External IP List / Cisco SI network-feed shape). ``domain`` is reserved for a
# future FQDN feed.
FIREWALL_FEED_KINDS: tuple[str, ...] = ("ip",)


class FirewallFeed(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A token-scoped block-list feed a firewall polls."""

    __tablename__ = "firewall_feed"
    __table_args__ = (Index("ix_firewall_feed_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    # Which ``NetworkBlock.kind`` this feed projects. ``ip`` today.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="ip")

    # Fernet-encrypted access token. Compared (constant-time) against the
    # ``?token=`` / bearer the polling firewall presents; revealed to the
    # operator via a password-confirm endpoint so they can build the poll URL.
    token_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, default=b"", server_default=sa_text("''::bytea")
    )

    # Poll telemetry — proves a firewall is actually consuming the feed.
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_polled_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    poll_count: Mapped[int] = mapped_column(nullable=False, default=0, server_default=sa_text("0"))


__all__ = ["FIREWALL_FEED_KINDS", "FirewallFeed"]
