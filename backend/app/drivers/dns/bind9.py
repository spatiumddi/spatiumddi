"""BIND9 DNS driver.

Renders ``named.conf``, zone files, and RPZ zone files from a neutral
``ConfigBundle``. Applies single-record changes via RFC 2136 ``nsupdate``
(using ``dnspython``) signed with TSIG keys — intended to be invoked by the
DNS agent running next to ``named`` over loopback.

No call to ``named``'s control channel (``rndc``) happens from the control
plane — per ``docs/deployment/DNS_AGENT.md`` §3 ``rndc`` is agent-local only.
The ``reload_config``/``reload_zone`` methods here are surface for the agent.
"""

from __future__ import annotations

import base64
import dataclasses
from pathlib import Path
from typing import Any

import structlog
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from app.core.dns_names import strip_control_chars
from app.drivers.dns.base import (
    ConfigBundle,
    DNSDriver,
    DynamicUpdateCaps,
    EffectiveBlocklistData,
    RecordChange,
    RecordData,
    ServerOptions,
    ZoneData,
)

logger = structlog.get_logger(__name__)

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "bind9"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(enabled_extensions=(), default=False),
        keep_trailing_newline=True,
        undefined=StrictUndefined,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def _quote_txt(value: str) -> str:
    """Quote a TXT record value per RFC 1035 (split long strings into chunks ≤255)."""
    # Strip any caller-supplied outer quotes.
    s = value
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        s = s[1:-1]
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    # Chunk into 255-byte pieces
    chunks = [s[i : i + 255] for i in range(0, len(s), 255)] or [""]
    return " ".join(f'"{c}"' for c in chunks)


def _render_record(zone: ZoneData, r: RecordData) -> str:
    """Render a single RR line in RFC 1035 format."""
    name = "@" if r.name in ("@", "", zone.name.rstrip(".")) else r.name
    ttl = f"{r.ttl} " if r.ttl is not None else ""
    rtype = r.record_type.upper()
    # Render-boundary neutralizer (issue #597): strip control characters /
    # newlines from the owner + rdata so a value that slipped past the field
    # validators (importer, legacy row) can't inject a second record line.
    # Spaces / quotes survive, so structured rdata renders intact.
    name = strip_control_chars(name)
    value = strip_control_chars(r.value)

    if rtype == "TXT":
        rdata = _quote_txt(value)
    elif rtype == "MX":
        prio = r.priority if r.priority is not None else 10
        rdata = f"{prio} {value}"
    elif rtype == "SRV":
        prio = r.priority if r.priority is not None else 0
        weight = r.weight if r.weight is not None else 0
        port = r.port if r.port is not None else 0
        rdata = f"{prio} {weight} {port} {value}"
    else:
        rdata = value

    return f"{name} {ttl}IN {rtype} {rdata}"


_UPDATE_POLICY_NAMED_SCOPES = frozenset({"subdomain", "name", "wildcard", "self"})


def _render_update_clause(zone: ZoneData) -> str:
    """Coarse ``allow-update`` or fine ``update-policy`` clause for the
    preview / agentless render path (issue #641). Operator entries only —
    the agent adds its own loopback grant. Returns "" when nothing to render.

    Mirrors the agent-side renderer (``agent/.../drivers/bind9.py``); the two
    live in separate packages so the logic is intentionally duplicated.
    """
    if not zone.dynamic_update_enabled or not zone.update_acl:
        return ""
    needs_policy = any(
        e.action == "deny" or e.name_scope or e.name_pattern or e.record_types
        for e in zone.update_acl
    )
    if needs_policy:
        lines: list[str] = []
        for e in zone.update_acl:
            if e.match_kind != "tsig_key" or not e.tsig_key_name:
                continue  # update-policy is TSIG-identity only
            action = "deny" if e.action == "deny" else "grant"
            scope = e.name_scope or "zonesub"
            types = " ".join(str(t) for t in (e.record_types or ()) if t)
            suffix = f" {types}" if types else ""
            if scope in _UPDATE_POLICY_NAMED_SCOPES:
                name = (e.name_pattern or "").strip()
                if not name:
                    continue
                lines.append(f"{action} {e.tsig_key_name} {scope} {name}{suffix};")
            else:
                lines.append(f"{action} {e.tsig_key_name} zonesub{suffix};")
        return f"update-policy {{ {' '.join(lines)} }};" if lines else ""
    # Coarse allow-update — grant-only IP + TSIG.
    items: list[str] = []
    for e in zone.update_acl:
        if e.action != "grant":
            continue
        if e.match_kind == "ip" and e.ip_cidr:
            items.append(f"{e.ip_cidr};")
        elif e.match_kind == "tsig_key" and e.tsig_key_name:
            items.append(f'key "{e.tsig_key_name}";')
    return f"allow-update {{ {' '.join(items)} }};" if items else ""


