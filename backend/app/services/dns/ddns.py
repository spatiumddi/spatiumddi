"""DDNS — dynamic DNS reconciliation from DHCP leases.

Complements the IPAM-driven A/AAAA + PTR sync (``_sync_dns_record`` in
``app.api.v1.ipam.router``) with a lease-driven path. When a lease
lands — either via an agent lease event or the agentless lease pull —
the DDNS service resolves a hostname per the subnet's
``ddns_hostname_policy`` and then calls the same sync path static IP
allocations use. So the DNS side of DDNS is identical to the DNS side
of static allocations; the only DDNS-specific logic is hostname
resolution.

Design:

* **Subnet-level opt-in.** ``Subnet.ddns_enabled`` gates everything;
  policy is only consulted when enabled. No DHCP scope / server / space
  toggle — it's one knob per subnet.
* **Static wins.** If the lease IP matches a ``DHCPStaticAssignment``
  with a hostname, we publish the static hostname regardless of the
  subnet policy (matches user expectation: "I gave this MAC a name,
  use that name").
* **Policy for dynamic leases:**
    ``client_provided``       — only publish if the lease has a hostname.
    ``client_or_generated``   — use client hostname if present, else
                                generate ``dhcp-<hyphenated-last-octets>``.
    ``always_generate``       — always synthesise, ignore client hostname.
    ``disabled``               — never publish.
* **Generated hostnames** use the last two octets for IPv4 (compact,
  readable — ``dhcp-20-5`` for ``10.1.20.5``). For IPv6 we use the low
  32 bits hex-encoded (``dhcp-0-abcd1234``) — it's ugly but unique,
  and v6 DDNS is a rare path.
* **Idempotent.** If the IPAM row already has the same hostname, no
  DNS op is queued.

Circular-import note: ``_sync_dns_record`` lives in
``app.api.v1.ipam.router`` and is the canonical A/PTR pipeline. The
router imports DDNS-adjacent helpers (``services.dns.sync_check``,
``services.dns.reverse_zone``) at module load, so a top-level import
of the router from *here* would close the loop. The two entry points
lazy-import the router function at call time to dodge it.
"""

from __future__ import annotations

import ipaddress
import re

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import DHCPScope, DHCPStaticAssignment
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet

logger = structlog.get_logger(__name__)


class EffectiveDDNS:
    """Resolved DDNS config for a subnet, after walking subnet → block → space.

    Uses ``__slots__`` + plain attributes instead of a dataclass so it
    stays cheap on the hot path (every lease upsert calls this).
    """

    __slots__ = ("enabled", "hostname_policy", "domain_override", "ttl", "source")

    def __init__(
        self,
        *,
        enabled: bool,
        hostname_policy: str,
        domain_override: str | None,
        ttl: int | None,
        source: str,
    ) -> None:
        self.enabled = enabled
        self.hostname_policy = hostname_policy
        self.domain_override = domain_override
        self.ttl = ttl
        # source = "subnet" | "block:<id>" | "space:<id>" — useful for the
        # UI (effective-fields placeholder) and for debug logging.
        self.source = source


async def resolve_effective_ddns(db: AsyncSession, subnet: Subnet) -> EffectiveDDNS:
    """Return the effective DDNS config for a subnet.

    Walks the same chain as ``_resolve_effective_dns`` in the IPAM router:
    the subnet's own values win if ``ddns_inherit_settings`` is False;
    otherwise we walk up the block chain to the first non-inheriting
    ancestor, and finally fall back to the containing IPSpace (which has
    no inherit toggle — it's the root).
    """
    if not getattr(subnet, "ddns_inherit_settings", True):
        return EffectiveDDNS(
            enabled=subnet.ddns_enabled,
            hostname_policy=subnet.ddns_hostname_policy,
            domain_override=subnet.ddns_domain_override,
            ttl=subnet.ddns_ttl,
            source="subnet",
        )

    current = await db.get(IPBlock, subnet.block_id) if subnet.block_id else None
    while current is not None:
        if not current.ddns_inherit_settings:
            return EffectiveDDNS(
                enabled=current.ddns_enabled,
                hostname_policy=current.ddns_hostname_policy,
                domain_override=current.ddns_domain_override,
                ttl=current.ddns_ttl,
                source=f"block:{current.id}",
            )
        if current.parent_block_id:
            current = await db.get(IPBlock, current.parent_block_id)
        else:
            space = await db.get(IPSpace, current.space_id)
            if space is None:
                break
            return EffectiveDDNS(
                enabled=space.ddns_enabled,
                hostname_policy=space.ddns_hostname_policy,
                domain_override=space.ddns_domain_override,
                ttl=space.ddns_ttl,
                source=f"space:{space.id}",
            )
    # Orphan subnet with no block? Fall back to its own values (which
    # default to disabled/client_or_generated).
    return EffectiveDDNS(
        enabled=subnet.ddns_enabled,
        hostname_policy=subnet.ddns_hostname_policy,
        domain_override=subnet.ddns_domain_override,
        ttl=subnet.ddns_ttl,
        source="subnet",
    )


_POLICIES: frozenset[str] = frozenset(
    {"client_provided", "client_or_generated", "always_generate", "disabled"}
)

# Hostnames are a subset of RFC 1035 labels. We clamp to lower-case
# alphanumerics + hyphen and strip anything else; empty after strip
# means "use the generated form instead".
_HOSTNAME_SAFE_RE = re.compile(r"[^a-z0-9-]+")


