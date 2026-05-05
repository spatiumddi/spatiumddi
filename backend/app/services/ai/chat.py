"""Operator Copilot chat orchestrator (issue #90 Wave 3).

Runs the tool-call loop on the backend, hiding multi-round
tool-calling behind a single streamed response so the frontend
just sees text tokens + tool-call cards arrive in order.

Flow per inbound user message::

    1. Persist user message.
    2. Build messages = [system, …history…, user].
    3. driver.chat(messages, tools=registry.read_only()) → async iter.
    4. Buffer chunks:
        - content_delta → emit to caller, accumulate to assistant content.
        - tool_call_delta → buffer tool calls until finish_reason
          arrives; persist + dispatch each one.
        - finish_reason="tool_calls" → loop back to step 3 with the
          tool result messages appended (cap at MAX_TOOL_ROUNDS to
          prevent runaway loops).
        - finish_reason="stop"|"length" → persist final assistant
          message, emit ``done``, exit.

The orchestrator is the canonical interface for chat — both the
streaming HTTP endpoint (Wave 3) and any non-streaming test path
go through ``ChatOrchestrator``. Token / cost accounting happens
here so it's centralised for Wave 4 to extend.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.drivers.llm import get_driver
from app.drivers.llm.base import (
    ChatMessage,
    ChatRequest,
    ToolCall,
    ToolDefinition,
)
from app.models.ai import AIChatMessage, AIChatSession, AIProvider
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.dhcp import DHCPScope, DHCPServerGroup
from app.models.dns import DNSRecord, DNSServerGroup, DNSZone
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.network import NetworkDevice
from app.models.settings import PlatformSettings
from app.services.ai.pricing import compute_cost
from app.services.ai.tools import REGISTRY, ToolArgumentError, ToolNotFound

logger = structlog.get_logger(__name__)

# Cap on tool-call iterations per user turn. Five rounds is plenty
# for the read-only tool surface — most queries resolve in 1-2.
MAX_TOOL_ROUNDS: int = 5

# Hard cap on total chat history length sent to the model. Beyond
# this we drop oldest messages (preserving the system message). Stops
# token cost from running away on long sessions.
MAX_HISTORY_MESSAGES: int = 40


def _is_transient_failure(exc: BaseException) -> bool:
    """Decide whether a driver error is worth retrying on a fallback
    provider. Transient = network hiccup, server-side 5xx, rate limit.
    Configuration errors (auth, schema) are NOT retried — they need
    operator action and surface untouched.

    The match is on the SDK exception class name rather than ``isinstance``
    so we can recognise both OpenAI- and Anthropic-flavoured errors
    without import-time coupling between drivers.
    """
    name = type(exc).__name__
    transient_class_names = {
        # OpenAI + Anthropic share these names.
        "APIConnectionError",
        "APITimeoutError",
        "APIConnectionTimeoutError",
        "APIResponseValidationError",
        "InternalServerError",
        "ServiceUnavailableError",
        "RateLimitError",
        # Generic asyncio / httpx
        "TimeoutError",
        "ReadTimeout",
        "ConnectError",
        "ConnectTimeout",
    }
    if name in transient_class_names:
        return True
    # APIStatusError covers any non-2xx — only treat 5xx + 429 as transient.
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and (status_code >= 500 or status_code == 429):
        return True
    return False


# ── Streaming events ──────────────────────────────────────────────────


@dataclass(frozen=True)
class StreamEvent:
    """One event yielded by :meth:`ChatOrchestrator.stream_turn`. The
    HTTP endpoint translates these to SSE frames; tests can consume
    them directly.
    """

    kind: str  # "session" | "content" | "tool_call" | "tool_result" | "done" | "error"
    data: dict[str, Any] = field(default_factory=dict)


# ── System prompt ─────────────────────────────────────────────────────


_STATIC_SYSTEM_PROMPT = """\
You are SpatiumDDI's Operator Copilot — a helpful assistant for a \
network operator running an IPAM / DNS / DHCP control plane. The user \
is an authenticated admin or department admin; everything they can \
see in the UI you can answer about.

