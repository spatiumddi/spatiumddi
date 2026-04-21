"""IEEE OUI vendor lookup — one row per assigned 24-bit prefix.

Loaded from ``https://standards-oui.ieee.org/oui/oui.csv`` by the
``app.tasks.oui_update`` Celery task when the operator opts in via
``PlatformSettings.oui_lookup_enabled``. Each daily run replaces the
table atomically (truncate + bulk insert inside a single transaction)
so lookups always see a consistent snapshot.

``prefix`` stores the first three MAC octets as six lowercase hex
characters — e.g. ``001122`` for ``00:11:22:…``. This matches what
``_normalize_mac`` already produces in the IPAM router, so the lookup
is a single indexed equality join:

    prefix = normalize(mac)[:6]
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OUIVendor(Base):
    __tablename__ = "oui_vendor"

    prefix: Mapped[str] = mapped_column(String(6), primary_key=True)
    vendor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
