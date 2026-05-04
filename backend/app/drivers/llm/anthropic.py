"""Anthropic (Claude) LLM driver.

Translates the provider-neutral ``ChatRequest`` / ``ChatChunk`` shapes
defined in ``app.drivers.llm.base`` into Anthropic's Messages API
schema, which differs from OpenAI's in several material ways:

* **System prompt** is a top-level field (``system=``) on the request,
  not a ``role=system`` message in the messages list.
* **Tool calling** uses ``input_schema`` not ``parameters``, and tool
  results come back as a structured ``tool_use`` content block rather
  than a JSON-string ``arguments`` field.
* **Streaming events** are typed (``message_start`` / ``content_block_start``
  / ``content_block_delta`` / ``content_block_stop`` / ``message_delta``
  / ``message_stop``) rather than the OpenAI delta union.
* **Tool results** are returned as ``role=user`` messages with
  ``content=[{"type":"tool_result","tool_use_id":...}]`` rather than
  ``role=tool`` messages.

The chat orchestrator stays neutral — this driver hides the
translation cost. Models supporting prompt caching (``cache_control``
hints) are out of scope for v1; add later if cost becomes an issue.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from anthropic import (
    APIConnectionError,
    APIStatusError,
    AsyncAnthropic,
    AuthenticationError,
)

from app.core.crypto import decrypt_str
from app.drivers.llm.base import (
    ChatChunk,
    ChatRequest,
    LLMDriver,
    ModelInfo,
    TestConnectionResult,
    ToolCall,
)


class AnthropicDriver(LLMDriver):
    kind: str = "anthropic"

    _DEFAULT_TIMEOUT_S: float = 60.0
    # Anthropic's Messages API requires ``max_tokens`` on every call.
    # If the operator hasn't set one in provider options, fall back to
    # this default — generous enough that most operator queries fit.
    _DEFAULT_MAX_TOKENS: int = 2048

    def _client(self) -> AsyncAnthropic:
        api_key = ""
        if self.provider.api_key_encrypted:
            api_key = decrypt_str(self.provider.api_key_encrypted)
        if not api_key:
            # Anthropic always requires a key. Raise a plain ValueError —
            # caller (test_connection / chat) translates to a graceful
            # error message. Can't construct the SDK's AuthenticationError
            # synthetically — it requires response + body kwargs.
            raise ValueError(
                "Anthropic provider has no API key configured — set one " "in /admin/ai/providers."
            )
        timeout = float(
            self.provider.options.get("request_timeout_seconds", self._DEFAULT_TIMEOUT_S)
        )
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        # Operator-overridable base URL — useful for self-hosted
        # Anthropic-compat gateways (Bedrock-via-proxy, etc.). Empty
        # string falls through to the SDK's default.
        if self.provider.base_url:
            kwargs["base_url"] = self.provider.base_url
        return AsyncAnthropic(**kwargs)

    @staticmethod
    def _split_system(messages: list[Any]) -> tuple[str, list[dict[str, Any]]]:
        """Anthropic wants ``system`` as a top-level field, not a
        ``role=system`` message. Concatenate every system message and
        return it alongside the remaining message list.
        """
        system_parts: list[str] = []
        rest: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                if m.content:
                    system_parts.append(m.content)
                continue
            rest.append(m)
        return "\n\n".join(system_parts), rest

    @staticmethod
    def _encode_messages(rest: list[Any]) -> list[dict[str, Any]]:
        """Translate the post-system message list into Anthropic's
        request shape. Tool calls / tool results require a structured
        ``content`` array — bare-string content is fine for plain
        user / assistant messages.
        """
        out: list[dict[str, Any]] = []
        for m in rest:
            if m.role == "tool":
                # Anthropic returns tool results as a user message with a
                # structured content block, NOT a role=tool message.
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id or "",
                                "content": m.content or "",
                            }
                        ],
                    }
                )
                continue
            if m.role == "assistant" and m.tool_calls:
                # Assistant message that requested tools. Mix any
                # leading text content with the tool_use blocks.
                content: list[dict[str, Any]] = []
                if m.content:
                    content.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    try:
                        args = json.loads(tc.arguments_json or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": args,
                        }
                    )
                out.append({"role": "assistant", "content": content})
                continue
            # Plain user / assistant — string content is fine.
            out.append({"role": m.role, "content": m.content or ""})
        return out

    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        """Stream chat completion chunks from Anthropic's Messages API.

        Anthropic's streaming events are typed; we translate each one
        into a neutral ``ChatChunk``. Tool-use blocks accumulate their
        JSON ``input`` across multiple ``input_json_delta`` events, then
        emit one consolidated ``ToolCall`` when the block closes.
        """
        client = self._client()
        system, rest = self._split_system(list(request.messages))
        messages = self._encode_messages(rest)

        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens or self._DEFAULT_MAX_TOKENS,
        }
        if system:
            kwargs["system"] = system
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p
        if request.stop:
            kwargs["stop_sequences"] = list(request.stop)
        if request.tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in request.tools
            ]

        # Tool-use accumulator keyed by content-block index. Anthropic
        # streams ``input_json_delta`` fragments that we concatenate
        # before emitting the consolidated tool call.
        pending: dict[int, dict[str, Any]] = {}
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        finish_reason: str | None = None

        async with client.messages.stream(**kwargs) as stream:
            async for event in stream:
                etype = getattr(event, "type", "")

                if etype == "message_start":
                    msg = getattr(event, "message", None)
                    if msg is not None:
                        usage = getattr(msg, "usage", None)
                        if usage is not None:
                            prompt_tokens = getattr(usage, "input_tokens", None)
                            completion_tokens = getattr(usage, "output_tokens", None)

                elif etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    idx = getattr(event, "index", 0)
                    if block is not None and getattr(block, "type", "") == "tool_use":
                        pending[idx] = {
                            "id": getattr(block, "id", "") or "",
                            "name": getattr(block, "name", "") or "",
                            "arguments": "",
                        }

                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if delta is None:
                        continue
                    dtype = getattr(delta, "type", "")
                    if dtype == "text_delta":
                        text = getattr(delta, "text", "") or ""
                        if text:
                            yield ChatChunk(content_delta=text)
                    elif dtype == "input_json_delta":
                        idx = getattr(event, "index", 0)
                        slot = pending.get(idx)
                        if slot is not None:
                            slot["arguments"] += getattr(delta, "partial_json", "")

                elif etype == "content_block_stop":
                    idx = getattr(event, "index", 0)
                    slot = pending.pop(idx, None)
                    if slot is not None and slot["name"]:
                        yield ChatChunk(
                            tool_call_delta=ToolCall(
                                id=slot["id"] or slot["name"],
                                name=slot["name"],
                                arguments_json=slot["arguments"] or "{}",
                            )
                        )

                elif etype == "message_delta":
                    delta = getattr(event, "delta", None)
                    if delta is not None:
                        # Anthropic's ``stop_reason`` mirrors finish_reason —
                        # ``end_turn`` / ``tool_use`` / ``max_tokens`` /
                        # ``stop_sequence``. Map ``tool_use`` to OpenAI-shaped
                        # "tool_calls" so the orchestrator's existing branch works.
                        sr = getattr(delta, "stop_reason", None)
                        if sr == "tool_use":
                            finish_reason = "tool_calls"
                        elif sr == "end_turn":
                            finish_reason = "stop"
                        elif sr == "max_tokens":
                            finish_reason = "length"
                        elif sr is not None:
                            finish_reason = sr
                    usage = getattr(event, "usage", None)
                    if usage is not None:
                        # ``message_delta`` carries a final usage block on
                        # the closing event with output_tokens populated.
                        out_t = getattr(usage, "output_tokens", None)
                        if out_t is not None:
                            completion_tokens = out_t

                elif etype == "message_stop":
                    # Final event — flush any accumulated tool calls
                    # whose stop event we somehow missed (defensive),
                    # then emit the finish chunk.
                    for slot in list(pending.values()):
                        if slot["name"]:
                            yield ChatChunk(
                                tool_call_delta=ToolCall(
                                    id=slot["id"] or slot["name"],
                                    name=slot["name"],
                                    arguments_json=slot["arguments"] or "{}",
                                )
                            )
                    pending.clear()
                    yield ChatChunk(
                        finish_reason=finish_reason or "stop",
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )

    async def list_models(self) -> list[ModelInfo]:
        """Anthropic's ``models.list`` was added in 2025. Returns the
        operator-accessible model catalog. We fail gracefully on older
        SDK versions / endpoints that don't have it.
        """
        client = self._client()
        out: list[ModelInfo] = []
        try:
            page = await client.models.list(limit=100)
        except (APIConnectionError, APIStatusError, AuthenticationError):
            return out
        except AttributeError:
            # Old SDK without models.list — surface a hardcoded
            # "best-effort" set so the picker isn't empty.
            return [
                ModelInfo(id=name, owned_by="Anthropic")
                for name in (
                    "claude-opus-4-7",
                    "claude-sonnet-4-6",
                    "claude-haiku-4-5",
                    "claude-3-5-sonnet-latest",
                    "claude-3-5-haiku-latest",
                    "claude-3-opus-latest",
                    "claude-3-haiku-20240307",
                )
            ]
        for m in getattr(page, "data", []) or []:
            mid = getattr(m, "id", None)
            if not mid:
                continue
            out.append(ModelInfo(id=mid, owned_by="Anthropic"))
        return sorted(out, key=lambda x: x.id.lower())

    async def test_connection(self) -> TestConnectionResult:
        """Probe Anthropic with a single ``models.list`` call. Same
        cost / safety profile as the OpenAI-compat probe — no chat
        completion, no token spend.

        Talks to the SDK directly (instead of going through
        :meth:`list_models`) so connection / auth / status errors
        surface distinctly rather than getting swallowed as "no
        models" — same fix as on the OpenAI-compat driver.
        """
        started = time.monotonic()
        try:
            client = self._client()
        except ValueError as exc:
            return TestConnectionResult(
                ok=False,
                detail=str(exc),
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        try:
            page = await client.models.list(limit=20)
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
        except AttributeError:
            # Old SDK without ``models.list``. Fall back to a chat probe
            # (the cheapest possible — 1 token) just to verify auth.
            try:
                await client.messages.create(
                    model=self.provider.default_model or "claude-3-5-haiku-latest",
                    max_tokens=1,
                    messages=[{"role": "user", "content": "."}],
                )
            except AuthenticationError as exc:
                return TestConnectionResult(
                    ok=False,
                    detail=f"auth failed — {exc}",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            except Exception as exc:  # noqa: BLE001
                return TestConnectionResult(
                    ok=False,
                    detail=f"unexpected error — {type(exc).__name__}: {exc}",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            return TestConnectionResult(
                ok=True,
                detail="OK (auth verified via chat probe; old SDK has no models.list)",
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
            [getattr(m, "id", "") for m in getattr(page, "data", []) or []],
            key=str.lower,
        )
        ids = [i for i in ids if i]
        if not ids:
            return TestConnectionResult(
                ok=False,
                detail=(
                    "Connected, but the Anthropic API returned no models. "
                    "Check your API key has access to at least one model."
                ),
                latency_ms=latency_ms,
            )
        return TestConnectionResult(
            ok=True,
            detail=f"OK — {len(ids)} models available",
            latency_ms=latency_ms,
            sample_models=tuple(ids[:10]),
        )
