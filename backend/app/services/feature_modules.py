"""Feature-module catalog + cached enabled-set.

The catalog is the source of truth for *which modules exist*. The
``feature_module`` DB table stores per-module operator overrides;
unknown rows in the table are tolerated (forward-compat with
downgrades) but never gate anything.

Default policy:
    Default-enabled-on-install. Operators can't disable what they don't
    know exists. Off-prem / secret-touching modules override this by
    declaring ``default_enabled=False`` here — the migration seeds a
    matching row.

When a route gate (``require_module``) fails it raises 404, not 403:
    a disabled module is "not present" from the API surface's
    perspective, not "you can't access it". Mirrors how a not-installed
    plugin would behave in NetBox / Grafana.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import structlog
from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.feature_module import FeatureModule

logger = structlog.get_logger(__name__)


# ── Catalog ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModuleSpec:
    """Static catalog entry for a togglable feature."""

    id: str
    label: str
    group: str
    description: str
    default_enabled: bool = True


# Stable dotted-name ids. New modules append here AND seed a row in a
# migration (the seed value should match ``default_enabled``).
#
# Groups drive UI placement on Settings → Features. Three buckets so
# far — keep this list small; we collapse fine sub-groups into broader
# headings on the page.
MODULES: Final[tuple[ModuleSpec, ...]] = (
    # Network — everything under the sidebar's "Network" section.
    ModuleSpec(
        id="network.customer",
        label="Customers",
        group="Network",
        description="Customer ownership records — operator-facing entity attached to IPAM/DNS/DHCP/Network rows.",
    ),
    ModuleSpec(
        id="network.provider",
        label="Providers",
        group="Network",
        description="Carrier/upstream provider records, used as RESTRICT FK on circuits.",
    ),
    ModuleSpec(
        id="network.site",
        label="Sites",
        group="Network",
        description="Physical/logical site records attached as ownership FKs.",
    ),
    ModuleSpec(
        id="network.service",
        label="Services",
        group="Network",
        description="Service catalog (MPLS L3VPN, SD-WAN, …) bound to underlying VRFs / subnets / circuits.",
    ),
    ModuleSpec(
        id="network.asn",
        label="ASNs",
        group="Network",
        description="Autonomous-system records with RDAP holder + RPKI ROA enrichment.",
    ),
    ModuleSpec(
        id="network.circuit",
        label="Circuits",
        group="Network",
        description="WAN circuits — carrier-supplied logical pipes between sites/providers.",
    ),
    ModuleSpec(
        id="network.device",
        label="Network devices",
        group="Network",
        description="Routers/switches discovered via SNMP polling and their ARP/FDB/interface tables.",
    ),
    ModuleSpec(
        id="network.overlay",
        label="Overlays",
        group="Network",
        description="SD-WAN overlay topology — sites + circuits + routing policies.",
    ),
    ModuleSpec(
        id="network.vlan",
        label="VLANs",
        group="Network",
        description="VLAN registry + Router groups.",
    ),
    ModuleSpec(
        id="network.vrf",
        label="VRFs",
        group="Network",
        description="VRF records replacing freeform RD/RT text on IPSpace.",
    ),
    # AI — operator copilot, gated as a whole.
    ModuleSpec(
        id="ai.copilot",
        label="Operator Copilot",
        group="AI",
        description="Multi-vendor LLM chat + MCP tool surface. Disabling hides the chat drawer and 404s /ai endpoints.",
    ),
    # Compliance / observability extras.
    ModuleSpec(
        id="compliance.conformity",
        label="Conformity evaluations",
        group="Compliance",
        description="Declarative compliance checks + PDF export. Auditor / Compliance Editor builtin roles depend on it.",
    ),
    # Tools.
    ModuleSpec(
        id="tools.nmap",
        label="Nmap scanning",
        group="Tools",
        description="On-demand nmap with live SSE output + history. Subnet/IP scan buttons hide when off.",
    ),
    # Integrations — read-only mirrors of external orchestrators.
    # Default-disabled: each one needs operator-supplied credentials
    # before it does anything useful, and the kickoff lives behind
    # the per-integration page anyway. Toggle on here makes the
    # integration's sidebar entry + REST surface appear; the actual
    # poll only starts once the operator configures a target.
    #
    # The matching ``PlatformSettings.integration_*_enabled`` columns
    # are kept in lock-step by the toggle endpoint (Celery beat tasks
    # gate on them and we don't want to fan out the read-feature_module
    # change across every reconciler in one PR).
    ModuleSpec(
        id="integrations.kubernetes",
        label="Kubernetes",
        group="Integrations",
        description="Read-only mirror of Kubernetes pods + services into IPAM. Connect clusters from the Kubernetes page once enabled.",
        default_enabled=False,
    ),
    ModuleSpec(
        id="integrations.docker",
        label="Docker",
        group="Integrations",
        description="Read-only mirror of Docker container IPs into IPAM. Connect hosts from the Docker page once enabled.",
        default_enabled=False,
    ),
    ModuleSpec(
        id="integrations.proxmox",
        label="Proxmox VE",
        group="Integrations",
        description="Read-only mirror of Proxmox guests + bridges + SDN VNets into IPAM. Connect endpoints from the Proxmox page once enabled.",
        default_enabled=False,
    ),
    ModuleSpec(
        id="integrations.tailscale",
        label="Tailscale",
        group="Integrations",
        description="Read-only mirror of tailnet devices into IPAM. Connect tenants from the Tailscale page once enabled.",
        default_enabled=False,
    ),
    ModuleSpec(
        id="integrations.unifi",
        label="UniFi",
        group="Integrations",
        description="Read-only mirror of UniFi networks + clients into IPAM. Supports local + cloud-hosted controllers; connect controllers from the UniFi page once enabled.",
        default_enabled=False,
    ),
)

# Map a feature_module id to the ``PlatformSettings`` column whose
# ``True``/``False`` value mirrors it. The toggle endpoint writes both
# sides in the same transaction so reconciler tasks (which gate on the
# settings column) see the change without a separate migration. New
# integrations register here in the same PR that adds them.
INTEGRATION_SETTINGS_MIRROR: Final[dict[str, str]] = {
    "integrations.kubernetes": "integration_kubernetes_enabled",
    "integrations.docker": "integration_docker_enabled",
    "integrations.proxmox": "integration_proxmox_enabled",
    "integrations.tailscale": "integration_tailscale_enabled",
    "integrations.unifi": "integration_unifi_enabled",
}

MODULES_BY_ID: Final[dict[str, ModuleSpec]] = {m.id: m for m in MODULES}


def all_module_ids() -> set[str]:
    return set(MODULES_BY_ID.keys())


def is_known(module_id: str) -> bool:
    return module_id in MODULES_BY_ID


# ── Enabled-set cache ──────────────────────────────────────────────────
#
# Process-local cache — the toggle set is tiny (~14 rows) and changes
# rarely, so we cache it for a short TTL rather than hitting the DB on
# every request. ``invalidate_cache`` is called from the toggle endpoint
# so an admin's flip takes effect immediately for that worker. Other
# workers pick it up at their next TTL expiry (within ``_CACHE_TTL_S``).

_CACHE_TTL_S: Final[float] = 5.0
_cache_loaded_at: float = 0.0
_cached_enabled: set[str] = set()


def invalidate_cache() -> None:
    """Drop the cached enabled-set. Called from the admin toggle so the
    flipping worker sees the change instantly."""
    global _cache_loaded_at
    _cache_loaded_at = 0.0


async def get_enabled_modules(db: AsyncSession) -> set[str]:
    """Return the set of currently-enabled module ids.

    Resolved as:
        for each module in the catalog:
            if a DB override exists, honour it
            else honour the catalog's default_enabled
    """
    global _cache_loaded_at, _cached_enabled
    now = time.monotonic()
    if now - _cache_loaded_at < _CACHE_TTL_S and _cached_enabled:
        return _cached_enabled

    rows = (await db.execute(select(FeatureModule))).scalars().all()
    overrides: dict[str, bool] = {row.id: row.enabled for row in rows}

    enabled: set[str] = set()
    for spec in MODULES:
        is_on = overrides.get(spec.id, spec.default_enabled)
        if is_on:
            enabled.add(spec.id)

    _cached_enabled = enabled
    _cache_loaded_at = now
    return enabled


async def is_module_enabled(db: AsyncSession, module_id: str) -> bool:
    """Convenience wrapper. Unknown ids resolve to True so a renamed/
    removed module never accidentally hides a route — defensive. The
    catalog itself is the source of truth for what's gateable."""
    if not is_known(module_id):
        return True
    enabled = await get_enabled_modules(db)
    return module_id in enabled


