"""Assemble a neutral ``ConfigBundle`` for a DNS server.

The bundle is the canonical, driver-agnostic projection of DB state that the
agent long-poll endpoint returns. Drivers consume it for rendering.

The bundle type is re-exported from ``app.drivers.dns.base`` so both this
module and the driver layer share the exact same definition. See
``docs/deployment/DNS_AGENT.md`` §3 and CLAUDE.md non-negotiables #5 and #10.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.drivers.dns.base import (
    AclData,
    BlocklistEntry,
    ConfigBundle,
    EffectiveBlocklistData,
    RecordData,
    ServerOptions,
    TrustAnchorData,
    TsigKey,
    ViewData,
    ZoneData,
)
from app.models.dns import (
    DNSAcl,
    DNSRecord,
    DNSServer,
    DNSServerOptions,
    DNSView,
    DNSZone,
)
from app.services.dns_blocklist import (
    EffectiveBlocklist,
    build_effective_for_group,
    build_effective_for_view,
)


def _tuple_or_none(v: list | None) -> tuple[str, ...] | None:
    if v is None:
        return None
    return tuple(str(x) for x in v)


def _to_blocklist_data(
    eff: EffectiveBlocklist, rpz_name: str
) -> EffectiveBlocklistData:
    return EffectiveBlocklistData(
        rpz_zone_name=rpz_name,
        entries=tuple(
            BlocklistEntry(
                domain=e.domain,
                action=e.action,
                block_mode=e.block_mode,
                sinkhole_ip=e.sinkhole_ip,
                target=e.target,
                is_wildcard=e.is_wildcard,
            )
            for e in eff.entries
        ),
        exceptions=frozenset(eff.exceptions),
    )


async def build_config_bundle(
    db: AsyncSession, server: DNSServer
) -> ConfigBundle:
    """Build a fully-populated ``ConfigBundle`` for the given server."""
    # Options (group-level)
    opts_row = (
        await db.execute(
            select(DNSServerOptions)
            .where(DNSServerOptions.group_id == server.group_id)
            .options(selectinload(DNSServerOptions.trust_anchors))
        )
    ).scalar_one_or_none()

    if opts_row is None:
        options = ServerOptions()
    else:
        options = ServerOptions(
            forwarders=tuple(opts_row.forwarders or ()),
            forward_policy=opts_row.forward_policy,
            recursion_enabled=opts_row.recursion_enabled,
            allow_recursion=tuple(opts_row.allow_recursion or ("any",)),
            dnssec_validation=opts_row.dnssec_validation,
            notify_enabled=opts_row.notify_enabled,
            also_notify=tuple(opts_row.also_notify or ()),
            allow_notify=tuple(opts_row.allow_notify or ()),
            allow_query=tuple(opts_row.allow_query or ("any",)),
            allow_query_cache=tuple(
                opts_row.allow_query_cache or ("localhost", "localnets")
            ),
            allow_transfer=tuple(opts_row.allow_transfer or ("none",)),
            blackhole=tuple(opts_row.blackhole or ()),
            query_log_enabled=opts_row.query_log_enabled,
            query_log_channel=opts_row.query_log_channel,
            query_log_file=opts_row.query_log_file,
            query_log_severity=opts_row.query_log_severity,
            query_log_print_category=opts_row.query_log_print_category,
            query_log_print_severity=opts_row.query_log_print_severity,
            query_log_print_time=opts_row.query_log_print_time,
            trust_anchors=tuple(
                TrustAnchorData(
                    zone_name=ta.zone_name,
                    algorithm=ta.algorithm,
                    key_tag=ta.key_tag,
                    public_key=ta.public_key,
                    is_initial_key=ta.is_initial_key,
                )
                for ta in (opts_row.trust_anchors or ())
            ),
        )

    # ACLs
    acls_rows = (
        await db.execute(
            select(DNSAcl)
            .where(DNSAcl.group_id == server.group_id)
            .options(selectinload(DNSAcl.entries))
        )
    ).scalars().all()
    acls = tuple(
        AclData(
            name=a.name,
            entries=tuple(
                (e.value, bool(e.negate))
                for e in sorted(a.entries, key=lambda x: x.order)
            ),
        )
        for a in acls_rows
    )

    # Views
    view_rows = (
        await db.execute(select(DNSView).where(DNSView.group_id == server.group_id))
    ).scalars().all()
    views = tuple(
        ViewData(
            name=v.name,
            match_clients=tuple(v.match_clients or ()),
            match_destinations=tuple(v.match_destinations or ()),
            recursion=v.recursion,
            order=v.order,
        )
        for v in view_rows
    )
    view_by_id: dict[uuid.UUID, DNSView] = {v.id: v for v in view_rows}

    # Zones + records
    zone_rows = (
        await db.execute(
            select(DNSZone)
            .where(DNSZone.group_id == server.group_id)
            .options(selectinload(DNSZone.records))
        )
    ).scalars().all()

    zones: list[ZoneData] = []
    for z in zone_rows:
        records = tuple(
            RecordData(
                name=r.name,
                record_type=r.record_type,
                value=r.value,
                ttl=r.ttl,
                priority=r.priority,
                weight=r.weight,
                port=r.port,
            )
            for r in z.records
        )
        view_name = view_by_id[z.view_id].name if z.view_id in view_by_id else None
        zones.append(
            ZoneData(
                name=z.name,
                zone_type=z.zone_type,
                kind=z.kind,
                ttl=z.ttl,
                refresh=z.refresh,
                retry=z.retry,
                expire=z.expire,
                minimum=z.minimum,
                primary_ns=z.primary_ns or "",
                admin_email=z.admin_email or "",
                serial=z.last_serial or 0,
                records=records,
                allow_query=_tuple_or_none(z.allow_query),
                allow_transfer=_tuple_or_none(z.allow_transfer),
                also_notify=_tuple_or_none(z.also_notify),
                notify_enabled=z.notify_enabled,
                view_name=view_name,
            )
        )

    # Blocklists: one RPZ zone per view if views present, plus group-level.
    blocklists: list[EffectiveBlocklistData] = []
    if view_rows:
        for v in view_rows:
            eff = await build_effective_for_view(db, v.id)
            if eff.entries:
                blocklists.append(
                    _to_blocklist_data(
                        eff, f"spatium-blocklist-{v.name}.rpz."
                    )
                )
    else:
        eff = await build_effective_for_group(db, server.group_id)
        if eff.entries:
            blocklists.append(
                _to_blocklist_data(eff, "spatium-blocklist.rpz.")
            )

    # TSIG keys: optional; not yet modelled on DNSServer. Drivers handle empty.
    tsig_keys: tuple[TsigKey, ...] = ()
    tsig_name = getattr(server, "tsig_key_name", None)
    tsig_secret = getattr(server, "tsig_key_secret", None)
    tsig_algo = getattr(server, "tsig_key_algorithm", None) or "hmac-sha256"
    if tsig_name and tsig_secret:
        tsig_keys = (TsigKey(name=tsig_name, algorithm=tsig_algo, secret=tsig_secret),)

    bundle = ConfigBundle(
        server_id=str(server.id),
        server_name=server.name,
        driver=server.driver,
        roles=tuple(server.roles or ()),
        options=options,
        acls=acls,
        views=views,
        zones=tuple(zones),
        tsig_keys=tsig_keys,
        blocklists=tuple(blocklists),
        generated_at=datetime.now(UTC),
    )
    bundle.etag = bundle.compute_etag()
    return bundle


__all__ = ["ConfigBundle", "build_config_bundle"]