def _sanitise(raw: str | None) -> str:
    """Fold a raw client hostname into a safe DNS label.

    Strips quotes, lower-cases, collapses runs of unsafe chars to a
    single hyphen, trims leading/trailing hyphens, truncates at 63
    (the RFC 1035 label limit).
    """
    if not raw:
        return ""
    s = raw.strip().strip('"').lower()
    s = _HOSTNAME_SAFE_RE.sub("-", s)
    s = s.strip("-")
    return s[:63]


def _generate_hostname(ip_str: str) -> str:
    """Synthesise ``dhcp-<hyphenated-tail>`` for an IP with no client name."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return "dhcp-unknown"
    if isinstance(addr, ipaddress.IPv4Address):
        parts = str(addr).split(".")
        tail = "-".join(parts[-2:])  # ``20-5`` for 10.1.20.5
        return f"dhcp-{tail}"
    # IPv6: last 32 bits, no separators. Rare path.
    low = int(addr) & 0xFFFFFFFF
    return f"dhcp-{low:08x}"


async def _static_hostname_for(db: AsyncSession, subnet: Subnet, ip_str: str) -> str | None:
    """If this IP is a static DHCP reservation with a hostname, return it.

    Joins ``DHCPStaticAssignment`` through ``DHCPScope.subnet_id``.
    Returns the sanitised hostname, or ``None`` if no static match or
    the static has no hostname.
    """
    res = await db.execute(
        select(DHCPStaticAssignment.hostname)
        .join(DHCPScope, DHCPStaticAssignment.scope_id == DHCPScope.id)
        .where(
            DHCPScope.subnet_id == subnet.id,
            DHCPStaticAssignment.ip_address == ip_str,
        )
    )
    row = res.first()
    if row is None:
        return None
    return _sanitise(row[0]) or None


async def resolve_ddns_hostname(
    db: AsyncSession,
    subnet: Subnet,
    ip_str: str,
    client_hostname: str | None,
) -> str | None:
    """Pick a hostname for a lease per the subnet's DDNS policy.

    Returns ``None`` when DDNS should not fire for this lease (subnet
    disabled, policy ``disabled``, or ``client_provided`` without a
    client hostname). Otherwise returns a sanitised DNS label.

    Static assignments override the policy — a static with a hostname
    always publishes, regardless of what the lease client sent.

    Uses ``resolve_effective_ddns`` so block / space-level overrides are
    honoured — see ``ddns_inherit_settings`` on each model.
    """
    eff = await resolve_effective_ddns(db, subnet)
    if not eff.enabled:
        return None
    policy = eff.hostname_policy
    if policy not in _POLICIES or policy == "disabled":
        return None

    # Static wins — even over ``always_generate``, because a static
    # hostname is an explicit admin choice.
    static_name = await _static_hostname_for(db, subnet, ip_str)
    if static_name:
        return static_name

    client_name = _sanitise(client_hostname)

    if policy == "client_provided":
        return client_name or None
    if policy == "always_generate":
        return _generate_hostname(ip_str)
    # client_or_generated
    return client_name or _generate_hostname(ip_str)


async def apply_ddns_for_lease(
    db: AsyncSession,
    *,
    subnet: Subnet,
    ipam_row: IPAddress,
    client_hostname: str | None,
) -> bool:
    """Evaluate policy + push A/AAAA + PTR for a freshly-mirrored lease.

    Returns True if DDNS fired (records enqueued), False otherwise.

    Only acts when:
      * the subnet has DDNS enabled,
      * the IPAM row is ``auto_from_lease=True`` (manual allocations
        are never touched by DDNS — the owner picked the hostname),
      * the resolved hostname differs from what's already on the row.

    Lazy-imports ``_sync_dns_record`` from the IPAM router to avoid a
    circular import at module load.
    """
    if not ipam_row.auto_from_lease:
        return False
    eff = await resolve_effective_ddns(db, subnet)
    if not eff.enabled:
        return False

    hostname = await resolve_ddns_hostname(db, subnet, str(ipam_row.address), client_hostname)
    if not hostname:
        return False

    # Idempotency: if the hostname is unchanged and an auto-generated
    # DNS record already points at this IP, skip the sync entirely.
    if ipam_row.hostname == hostname and ipam_row.dns_record_id is not None:
        return False

    ipam_row.hostname = hostname

    # Lazy-import: the router imports sync_check / reverse_zone at top
    # level, so a top-level import of the router here would close the
    # cycle. Calling at the bottom of the service call is cycle-free.
    from app.api.v1.ipam.router import _sync_dns_record  # noqa: PLC0415

    await _sync_dns_record(db, ipam_row, subnet, action="create")
    logger.info(
        "ddns_applied",
        subnet_id=str(subnet.id),
        ip=str(ipam_row.address),
        hostname=hostname,
        policy=subnet.ddns_hostname_policy,
    )
    return True


async def revoke_ddns_for_lease(
    db: AsyncSession,
    *,
    subnet: Subnet,
    ipam_row: IPAddress,
) -> bool:
    """Delete DDNS-published records when a lease expires / is cleaned up.

    Mirrors ``apply_ddns_for_lease`` on the cleanup path. Only acts on
    ``auto_from_lease=True`` rows that actually have a linked DNS
    record — otherwise there's nothing to delete.
    """
    if not ipam_row.auto_from_lease:
        return False
    if ipam_row.dns_record_id is None and not ipam_row.hostname:
        return False

    from app.api.v1.ipam.router import _sync_dns_record  # noqa: PLC0415

    await _sync_dns_record(db, ipam_row, subnet, action="delete")
    logger.info(
        "ddns_revoked",
        subnet_id=str(subnet.id),
        ip=str(ipam_row.address),
    )
    return True


__all__ = [
    "EffectiveDDNS",
    "apply_ddns_for_lease",
    "resolve_ddns_hostname",
    "resolve_effective_ddns",
    "revoke_ddns_for_lease",
]
