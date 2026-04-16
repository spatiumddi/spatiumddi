"""Assemble a neutral ``ConfigBundle`` for a DHCP server.

Mirrors ``app.services.dns.config_bundle``. The bundle is the canonical
driver-agnostic projection of DB state that the agent long-poll endpoint
returns. Drivers consume it for rendering.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.drivers.dhcp.base import (
    ClientClassDef,
    ConfigBundle,
    PoolDef,
    ScopeDef,
    ServerOptionsDef,
    StaticAssignmentDef,
)
from app.models.dhcp import DHCPClientClass, DHCPScope, DHCPServer
from app.models.ipam import Subnet


async def build_config_bundle(db: AsyncSession, server: DHCPServer) -> ConfigBundle:
    """Build a fully-populated ``ConfigBundle`` for the given DHCP server."""
    # Scopes + pools + statics (lazy="joined" on the model already handles pools/statics)
    scope_rows = (
        (
            await db.execute(
                select(DHCPScope)
                .where(DHCPScope.server_id == server.id, DHCPScope.is_active.is_(True))
                .options(
                    selectinload(DHCPScope.pools),
                    selectinload(DHCPScope.statics),
                )
            )
        )
        .scalars()
        .all()
    )

    # Pre-fetch subnet CIDRs
    subnet_ids = [s.subnet_id for s in scope_rows]
    subnet_map: dict = {}
    if subnet_ids:
        res = await db.execute(select(Subnet).where(Subnet.id.in_(subnet_ids)))
        for s in res.scalars().all():
            subnet_map[s.id] = s

    scopes: list[ScopeDef] = []
    for sc in scope_rows:
        subnet = subnet_map.get(sc.subnet_id)
        if subnet is None:
            continue
        subnet_cidr = str(subnet.network) if subnet.network else ""
        pools = tuple(
            PoolDef(
                start_ip=str(p.start_ip),
                end_ip=str(p.end_ip),
                pool_type=p.pool_type,
                name=p.name or "",
                class_restriction=p.class_restriction,
                lease_time_override=p.lease_time_override,
                options_override=p.options_override or None,
            )
            for p in sc.pools
        )
        statics = tuple(
            StaticAssignmentDef(
                ip_address=str(s.ip_address),
                mac_address=str(s.mac_address),
                hostname=s.hostname or "",
                client_id=s.client_id,
                options_override=s.options_override or None,
            )
            for s in sc.statics
        )
        scopes.append(
            ScopeDef(
                subnet_cidr=subnet_cidr,
                lease_time=sc.lease_time,
                min_lease_time=sc.min_lease_time,
                max_lease_time=sc.max_lease_time,
                options=dict(sc.options or {}),
                pools=pools,
                statics=statics,
                ddns_enabled=sc.ddns_enabled,
                ddns_hostname_policy=sc.ddns_hostname_policy,
                is_active=sc.is_active,
            )
        )

    # Client classes
    cc_rows = (
        (await db.execute(select(DHCPClientClass).where(DHCPClientClass.server_id == server.id)))
        .scalars()
        .all()
    )
    client_classes = tuple(
        ClientClassDef(
            name=c.name,
            match_expression=c.match_expression or "",
            description=c.description or "",
            options=dict(c.options or {}),
        )
        for c in cc_rows
    )

    bundle = ConfigBundle(
        server_id=str(server.id),
        server_name=server.name,
        driver=server.driver,
        roles=tuple(server.roles or ()),
        options=ServerOptionsDef(options={}, lease_time=86400),
        scopes=tuple(scopes),
        client_classes=client_classes,
        generated_at=datetime.now(UTC),
    )
    bundle.etag = bundle.compute_etag()
    return bundle


__all__ = ["ConfigBundle", "build_config_bundle"]
