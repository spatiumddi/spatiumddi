"""Passive DHCP fingerprinting — Phase 2 of device profiling.

One row per MAC address (not per lease — the same device produces the
same option-55/60 signature across lease renewals, and we don't want
to hammer fingerbank for every DHCPREQUEST).

The agent's scapy sniffer captures DISCOVER + REQUEST packets on the
DHCP server's interface and ships their option-55 (parameter request
list) / option-60 (vendor class identifier) / option-77 (user class)
/ option-61 (client id) fields here. A Celery task picks each new /
stale fingerprint up and queries fingerbank for an enriched
``device_type`` / ``device_class`` / ``device_manufacturer`` triple.

The fingerbank result is cached on the row for 7 days — see
``services.profiling.fingerbank.FINGERBANK_CACHE_DAYS``. After that
window the next ingestion of the same fingerprint re-triggers a
lookup so we eventually catch device-class refinements as fingerbank
improves their corpus.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import MACADDR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DHCPFingerprint(TimestampMixin, Base):
    """One row per device MAC, carrying the raw DHCP signature + fingerbank result."""

    __tablename__ = "dhcp_fingerprint"
    __table_args__ = (Index("ix_dhcp_fingerprint_last_seen_at", "last_seen_at"),)

    # MAC is the natural key — fingerbank's input space is the (option-55,
    # option-60, mac, user_class) tuple, but in practice option-55 is
    # device-stable and we want one cached lookup per device, not per
    # transaction. PG MACADDR canonicalises ``aa:bb:cc:dd:ee:ff`` so
    # matching against IPAddress.mac_address is a straight equality join.
    mac_address: Mapped[str] = mapped_column(MACADDR, primary_key=True)

    # Raw DHCP options observed in the most recent DISCOVER / REQUEST.
    # ``option_55`` is the comma-separated decimal byte list (e.g.
    # ``"1,3,6,15,31,33,43,44,46,47,119,121,249,252"``) — that's the
    # canonical fingerbank wire format too, so we can pass it straight
    # through. ``option_60`` is the vendor class identifier as a UTF-8
    # string (decoded with ``replace`` to handle the non-ASCII vendor
    # blobs some IoT devices ship). ``option_77`` is user class (rare
    # but useful for Windows machines + iPXE). ``client_id`` is hex-
    # encoded so the JSON transit doesn't have to deal with arbitrary
    # bytes.
    option_55: Mapped[str | None] = mapped_column(Text, nullable=True)
    option_60: Mapped[str | None] = mapped_column(Text, nullable=True)
    option_77: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Fingerbank lookup result. ``device_id`` deep-links into the
    # fingerbank UI (https://api.fingerbank.org/devices/{id}) for
    # operators who want the full taxonomy. ``score`` is fingerbank's
    # 0-100 confidence value; we surface it in the IP detail modal so
    # operators can tell a confident "iOS device, score 95" from a
    # speculative "Generic Linux, score 30".
    fingerbank_device_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fingerbank_device_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fingerbank_device_class: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fingerbank_manufacturer: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fingerbank_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fingerbank_last_lookup_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when the most recent lookup hit a network / API error so the
    # IP detail modal can surface "fingerbank unreachable" rather than
    # "unknown device". Cleared on a successful lookup.
    fingerbank_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


__all__ = ["DHCPFingerprint"]
