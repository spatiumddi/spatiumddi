"""RFC 3339 timestamps with a fixed millisecond precision (issue #907).

Python's ``datetime.isoformat()`` — which is what pydantic's JSON serialiser
and FastAPI's ``jsonable_encoder`` both reach for — emits *six* fractional
digits, and omits the fraction entirely when the value happens to land on a
whole second::

    2026-05-14T21:59:10.586198Z     # the usual case
    2026-05-14T21:59:10Z            # microsecond == 0

Both are legal RFC 3339. Neither is what most generated clients decode.
Foundation's ``ISO8601DateFormatter`` rejects fractional seconds unless
``.withFractionalSeconds`` is set, and is unreliable above three digits even
then — so a Swift client generated straight from our own OpenAPI document
failed to decode 6 of 7 endpoints it called, every one of them a 200 OK. The
second shape is the nastier of the two: a decoder configured *for* fractional
seconds fails on a timestamp that has none, so the failure is intermittent and
depends on whether a row happened to be written on a second boundary.

So this module normalises every JSON-serialised ``datetime`` to exactly three
fractional digits, keeping the ``Z`` suffix pydantic already emits for UTC::

    2026-05-14T21:59:10.586Z
    2026-05-14T21:59:10.000Z

Nothing a DDI API exposes needs microseconds on a ``created_at``, and a fixed
shape removes a hand-written workaround from every client that will ever be
generated against this document.

**Why a serialiser and not truncated values.** Rounding the ``datetime``
objects themselves at the source fixes neither half: pydantic pads a
millisecond-precision value back out to ``.586000`` (six digits again), and a
value truncated to whole seconds serialises with no fraction at all — which is
the intermittent case above, made permanent.

**Why patch the schema builder.** The framework-blessed way to attach a
serialiser to a type is ``Annotated[datetime, PlainSerializer(...)]``, which
here means editing 714 annotations across 166 files and trusting every future
model to remember — the definition of silent drift, and the sort of thing that
regresses one PR later with no symptom. The alternatives are worse:
``model_config['json_encoders']`` is deprecated and removed in pydantic v3
(and warns once per field at import), and rewriting timestamps in the rendered
response body means sniffing every string in every response for something that
looks like a date — mutating opaque operator data (a raw BIND query-log line
carries a timestamp) and paying a second full traversal per request.

``pydantic_core.core_schema.datetime_schema()`` is the single point every
``datetime`` core schema is built through, and ``serialization`` is a
documented part of that schema. Patching it costs nothing per request (it runs
at model-class creation), leaves validation and ``mode="python"`` dumps
untouched, and cannot be forgotten by a new model.

**What it does not reach.** A ``datetime`` sitting inside an ``Any`` /
``dict[str, Any]`` field never builds a ``datetime_schema`` — pydantic infers
a serialiser for it at runtime — so such a value still goes out at six digits.
Left alone deliberately: those fields publish as untyped JSON, so no generated
client points a date decoder at them, and covering them would mean walking the
contents of every opaque payload on every response. Same for a timestamp a
handler formatted into a string itself; the fix for one of those is to declare
the field ``datetime`` and let this module serialise it, which is what
``AuditLogResponse.timestamp`` and the session / user rows now do.

The measurable cost is a Python call where pydantic-core had a Rust
formatter: ~0.7 µs per timestamp, i.e. ~1.4 ms added to a 1,000-row response
carrying two timestamps a row. Paid on serialisation only.

The other cost is a dependency on a pydantic internal: if a future pydantic
stops routing through this function, timestamps quietly revert to six digits.
``backend/tests/test_json_datetime.py`` asserts on the serialised wire format
rather than on the patch, so that regression fails a test instead of shipping.

``install()`` is called from ``app/__init__.py`` — see the comment there for
why that is the only import site that reliably runs first.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

#: Stamped on the wrapper so ``_patch_pydantic`` can recognise its own work.
#: On the function rather than in a module-level flag because the question is
#: "is ``core_schema`` already patched", not "has this module run" — the two
#: differ if this module is ever imported twice under different names, and
#: only the first one stops a wrapper wrapping a wrapper. Same shape as the
#: encoder guard below, which asks its own target the same way.
_PATCH_MARKER = "_spatium_rfc3339_ms"


def to_rfc3339_ms(value: datetime) -> str:
    """Serialise ``value`` as RFC 3339 with exactly three fractional digits.

    Truncates rather than rounds — a timestamp must never move forward past
    the instant it records — and preserves whatever offset the value carries:
    ``Z`` for UTC (matching what pydantic already emitted), ``+02:00`` for
    another zone, and no suffix at all for a naive datetime, which is the one
    case where inventing an offset would be a lie rather than a reformat.
    """
    text = value.isoformat(timespec="milliseconds")
    if text.endswith("+00:00"):
        return f"{text[:-6]}Z"
    return text


def install() -> None:
    """Make every JSON-serialised ``datetime`` millisecond-precise.

    Idempotent, and must run before the first pydantic model class is created:
    a core schema is built once, at class-creation time, so a model defined
    before this call keeps the default six-digit serialiser forever.
    """
    _patch_pydantic()
    _patch_fastapi_encoders()


def _patch_pydantic() -> None:
    """Attach the serialiser to every ``datetime`` core schema pydantic builds.

    ``when_used="json"`` confines it to JSON output — ``model_dump()`` in
    python mode still yields real ``datetime`` objects at full precision, so
    internal callers that compare or do arithmetic on them are unaffected.

    ``setdefault`` rather than assignment: a field that declares its own
    serialiser (``Annotated[datetime, PlainSerializer(...)]``) has said
    something more specific than this default, and keeps it.

    No ``return_schema``. Declaring the obvious ``str_schema()`` there rewrites
    what the *serialisation* JSON schema says the field is — and FastAPI
    publishes response models in serialisation mode, so every ``created_at``
    in the OpenAPI document would go from ``{"type": "string", "format":
    "date-time"}`` to a bare string. ``format: date-time`` is exactly what a
    generator reads to emit a date decoder rather than a ``String``, so
    declaring the return type would have cost the clients this exists for far
    more than it bought. Left absent, pydantic keeps the declared type's own
    schema and the serialiser still runs.
    """
    from pydantic_core import core_schema  # noqa: PLC0415 — patch target

    build_datetime_schema = core_schema.datetime_schema
    if getattr(build_datetime_schema, _PATCH_MARKER, False):
        return

    def datetime_schema(*args: Any, **kwargs: Any) -> Any:
        schema = build_datetime_schema(*args, **kwargs)
        schema.setdefault(
            "serialization",
            core_schema.plain_serializer_function_ser_schema(
                to_rfc3339_ms,
                when_used="json",
            ),
        )
        return schema

    setattr(datetime_schema, _PATCH_MARKER, True)  # noqa: B010 — name is a constant
    core_schema.datetime_schema = datetime_schema  # type: ignore[assignment]


def _patch_fastapi_encoders() -> None:
    """Cover the responses pydantic never sees.

    A handler with no ``response_model`` (or one returning a bare dict) is
    serialised by ``fastapi.encoders.jsonable_encoder``, which looks the type
    up in its own ``ENCODERS_BY_TYPE`` table instead of going through pydantic.
    Without this, the wire format would depend on whether a route happened to
    declare a response model — the worst kind of inconsistency, because it is
    invisible until a client hits the one endpoint that differs.

    ``encoders_by_class_tuples`` is derived from that table once at import, and
    ``jsonable_encoder`` reads it for subclass matches, so it has to be rebuilt
    rather than left pointing at the stock encoder.

    Rebuilt with our entry FIRST, which is load-bearing. ``jsonable_encoder``
    takes the exact-type fast path for a plain ``datetime``, but falls to an
    ``isinstance`` scan of that map in first-match order for anything else —
    and ``datetime`` is a subclass of ``date``, whose entry is stock
    ``isoformat``. Since ``date`` is registered ahead of ``datetime`` upstream,
    leaving the order alone means a datetime SUBCLASS (freezegun's
    ``FakeDatetime``, a driver's own) serialises in the six-digit format the
    plain type no longer uses — the intermittent kind of inconsistency this
    module exists to remove.
    """
    from fastapi import encoders  # noqa: PLC0415 — patch target

    if encoders.ENCODERS_BY_TYPE.get(datetime) is to_rfc3339_ms:
        return
    encoders.ENCODERS_BY_TYPE[datetime] = to_rfc3339_ms
    by_class = encoders.generate_encoders_by_class_tuples(encoders.ENCODERS_BY_TYPE)
    encoders.encoders_by_class_tuples = {
        to_rfc3339_ms: by_class.pop(to_rfc3339_ms),
        **by_class,
    }
