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
    ) -> Any:
        """Validate ``raw_args`` against the tool's Pydantic model and
        dispatch. Raises :class:`ToolNotFound` / :class:`ToolArgumentError`
        on the obvious failure modes.
        """
        tool = self.get(name)
        if tool is None:
            raise ToolNotFound(name)
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
            )
        )
        return fn

    return decorator
