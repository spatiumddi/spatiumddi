"""Shared AXFR helper used by BIND9 + Windows DNS drivers.

Both drivers implement ``pull_zone_records`` by doing a standard AXFR over
TCP/53 and walking the returned zone. The only thing that differs is the
driver name in log lines — the wire protocol and the rdata shaping are
identical. Keep this in one place so they can't drift.

Also home to :func:`resolve_server_address`, shared with the RFC 2136 write
paths — see its docstring for why every ``dnspython`` call site needs it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any

import structlog

from app.drivers.dns.base import RecordData

logger = structlog.get_logger(__name__)


def resolve_server_address(host: str, port: int, *, what: str) -> list[str]:
    """Return IP literals for ``host``, in the order to try them.

    Every low-level ``dnspython`` entry point we use — ``dns.query.xfr`` for
    transfers, ``dns.query.tcp`` for RFC 2136 updates — takes an IP *literal*,
    not a name. Each calls ``dns.inet.af_for_address(where)`` up front to pick
    the address family, and that raises a **bare, message-less**
    ``ValueError()`` for anything that doesn't parse as an address, before a
    single packet leaves the box.

    So any ``DNSServer.host`` holding a hostname — which is every
    containerised deployment and most others — failed 100% of the time, and
    the reason surfaced to the operator was the empty string. Resolve here and
    hand dnspython an address.

    ``what`` is the operation description used to prefix errors, e.g.
    ``"AXFR of example.com."``.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [host]

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise RuntimeError(
            f"{what} from {host}:{port} failed: could not resolve {host!r} ({exc})."
        ) from exc

    # Preserve getaddrinfo's ordering (RFC 6724 / system policy) but drop
    # duplicates — a dual-stack host commonly returns the same address for
    # both SOCK_STREAM and SOCK_DGRAM entries.
    addrs: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise RuntimeError(f"{what} from {host}:{port} failed: {host!r} resolved to no addresses.")
    return addrs


def _hint(exc: BaseException, port: int) -> str:
    """Return a next-step hint matched to *why* the transfer failed.

    An unconditional "check allow-transfer" is worse than no hint at all when
    the real problem is that the host is unreachable — it sends the operator
    to the DNS config of a box they can't even open a socket to. Only mention
    the transfer ACL when the server actually answered and said no.
    """
    text = str(exc).upper()
    if isinstance(exc, TimeoutError) or "TIMED OUT" in text or "TIMEOUT" in text:
        return (
            f" The server did not answer in time — check that it is running and that "
            f"TCP/{port} is open (a transfer needs TCP, not just UDP)."
        )
    if isinstance(exc, OSError):
        # Connection refused / no route / network unreachable: we never got a
        # DNS-level answer, so the transfer ACL is not what's in the way.
        return f" The server could not be reached on TCP/{port} — check the address and firewall."
    if "REFUSED" in text or "NOTAUTH" in text:
        return (
            " The server answered but declined the transfer — check that its "
            "allow-transfer permits the SpatiumDDI control plane, and that it is "
            "authoritative for this zone."
        )
    return (
        f" Check that the server's allow-transfer permits the SpatiumDDI control "
        f"plane and that TCP/{port} is reachable."
    )


# Signing artefacts the authoritative server generates and owns. Never
# modelled as SpatiumDDI records, so they must not surface as drift.
_DNSSEC_ARTEFACTS = frozenset({"RRSIG", "NSEC", "NSEC3", "NSEC3PARAM", "DNSKEY", "CDS", "CDNSKEY"})


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
    also skipped, as are DNSSEC signing artefacts (RRSIG / NSEC* / DNSKEY /
    CDS / CDNSKEY), which the signing server owns and rotates itself.
    """
    import dns.name  # noqa: PLC0415
    import dns.query  # noqa: PLC0415
    import dns.rdatatype  # noqa: PLC0415
    import dns.zone  # noqa: PLC0415

    zone_origin = dns.name.from_text(zone_name)

    what = f"AXFR of {zone_name}"

    def _axfr() -> dns.zone.Zone:
        # A dual-stack host can resolve to an address that isn't the one
        # permitted by allow-transfer (or isn't routable from here at all).
        # An AXFR is a read with no side effects, so walking the candidate
        # list is safe — try each and report the last failure if none work.
        addrs = resolve_server_address(host, port, what=what)
        last: Exception | None = None
        for addr in addrs:
            try:
                return dns.zone.from_xfr(
                    dns.query.xfr(addr, zone_origin, port=port, timeout=timeout)
                )
            except Exception as exc:  # noqa: PERF203 — retry the next address
                last = exc
        assert last is not None  # resolve_server_address never returns []
        raise last

    try:
        z = await asyncio.to_thread(_axfr)
    except RuntimeError:
        raise  # already carries a specific, operator-readable message
    except Exception as exc:
        # dnspython also raises message-less exceptions for a refused or
        # malformed transfer, so ``str(exc)`` is routinely "". Never let a
        # blank reason reach the operator — drift reports and pull-imports
        # both surface this string verbatim.
        detail = str(exc).strip() or type(exc).__name__
        raise RuntimeError(
            f"{what} from {host}:{port} failed: {detail}.{_hint(exc, port)}"
        ) from exc

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
            if rtype in _DNSSEC_ARTEFACTS:
                # The daemon owns these — it mints them when the zone is
                # signed and rotates them on its own schedule. They exist
                # on the wire but never as SpatiumDDI records, so a drift
                # report would list every signature as "extra on server"
                # and a signed zone could never read as in sync. Affects
                # every AXFR caller, not one driver.
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