async def set_module_enabled(
    db: AsyncSession,
    module_id: str,
    enabled: bool,
    *,
    user_id,  # type: ignore[no-untyped-def]
) -> FeatureModule:
    """Upsert the override. Caller commits + writes audit + invalidates
    the cache. Raises ``ValueError`` if the id isn't in the catalog."""
    if not is_known(module_id):
        raise ValueError(f"Unknown feature module: {module_id!r}")
    stmt = (
        pg_insert(FeatureModule)
        .values(
            id=module_id,
            enabled=enabled,
            updated_at=datetime.now(UTC),
            updated_by_id=user_id,
        )
        .on_conflict_do_update(
            index_elements=[FeatureModule.id],
            set_=dict(
                enabled=enabled,
                updated_at=datetime.now(UTC),
                updated_by_id=user_id,
            ),
        )
        .returning(FeatureModule)
    )
    row = (await db.execute(stmt)).scalar_one()
    return row


# ── FastAPI dependency ─────────────────────────────────────────────────


def require_module(module_id: str):
    """Build a FastAPI dependency that 404s when ``module_id`` is
    disabled. Apply at the router level::

        api_v1_router.include_router(
            customers_router,
            prefix="/customers",
            dependencies=[Depends(require_module("network.customer"))],
            tags=["customers"],
        )

    404 (not 403) so the API surface mirrors what an air-gapped
    deployment would look like with the module not installed.
    """
    if not is_known(module_id):
        # Catch typos at app-boot so we never deploy a gate against a
        # nonexistent module id.
        raise RuntimeError(f"require_module: unknown module {module_id!r}")

    async def _gate(db: AsyncSession = Depends(get_db)) -> None:
        if not await is_module_enabled(db, module_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Feature '{module_id}' is disabled.",
            )

    return _gate


def filter_to_enabled_tools(
    *,
    enabled_modules: Iterable[str],
    tool_modules: dict[str, str | None],
) -> set[str]:
    """Given a map of ``tool_name -> module_id`` (or None for "always
    enabled"), return the set of tool names that survive the module
    filter. Used by the MCP registry layer to strip tools whose module
    is disabled, regardless of per-tool default_enabled.
    """
    enabled_set = set(enabled_modules)
    return {name for name, mod in tool_modules.items() if mod is None or mod in enabled_set}
