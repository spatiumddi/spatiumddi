"""Resolve the TSIG key the control plane signs zone transfers with (#734).

Agent-managed BIND9 and Technitium grant ``allow-transfer`` to a **key**,
not to a source address — the control plane's address is not knowable on
the appliance, where it can be any node in an HA control plane behind a
VIP. So every read-the-live-zone path (drift #61, sync-with-servers) has
to sign, and this module is the one place that decides *with which key*.

The choice has to agree with what the agent granted, or the transfer is
REFUSED just as surely as an unsigned one. Both agent drivers grant every
key in ``bundle["tsig_keys"]``, and ``build_agent_bundle`` builds that list
as ``[group legacy key] + [operator DNSTSIGKey rows, sorted by name]``.
Picking the head of that same list keeps the two ends in agreement by
construction, and prefers the legacy group key — which exists on every
agent-managed group without operator action, so drift works out of the box
rather than only after someone creates a key.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_str
from app.drivers.dns import AXFR_TSIG_DRIVERS
from app.drivers.dns.base import TsigKey
from app.models.dns import DNSServer, DNSServerGroup, DNSTSIGKey

logger = structlog.get_logger(__name__)


def transfer_needs_tsig(server: DNSServer) -> bool:
    """True when this server's zone transfers have to be TSIG-signed.

    The driver alone is necessary but **not sufficient**. ``bind9`` and
    ``technitium`` are the drivers whose *agent* renders the key-gated
    ``allow-transfer`` — but a row with those drivers can equally be an
    operator's own BIND9 that SpatiumDDI never configures, pointed at by
    host and authorised the ordinary way, by address. Nothing was ever
    deployed there, so there is no group key on that server: signing would
    turn a working unsigned pull into NOTAUTH, and refusing to pull for
    lack of a key would break it a different way.

    ``agent_id`` is the discriminator, set only by
    ``POST /dns/agents/register``. A row awaiting its first registration
    reads as not-agent-managed, which is right — until the agent checks in
    there is no agent-rendered ``named.conf`` to have granted anything.
    """
    return server.driver in AXFR_TSIG_DRIVERS and server.agent_id is not None


async def resolve_group_transfer_key(db: AsyncSession, group_id: uuid.UUID) -> TsigKey | None:
    """Return the key to sign transfers from ``group_id`` with, or None.

    None means the group has no usable key. Callers must treat that as
    "this transfer cannot succeed" for a driver in ``AXFR_TSIG_DRIVERS``
    rather than falling through to an unsigned attempt, which fails the
    same way but reports a misleading reason.
    """
    grp = await db.get(DNSServerGroup, group_id)
    if grp is not None and grp.tsig_key_name and grp.tsig_key_secret:
        return TsigKey(
            name=grp.tsig_key_name,
            algorithm=grp.tsig_key_algorithm or "hmac-sha256",
            secret=grp.tsig_key_secret,
        )

    # No legacy key — fall back to the first operator-managed key, matching
    # the bundle's ordering so the agent has granted this one too.
    rows = (
        (
            await db.execute(
                select(DNSTSIGKey).where(DNSTSIGKey.group_id == group_id).order_by(DNSTSIGKey.name)
            )
        )
        .scalars()
        .all()
    )
    for k in rows:
        try:
            secret = decrypt_str(k.secret_encrypted)
        except ValueError:
            # Same posture as the bundle builder: a row whose secret won't
            # decrypt is skipped, not fatal. It is also skipped from the
            # bundle, so the agent never granted it either — the two stay
            # consistent, and a later key in the list may still work.
            logger.warning(
                "dns.tsig.transfer_key_undecryptable",
                group=str(group_id),
                key=k.name,
            )
            continue
        return TsigKey(name=k.name, algorithm=k.algorithm, secret=secret)
    return None


__all__ = ["resolve_group_transfer_key", "transfer_needs_tsig"]
