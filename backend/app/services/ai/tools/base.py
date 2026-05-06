"""Tool registry primitives (issue #90 — Operator Copilot Wave 2).

A tool is a Python async function the LLM may invoke. Each tool
declares its arguments via a Pydantic model — that gives us
auto-generated JSON Schema (which both the OpenAI Chat Completions
``tools`` parameter and the MCP ``tools/list`` response consume) for
free.

Tools are registered on import (see ``tools/__init__.py``) via the
``@register_tool`` decorator. The :class:`ToolRegistry` is the
canonical interface used by both the in-app chat orchestrator
(Wave 3, in-process) and the MCP HTTP endpoint (Wave 2, external
clients) — keeping a single source of truth for "what can the
operator copilot do?".

All Wave 2 tools are read-only. Write tools (Phase 3) will gate on
a ``writes: bool`` flag plus the existing ``requires_confirmation``
preview / commit pattern.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User

# Each tool's executor: takes the user's DB session, the requesting
# user (for permission scoping if needed), and a parsed Pydantic args
# instance. ``args`` is typed ``Any`` rather than ``BaseModel`` so each
# tool can declare its concrete subclass in its function signature
# without tripping mypy's invariance — the registry validates against
# the declared model on dispatch, so the contract is preserved.
ToolExecutor = Callable[[AsyncSession, User, Any], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    """A registered tool. Carries everything needed to:

    - Translate to OpenAI's ``tools`` parameter shape
    - Translate to MCP's ``tools/list`` response shape
    - Validate inbound arguments (via the Pydantic model)
    - Dispatch to the executor
    """

    name: str
    description: str
    args_model: type[BaseModel]
    executor: ToolExecutor
    # ``writes`` is False for every Wave 2 tool. Phase 3 introduces
    # write tools that flip this true, gated behind a per-conversation
    # toggle and the preview / commit pattern.
    writes: bool = False
    # Free-form category used by the admin "available tools" page to
    # group tools — "ipam", "dns", "dhcp", "network", "ops".
    category: str = "ops"
    # ``default_enabled`` controls whether the tool appears in the
    # effective set for a fresh install. Niche tools (TLS chain
    # check, public WHOIS lookups, propose-* writes) ship as False so
    # operators opt in via Settings → AI → Tool Catalog. The default
    # can always be overridden per-platform via
    # ``PlatformSettings.ai_tools_enabled`` and per-provider via
    # ``AIProvider.enabled_tools``.
    default_enabled: bool = True
    # Optional feature-module id (see
    # ``app.services.feature_modules.MODULES``). When set and the
    # operator has disabled that module, the tool is stripped from
    # the registry's effective set regardless of its
    # ``default_enabled`` / per-platform / per-provider state. None
    # means "always available" (the cross-cutting tools — IPAM/DNS/DHCP
    # core lookups, ops helpers).
    module: str | None = None

    def parameters_schema(self) -> dict[str, Any]:
        """JSON Schema for the args. Both OpenAI and MCP consume this
        verbatim. Pydantic emits ``$defs`` for nested models — we
        leave them in place; the model handles them.
        """
        return self.args_model.model_json_schema()

    def to_openai_tool(self) -> dict[str, Any]:
        """OpenAI Chat Completions ``tools`` entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema(),
            },
        }

    def to_mcp_tool(self) -> dict[str, Any]:
        """MCP ``tools/list`` entry. The protocol uses ``inputSchema``
        rather than ``parameters``.
        """
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters_schema(),
        }


