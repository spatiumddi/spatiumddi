"""OpenAI-compatible LLM driver.

One driver, ~90% of the LLM ecosystem. Works against any provider
that implements the OpenAI Chat Completions HTTP surface:

* OpenAI (``https://api.openai.com/v1``)
* Ollama (``http://host:11434/v1``) — note the ``/v1`` suffix
* OpenWebUI (``http://host:8080/api`` or wherever it's mounted)
* vLLM (``http://host:8000/v1``)
* LM Studio (``http://host:1234/v1``)
* llama.cpp server (``http://host:8080/v1``)
* LocalAI (``http://host:8080/v1``)
* Together AI, Groq, Fireworks (their cloud OpenAI-compat endpoints)

We construct the official ``openai`` SDK pointed at ``base_url`` —
that handles streaming SSE parsing, tool-call delta correlation,
and retries for free.

Models that don't support tool calling natively still work for
basic chat — the orchestrator detects "no tool_calls in response"
and surfaces the assistant text directly.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from openai import APIConnectionError, APIStatusError, AsyncOpenAI, AuthenticationError

from app.core.crypto import decrypt_str
from app.drivers.llm.base import (
    ChatChunk,
    ChatRequest,
    LLMDriver,
    ModelInfo,
    TestConnectionResult,
    ToolCall,
)


class OpenAICompatDriver(LLMDriver):
    kind: str = "openai_compat"

    # Default request timeout (seconds). Operators can override via
    # ``AIProvider.options.request_timeout_seconds``.
    _DEFAULT_TIMEOUT_S: float = 60.0

    def _client(self) -> AsyncOpenAI:
        """Build a configured ``AsyncOpenAI`` client.

        Local providers (Ollama, LM Studio) often don't require auth;
        we send a placeholder API key in that case because the SDK
        refuses to construct without one.
        """
        api_key = ""
        if self.provider.api_key_encrypted:
            api_key = decrypt_str(self.provider.api_key_encrypted)
        if not api_key:
            api_key = "no-key-required"  # placeholder for local providers
        timeout = float(
            self.provider.options.get("request_timeout_seconds", self._DEFAULT_TIMEOUT_S)
        )
        return AsyncOpenAI(
            api_key=api_key,
            base_url=self.provider.base_url or None,
            timeout=timeout,
        )

    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        """Stream chat completion chunks.

        Translates the provider-neutral :class:`ChatRequest` into an
        OpenAI-shaped streaming call, then translates each SSE delta
        back into a :class:`ChatChunk`.

        Tool-call deltas arrive fragmented (the model streams the
        function name in one delta and the arguments JSON across
        several more). We reassemble per ``index`` and emit one
        complete :class:`ToolCall` per tool when its delta stream
        terminates (signalled by the ``finish_reason='tool_calls'``).
        """
        client = self._client()
        messages = [self._encode_message(m) for m in request.messages]
        tools = (
            [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
            if request.tools
            else None
        )
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "stream": True,
            # Ask for usage on the final chunk. Some providers ignore
            # this; we fall back to None token counts in that case.
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p
        if request.stop:
            kwargs["stop"] = list(request.stop)

        # Tool-call accumulator keyed by index. The OpenAI streaming
        # spec sends incremental deltas — name typically arrives first,
        # arguments span multiple chunks.
        pending: dict[int, dict[str, str]] = {}

        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            # Final usage chunk — no choices, just usage. Some Ollama
            # versions emit an empty list here; guard accordingly.
            choices = chunk.choices or []
            for choice in choices:
                delta = choice.delta
                if delta is None:
                    continue
                if getattr(delta, "content", None):
                    yield ChatChunk(content_delta=delta.content)
                tool_calls = getattr(delta, "tool_calls", None) or []
                for tc in tool_calls:
                    idx = tc.index
                    slot = pending.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if fn.name:
                            slot["name"] = fn.name
                        if fn.arguments:
                            slot["arguments"] += fn.arguments

                if choice.finish_reason:
                    # Flush any accumulated tool calls before the
                    # terminal chunk.
                    for slot in pending.values():
                        if slot["name"]:
                            yield ChatChunk(
                                tool_call_delta=ToolCall(
                                    id=slot["id"] or slot["name"],
                                    name=slot["name"],
                                    arguments_json=slot["arguments"] or "{}",
                                )
                            )
                    pending.clear()
                    usage = getattr(chunk, "usage", None)
                    yield ChatChunk(
                        finish_reason=choice.finish_reason,
                        prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
                        completion_tokens=(
                            getattr(usage, "completion_tokens", None) if usage else None
                        ),
                    )

    async def list_models(self) -> list[ModelInfo]:
        """Hit ``/v1/models`` (or the provider's compat equivalent)
        and surface what comes back. Local providers (Ollama) list
        only the models the operator has pulled.
        """
        client = self._client()
        out: list[ModelInfo] = []
        try:
            page = await client.models.list()
        except (APIConnectionError, APIStatusError, AuthenticationError):
            return out
        for m in page.data:
            out.append(
                ModelInfo(
                    id=m.id,
                    owned_by=getattr(m, "owned_by", "") or "",
                )
            )
        return sorted(out, key=lambda x: x.id.lower())

    async def test_connection(self) -> TestConnectionResult:
        """Probe the provider with a single ``/v1/models`` call.

        Cheaper + safer than firing a chat completion (no token
        spend, works on every provider). Distinguishes connection
        failures from auth failures from "connected but empty"
        — :meth:`list_models` swallows errors for the UI's benefit,
        so this path calls the SDK directly to surface them.
        """
        started = time.monotonic()
        client = self._client()
        try:
            page = await client.models.list()
        except AuthenticationError as exc:
            return TestConnectionResult(
                ok=False,
                detail=f"auth failed — {exc}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except APIConnectionError as exc:
            return TestConnectionResult(
                ok=False,
                detail=f"connection failed — {exc}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except APIStatusError as exc:
            return TestConnectionResult(
                ok=False,
                detail=f"http {exc.status_code} — {exc.message}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            return TestConnectionResult(
                ok=False,
                detail=f"unexpected error — {type(exc).__name__}: {exc}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        ids = sorted([m.id for m in page.data], key=str.lower)
        if not ids:
            return TestConnectionResult(
                ok=False,
                detail=(
                    "Connected, but the provider returned no models. "
                    "For local providers, pull at least one model first."
                ),
                latency_ms=latency_ms,
            )
        return TestConnectionResult(
            ok=True,
            detail=f"OK — {len(ids)} models available",
            latency_ms=latency_ms,
            sample_models=tuple(ids[:10]),
        )

    @staticmethod
    def _encode_message(m: Any) -> dict[str, Any]:
        """Translate a :class:`ChatMessage` into the OpenAI dict shape."""
        out: dict[str, Any] = {"role": m.role, "content": m.content or ""}
        if m.name:
            out["name"] = m.name
        if m.tool_call_id:
            out["tool_call_id"] = m.tool_call_id
        if m.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": tc.arguments_json,
                    },
                }
                for tc in m.tool_calls
            ]
            # Assistant messages with tool_calls usually have empty
            # content; the OpenAI SDK is happy with either ``""`` or
            # ``None`` but ``None`` confuses some compat servers.
            if not m.content:
                out["content"] = ""
        return out
