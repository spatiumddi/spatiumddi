"""LLM pricing rate sheet + cost computation (issue #90 Wave 4).

The rate sheet is hand-maintained; rates change quarterly so the
operator-set ``ai_pricing_overrides`` setting wins over the in-code
defaults. The ``compute_cost`` function returns ``None`` if a model
isn't recognised — the chat orchestrator persists ``cost_usd=None``
in that case rather than booking the wrong number.

All values are USD per million tokens. Numbers below are accurate
as of late 2025; treat them as defaults the operator should review
in PlatformSettings → AI before relying on cap enforcement.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ModelRate:
    """Pricing for one model. ``input`` / ``output`` are USD per
    million tokens.
    """

    input: Decimal
    output: Decimal


# In-code rate sheet. Keys are matched against the model id (case-
# insensitive, prefix-matched after a normalize pass) so common
# variants like "gpt-4o-2024-08-06" resolve to the right family.
#
# Operator-set ``ai_pricing_overrides`` is consulted first — this is
# only the fallback. Local providers (Ollama / LM Studio) deliberately
# don't appear here; their cost defaults to None ("not metered").
_RATES: dict[str, ModelRate] = {
    # OpenAI
    "gpt-4o": ModelRate(Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-mini": ModelRate(Decimal("0.15"), Decimal("0.60")),
    "gpt-4-turbo": ModelRate(Decimal("10.00"), Decimal("30.00")),
    "gpt-4": ModelRate(Decimal("30.00"), Decimal("60.00")),
    "gpt-3.5-turbo": ModelRate(Decimal("0.50"), Decimal("1.50")),
    "o1": ModelRate(Decimal("15.00"), Decimal("60.00")),
    "o1-mini": ModelRate(Decimal("3.00"), Decimal("12.00")),
    # Anthropic
    "claude-3-5-sonnet": ModelRate(Decimal("3.00"), Decimal("15.00")),
    "claude-3-5-haiku": ModelRate(Decimal("0.80"), Decimal("4.00")),
    "claude-3-opus": ModelRate(Decimal("15.00"), Decimal("75.00")),
    "claude-3-haiku": ModelRate(Decimal("0.25"), Decimal("1.25")),
    "claude-haiku-4-5": ModelRate(Decimal("1.00"), Decimal("5.00")),
    "claude-sonnet-4-6": ModelRate(Decimal("3.00"), Decimal("15.00")),
    "claude-opus-4-7": ModelRate(Decimal("15.00"), Decimal("75.00")),
    # Google
    "gemini-1.5-pro": ModelRate(Decimal("1.25"), Decimal("5.00")),
    "gemini-1.5-flash": ModelRate(Decimal("0.075"), Decimal("0.30")),
    "gemini-1.5-flash-8b": ModelRate(Decimal("0.0375"), Decimal("0.15")),
    "gemini-2.0-flash": ModelRate(Decimal("0.10"), Decimal("0.40")),
    "gemini-2.0-flash-lite": ModelRate(Decimal("0.075"), Decimal("0.30")),
    "gemini-2.5-pro": ModelRate(Decimal("1.25"), Decimal("10.00")),
    "gemini-2.5-flash": ModelRate(Decimal("0.30"), Decimal("2.50")),
    # Azure OpenAI — Azure publishes the same per-token rates as
    # OpenAI's direct API for the equivalent model family. Deployments
    # are operator-named (e.g. ``my-prod-gpt4o``) so longest-prefix
    # matching against the family alone won't help; operators should
    # pin overrides via ``ai_pricing_overrides`` keyed on the
    # deployment name. The entries below are a courtesy fallback for
    # operators who name deployments after the base model.
    # Together / Groq / Fireworks — representative averages; operator
    # should override for accuracy.
    "llama-3.1-70b": ModelRate(Decimal("0.88"), Decimal("0.88")),
    "llama-3.1-8b": ModelRate(Decimal("0.18"), Decimal("0.18")),
    "mixtral-8x7b": ModelRate(Decimal("0.60"), Decimal("0.60")),
}


def _normalize(model_id: str) -> str:
    """Canonicalise a model id so date suffixes / version stamps don't
    miss the rate-sheet entry.
    """
    s = model_id.strip().lower()
    # Strip leading provider prefix ("openai/gpt-4o" → "gpt-4o")
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    return s


def _lookup_in_sheet(model_id: str) -> ModelRate | None:
    """Find the longest in-code rate-sheet key that prefixes the
    normalised model id. Lets ``gpt-4o-2024-08-06`` resolve to ``gpt-4o``
    while still beating the shorter ``gpt-4`` entry.
    """
    n = _normalize(model_id)
    if n in _RATES:
        return _RATES[n]
    best: tuple[str, ModelRate] | None = None
    for key, rate in _RATES.items():
        if n.startswith(key):
            if best is None or len(key) > len(best[0]):
                best = (key, rate)
    return best[1] if best else None


def get_rate(model_id: str, overrides: dict[str, Any] | None = None) -> ModelRate | None:
    """Resolve a model id to its USD-per-million-token rate. Order:

    1. Operator-set ``ai_pricing_overrides`` (exact-match key).
    2. In-code rate sheet (longest-prefix match).
    3. ``None`` — caller should treat as "not metered".
    """
    if overrides:
        entry = overrides.get(model_id)
        if isinstance(entry, dict):
            try:
                return ModelRate(
                    input=Decimal(str(entry.get("input", "0"))),
                    output=Decimal(str(entry.get("output", "0"))),
                )
            except Exception:
                pass
    return _lookup_in_sheet(model_id)


def compute_cost(
    model_id: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    overrides: dict[str, Any] | None = None,
) -> Decimal | None:
    """Return the USD cost for one LLM call, or ``None`` when the model
    is unrecognised (cost is left None in the DB rather than guessed).
    """
    if prompt_tokens is None and completion_tokens is None:
        return None
    rate = get_rate(model_id, overrides)
    if rate is None:
        return None
    p = Decimal(prompt_tokens or 0)
    c = Decimal(completion_tokens or 0)
    return (p * rate.input + c * rate.output) / Decimal("1000000")
