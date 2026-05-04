"""Google Gemini driver.

Google ships an OpenAI-compatible REST surface for Gemini at
``https://generativelanguage.googleapis.com/v1beta/openai/`` — chat
completions, tool calling, and streaming all map onto the same shape
the rest of our codebase already speaks. We piggy-back on the
official ``openai`` SDK + reuse the streaming / tool-call delta
plumbing from :class:`OpenAICompatDriver`.

Why a dedicated ``google_gemini`` kind rather than just telling the
operator to pick ``openai_compat`` and paste the Google base URL:

* The base URL is well-known — operators don't have to look it up.
* The model picker lists ``models/gemini-*`` exactly as Google
  returns them (we strip the redundant ``models/`` prefix so the
  picker stays terse).
* Pricing + cost overrides can hang off the discriminator without
  the operator having to special-case rate-sheet entries.

If Google's compat endpoint diverges from the OpenAI spec in a way
that breaks tool calling, swap this for a real
``google-generativeai`` client; the public surface (the LLMDriver
ABC) doesn't change.
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

# Google's OpenAI-compat endpoint. The trailing slash matters — the
# SDK appends ``chat/completions`` etc. directly.
_GOOGLE_COMPAT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _normalise_model_id(model: str) -> str:
    """Strip Google's ``models/`` prefix.

    Google returns ids like ``models/gemini-2.0-flash``. Operators
    type / paste them as plain ``gemini-2.0-flash``. We accept either
    on the way in and strip the prefix on the way out so both spellings
    work in the picker + persisted snapshots.
    """
    return model.removeprefix("models/")


class GoogleGeminiDriver(LLMDriver):
    # Matches the ``ck_ai_provider_kind`` CHECK constraint reserved
    # slot from the original migration. We use ``google`` rather than
    # ``google_gemini`` so we don't have to widen the constraint.
    kind: str = "google"

    _DEFAULT_TIMEOUT_S: float = 60.0

    def _client(self) -> AsyncOpenAI:
        api_key = ""
        if self.provider.api_key_encrypted:
            api_key = decrypt_str(self.provider.api_key_encrypted)
        if not api_key:
            # Google requires an API key. Surface a helpful error
            # rather than letting the SDK 401.
            raise ValueError(
                "Google Gemini provider requires an API key — generate one at "
                "https://aistudio.google.com/apikey"
            )
        # Operators can still override the base URL (e.g. to point at
        # a Vertex AI proxy that speaks the same shape) but the default
        # is the AI-Studio compat endpoint.
        base_url = self.provider.base_url or _GOOGLE_COMPAT_BASE_URL
        timeout = float(
            self.provider.options.get("request_timeout_seconds", self._DEFAULT_TIMEOUT_S)
        )
        return AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
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
            "model": _normalise_model_id(request.model),
            "messages": messages,
            "stream": True,
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

        pending: dict[int, dict[str, str]] = {}

        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
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
        try:
            client = self._client()
        except ValueError:
            return []
        out: list[ModelInfo] = []
        try:
            page = await client.models.list()
        except (APIConnectionError, APIStatusError, AuthenticationError):
            return out
        for m in page.data:
            out.append(
                ModelInfo(
                    id=_normalise_model_id(m.id),
                    owned_by=getattr(m, "owned_by", "") or "google",
                )
            )
        return sorted(out, key=lambda x: x.id.lower())

    async def test_connection(self) -> TestConnectionResult:
        started = time.monotonic()
        try:
            client = self._client()
        except ValueError as exc:
            return TestConnectionResult(ok=False, detail=str(exc), latency_ms=0)
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
        ids = sorted(
            [_normalise_model_id(m.id) for m in page.data],
            key=str.lower,
        )
        if not ids:
            return TestConnectionResult(
                ok=False,
                detail="Connected, but Google returned no models.",
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
            if not m.content:
                out["content"] = ""
        return out
