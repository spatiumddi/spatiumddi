"""LLM provider configuration (issue #90 — Operator Copilot).

Mirrors ``app.models.auth_provider.AuthProvider`` — same priority +
is_enabled + JSONB-config + Fernet-encrypted-secret pattern.

The ``kind`` discriminator carries the driver name. Concrete drivers
live under ``app/drivers/llm/`` and are looked up via the registry
(``get_driver(kind)`` mirrors how DNS / DHCP drivers are resolved).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Driver discriminator values. Keep in sync with the CHECK constraint
# on ``ai_provider.kind`` in the migration and with the driver registry
# in ``app/drivers/llm/registry.py``.
AI_PROVIDER_KINDS: tuple[str, ...] = (
    "openai_compat",
    "anthropic",
    "google",
    "azure_openai",
)


class AIProvider(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Operator-configured LLM provider.

    The OpenAI-compat driver covers ~90% of the ecosystem in one row
    type — point ``base_url`` at OpenAI, Ollama, OpenWebUI, vLLM, LM
    Studio, llama.cpp's server, LocalAI, Together, Groq, or Fireworks
    and the same driver speaks to all of them.
    """

    __tablename__ = "ai_provider"
    __table_args__ = (UniqueConstraint("name", name="uq_ai_provider_name"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # openai_compat | anthropic | google | azure_openai
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # The endpoint base URL. Empty string for providers whose SDK has a
    # canonical default (Anthropic, Gemini); explicit for OpenAI-compat
    # since the whole point is pointing it at non-OpenAI hosts.
    base_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # Fernet-encrypted API key. Nullable because some local providers
    # (Ollama on localhost) don't require auth.
    api_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # The preferred model for this provider — used as the default when
    # a chat request doesn't specify one. ``list_models()`` populates
    # the picker in the UI.
    default_model: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Lower = higher priority. Mirrors ``auth_provider.priority``.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # Free-form per-provider tuning that doesn't warrant its own column:
    # temperature, max_tokens, top_p, top_k, request_timeout_seconds,
    # streaming-options overrides, etc. Drivers consume what they
    # recognize and ignore the rest.
    options: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


CHAT_ROLES: tuple[str, ...] = ("system", "user", "assistant", "tool")


class AIChatSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One Operator Copilot conversation. Messages live on
    :class:`AIChatMessage` linked by ``session_id``.

    Provider + model + system prompt are *snapshotted* on the session
    so a later edit of the global config doesn't retroactively change
    how this conversation was answered.
    """

    __tablename__ = "ai_chat_session"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="Untitled")
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_provider.id", ondelete="SET NULL"),
        nullable=True,
    )
    model: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AIChatMessage(UUIDPrimaryKeyMixin, Base):
    """One message in an :class:`AIChatSession`. Append-only — operators
    edit by starting a new session, not by mutating history.

    ``role`` mirrors the OpenAI chat schema. ``tool_calls`` is set on
    assistant messages that requested tool execution; the response
    arrives as one or more ``role=tool`` messages whose
    ``tool_call_id`` matches.
    """

    __tablename__ = "ai_chat_message"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_chat_session.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Shape: [{"id": str, "name": str, "arguments": str-as-json}, ...]
    tool_calls: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Per-message observability — populated when this message comes from
    # the LLM. ``cost_usd`` is computed at write time via the rate sheet
    # in ``app.services.ai.pricing``; None when the model is unknown to
    # the rate sheet (local Ollama, custom-hosted, etc.).
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