class ToolRegistry:
    """Process-wide tool registry. Tools register themselves on
    import via :func:`register_tool` (see ``tools/__init__.py``).
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def read_only(self) -> list[Tool]:
        """The subset safe to expose to read-only contexts (Wave 2)."""
        return [t for t in self.all() if not t.writes]

    async def call(
        self,
        name: str,
        raw_args: dict[str, Any],
        *,
        db: AsyncSession,
        user: User,
        effective: set[str] | None = None,
    ) -> Any:
        """Validate ``raw_args`` against the tool's Pydantic model and
        dispatch. Raises :class:`ToolNotFound` /
        :class:`ToolArgumentError` / :class:`ToolDisabled` on the
        obvious failure modes.

        ``effective`` is the operator's resolved tool set
        (Tool Catalog × per-provider allowlist). When supplied, the
        registry refuses to dispatch tools outside the set so a
        hallucinating LLM can't call something the operator
        explicitly disabled. Pass None for "no gating" — legitimate
        for the MCP HTTP endpoint where the caller has already
        filtered against ``tools/list``.
        """
        tool = self.get(name)
        if tool is None:
            raise ToolNotFound(name)
        if effective is not None and name not in effective:
            raise ToolDisabled(name, scope="platform")
        try:
            args = tool.args_model.model_validate(raw_args or {})
        except Exception as exc:
            raise ToolArgumentError(name, str(exc)) from exc
        return await tool.executor(db, user, args)


# Module-level singleton. Tool modules call ``register_tool(...)`` on
# import to populate it.
REGISTRY = ToolRegistry()


class ToolNotFound(KeyError):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class ToolArgumentError(ValueError):
    def __init__(self, name: str, detail: str) -> None:
        super().__init__(detail)
        self.name = name
        self.detail = detail


def register_tool(
    *,
    name: str,
    description: str,
    args_model: type[BaseModel],
    writes: bool = False,
    category: str = "ops",
    default_enabled: bool = True,
    module: str | None = None,
) -> Callable[[ToolExecutor], ToolExecutor]:
    """Decorator. Use on each tool's executor function.

    Example::

        class ListSpacesArgs(BaseModel):
            search: str | None = None

        @register_tool(
            name="list_ip_spaces",
            description="...",
            args_model=ListSpacesArgs,
            category="ipam",
        )
        async def list_ip_spaces(
            db: AsyncSession, user: User, args: ListSpacesArgs
        ) -> list[dict[str, Any]]:
            ...
    """

    def decorator(fn: ToolExecutor) -> ToolExecutor:
        REGISTRY.register(
            Tool(
                name=name,
                description=description,
                args_model=args_model,
                executor=fn,
                writes=writes,
                category=category,
                default_enabled=default_enabled,
                module=module,
            )
        )
        return fn

    return decorator


# ── Tool resolution ────────────────────────────────────────────────


class ToolDisabled(KeyError):
    """Raised when a tool is registered but disabled in the operator's
    Tool Catalog or per-provider allowlist. The chat orchestrator
    surfaces the failure as a tool-result message so the LLM can
    explain to the user how to enable it."""

    def __init__(self, name: str, scope: str) -> None:
        super().__init__(name)
        self.name = name
        # ``scope`` is "platform" (operator-level disable) or
        # "provider" (per-provider allowlist). Drives the message the
        # user sees.
        self.scope = scope


def _platform_enabled_set(platform_enabled: list[str] | None) -> set[str] | None:
    """Resolve ``PlatformSettings.ai_tools_enabled`` against the
    registry defaults. Returns the explicit set when the setting is
    non-NULL, else None meaning "use registry defaults"."""
    if platform_enabled is None:
        return None
    return set(platform_enabled)


def effective_tool_names(
    *,
    platform_enabled: list[str] | None,
    provider_enabled: list[str] | None,
    enabled_modules: set[str] | None = None,
) -> set[str]:
    """Resolve which tools are enabled for *this* request.

    Layering, narrow-down semantics:

    1. Start with the registry's ``default_enabled=True`` set.
    2. If ``platform_enabled`` is non-NULL, replace step 1 with that
       explicit list (operator's Tool Catalog override).
    3. If ``provider_enabled`` is non-NULL, intersect with it
       (per-provider narrowing for small-context models).
    4. If ``enabled_modules`` is non-NULL, drop every tool whose
       ``module`` id isn't in that set. ``module=None`` is always
       kept. This makes feature-module toggles a hard kill-switch
       over the AI surface — disabling ``network.customer`` removes
       the customer find/count tools regardless of any catalog or
       provider override.

    NULL at any layer means "no override at this layer" — the
    behaviour falls through to the wider layer.
    """
    platform = _platform_enabled_set(platform_enabled)
    if platform is None:
        eligible = {t.name for t in REGISTRY.all() if t.default_enabled and not t.writes}
    else:
        # Operator-explicit list. Filter to tools that actually exist
        # so a renamed / removed tool doesn't break chat — same
        # forward-compat we already do for provider allowlists.
        registered = {t.name for t in REGISTRY.all() if not t.writes}
        eligible = platform & registered
    if provider_enabled is not None:
        eligible &= set(provider_enabled)
    if enabled_modules is not None:
        modules_by_tool = {t.name: t.module for t in REGISTRY.all()}
        eligible = {
            n
            for n in eligible
            if modules_by_tool.get(n) is None or modules_by_tool[n] in enabled_modules
        }
    return eligible
