"""Operator Copilot read tool for the appliance SSH surface (issue #157).

Surfaces the singleton ``platform_settings`` SSH config so an operator can
ask the Copilot "is password auth on?", "is root login allowed?", "what port
is sshd on?", "which keys are authorized?". Public keys are NOT secrets, so
this tool returns each key's name / comment / fingerprint (and the full
public key, which is safe to surface) — there is nothing to redact, unlike
the SNMP community / syslog CA PEM.

There is NO ``propose_*`` write tool — SSH config is changed through the
Appliance → SSH form, same as SNMP / syslog (those writes carry a lockout-
safety cross-check the form path enforces).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.settings import PlatformSettings
from app.services.ai.tools.base import register_tool
from app.services.appliance.ssh import is_valid_public_key, key_fingerprint


class FindSshSettingsArgs(BaseModel):
    """No arguments — there is exactly one SSH config row."""

    pass


@register_tool(
    name="find_ssh_settings",
    description=(
        "Return the appliance SSH configuration: whether password "
        "authentication is enabled, whether root login is permitted, the sshd "
        "port, the allowed source-network CIDRs (the host firewall scopes the "
        "ssh port to these — empty means open from anywhere), and the list of "
        "authorized public keys (each with its operator label / comment / "
        "SHA256 fingerprint). Public keys are not secrets and are returned in "
        "full. Use to answer 'is password auth on?', 'is root login allowed?', "
        "'what port is sshd on?', 'which keys can log in?'. sshd runs on every "
        "SpatiumDDI appliance host; on docker / k8s deploys these settings "
        "still drive any registered appliance agents in a hybrid topology."
    ),
    args_model=FindSshSettingsArgs,
    category="admin",
    # Default enabled (NN #13) — read-only, no secrets (public keys are not
    # secret), no off-prem calls. module=None: SSH is plain host-config (like
    # SNMP / NTP / LLDP / syslog), not a feature module.
    default_enabled=True,
    module=None,
)
async def find_ssh_settings(
    db: AsyncSession, user: User, args: FindSshSettingsArgs
) -> dict[str, Any]:
    settings = await db.get(PlatformSettings, 1)
    if settings is None:
        return {"note": "platform_settings row missing"}
    keys = list(settings.ssh_authorized_keys or [])
    return {
        "password_auth_enabled": bool(settings.ssh_password_auth_enabled),
        "allow_root_login": bool(settings.ssh_allow_root_login),
        "port": int(settings.ssh_port or 22),
        "allowed_source_networks": list(settings.ssh_allowed_source_networks or []),
        "authorized_keys": [
            {
                "name": (k.get("name") or "") if isinstance(k, dict) else "",
                "comment": (k.get("comment") or "") if isinstance(k, dict) else "",
                "public_key": (k.get("public_key") or "") if isinstance(k, dict) else "",
                "fingerprint": (
                    key_fingerprint(str(k.get("public_key") or "")) if isinstance(k, dict) else None
                ),
                "valid": (
                    is_valid_public_key(str(k.get("public_key") or ""))
                    if isinstance(k, dict)
                    else False
                ),
            }
            for k in keys
        ],
        # Aggregate signal for the LLM: is there at least one way in?
        # (password auth on OR at least one valid key) — mirrors the
        # lockout-safety invariant the form enforces.
        "lockout_safe": bool(settings.ssh_password_auth_enabled)
        or any(
            is_valid_public_key(str(k.get("public_key") or "")) for k in keys if isinstance(k, dict)
        ),
    }
