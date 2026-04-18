"""Shared AXFR helper used by BIND9 + Windows DNS drivers.

Both drivers implement ``pull_zone_records`` by doing a standard AXFR over
TCP/53 and walking the returned zone. The only thing that differs is the
driver name in log lines — the wire protocol and the rdata shaping are
identical. Keep this in one place so they can't drift.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from app.drivers.dns.base import RecordData

logger = structlog.get_logger(__name__)


async def axfr_zone_records(
    *,
    host: str,
    port: int,
    zone_name: str,
    timeout: int = 20,
    log_driver: str = "dns",
    server_id: str | None = None,
) -> list[RecordData]:
    """AXFR ``zone_name`` from ``host:port`` and return neutral record dicts.

    Apex SOA and NS are filtered out — SpatiumDDI manages those at the zone
    level, so importing them as user records would create duplicate control
    surfaces. Out-of-zone glue (an NS target living in a different zone) is
    also skipped.
    """
    import dns.name  # noqa: PLC0415
    import dns.query  # noqa: PLC0415
    import dns.rdatatype  # noqa: PLC0415
    import dns.zone  # noqa: PLC0415

    zone_origin = dns.name.from_text(zone_name)

    def _axfr() -> dns.zone.Zone:
        return dns.zone.from_xfr(dns.query.xfr(host, zone_origin, port=port, timeout=timeout))

    z = await asyncio.to_thread(_axfr)

    def _absolutize(target: Any) -> str:
        """Return ``target`` as its absolute form with trailing dot.

        dnspython's ``from_xfr`` relativizes names; for in-zone CNAME / MX
        exchange / SRV target / NS / PTR the rdata target is relative.
        ``to_text()`` on a relative target gives the bare label ("host")
        while the rest of SpatiumDDI stores FQDNs ("host.zone.example.").
        The two are equivalent on the wire but diverge when the pull
        importer dedups by raw string match — always emit absolute form
        so everything downstream sees one representation.
        """
        if target.is_absolute():
            return target.to_text()
        return target.derelativize(zone_origin).to_text()

    out: list[RecordData] = []
    for name, node in z.items():
        rel = name.relativize(zone_origin)
        if rel.is_absolute():
            logger.debug(
                f"{log_driver}.skipping_out_of_zone_record",
                zone=zone_name,
                name=name.to_text(),
            )
            continue
        rel_label = "@" if rel == dns.name.empty else rel.to_text()
        for rdataset in node.rdatasets:
            rtype = dns.rdatatype.to_text(rdataset.rdtype)
            if rtype == "SOA":
                continue
            if rtype == "NS" and rel_label == "@":
                continue
            for rdata in rdataset:
                priority: int | None = None
                weight: int | None = None
                port_field: int | None = None
                if rtype == "CNAME":
                    value = _absolutize(rdata.target)
                elif rtype == "NS":
                    value = _absolutize(rdata.target)
                elif rtype == "PTR":
                    value = _absolutize(rdata.target)
                elif rtype == "MX":
                    priority = rdata.preference
                    value = _absolutize(rdata.exchange)
                elif rtype == "SRV":
                    priority = rdata.priority
                    weight = rdata.weight
                    port_field = rdata.port
                    value = _absolutize(rdata.target)
                elif rtype == "TXT":
                    value = "".join(s.decode("utf-8", errors="replace") for s in rdata.strings)
                else:
                    value = rdata.to_text()
                out.append(
                    RecordData(
                        name=rel_label,
                        record_type=rtype,
                        value=value,
                        ttl=int(rdataset.ttl) if rdataset.ttl else None,
                        priority=priority,
                        weight=weight,
                        port=port_field,
                    )
                )
    logger.info(
        f"{log_driver}.pull_zone_records",
        server=server_id or "",
        host=host,
        zone=zone_name,
        count=len(out),
    )
    return out


__all__ = ["axfr_zone_records"]
