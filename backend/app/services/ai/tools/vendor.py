"""Vendor-rollup tools for the Operator Copilot.

These answer questions like 'how many Apple devices are on my
network?', 'do I have any Raspberry Pis?', or 'breakdown of devices
by manufacturer'. They aggregate MAC addresses across IPAM rows and
/ or active DHCP leases through the existing OUI table populated by
``app.tasks.oui_update``.

Both tools short-circuit cleanly when ``PlatformSettings.oui_lookup_enabled``
is False — :func:`bulk_lookup_vendors` returns ``{}`` in that case, so
the rollup just shows zero matches with the same shape and the LLM
can naturally surface 'OUI lookup is disabled' to the operator.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.dhcp import DHCPLease
from app.models.ipam import IPAddress, Subnet
from app.services.ai.tools.base import register_tool
from app.services.oui import bulk_lookup_vendors, normalize_mac_key


async def _collect_mac_keys(db: AsyncSession, source: str) -> set[str]:
    """Return the set of normalized MAC keys present in scope.

    ``source='ipam'`` reads ``IPAddress.mac_address`` (managed +
    DHCP-mirrored rows). ``source='dhcp_active'`` reads currently-active
    DHCP leases. ``source='all'`` is the union, deduplicated by MAC.
    """
    out: set[str] = set()

    if source in ("ipam", "all"):
        rows = (
            (
                await db.execute(
                    select(IPAddress.mac_address).where(IPAddress.mac_address.is_not(None))
                )
            )
            .scalars()
            .all()
        )
        for raw in rows:
            key = normalize_mac_key(str(raw)) if raw is not None else None
            if key:
                out.add(key)

    if source in ("dhcp_active", "all"):
        rows = (
            (await db.execute(select(DHCPLease.mac_address).where(DHCPLease.state == "active")))
            .scalars()
            .all()
        )
        for raw in rows:
            key = normalize_mac_key(str(raw)) if raw is not None else None
            if key:
                out.add(key)

    return out


class CountDevicesByVendorArgs(BaseModel):
    vendor_search: str | None = Field(
        default=None,
        description=(
            "Optional case-insensitive substring filter on the vendor "
            "name (e.g. 'apple', 'raspberry', 'sonos'). Leave empty "
            "for the full breakdown."
        ),
    )
    source: Literal["ipam", "dhcp_active", "all"] = Field(
        default="ipam",
        description=(
            "Where to draw MACs from. 'ipam' = managed IPAddress rows; "
            "'dhcp_active' = currently-active DHCP leases; 'all' = the "
            "deduplicated union. Defaults to 'ipam' (stable, "
            "operator-curated view)."
        ),
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Maximum vendor buckets to return.",
    )


@register_tool(
    name="count_devices_by_vendor",
    description=(
        "Roll up MAC addresses by vendor (OUI lookup). Use for "
        "questions like 'how many Apple devices are on my "
        "network?', 'do I have any Raspberry Pis?', or 'breakdown of "
        "devices by manufacturer'. Returns vendor → count buckets "
        "sorted by count descending. Optional vendor_search narrows "
        "to a specific vendor substring. Set source='ipam' for "
        "managed rows, 'dhcp_active' for currently-leased devices, "
        "or 'all' for the union. Requires OUI lookup to be enabled "
        "in Settings → IPAM (admin) — when off, the rollup is empty."
    ),
    args_model=CountDevicesByVendorArgs,
    category="ipam",
)
async def count_devices_by_vendor(
    db: AsyncSession, user: User, args: CountDevicesByVendorArgs
) -> dict[str, Any]:
    keys = await _collect_mac_keys(db, args.source)
    # bulk_lookup_vendors keys its output by the same canonical
    # 12-char form normalize_mac_key produces, so we can pass our
    # ``keys`` set directly and dict-lookup with no second pass.
    vendors_map = await bulk_lookup_vendors(db, list(keys))

    counter: Counter[str] = Counter()
    for key in keys:
        vendor = vendors_map.get(key)
        if vendor:
            counter[vendor] += 1

    if args.vendor_search:
        needle = args.vendor_search.lower()
        counter = Counter({v: c for v, c in counter.items() if needle in v.lower()})

    top = counter.most_common(args.limit)
    return {
        "source": args.source,
        "total_macs_seen": len(keys),
        "total_with_vendor": sum(counter.values()),
        "distinct_vendors": len(counter),
        "vendors": [{"vendor": v, "count": c} for v, c in top],
    }


class FindDevicesByVendorArgs(BaseModel):
    vendor_search: str = Field(
        description=(
            "Case-insensitive substring match on the OUI vendor name. "
            "Examples: 'apple', 'raspberry', 'sonos', 'cisco'."
        ),
    )
    source: Literal["ipam", "dhcp_active", "all"] = Field(
        default="ipam",
        description=(
            "Where to look. 'ipam' = managed IPAddress rows (stable); "
            "'dhcp_active' = currently-active DHCP leases (transient); "
            "'all' = both, deduplicated by MAC."
        ),
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="find_devices_by_vendor",
    description=(
        "List specific devices whose MAC OUI matches a vendor "
        "substring. Use after count_devices_by_vendor when the "
        "operator wants to see the actual rows ('show me my "
        "Raspberry Pis', 'what are all my Apple devices?'). Returns "
        "IP, MAC, vendor, hostname, and source per match. Sourced "
        "from IPAM, active DHCP leases, or both."
    ),
    args_model=FindDevicesByVendorArgs,
    category="ipam",
)
async def find_devices_by_vendor(
    db: AsyncSession, user: User, args: FindDevicesByVendorArgs
) -> dict[str, Any]:
    needle = args.vendor_search.lower().strip()
    if not needle:
        return {
            "vendor_search": args.vendor_search,
            "error": "vendor_search must be a non-empty string",
            "matches": [],
        }

    matches: list[dict[str, Any]] = []
    seen_macs: set[str] = set()

    # IPAM rows first — they carry hostname / subnet context the
    # transient lease view doesn't have, so when a MAC appears in
    # both we keep the richer row.
    if args.source in ("ipam", "all"):
        ipam_rows = (
            (await db.execute(select(IPAddress).where(IPAddress.mac_address.is_not(None))))
            .scalars()
            .all()
        )
        ip_macs = [str(r.mac_address) if r.mac_address else None for r in ipam_rows]
        vendors_map = await bulk_lookup_vendors(db, ip_macs)
        for r in ipam_rows:
            key = normalize_mac_key(str(r.mac_address)) if r.mac_address else None
            if not key:
                continue
            vendor = vendors_map.get(key)
            if not vendor or needle not in vendor.lower():
                continue
            if key in seen_macs:
                continue
            seen_macs.add(key)
            sub = await db.get(Subnet, r.subnet_id) if r.subnet_id else None
            matches.append(
                {
                    "source": "ipam",
                    "ip_address": str(r.address),
                    "mac_address": str(r.mac_address),
                    "vendor": vendor,
                    "hostname": r.hostname,
                    "fqdn": r.fqdn,
                    "subnet_id": str(r.subnet_id) if r.subnet_id else None,
                    "subnet_network": str(sub.network) if sub else None,
                    "subnet_name": sub.name if sub else None,
                    "status": r.status,
                    "last_seen_at": (r.last_seen_at.isoformat() if r.last_seen_at else None),
                }
            )
            if len(matches) >= args.limit:
                return {"vendor_search": args.vendor_search, "matches": matches}

    if args.source in ("dhcp_active", "all"):
        lease_rows = (
            (await db.execute(select(DHCPLease).where(DHCPLease.state == "active"))).scalars().all()
        )
        # mypy invariance — bulk_lookup_vendors signature is
        # list[str | None]; spell it explicitly so the literal-comp
        # type doesn't lock to list[str].
        lease_macs: list[str | None] = [str(le.mac_address) for le in lease_rows]
        vendors_map = await bulk_lookup_vendors(db, lease_macs)
        for le in lease_rows:
            key = normalize_mac_key(str(le.mac_address))
            if not key:
                continue
            vendor = vendors_map.get(key)
            if not vendor or needle not in vendor.lower():
                continue
            if key in seen_macs:
                continue
            seen_macs.add(key)
            matches.append(
                {
                    "source": "dhcp_lease",
                    "ip_address": str(le.ip_address),
                    "mac_address": str(le.mac_address),
                    "vendor": vendor,
                    "hostname": le.hostname,
                    "fqdn": None,
                    "subnet_id": None,
                    "subnet_network": None,
                    "subnet_name": None,
                    "status": le.state,
                    "last_seen_at": (le.starts_at.isoformat() if le.starts_at else None),
                }
            )
            if len(matches) >= args.limit:
                break

    return {"vendor_search": args.vendor_search, "matches": matches}
