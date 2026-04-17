"""Windows DNS driver (RFC 2136 dynamic updates, agentless).

Talks straight to a Windows Server DNS instance using RFC 2136 dynamic
updates via ``dnspython``. Unlike the BIND9 driver, there is no on-host
agent: the control plane sends the update itself. This is possible because
Windows DNS Server natively speaks RFC 2136 and does not require an
application-specific admin API for record CRUD.

Scope (Path A, first iteration):
  * Record CRUD — A / AAAA / CNAME / MX / TXT / PTR / SRV / NS
  * Optional TSIG (HMAC) signing for lab setups that front Windows DNS
    with a separate stub/forwarder that supports TSIG. Default is
    unsigned.
  * Zone creation, views, ACLs, RPZ, server config — **not supported**.
    The Windows DNS Server admin is expected to create zones (AD-integrated
    or standalone primary) in DNS Manager and grant dynamic updates.

Not yet (Path B, future work):
  * GSS-TSIG (Kerberos-signed updates) — Windows' default for AD-integrated
    zones' "Secure only" setting.
  * SIG(0) authentication.
  * Zone CRUD / server-level config (requires PowerShell remoting — see
    docs/drivers/DNS_DRIVERS.md §Windows).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from app.drivers.dns.base import (
    ConfigBundle,
    DNSDriver,
    EffectiveBlocklistData,
    RecordChange,
    RecordData,
    ServerOptions,
    ZoneData,
)

logger = structlog.get_logger(__name__)


# Record types this driver knows how to format for an RFC 2136 update.
_SUPPORTED_RECORD_TYPES = (
    "A",
    "AAAA",
    "CNAME",
    "MX",
    "TXT",
    "PTR",
    "SRV",
    "NS",
    "TLSA",
)


def _format_rdata(r: RecordData) -> str:
    """Render ``RecordData`` into the wire-format string dnspython expects."""
    rtype = r.record_type.upper()
    if rtype == "MX":
        return f"{r.priority or 10} {r.value}"
    if rtype == "SRV":
        return f"{r.priority or 0} {r.weight or 0} {r.port or 0} {r.value}"
    if rtype == "TXT":
        s = r.value
        if s.startswith('"') and s.endswith('"') and len(s) >= 2:
            s = s[1:-1]
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        chunks = [s[i : i + 255] for i in range(0, len(s), 255)] or [""]
        return " ".join(f'"{c}"' for c in chunks)
    return r.value


class WindowsDNSDriver(DNSDriver):
    """Agentless RFC 2136 driver for Windows Server DNS.

    The rendering methods return empty strings: SpatiumDDI does not write
    ``named.conf``-style config for Windows DNS. Only ``apply_record_change``
    does real work. ``reload_*`` are no-ops — AD replication handles zone
    propagation across DCs.
    """

    name: str = "windows_dns"

    # ── Rendering (all no-ops; Windows manages its own zones) ─────────────

    def render_server_config(
        self, server: Any, options: ServerOptions, *, bundle: ConfigBundle | None = None
    ) -> str:
        return ""

    def render_zone_config(self, zone: ZoneData) -> str:
        return ""

    def render_zone_file(self, zone: ZoneData, records: list[RecordData]) -> str:
        return ""

    def render_rpz_zone(self, blocklist: EffectiveBlocklistData) -> str:
        return ""

    # ── Runtime — this is the only method that talks over the wire ──────

    async def apply_record_change(self, server: Any, change: RecordChange) -> None:
        """Send an RFC 2136 update to the Windows DC.

        The target zone must have **Nonsecure and secure** dynamic updates
        enabled in DNS Manager (or be a non-AD-integrated primary). With
        "Secure only" the update will succeed from the wire but Windows
        will silently drop the change.

        Optional TSIG signing: if the server row carries
        ``tsig_key_name`` + ``tsig_key_secret``, the update is signed.
        Otherwise an unsigned update is sent — fine for a lab, not for
        prod.
        """
        import dns.message
        import dns.name
        import dns.query
        import dns.rdatatype
        import dns.tsigkeyring
        import dns.update

        host = getattr(server, "host", None)
        if not host:
            raise RuntimeError("WindowsDNSDriver: server.host is required")
        port = getattr(server, "api_port", None) or 53

        rtype = change.record.record_type.upper()
        if rtype not in _SUPPORTED_RECORD_TYPES:
            raise ValueError(
                f"WindowsDNSDriver does not support record type {rtype!r}; "
                f"supported: {_SUPPORTED_RECORD_TYPES}"
            )

        tsig_name = change.tsig_key_name or getattr(server, "tsig_key_name", None)
        tsig_secret = getattr(server, "tsig_key_secret", None)
        tsig_algorithm = getattr(server, "tsig_key_algorithm", "hmac-sha256")
        if tsig_name and tsig_secret:
            keyring = dns.tsigkeyring.from_text({tsig_name: tsig_secret})
            update = dns.update.Update(
                change.zone_name,
                keyring=keyring,
                keyalgorithm=dns.name.from_text(tsig_algorithm),
            )
        else:
            keyring = None
            update = dns.update.Update(change.zone_name)

        rr = change.record
        rel_name = "@" if rr.name in ("", "@") else rr.name
        rdtype = dns.rdatatype.from_text(rtype)

        if change.op == "delete":
            update.delete(rel_name, rdtype)
        else:  # create | update
            if change.op == "update":
                update.delete(rel_name, rdtype)
            update.add(rel_name, rr.ttl or 3600, rtype, _format_rdata(rr))

        # Windows DNS manages SOA serials internally for AD-integrated zones
        # via directory replication; an explicit SOA bump from the client is
        # unnecessary and can actually be refused. Skip the BIND9-style bump.

        logger.info(
            "windows_dns.apply_record_change",
            server=str(getattr(server, "id", "")),
            host=host,
            port=port,
            zone=change.zone_name,
            op=change.op,
            name=rr.name,
            type=rtype,
            signed=keyring is not None,
        )

        # dnspython.query.tcp is blocking — run in a thread so we don't pin
        # the event loop. 10 s timeout matches the BIND9 driver.
        await asyncio.to_thread(dns.query.tcp, update, host, port=port, timeout=10)

    async def reload_config(self, server: Any) -> None:
        # Windows handles its own config lifecycle; nothing to do remotely.
        return

    async def reload_zone(self, server: Any, zone_name: str) -> None:
        return

    async def pull_zone_records(self, server: Any, zone_name: str) -> list[RecordData]:
        """AXFR the zone from the Windows DC and return its records.

        Windows DNS responds to AXFR when the target zone has **Zone
        Transfers → Allow zone transfers** enabled with the SpatiumDDI
        host in the permitted list (or "To any server", which is lab-only).

        Apex SOA and NS records are filtered out of the return value so
        they don't collide with SpatiumDDI's zone-level SOA/NS handling.
        The caller uses this output for "pull from server" diff + import.
        """
        import dns.name
        import dns.query
        import dns.rdatatype
        import dns.zone

        host = getattr(server, "host", None)
        if not host:
            raise RuntimeError("WindowsDNSDriver.pull_zone_records: server.host is required")
        port = getattr(server, "api_port", None) or 53

        zone_origin = dns.name.from_text(zone_name)

        def _axfr() -> dns.zone.Zone:
            return dns.zone.from_xfr(dns.query.xfr(host, zone_origin, port=port, timeout=20))

        z = await asyncio.to_thread(_axfr)

        def _absolutize(target: Any) -> str:
            """Return a DNS name as its absolute form with trailing dot.

            dnspython's ``from_xfr`` relativizes names and, for in-zone
            targets (CNAME / MX exchange / SRV target / NS / PTR), the
            rdata target is relative. If we ``to_text()`` it directly we
            get the bare label ("aaaaaaaaaaaa") while the IPAM push path
            stores the FQDN ("aaaaaaaaaaaa.windows.lab.local."). The two
            representations are equivalent on the wire but create spurious
            duplicates when the pull importer dedups by raw string match.
            Always emit absolute form so everything downstream sees one
            representation.
            """
            if target.is_absolute():
                return target.to_text()
            return target.derelativize(zone_origin).to_text()

        out: list[RecordData] = []
        for name, node in z.items():
            rel = name.relativize(zone_origin)
            # Out-of-zone glue records (e.g. an A for the NS target when the
            # NS points at a host in a different zone) come through in AXFR
            # but `relativize` leaves them absolute. Skip them — they belong
            # to their parent zone, not this one.
            if rel.is_absolute():
                logger.debug(
                    "windows_dns.skipping_out_of_zone_record",
                    zone=zone_name,
                    name=name.to_text(),
                )
                continue
            rel_label = "@" if rel == dns.name.empty else rel.to_text()
            for rdataset in node.rdatasets:
                rtype = dns.rdatatype.to_text(rdataset.rdtype)
                # Skip zone-level metadata we don't want to import as
                # user-editable records — SOA is managed by the server,
                # apex NS is implicit in the zone config.
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
                        # dnspython returns TXT as quoted chunks; join the
                        # bytes segments into a single user-visible string.
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
            "windows_dns.pull_zone_records",
            server=str(getattr(server, "id", "")),
            host=host,
            zone=zone_name,
            count=len(out),
        )
        return out

    # ── Validation / capabilities ────────────────────────────────────────

    def validate_config(self, bundle: ConfigBundle) -> tuple[bool, list[str]]:
        # We don't render config for Windows; the bundle is informational
        # only. Accept anything so upstream validators don't block.
        return (True, [])

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": "windows_dns",
            "agentless": True,
            "manages_zones": False,
            "views": False,
            "rpz": False,
            "dnssec_inline_signing": False,
            "incremental_updates": "rfc2136",
            "tsig": "optional",
            "zone_types": ["primary (external)", "secondary (external)"],
            "record_types": list(_SUPPORTED_RECORD_TYPES),
            "notes": (
                "Agentless driver for Windows Server DNS. Record CRUD only; "
                "zones must be pre-created in Windows DNS Manager with "
                "'Nonsecure and secure' dynamic updates enabled."
            ),
        }