Use the provided tools to answer factual questions about the operator's \
infrastructure. Prefer multiple small tool calls over one big one — \
filters keep results readable. Don't fabricate data: if a tool returns \
no results, say so and suggest a different filter.

When showing structured data, prefer concise tables or bullet lists. \
Use Markdown formatting. Keep responses short and direct — the operator \
is at a terminal, not reading a report.

Most tools are read-only. A small set of ``propose_*`` write tools \
exists for resource creation: those NEVER mutate state directly — \
they return a ``kind="proposal"`` payload that the UI surfaces as an \
Apply / Discard card the operator must click. Always go through a \
``propose_*`` tool when the operator asks you to create / modify / \
delete something. If no propose_* tool covers the request, explain \
which UI page handles it instead.
"""


# Recent-activity window — how far back the dynamic context should
# look when building the "what changed lately" snapshot. Small enough
# that the data is genuinely *recent* (not noise from days ago);
# wide enough that quiet shops still surface something useful.
_RECENT_ACTIVITY_WINDOW = timedelta(hours=24)
_RECENT_ACTIVITY_LIMIT = 5


async def gather_dynamic_context(db: AsyncSession, user: User) -> dict[str, Any]:
    """Collect the topology counts + recent activity that flesh out the
    system prompt. Pure read-only — runs on session creation only, so
    we trade a few count queries for a markedly more useful first turn.

    Counts are global (the operator is admin in v1); when group-scoped
    visibility lands we'll filter these by ``user``'s permission set.
    Soft-deleted rows are excluded — those don't exist for the operator.
    """

    async def _count(stmt: Any) -> int:
        return int((await db.execute(stmt)).scalar_one() or 0)

    spaces = await _count(
        select(func.count()).select_from(IPSpace).where(IPSpace.deleted_at.is_(None))
    )
    blocks = await _count(
        select(func.count()).select_from(IPBlock).where(IPBlock.deleted_at.is_(None))
    )
    subnets = await _count(
        select(func.count()).select_from(Subnet).where(Subnet.deleted_at.is_(None))
    )
    addresses = await _count(select(func.count()).select_from(IPAddress))
    dns_groups = await _count(select(func.count()).select_from(DNSServerGroup))
    dns_zones = await _count(
        select(func.count()).select_from(DNSZone).where(DNSZone.deleted_at.is_(None))
    )
    dns_records = await _count(
        select(func.count()).select_from(DNSRecord).where(DNSRecord.deleted_at.is_(None))
    )
    dhcp_groups = await _count(select(func.count()).select_from(DHCPServerGroup))
    dhcp_scopes = await _count(
        select(func.count()).select_from(DHCPScope).where(DHCPScope.deleted_at.is_(None))
    )
    devices = await _count(select(func.count()).select_from(NetworkDevice))

    cutoff = datetime.utcnow() - _RECENT_ACTIVITY_WINDOW
    recent_audits = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.timestamp >= cutoff)
                .order_by(desc(AuditLog.timestamp))
                .limit(_RECENT_ACTIVITY_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    return {
        "topology": {
            "spaces": spaces,
            "blocks": blocks,
            "subnets": subnets,
            "addresses": addresses,
            "dns_groups": dns_groups,
            "dns_zones": dns_zones,
            "dns_records": dns_records,
            "dhcp_groups": dhcp_groups,
            "dhcp_scopes": dhcp_scopes,
            "devices": devices,
        },
        "recent_activity": [
            {
                "timestamp": ev.timestamp.isoformat() if ev.timestamp else None,
                "action": ev.action,
                "resource_type": ev.resource_type,
                "resource_display": ev.resource_display,
                "user": ev.user_display_name,
                "result": ev.result,
            }
            for ev in recent_audits
        ],
    }


def _format_topology_line(topology: dict[str, int]) -> str:
    """Render the topology counts as a single readable line. Skip
    zero-valued resource families so the prompt isn't padded with
    "0 DNS zones · 0 DHCP scopes" on a fresh install.
    """
    parts: list[tuple[str, int]] = [
        ("space", topology["spaces"]),
        ("block", topology["blocks"]),
        ("subnet", topology["subnets"]),
        ("IP", topology["addresses"]),
        ("DNS group", topology["dns_groups"]),
        ("DNS zone", topology["dns_zones"]),
        ("DNS record", topology["dns_records"]),
        ("DHCP group", topology["dhcp_groups"]),
        ("DHCP scope", topology["dhcp_scopes"]),
        ("network device", topology["devices"]),
    ]
    pieces: list[str] = []
    for label, n in parts:
        if n == 0:
            continue
        # English plural — close enough for prompt cosmetics.
        plural = label + ("s" if n != 1 and not label.endswith("s") else "")
        pieces.append(f"{n} {plural}")
    return ", ".join(pieces) if pieces else "no resources tracked yet"


def _format_recent_activity(events: list[dict[str, Any]]) -> str:
    """Render the last few audit events as bullet lines. Capped at
    ``_RECENT_ACTIVITY_LIMIT``; if the deployment is quiet, we say so.
    """
    if not events:
        return "No audit activity in the last 24 h."
    lines: list[str] = []
    for ev in events:
        ts = ev.get("timestamp", "")[:19].replace("T", " ")  # YYYY-MM-DD HH:MM:SS
        lines.append(
            f"- {ts} · {ev.get('user') or 'system'} → "
            f"{ev.get('action')} {ev.get('resource_type')} "
            f"({ev.get('resource_display') or '?'})"
            + (f" [{ev.get('result')}]" if ev.get("result") not in (None, "success") else "")
        )
    return "\n".join(lines)


async def build_system_prompt(db: AsyncSession, user: User, tools: list[Any]) -> str:
    """Build the system prompt for a new session. Snapshotted onto
    ``AIChatSession.system_prompt`` so the conversation stays coherent
    if the global prompt later changes — every later turn replays from
    the snapshot, not from a fresh build.

    The dynamic block carries:
        * Who: username + display name.
        * When: today's UTC date.
        * What: live topology counts (skipping zeros).
        * What changed: last few audit events.
    Total payload is well under 500 tokens for a typical deployment;
    quiet installs come in under 200.
    """
    today = datetime.utcnow().strftime("%Y-%m-%d")
    ctx = await gather_dynamic_context(db, user)
    topology_line = _format_topology_line(ctx["topology"])
    activity_block = _format_recent_activity(ctx["recent_activity"])
    dynamic = (
        f"\n\n---\nContext for this session:\n"
        f"- Operator: {user.username!r} (display name {user.display_name!r}).\n"
        f"- Today (UTC): {today}.\n"
        f"- Tools available: {len(tools)}.\n"
        f"- Topology: {topology_line}.\n"
        f"\nRecent activity (last 24 h):\n"
        f"{activity_block}\n---"
    )
    return _STATIC_SYSTEM_PROMPT + dynamic


# ── Orchestrator ──────────────────────────────────────────────────────


class ChatOrchestrator:
    """Process-wide stateless orchestrator. One instance per request
    — holds DB session, user, target session row.
    """

    def __init__(self, db: AsyncSession, user: User) -> None:
        self.db = db
        self.user = user

    async def get_or_create_session(
        self,
        *,
        session_id: str | None,
        provider: AIProvider,
        model: str,
        initial_context: str | None = None,
    ) -> AIChatSession:
        if session_id:
            row = await self.db.get(AIChatSession, session_id)
            if row is None or row.user_id != self.user.id:
                raise PermissionError("session not found or not yours")
            return row
        tools = REGISTRY.read_only()
        system_prompt = await build_system_prompt(self.db, self.user, tools)
        # "Ask AI about this" — operator clicked a context affordance
        # in the IPAM / DNS / DHCP UI; the frontend supplied a
        # human-readable summary of what they were looking at. Append
        # it to the system prompt so the model can answer questions
        # without the operator having to restate the context. Wrapping
        # in delimiters helps the model treat it as factual reference.
        if initial_context:
            system_prompt += (
                "\n\n---\nThe operator started this chat from a specific "
                "resource. Use this as background; their question may or "
                "may not be about it.\n"
                f"{initial_context.strip()}\n---"
            )
        session = AIChatSession(
            user_id=self.user.id,
            name="Untitled",
            provider_id=provider.id,
            model=model,
            system_prompt=system_prompt,
        )
        self.db.add(session)
        await self.db.flush()
        # Persist the system message — it's the first row in the chat
        # and the rendering layer needs it to be present in history.
        self.db.add(
            AIChatMessage(
                session_id=session.id,
                role="system",
                content=session.system_prompt,
            )
        )
        await self.db.flush()
        return session

    async def _load_history(self, session: AIChatSession) -> list[ChatMessage]:
        """Load message history as a list of neutral ``ChatMessage``s
        ready to feed back to the driver.
        """
        rows = (
            (
                await self.db.execute(
                    select(AIChatMessage)
                    .where(AIChatMessage.session_id == session.id)
                    .order_by(AIChatMessage.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        msgs: list[ChatMessage] = []
        for r in rows:
            tool_calls: tuple[ToolCall, ...] = ()
            if r.tool_calls:
                tool_calls = tuple(
                    ToolCall(
                        id=tc.get("id", ""),
                        name=tc.get("name", ""),
                        arguments_json=tc.get("arguments", "{}"),
                    )
                    for tc in r.tool_calls
                )
            msgs.append(
                ChatMessage(
                    role=r.role,  # type: ignore[arg-type]
                    content=r.content,
                    name=r.name,
                    tool_call_id=r.tool_call_id,
                    tool_calls=tool_calls,
                )
            )
        # Hard cap — drop oldest non-system rows if too long.
        if len(msgs) > MAX_HISTORY_MESSAGES:
            head = [m for m in msgs if m.role == "system"][:1]
            tail = msgs[-(MAX_HISTORY_MESSAGES - len(head)) :]
            msgs = head + [m for m in tail if m.role != "system"]
        return msgs

    @staticmethod
    def _tools_for_request() -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name=t.name,
                description=t.description,
                parameters=t.parameters_schema(),
            )
            for t in REGISTRY.read_only()
        ]

    async def _build_fallback_chain(self, primary: AIProvider) -> list[AIProvider]:
        """Ordered list of providers to try this turn — primary first,
        then every other enabled provider in priority order.

        Falling back across kinds (e.g. Anthropic → Ollama) is the
        intended use case: keep operators productive when their cloud
        provider has an outage or rate-limit blip. The fallback uses
        the fallback's own ``default_model`` since the primary's model
        name probably doesn't exist on the fallback.
        """
        rows = (
            (
                await self.db.execute(
                    select(AIProvider)
                    .where(AIProvider.is_enabled.is_(True))
                    .where(AIProvider.id != primary.id)
                    .order_by(AIProvider.priority.asc(), AIProvider.name.asc())
                )
            )
            .scalars()
            .all()
        )
        return [primary, *rows]

    async def stream_turn(
        self,
        *,
        session: AIChatSession,
        user_text: str,
    ) -> AsyncIterator[StreamEvent]:
        """Persist the new user message, run the tool-call loop,
        stream events to the caller. Final event is always either
        ``done`` or ``error``.
        """
        # Persist the user message immediately so it shows up in
        # history even if the LLM call later fails.
        self.db.add(
            AIChatMessage(
                session_id=session.id,
                role="user",
                content=user_text,
            )
        )
        # Auto-name new sessions from the first user message.
        if session.name == "Untitled":
            session.name = user_text[:80] + ("…" if len(user_text) > 80 else "")
        await self.db.commit()
        yield StreamEvent("session", {"session_id": str(session.id), "name": session.name})

        # Load provider + driver
        provider = (
            await self.db.get(AIProvider, session.provider_id) if session.provider_id else None
        )
        if provider is None or not provider.is_enabled:
            yield StreamEvent(
                "error",
                {"message": "session's AI provider is missing or disabled"},
            )
            return

        # Failover chain (Phase 2). The session's snapshotted provider
        # is tried first; on a transient failure (network / 5xx /
        # rate-limit) we fall back to the next-lowest-priority enabled
        # provider with its own ``default_model``. 4xx auth / validation
        # errors are NOT retried — they're configuration bugs the
        # operator needs to see, not transient infrastructure flaps.
        # The snapshot stays untouched; the next turn tries primary again.
        fallback_chain = await self._build_fallback_chain(provider)
        tools = self._tools_for_request()

        # Tool-call loop
        for round_idx in range(MAX_TOOL_ROUNDS):
            messages = await self._load_history(session)
            attempt_idx = 0
            chunks_yielded = False
            assistant_buf = ""
            tool_calls: list[ToolCall] = []
            finish_reason: str | None = None
            prompt_tokens: int | None = None
            completion_tokens: int | None = None
            started = time.monotonic()
            current_provider = provider
            current_model = session.model

            while True:
                # Fresh request bound to whatever provider/model we're
                # currently attempting (might be a fallback).
                request = ChatRequest(
                    messages=messages,
                    model=current_model,
                    tools=tools,
                    temperature=float(current_provider.options.get("temperature", 0.3)),
                    max_tokens=int(current_provider.options.get("max_tokens", 2048)),
                )
                driver = get_driver(current_provider)
                try:
                    async for chunk in driver.chat(request):
                        if chunk.content_delta:
                            assistant_buf += chunk.content_delta
                            chunks_yielded = True
                            yield StreamEvent(
                                "content",
                                {"delta": chunk.content_delta},
                            )
                        if chunk.tool_call_delta is not None:
                            tool_calls.append(chunk.tool_call_delta)
                            chunks_yielded = True
                        if chunk.finish_reason:
                            finish_reason = chunk.finish_reason
                            prompt_tokens = chunk.prompt_tokens
                            completion_tokens = chunk.completion_tokens
                    # Stream completed cleanly — break out of the
                    # failover ``while True`` to persist + decide next.
                    break
                except Exception as exc:  # noqa: BLE001
                    if not _is_transient_failure(exc) or chunks_yielded:
                        # Either a configuration error (auth, schema)
                        # or we've already streamed partial output —
                        # don't risk a duplicated/contradictory reply.
                        logger.exception(
                            "ai_chat_driver_error",
                            session_id=str(session.id),
                            provider=current_provider.name,
                        )
                        yield StreamEvent(
                            "error",
                            {"message": f"{type(exc).__name__}: {exc}"},
                        )
                        return
                    attempt_idx += 1
                    if attempt_idx >= len(fallback_chain):
                        # Out of fallbacks. Surface the last error.
                        logger.exception(
                            "ai_chat_failover_exhausted",
                            session_id=str(session.id),
                            tried=[p.name for p in fallback_chain],
                        )
                        yield StreamEvent(
                            "error",
                            {
                                "message": (
                                    f"all {len(fallback_chain)} provider(s) "
                                    f"failed; last error: {type(exc).__name__}: {exc}"
                                )
                            },
                        )
                        return
                    next_provider = fallback_chain[attempt_idx]
                    next_model = next_provider.default_model or current_model
                    logger.warning(
                        "ai_chat_failover",
                        session_id=str(session.id),
                        from_provider=current_provider.name,
                        to_provider=next_provider.name,
                        to_model=next_model,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                    yield StreamEvent(
                        "info",
                        {
                            "kind": "failover",
                            "from_provider": current_provider.name,
                            "to_provider": next_provider.name,
                            "to_model": next_model,
                            "reason": f"{type(exc).__name__}",
                        },
                    )
                    current_provider = next_provider
                    current_model = next_model
                    # Reset attempt-local state and retry the round.
                    assistant_buf = ""
                    tool_calls = []
                    finish_reason = None
                    prompt_tokens = None
                    completion_tokens = None
                    started = time.monotonic()

            elapsed_ms = int((time.monotonic() - started) * 1000)

            # Compute cost via the rate sheet (Wave 4) using whichever
            # model actually served the request — could be the
            # primary or, if we failed over, the fallback's
            # ``default_model``. Returns None when the model isn't
            # recognised (local LLMs / custom hosts).
            settings = await self.db.scalar(select(PlatformSettings))
            overrides = (settings.ai_pricing_overrides if settings else None) or None
            cost_usd = compute_cost(current_model, prompt_tokens, completion_tokens, overrides)

            # Persist whatever the assistant produced this round.
            assistant_msg = AIChatMessage(
                session_id=session.id,
                role="assistant",
                content=assistant_buf,
                tool_calls=(
                    [
                        {
                            "id": tc.id,
                            "name": tc.name,
                            "arguments": tc.arguments_json,
                        }
                        for tc in tool_calls
                    ]
                    if tool_calls
                    else None
                ),
                tokens_in=prompt_tokens,
                tokens_out=completion_tokens,
                latency_ms=elapsed_ms,
                cost_usd=cost_usd,
            )
            self.db.add(assistant_msg)
            await self.db.commit()

            # If no tool calls requested, we're done.
            if finish_reason != "tool_calls" and not tool_calls:
                yield StreamEvent(
                    "done",
                    {
                        "finish_reason": finish_reason or "stop",
                        "tokens_in": prompt_tokens,
                        "tokens_out": completion_tokens,
                        "latency_ms": elapsed_ms,
                    },
                )
                return

            # Dispatch each tool call, persist result messages,
            # then loop for another round of model output.
            for tc in tool_calls:
                yield StreamEvent(
                    "tool_call",
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments_json,
                    },
                )
                try:
                    raw_args = json.loads(tc.arguments_json or "{}")
                except json.JSONDecodeError:
                    raw_args = {}
                try:
                    result = await REGISTRY.call(tc.name, raw_args, db=self.db, user=self.user)
                    result_text = json.dumps(result, default=str)
                    is_error = False
                except ToolNotFound:
                    result_text = json.dumps({"error": f"tool not found: {tc.name}"})
                    is_error = True
                except ToolArgumentError as exc:
                    result_text = json.dumps({"error": f"invalid args: {exc.detail}"})
                    is_error = True
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "ai_chat_tool_error",
                        tool=tc.name,
                        session_id=str(session.id),
                    )
                    result_text = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
                    is_error = True

                self.db.add(
                    AIChatMessage(
                        session_id=session.id,
                        role="tool",
                        content=result_text,
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                )
                yield StreamEvent(
                    "tool_result",
                    {
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "is_error": is_error,
                        # Truncate to keep the SSE frame small — the
                        # full result is in the DB if anyone wants it.
                        "preview": result_text[:500],
                    },
                )
            await self.db.commit()

        # Hit the round cap — bail with an error.
        yield StreamEvent(
            "error",
            {
                "message": (
                    f"reached MAX_TOOL_ROUNDS ({MAX_TOOL_ROUNDS}) — the "
                    f"model kept asking for tools without producing a "
                    f"final answer. Try asking a more specific question."
                )
            },
        )
