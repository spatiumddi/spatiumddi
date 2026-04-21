"""Assemble a neutral ``ConfigBundle`` for a DHCP server.

Scopes + client classes live on ``DHCPServerGroup`` — every server in
a group renders the same config. HA tuning also lives on the group;
a group with ≥ 2 Kea members is an HA pair and emits a
``FailoverConfig``, a single-member group is standalone and doesn't.
Peer URLs are per-server (``DHCPServer.ha_peer_url``) — Kea uses them
to reach the other peer for heartbeats + lease updates.

Mirrors ``app.services.dns.config_bundle``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.drivers.dhcp.base import (
    ClientClassDef,
    ConfigBundle,
    FailoverConfig,
    PoolDef,
    ScopeDef,
    ServerOptionsDef,
    StaticAssignmentDef,
)
from app.models.dhcp import (
    DHCPClientClass,
    DHCPScope,
    DHCPServer,
    DHCPServerGroup,
)
from app.models.ipam import Subnet


async def _resolve_failover(
    db: AsyncSession, server: DHCPServer, group: DHCPServerGroup | None
) -> FailoverConfig | None:
    """Return the FailoverConfig for ``server`` if its group is an HA pair.

    An HA pair is a DHCPServerGroup with exactly 2 Kea members. The
    ``peers`` array carries both entries regardless of this server's
    role; ``this_server_name`` tells Kea which one is "us". Non-Kea
    servers in the group (e.g. Windows DHCP read-only) are ignored
    for HA purposes — Kea HA only speaks to Kea.

    Peer URLs come from ``DHCPServer.ha_peer_url`` on each member.
    An empty peer URL on either server suppresses HA rendering (the
    operator hasn't finished setting things up yet).
    """
    if group is None:
        return None
    kea_peers = [s for s in group.servers if s.driver == "kea"]
    if len(kea_peers) < 2:
        return None
    # Stable ordering so both peers render the same peers array regardless
    # of which server is asking for its bundle. Sort by id (UUID) — the
    # concept of "primary" in a group is not a model field today; whichever
    # Kea peer ends up first in the sort plays the ``primary`` role.
    kea_peers.sort(key=lambda s: str(s.id))
    primary, secondary = kea_peers[0], kea_peers[1]

    if not primary.ha_peer_url or not secondary.ha_peer_url:
        return None

    secondary_role = "standby" if group.mode == "hot-standby" else "secondary"
    peers: list[dict] = [
        {
            "name": primary.name,
            "url": primary.ha_peer_url,
            "role": "primary",
            "auto-failover": group.auto_failover,
        },
        {
            "name": secondary.name,
            "url": secondary.ha_peer_url,
            "role": secondary_role,
            "auto-failover": group.auto_failover,
        },
    ]
    return FailoverConfig(
        channel_id=str(group.id),
        channel_name=group.name,
        mode=group.mode,
        this_server_name=server.name,
        peers=tuple(peers),
        heartbeat_delay_ms=group.heartbeat_delay_ms,
        max_response_delay_ms=group.max_response_delay_ms,
        max_ack_delay_ms=group.max_ack_delay_ms,
        max_unacked_clients=group.max_unacked_clients,
    )


async def build_config_bundle(db: AsyncSession, server: DHCPServer) -> ConfigBundle:
    """Build a fully-populated ``ConfigBundle`` for the given DHCP server."""
    # Resolve the server's group. Groupless servers are allowed but render
    # an empty bundle (no scopes, no HA) — the server is registered but
    # not yet attached to a logical service.
    group: DHCPServerGroup | None = None
    if server.server_group_id is not None:
        group = (
            await db.execute(
                select(DHCPServerGroup)
                .where(DHCPServerGroup.id == server.server_group_id)
                .options(selectinload(DHCPServerGroup.servers))
            )
        ).scalar_one_or_none()

    scope_rows: list[DHCPScope] = []
    cc_rows: list[DHCPClientClass] = []
    if group is not None:
        scope_rows = list(
            (
                await db.execute(
                    select(DHCPScope)
                    .where(
                        DHCPScope.group_id == group.id,
                        DHCPScope.is_active.is_(True),
                    )
                    .options(
                        selectinload(DHCPScope.pools),
                        selectinload(DHCPScope.statics),
                    )
                )
            )
            .scalars()
            .all()
        )
        cc_rows = list(
            (await db.execute(select(DHCPClientClass).where(DHCPClientClass.group_id == group.id)))
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
                address_family=getattr(sc, "address_family", "ipv4") or "ipv4",
            )
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

    failover = await _resolve_failover(db, server, group)
    bundle = ConfigBundle(
        server_id=str(server.id),
        server_name=server.name,
        driver=server.driver,
        roles=tuple(server.roles or ()),
        options=ServerOptionsDef(options={}, lease_time=86400),
        scopes=tuple(scopes),
        client_classes=client_classes,
        generated_at=datetime.now(UTC),
        failover=failover,
    )
    bundle.etag = bundle.compute_etag()
    return bundle


__all__ = ["ConfigBundle", "build_config_bundle"]
