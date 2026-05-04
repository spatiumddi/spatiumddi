"""LLM driver registry.

Per CLAUDE.md non-negotiable #10, the chat orchestrator obtains a
driver via :func:`get_driver` and speaks only to the abstract
interface. Mirrors ``app.drivers.dhcp.registry``.

Phase 1 shipped ``openai_compat`` (covers OpenAI, Ollama, OpenWebUI,
vLLM, LM Studio, llama.cpp server, LocalAI, Together, Groq,
Fireworks). Phase 2 adds ``anthropic`` (Claude). ``google`` and
``azure_openai`` drivers will register themselves here in
follow-up commits.
"""

from __future__ import annotations

from typing import Any

from app.drivers.llm.anthropic import AnthropicDriver
from app.drivers.llm.base import LLMDriver
from app.drivers.llm.openai_compat import OpenAICompatDriver

_DRIVERS: dict[str, type[LLMDriver]] = {
    OpenAICompatDriver.kind: OpenAICompatDriver,
    AnthropicDriver.kind: AnthropicDriver,
}


def get_driver(provider: Any) -> LLMDriver:
    """Resolve an :class:`AIProvider` ORM row to a concrete driver."""
    cls = _DRIVERS.get(provider.kind)
    if cls is None:
        raise ValueError(f"Unknown LLM driver kind: {provider.kind!r}")
    return cls(provider)


def register_driver(name: str, driver_cls: type[LLMDriver]) -> None:
    """Register a new driver class. Tests + future Phase 2 drivers
    use this.
    """
    _DRIVERS[name] = driver_cls


def known_kinds() -> list[str]:
    """Discriminator values currently registered. Used by the API to
    validate ``AIProvider.kind`` against the live driver set rather
    than just the DB CHECK constraint.
    """
    return sorted(_DRIVERS.keys())
