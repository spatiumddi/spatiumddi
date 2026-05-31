"""Operator Copilot read tool for the appliance LLDP surface (issue #343).

Surfaces the singleton ``platform_settings`` LLDP config so an operator can
ask the Copilot "is LLDP on?", "what interfaces does it advertise on?", or
"what TTL are we sending?". No secrets — LLDP advertises public identity, so
the response mirrors the stored shape directly.

The neighbour-data tool ``find_lldp_neighbors`` (joining the appliance's
discovered L2 neighbours into IPAM) is Phase 2 — it needs the supervisor to
ship ``lldpcli show neighbors`` back to the control plane first. This tool is
the Phase-1 config-visibility counterpart, matching ``find_snmp_settings``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.settings import PlatformSettings
from app.services.ai.tools.base import register_tool


class FindLLDPSettingsArgs(BaseModel):
    """No arguments — there is exactly one LLDP config row."""

    pass


@register_tool(
    name="find_lldp_settings",
    description=(
        "Return the appliance LLDP configuration — master toggle, transmit "
        "interval + hold (and the advertised TTL = interval × hold), which "
        "extra neighbour protocols (CDP / EDP / FDP / SONMP) are received, the "
        "interface allowlist pattern, the management-address pattern, and any "
        "system-name / description overrides. Use to answer 'is LLDP on?', "
        "'what interfaces do we advertise on?', 'what TTL are neighbours told?'. "
        "lldpd runs on every SpatiumDDI appliance host; on docker / k8s deploys "
        "these settings still drive registered appliance agents."
    ),
    args_model=FindLLDPSettingsArgs,
    category="admin",
    # Default enabled (NN #13) — read-only, no secrets, no off-prem calls.
    # module=None: LLDP is plain host-config (like SNMP/NTP), not a feature
    # module, so it stays unambiguously always-available.
    default_enabled=True,
    module=None,
)
async def find_lldp_settings(
    db: AsyncSession, user: User, args: FindLLDPSettingsArgs
) -> dict[str, Any]:
    settings = await db.get(PlatformSettings, 1)
    if settings is None:
        return {"enabled": False, "note": "platform_settings row missing"}
    interval = int(settings.lldp_tx_interval or 30)
    hold = int(settings.lldp_tx_hold or 4)
    return {
        "enabled": bool(settings.lldp_enabled),
        "tx_interval_seconds": interval,
        "tx_hold": hold,
        "advertised_ttl_seconds": max(1, interval) * max(1, hold),
        "extra_protocols_received": list(settings.lldp_protocols or []),
        "interface_pattern": settings.lldp_interface_pattern,
        "management_pattern": settings.lldp_management_pattern or "(auto)",
        "system_name_override": settings.lldp_sys_name or None,
        "system_description_override": settings.lldp_sys_description or None,
    }
