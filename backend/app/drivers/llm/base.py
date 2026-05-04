"""Abstract LLM driver base class and neutral data structures.

The control-plane LLM driver is a *thin* translator: it takes a list
of provider-neutral chat messages + tool definitions, calls the
upstream provider (OpenAI / Ollama / Anthropic / …), and yields back
a stream of provider-neutral chunks.

CLAUDE.md non-negotiable #10 (driver abstraction) — concrete provider
specifics never leak into the chat orchestrator. The orchestrator
calls ``get_driver(provider.kind)(provider).chat(...)`` and consumes
``ChatChunk`` objects whose shape is identical regardless of the
underlying SDK.

The neutral shapes intentionally mirror the OpenAI Chat Completions
schema (the de-facto industry standard) — Anthropic / Gemini drivers
translate inbound + outbound at the SDK boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

# ── Neutral request / response data shapes ─────────────────────────────

ChatRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ChatMessage:
    """One turn in a conversation.

    ``role`` mirrors the OpenAI chat schema. When ``role='tool'``,
    ``tool_call_id`` carries the matching call id so the provider can
    correlate tool results with prior assistant tool-call deltas.
    """

    role: ChatRole
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    # Present on assistant messages that requested tool execution.
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ToolDefinition:
    """A tool the LLM may call. Schema follows the OpenAI function spec
    (JSON Schema for parameters). Anthropic / Gemini drivers translate
    at the SDK boundary.
    """

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the args


@dataclass(frozen=True)
class ToolCall:
    """An assistant's request to invoke a named tool with arguments."""

    id: str
    name: str
    arguments_json: str  # raw JSON string as produced by the model


@dataclass(frozen=True)
class ToolResult:
    """The dispatch outcome of a single tool call. Fed back as a
    ``ChatMessage(role='tool')`` on the next turn.
    """

    tool_call_id: str
    content: str  # JSON-stringified result; the model treats this as text


@dataclass(frozen=True)
class ChatRequest:
    """Inbound chat request. ``stream`` is honoured by all drivers —
    non-streaming providers buffer internally and emit one final chunk.
    """

    messages: Sequence[ChatMessage]
    model: str
    tools: Sequence[ToolDefinition] = ()
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: Sequence[str] | None = None
    # Driver-specific knobs surface here when the operator sets them on
    # ``AIProvider.options``. Drivers consume what they recognize.
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatChunk:
    """One streamed delta. Either content text, a tool-call delta, or
    a final-usage chunk when the response is complete.

    Drivers emit chunks in roughly this order:
    1. zero or more ``content_delta`` chunks
    2. zero or more ``tool_call_delta`` chunks (one per tool call,
       fragments concatenated by id)
    3. exactly one ``finish`` chunk at the end carrying ``usage``
    """

    content_delta: str = ""
    tool_call_delta: ToolCall | None = None
    finish_reason: str | None = None  # "stop" | "length" | "tool_calls" | …
    # Token usage. Populated only on the final chunk.
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True)
class ChatResponse:
    """Buffered, non-streaming convenience wrapper. Useful for tests
    and for drivers that return a complete response in one call.
    """

    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True)
class ModelInfo:
    """One model entry as returned by :meth:`LLMDriver.list_models`."""

    id: str
    # Free-form vendor label — "OpenAI", "Ollama", etc. — used as a
    # group header in the model picker UI.
    owned_by: str = ""
    # Some providers report context-window size. None when unknown.
    context_window: int | None = None


@dataclass(frozen=True)
class TestConnectionResult:
    """Outcome of :meth:`LLMDriver.test_connection`."""

    ok: bool
    detail: str  # human-readable success message or error string
    latency_ms: int | None = None
    # Surface a sample of available models on success — lets the UI
    # populate the model picker without a follow-up call.
    sample_models: tuple[str, ...] = ()


# ── Driver ABC ─────────────────────────────────────────────────────────


class LLMDriver(ABC):
    """Concrete drivers (``OpenAICompatDriver``, future ``AnthropicDriver``
    / ``GoogleDriver`` / ``AzureOpenAIDriver``) subclass this and implement
    the three abstract methods.

    Construction takes the ``AIProvider`` ORM row so the driver can
    pull base_url / decrypted api_key / options without the caller
    threading them through every call site.
    """

    # ``kind`` value the registry maps to this driver. Subclasses must
    # set this; the registry uses it to build the kind → driver map.
    kind: str = ""

    def __init__(self, provider: Any) -> None:
        self.provider = provider

    @abstractmethod
    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        """Stream chat completion chunks for the given request.

        Always returns an async iterator — non-streaming providers
        buffer internally and emit a single final chunk.
        """
        ...
        # The ``yield`` below is unreachable — it exists so static
        # analyzers see this method as a true async generator.
        yield ChatChunk()  # type: ignore[unreachable]

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]:
        """Return models the configured provider exposes. Used to
        populate the model picker in the admin UI.
        """
        raise NotImplementedError

    @abstractmethod
    async def test_connection(self) -> TestConnectionResult:
        """Probe the provider with the smallest possible request that
        validates auth + reachability. Used by the "Test" button in
        the admin UI before saving a provider config.
        """
        raise NotImplementedError
