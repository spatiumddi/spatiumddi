"""LLM driver registry.

Per CLAUDE.md non-negotiable #10, the chat orchestrator obtains a
driver via :func:`get_driver` and speaks only to the abstract
interface. Mirrors ``app.drivers.dhcp.registry``.

Wave 1 ships the ``openai_compat`` driver (covers OpenAI, Ollama,
OpenWebUI, vLLM, LM Studio, llama.cpp server, LocalAI, Together,
Groq, Fireworks). ``anthropic`` / ``google`` / ``azure_openai``
drivers register themselves here in Phase 2.
"""

from __future__ import annotations

from typing import Any

from app.drivers.llm.base import LLMDriver
from app.drivers.llm.openai_compat import OpenAICompatDriver

_DRIVERS: dict[str, type[LLMDriver]] = {
    OpenAICompatDriver.kind: OpenAICompatDriver,
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
