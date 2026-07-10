"""Read-only conformance report for existing DNS-shaped names (issue #597).

The #597 validators reject non-conforming names on *write*, but rows that
predate them stay in the database untouched (the deliberate "validate +
report, never auto-mutate" stance). This module scans the existing
IPAM hostnames, DNS record owners, DNS zone names, and DHCP static
reservation hostnames and reports which ones would now be rejected, so an
operator can fix them deliberately. It never mutates anything.

Each category caps the number of returned examples (the full offending
set on a large install could be huge) but reports the true total count so
the operator knows the real scale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dns_names import (
    validate_fqdn,
    validate_hostname,
    validate_record_owner,
)
from app.models.dhcp import DHCPStaticAssignment
from app.models.dns import DNSRecord, DNSZone
from app.models.ipam import IPAddress

# Cap the examples returned per category — the count is always exact, only
# the ``examples`` list is truncated.
_EXAMPLES_PER_CATEGORY = 100
# Hard ceiling on rows scanned per category so the report can't run away on
# a multi-million-row install; ``scanned_capped`` flags when it bit.
_MAX_SCAN_PER_CATEGORY = 200_000


@dataclass
class _CategoryReport:
    category: str
    total: int = 0
    scanned_capped: bool = False
    examples: list[dict[str, Any]] = field(default_factory=list)

    def add(self, *, row_id: Any, value: str, reason: str, context: str | None = None) -> None:
        self.total += 1
        if len(self.examples) < _EXAMPLES_PER_CATEGORY:
            ex: dict[str, Any] = {"id": str(row_id), "value": value, "reason": reason}
            if context is not None:
                ex["context"] = context
            self.examples.append(ex)

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "total": self.total,
            "scanned_capped": self.scanned_capped,
            "examples": self.examples,
        }


async def scan_name_conformance(db: AsyncSession) -> dict[str, Any]:
    """Scan existing rows for names the #597 validators would now reject.

    Returns a dict with one entry per category plus a ``total`` rollup.
    Read-only — issues plain SELECTs and validates in Python.
    """
    ipam = _CategoryReport("ipam_hostname")
    rec = _CategoryReport("dns_record_name")
    zone = _CategoryReport("dns_zone_name")
    static = _CategoryReport("dhcp_static_hostname")

    # IPAM hostnames (host rule).
    n = 0
    for row_id, value in (
        await db.execute(
            select(IPAddress.id, IPAddress.hostname)
            .where(IPAddress.hostname.isnot(None), IPAddress.hostname != "")
            .limit(_MAX_SCAN_PER_CATEGORY)
        )
    ).all():
        n += 1
        try:
            validate_hostname(value)
        except ValueError as exc:
            ipam.add(row_id=row_id, value=value, reason=str(exc))
    ipam.scanned_capped = n >= _MAX_SCAN_PER_CATEGORY

    # DNS record owners (RFC 2181 rule).
    n = 0
    for row_id, value, ctx in (
        await db.execute(
            select(DNSRecord.id, DNSRecord.name, DNSRecord.fqdn).limit(_MAX_SCAN_PER_CATEGORY)
        )
    ).all():
        n += 1
        try:
            validate_record_owner(value or "@")
        except ValueError as exc:
            rec.add(row_id=row_id, value=value, reason=str(exc), context=ctx)
    rec.scanned_capped = n >= _MAX_SCAN_PER_CATEGORY

    # DNS zone names (FQDN rule). Strip the stored trailing dot first.
    n = 0
    for row_id, value in (
        await db.execute(select(DNSZone.id, DNSZone.name).limit(_MAX_SCAN_PER_CATEGORY))
    ).all():
        n += 1
        try:
            validate_fqdn((value or "").rstrip("."), field="zone name")
        except ValueError as exc:
            zone.add(row_id=row_id, value=value, reason=str(exc))
    zone.scanned_capped = n >= _MAX_SCAN_PER_CATEGORY

    # DHCP static reservation hostnames (host rule; empty is allowed here).
    n = 0
    for row_id, value in (
        await db.execute(
            select(DHCPStaticAssignment.id, DHCPStaticAssignment.hostname)
            .where(DHCPStaticAssignment.hostname.isnot(None), DHCPStaticAssignment.hostname != "")
            .limit(_MAX_SCAN_PER_CATEGORY)
        )
    ).all():
        n += 1
        try:
            validate_hostname(value)
        except ValueError as exc:
            static.add(row_id=row_id, value=value, reason=str(exc))
    static.scanned_capped = n >= _MAX_SCAN_PER_CATEGORY

    categories = [ipam, rec, zone, static]
    return {
        "total_nonconforming": sum(c.total for c in categories),
        "examples_per_category": _EXAMPLES_PER_CATEGORY,
        "categories": [c.as_dict() for c in categories],
    }
