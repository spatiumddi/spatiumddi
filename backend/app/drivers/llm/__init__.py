"""LLM driver abstraction.

Mirrors ``app.drivers.dns`` and ``app.drivers.dhcp`` — an abstract
base + concrete drivers + a registry. The chat orchestrator and
provider settings router only ever speak to ``LLMDriver``; concrete
drivers (``openai_compat``, future ``anthropic`` / ``google`` /
``azure_openai``) are looked up via :func:`get_driver`.
"""

from app.drivers.llm.base import (
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    LLMDriver,
    ModelInfo,
    TestConnectionResult,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from app.drivers.llm.registry import get_driver, register_driver

__all__ = [
    "ChatChunk",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "LLMDriver",
    "ModelInfo",
    "TestConnectionResult",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
    "get_driver",
    "register_driver",
]
