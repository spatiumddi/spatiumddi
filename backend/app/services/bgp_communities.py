"""BGP communities — well-known catalog + boot-time seed.

The standard / well-known communities from RFC 1997, RFC 7611, and
RFC 7999 are owned by the platform, not by individual operators —
they're the same on every install. We seed them as ``BGPCommunity``
rows with ``asn_id IS NULL`` on first boot and refresh their text on
every boot so upgrades that reword a description ship without an
admin edit.

Per-AS communities are entered via the CRUD API and never touched by
this seed; the seed only owns rows whose ``value`` matches a name in
``STANDARD_COMMUNITIES``.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models.asn import BGPCommunity

logger = structlog.get_logger(__name__)


# RFC 1997 + RFC 7611 + RFC 7999 well-knowns. Each entry is
# ``(value, name, description, kind)``.
#
# ``value`` is the shortcut name we render in the picker; the
# UI / docs surface notes the wire encoding alongside.
STANDARD_COMMUNITIES: tuple[tuple[str, str, str, str], ...] = (
    (
        "no-export",
        "NO_EXPORT",
        (
            "RFC 1997. Routes carrying this community must not be advertised "
            "to any eBGP peer outside the local AS confederation."
        ),
        "standard",
    ),
    (
        "no-advertise",
        "NO_ADVERTISE",
        (
            "RFC 1997. Routes carrying this community must not be advertised to "
            "any other BGP peer at all (eBGP or iBGP)."
        ),
        "standard",
    ),
    (
        "no-export-subconfed",
        "NO_EXPORT_SUBCONFED",
        (
            "RFC 1997 / RFC 5065. Routes must not be advertised to any external "
            "BGP peer, including peers in other sub-confederations."
        ),
        "standard",
    ),
    (
        "local-as",
        "LOCAL_AS",
        (
            "Cisco-historical synonym for ``no-export-subconfed``. Same wire "
            "value (``0xFFFFFF03``); kept here for operators who type it that way."
        ),
        "standard",
    ),
    (
        "graceful-shutdown",
        "GRACEFUL_SHUTDOWN",
        (
            "RFC 7611 / RFC 8326. Wire value ``65535:0``. Tagged on routes "
            "that the originating AS is about to withdraw (planned maintenance) "
            "so peers can lower their LOCAL_PREF and pre-empt the failover."
        ),
        "standard",
    ),
    (
        "blackhole",
        "BLACKHOLE",
        (
            "RFC 7999. Wire value ``65535:666``. Tagged on routes the upstream "
            "should drop on the floor — primary use is RTBH (remotely-triggered "
            "blackhole) for DDoS mitigation."
        ),
        "standard",
    ),
    (
        "accept-own",
        "ACCEPT_OWN",
        (
            "RFC 7611. Wire value ``65535:1``. Used in route reflection setups "
            "where a route reflector wants to accept and re-advertise routes it "
            "originated."
        ),
        "standard",
    ),
)


async def seed_standard_communities() -> None:
    """Insert / refresh the well-known catalog rows. Idempotent."""
    async with AsyncSessionLocal() as session:
        try:
            existing_rows = (
                (await session.execute(select(BGPCommunity).where(BGPCommunity.asn_id.is_(None))))
                .scalars()
                .all()
            )
            existing_by_value: dict[str, BGPCommunity] = {r.value: r for r in existing_rows}
            seen: set[str] = set()
            for value, name, description, kind in STANDARD_COMMUNITIES:
                seen.add(value)
                row = existing_by_value.get(value)
                if row is None:
                    session.add(
                        BGPCommunity(
                            asn_id=None,
                            value=value,
                            kind=kind,
                            name=name,
                            description=description,
                        )
                    )
                else:
                    row.kind = kind
                    row.name = name
                    row.description = description
            # Don't delete platform-level rows the catalog no longer
            # mentions — operators may have referenced them in their
            # own notes / scripts. Just leave them alone; they go grey
            # in the UI.
            await session.commit()
        except Exception as exc:  # noqa: BLE001 — never fail boot on this
            logger.debug("bgp_communities_seed_skipped", reason=str(exc))