class BIND9Driver(DNSDriver):
    name = "bind9"

    # ── Rendering ─────────────────────────────────────────────────────────

    def render_server_config(
        self,
        server: Any,
        options: ServerOptions,
        *,
        bundle: ConfigBundle | None = None,
    ) -> str:
        env = _env()
        tmpl = env.get_template("named.conf.j2")
        if bundle is None:
            zones: tuple[ZoneData, ...] = ()
            views: tuple[Any, ...] = ()
            acls: tuple[Any, ...] = ()
            tsig_keys: tuple[Any, ...] = ()
            blocklists: tuple[EffectiveBlocklistData, ...] = ()
            dnssec_policies: tuple[Any, ...] = ()
            server_id = getattr(server, "id", "")
            server_name = getattr(server, "name", "")
            generated_at = ""
        else:
            zones = bundle.zones
            views = bundle.views
            acls = bundle.acls
            tsig_keys = bundle.tsig_keys
            blocklists = bundle.blocklists
            dnssec_policies = bundle.dnssec_policies
            server_id = bundle.server_id
            server_name = bundle.server_name
            generated_at = bundle.generated_at.isoformat() if bundle.generated_at else ""

        # Key by (view_name, name): under split-horizon (issue #24) the same
        # zone name appears once per view, so a name-only key would collapse
        # the copies and drop all but one view's stanza from the preview.
        zone_stanzas = {(z.view_name, z.name): self.render_zone_config(z) for z in zones}

        return tmpl.render(
            server_id=str(server_id),
            server_name=server_name,
            generated_at=generated_at,
            options=options,
            acls=acls,
            views=views,
            zones=zones,
            tsig_keys=tsig_keys,
            blocklists=blocklists,
            dnssec_policies=dnssec_policies,
            zone_stanzas=zone_stanzas,
        )

    def render_zone_config(self, zone: ZoneData) -> str:
        env = _env()
        tmpl = env.get_template("zone.stanza.j2")
        return tmpl.render(zone=zone, update_clause=_render_update_clause(zone))

    def render_zone_file(self, zone: ZoneData, records: list[RecordData]) -> str:
        env = _env()
        tmpl = env.get_template("zone.file.j2")
        # Neutralize control chars in the SOA fields (issue #597 review): the
        # template interpolates ``primary_ns`` + ``admin_email`` raw into the
        # SOA line, so a newline in either would inject a record the same way
        # an unescaped owner/rdata would. ``_render_record`` already guards the
        # record lines; this closes the SOA line one altitude up.
        zone = dataclasses.replace(
            zone,
            primary_ns=strip_control_chars(zone.primary_ns),
            admin_email=strip_control_chars(zone.admin_email),
        )
        return tmpl.render(
            zone=zone,
            records=records,
            render_record=_render_record,
        )

    def render_rpz_zone(self, blocklist: EffectiveBlocklistData) -> str:
        env = _env()
        tmpl = env.get_template("rpz.zone.j2")

        rendered: list[str] = []
        excluded = {d.lower() for d in blocklist.exceptions}
        for e in blocklist.entries:
            # Feed-sourced domains are untrusted; neutralize control chars
            # so a malformed feed entry can't inject a zone-file line (#597).
            dom = strip_control_chars(e.domain.lower().rstrip("."))
            if dom in excluded:
                continue
            rpz_name = f"*.{dom}" if e.is_wildcard else dom
            if e.action == "redirect" and e.target:
                rendered.append(f"{rpz_name} IN A {e.target}")
            elif e.block_mode == "sinkhole" and e.sinkhole_ip:
                rendered.append(f"{rpz_name} IN A {e.sinkhole_ip}")
            elif e.block_mode == "refused":
                rendered.append(f"{rpz_name} IN CNAME rpz-drop.")
            else:  # nxdomain (default)
                rendered.append(f"{rpz_name} IN CNAME .")

        # Fixed SOA serial derived from blocklist size keeps this deterministic.
        serial = max(1, len(blocklist.entries))
        return tmpl.render(
            blocklist=blocklist,
            rendered_entries=rendered,
            serial=serial,
        )

    # ── Record application (RFC 2136) ─────────────────────────────────────

    async def apply_record_change(self, server: Any, change: RecordChange) -> None:
        """Build and send a TSIG-signed RFC 2136 update.

        This is invoked *inside the agent container* against loopback
        ``named``. The control-plane code path only formulates
        ``RecordChange`` objects; calling this method from the control plane
        requires explicit network-path configuration that the design defers
        to the agent. If the caller has no TSIG key configured, the method
        raises ``RuntimeError`` — it never sends an unsigned update.
        """
        import dns.inet
        import dns.message
        import dns.name
        import dns.query
        import dns.rdatatype
        import dns.tsig
        import dns.tsigkeyring
        import dns.update

        # TSIG is not modelled on DNSServer (no tsig_key_* columns — the getattr
        # shims here were always None; #483). Only change.tsig_key_name is
        # carried on the ChangeSet today; a secret/algorithm source for this
        # agentless RFC2136 path was never wired, so it raises below. Agent-
        # rendered BIND9 uses group-level TSIG on DNSServerGroup via agent_config.
        tsig_name = change.tsig_key_name
        tsig_secret: str | None = None
        tsig_algorithm = "hmac-sha256"
        if not tsig_name or not tsig_secret:
            raise RuntimeError("BIND9Driver.apply_record_change requires a TSIG key on the server")

        keyring = dns.tsigkeyring.from_text({tsig_name: tsig_secret})
        algo = dns.name.from_text(tsig_algorithm)
        update = dns.update.Update(change.zone_name, keyring=keyring, keyalgorithm=algo)

        rr = change.record
        rel_name = "@" if rr.name in ("", "@") else rr.name
        rdtype = dns.rdatatype.from_text(rr.record_type)

        if change.op == "delete":
            update.delete(rel_name, rdtype)
        else:  # create | update
            if change.op == "update":
                update.delete(rel_name, rdtype)
            rdata = rr.value
            if rr.record_type.upper() == "MX":
                rdata = f"{rr.priority or 10} {rr.value}"
            elif rr.record_type.upper() == "SRV":
                rdata = f"{rr.priority or 0} {rr.weight or 0} {rr.port or 0} {rr.value}"
            elif rr.record_type.upper() == "TXT":
                rdata = _quote_txt(rr.value)
            update.add(rel_name, rr.ttl or 3600, rr.record_type, rdata)

        # Atomic SOA bump within the same update.
        update.delete("@", dns.rdatatype.SOA)

        host = getattr(server, "host", "127.0.0.1")
        port = getattr(server, "api_port", None) or 53
        logger.info(
            "bind9.apply_record_change",
            server=str(getattr(server, "id", "")),
            zone=change.zone_name,
            op=change.op,
            name=rr.name,
            type=rr.record_type,
        )
        # dnspython.query.tcp is blocking; callers in the agent run this in a
        # thread executor. The control plane never reaches this code path.
        import asyncio

        await asyncio.to_thread(dns.query.tcp, update, host, port=port, timeout=10)

    async def reload_config(self, server: Any) -> None:
        """Surface for the agent's ``rndc reconfig``. Control plane no-op."""
        logger.info("bind9.reload_config", server=str(getattr(server, "id", "")))

    async def reload_zone(self, server: Any, zone_name: str) -> None:
        logger.info("bind9.reload_zone", server=str(getattr(server, "id", "")), zone=zone_name)

    async def pull_zone_records(self, server: Any, zone_name: str) -> list[RecordData]:
        """AXFR the zone from the BIND9 host.

        The server must have ``allow-transfer`` permitting the SpatiumDDI
        control plane — configured via the zone's ACL or a global
        ``allow-transfer`` in ``named.conf`` options. If transfers are
        denied the AXFR raises and the caller surfaces the error.
        """
        from app.drivers.dns._axfr import axfr_zone_records  # noqa: PLC0415

        host = getattr(server, "host", None)
        if not host:
            raise RuntimeError("BIND9Driver.pull_zone_records: server.host is required")
        port = getattr(server, "port", None) or 53
        return await axfr_zone_records(
            host=host,
            port=port,
            zone_name=zone_name,
            log_driver="bind9",
            server_id=str(getattr(server, "id", "")),
        )

    # ── Validation / capabilities ─────────────────────────────────────────

    def validate_config(self, bundle: ConfigBundle) -> tuple[bool, list[str]]:
        errors: list[str] = []
        seen: set[tuple[str | None, str]] = set()
        for z in bundle.zones:
            if not z.name.endswith("."):
                errors.append(f"zone {z.name!r}: name must end with '.'")
            key = (z.view_name, z.name)
            if key in seen:
                errors.append(f"duplicate zone {z.name!r} in view {z.view_name!r}")
            seen.add(key)
            if z.zone_type not in ("primary", "secondary", "stub", "forward"):
                errors.append(f"zone {z.name!r}: invalid zone_type {z.zone_type!r}")
        for k in bundle.tsig_keys:
            try:
                base64.b64decode(k.secret, validate=True)
            except Exception:
                errors.append(f"tsig key {k.name!r}: secret is not valid base64")
        return (not errors, errors)

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": "bind9",
            "views": True,
            "rpz": True,
            "dnssec_inline_signing": True,
            "incremental_updates": "rfc2136",
            "zone_types": ["primary", "secondary", "stub", "forward"],
            "record_types": [
                "A",
                "AAAA",
                "CNAME",
                "MX",
                "TXT",
                "NS",
                "PTR",
                "SRV",
                "CAA",
                "TLSA",
                "SSHFP",
                "NAPTR",
                "LOC",
                "SVCB",
                "HTTPS",
                "DNAME",
            ],
        }

    @property
    def dynamic_update_caps(self) -> DynamicUpdateCaps:
        # BIND9 expresses the full RFC 2136 ACL surface (issue #641):
        #   * coarse ``allow-update`` mixing IP + TSIG (P1), and
        #   * fine-grained ``update-policy`` — per-name / subdomain grants +
        #     per-type restriction + ``deny`` (P2, TSIG-identity only).
        # The renderer picks the clause per zone by what the ACL needs.
        return DynamicUpdateCaps(
            supports_ip_acl=True,
            supports_tsig_acl=True,
            supports_name_scoping=True,
            supports_per_type=True,
        )


__all__ = ["BIND9Driver"]
