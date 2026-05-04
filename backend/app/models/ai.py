"""LLM provider configuration (issue #90 — Operator Copilot).

Mirrors ``app.models.auth_provider.AuthProvider`` — same priority +
is_enabled + JSONB-config + Fernet-encrypted-secret pattern.

The ``kind`` discriminator carries the driver name. Concrete drivers
live under ``app/drivers/llm/`` and are looked up via the registry
(``get_driver(kind)`` mirrors how DNS / DHCP drivers are resolved).
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
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
