"""Azure OpenAI driver.

Wraps the official ``openai`` SDK's ``AsyncAzureOpenAI`` client. Most
of the streaming / tool-call delta plumbing is identical to the
:class:`OpenAICompatDriver` — what differs is how the client is
constructed:

* Azure URLs are deployment-scoped: the ``model`` argument in a
  chat-completions request is the **deployment name** the operator
  configured in the Azure portal (which may or may not match the
  underlying base model id, e.g. ``my-prod-gpt4o`` → ``gpt-4o``).
* Azure requires an ``api-version`` query string on every call —
  configurable per-provider via ``options.api_version``.
* ``base_url`` carries the resource endpoint
  (``https://<resource>.openai.azure.com``); the SDK appends the
  deployment + path automatically.

Auth uses the ``api_key`` field (the most common case). Azure AD /
managed-identity flows are out of scope for Phase 2 — operators
needing those can request a follow-up.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from openai import APIConnectionError, APIStatusError, AsyncAzureOpenAI, AuthenticationError

from app.core.crypto import decrypt_str
from app.drivers.llm.base import (
    ChatChunk,
    ChatRequest,
    LLMDriver,
    ModelInfo,
    TestConnectionResult,
    ToolCall,
)

# Azure rolls forward fairly aggressively; pick a recent stable preview
# as the fallback when the operator doesn't specify one. They can pin a
# different version per-provider via ``options.api_version``.
_DEFAULT_API_VERSION = "2024-08-01-preview"


class AzureOpenAIDriver(LLMDriver):
    kind: str = "azure_openai"

    _DEFAULT_TIMEOUT_S: float = 60.0

    def _client(self) -> AsyncAzureOpenAI:
        api_key = ""
        if self.provider.api_key_encrypted:
            api_key = decrypt_str(self.provider.api_key_encrypted)
        endpoint = self.provider.base_url or ""
        if not endpoint:
            raise ValueError(
                "Azure OpenAI provider requires base_url set to the resource endpoint "
                "(e.g. https://my-resource.openai.azure.com)."
            )
        api_version = str(self.provider.options.get("api_version", _DEFAULT_API_VERSION))
        timeout = float(
            self.provider.options.get("request_timeout_seconds", self._DEFAULT_TIMEOUT_S)
        )
        return AsyncAzureOpenAI(
            api_key=api_key or "no-key-required",
            azure_endpoint=endpoint,
            api_version=api_version,
            timeout=timeout,
        )

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
            # On Azure ``model`` is the deployment name the operator
            # picked in the portal — the SDK threads it through the URL.
            "model": request.model,
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
        """Azure exposes a ``/openai/deployments?api-version=...`` route
        rather than the OpenAI-style ``/v1/models``. The SDK's
        ``models.list`` happens to call the right endpoint. Each entry
        is a *deployment* — that's what the operator picks in the chat
        request, not the base model id.
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
                    owned_by=getattr(m, "owned_by", "") or "azure",
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
        ids = sorted([m.id for m in page.data], key=str.lower)
        if not ids:
            return TestConnectionResult(
                ok=False,
                detail=(
                    "Connected, but no deployments are configured on this Azure resource. "
                    "Create a deployment in the Azure portal first, then retry."
                ),
                latency_ms=latency_ms,
            )
        return TestConnectionResult(
            ok=True,
            detail=f"OK — {len(ids)} deployment{'s' if len(ids) != 1 else ''} available",
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
