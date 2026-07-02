"""Guard tests for Operator-Copilot tool feature-module tags (issue #479).

Every registered tool's ``module`` must be ``None`` or a real catalog id
(``feature_modules.MODULES``). If it isn't, ``effective_tool_names`` gates
the tool against a set that can never contain the id and silently drops it
from the copilot / MCP surface — the #479 defect, where conformity /
webhooks / DNS / appliance read tools were tagged with non-catalog ids
(``"compliance"`` / ``"dns"`` / ``"webhooks"`` / ``"appliance.*"``) and so
never made it into the effective set on any install.

These fail loudly in CI if a new tool reintroduces a bogus id, prove the
rescued tools are now reachable, and prove the gate fails OPEN on an
unknown id as defense-in-depth.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.models.auth import User
from app.services.ai.tools import REGISTRY, effective_tool_names
from app.services.ai.tools.base import Tool
from app.services.feature_modules import all_module_ids


def test_every_registered_tool_module_is_catalog_valid() -> None:
    catalog = all_module_ids()
    offenders = {
        t.name: t.module for t in REGISTRY.all() if t.module is not None and t.module not in catalog
    }
    assert not offenders, (
        "these tools tag a feature-module id that isn't in "
        "feature_modules.MODULES, so effective_tool_names would gate them "
        f"against a set that can never contain it (issue #479): {offenders}"
    )


# The read tools #479 rescued. Before the fix each was tagged with a
# non-catalog module id and silently dropped on every install.
_RESCUED_ALWAYS_ON = (  # now module=None → present regardless of modules
    "find_zone_dnssec_info",
    "list_webhooks",
    "get_webhook_event_types",
    "find_webhook_deliveries",
    "find_ntp_settings",
    "find_snmp_settings",
    "find_pairing_codes",
    "find_appliance_fleet",
    "find_pending_appliances",
)
_RESCUED_UNDER_CONFORMITY = (  # now module="compliance.conformity"
    "list_conformity_policies",
    "find_conformity_results",
    "get_conformity_summary",
)


def test_rescued_read_tools_present_with_all_modules_enabled() -> None:
    enabled = effective_tool_names(
        platform_enabled=None,
        provider_enabled=None,
        enabled_modules=all_module_ids(),
    )
    for name in (*_RESCUED_ALWAYS_ON, *_RESCUED_UNDER_CONFORMITY):
        assert name in enabled, f"{name} still dropped from the effective set"


def test_always_on_tools_survive_even_with_all_modules_disabled() -> None:
    # module=None tools stay available even when every feature module is
    # off — they're gated at the handler (superadmin / appliance mode),
    # not by a feature toggle.
    enabled = effective_tool_names(
        platform_enabled=None,
        provider_enabled=None,
        enabled_modules=set(),
    )
    for name in _RESCUED_ALWAYS_ON:
        assert name in enabled, f"{name} should be always-on (module=None)"


def test_conformity_tools_follow_conformity_module() -> None:
    # The corrected id is a real, gating module: present when enabled,
    # stripped when disabled (hard kill-switch preserved for known ids).
    with_module = effective_tool_names(
        platform_enabled=None,
        provider_enabled=None,
        enabled_modules=all_module_ids(),
    )
    without_module = effective_tool_names(
        platform_enabled=None,
        provider_enabled=None,
        enabled_modules=all_module_ids() - {"compliance.conformity"},
    )
    for name in _RESCUED_UNDER_CONFORMITY:
        assert name in with_module
        assert name not in without_module


class _BogusArgs(BaseModel):
    pass


async def _bogus_exec(db: object, user: User, args: _BogusArgs) -> None:
    return None


def test_unknown_module_id_fails_open(monkeypatch) -> None:
    # Defense-in-depth: a tool tagged with an unknown/mistyped module id
    # must be KEPT (fail open), mirroring is_module_enabled — never
    # silently dropped the way #479's bad tags were.
    bogus = Tool(
        name="__test_bogus_module_tool__",
        description="test",
        args_model=_BogusArgs,
        executor=_bogus_exec,
        module="does.not.exist",
    )
    monkeypatch.setitem(REGISTRY._tools, bogus.name, bogus)
    enabled = effective_tool_names(
        platform_enabled=None,
        provider_enabled=None,
        enabled_modules=all_module_ids(),
    )
    assert bogus.name in enabled
