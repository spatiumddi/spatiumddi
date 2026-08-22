"""Timestamps go out RFC 3339 with exactly three fractional digits (#907).

A Swift client generated straight from this server's own OpenAPI document
failed to decode 6 of 7 endpoints it called — all 200 OK, all rejected by
Foundation's ``ISO8601DateFormatter``, which will not take the six fractional
digits ``datetime.isoformat()`` emits::

    DecodingError: dataCorrupted — Expected date string to be ISO8601-formatted
    operationID: list_spaces_api_v1_ipam_spaces_get

The assertions here are on the WIRE FORMAT, never on the patch that produces
it. ``app/core/json_datetime.py`` works by wrapping
``pydantic_core.core_schema.datetime_schema`` — an internal of a dependency we
do not control — so the failure mode worth guarding is a future pydantic that
stops routing through it and silently restores six digits. Asserting on the
bytes catches that; asserting that the patch is installed would not.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

import pytest
from fastapi.encoders import jsonable_encoder
from httpx import AsyncClient
from pydantic import BaseModel, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.json_datetime import install, to_rfc3339_ms
from app.core.security import create_access_token, hash_password
from app.main import app
from app.models.auth import User

#: Exactly three fractional digits, and an offset that is either ``Z`` or
#: ``±HH:MM``. Anchored: a partial match would accept the six-digit form.
RFC3339_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}(Z|[+-]\d{2}:\d{2})$")

_MICROSECONDS = datetime(2026, 5, 14, 21, 59, 10, 586198, tzinfo=UTC)


# ── the formatter itself ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (_MICROSECONDS, "2026-05-14T21:59:10.586Z"),
        # A whole second is the case that breaks decoders configured FOR
        # fractional seconds: isoformat() drops the fraction entirely, so the
        # shape changes depending on when the row happened to be written.
        (datetime(2026, 5, 14, 21, 59, 10, tzinfo=UTC), "2026-05-14T21:59:10.000Z"),
        # Truncated, never rounded — a timestamp must not move forward past
        # the instant it records.
        (datetime(2026, 5, 14, 21, 59, 10, 586999, tzinfo=UTC), "2026-05-14T21:59:10.586Z"),
        (datetime(2026, 5, 14, 21, 59, 10, 999, tzinfo=UTC), "2026-05-14T21:59:10.000Z"),
        # Offsets other than UTC keep their offset; only +00:00 becomes Z,
        # which is what pydantic already emitted.
        (
            datetime(2026, 5, 14, 21, 59, 10, 586198, tzinfo=timezone(timedelta(hours=2))),
            "2026-05-14T21:59:10.586+02:00",
        ),
        # Naive stays naive. Inventing an offset would be a lie, not a reformat.
        (datetime(2026, 5, 14, 21, 59, 10, 586198), "2026-05-14T21:59:10.586"),
    ],
)
def test_to_rfc3339_ms(value: datetime, expected: str) -> None:
    assert to_rfc3339_ms(value) == expected


# ── the two serialisation paths a response can take ───────────────────────


class _Nested(BaseModel):
    at: datetime


class _Payload(BaseModel):
    at: datetime
    optional: datetime | None = None
    nested: _Nested
    listed: list[datetime] = []
    mapped: dict[str, datetime] = {}


def test_pydantic_json_serialisation_is_millisecond_precise() -> None:
    """The response-model path: FastAPI serialises through pydantic in JSON
    mode, and every datetime in the tree has to come out the same shape —
    nested models, lists and dict values included."""
    payload = _Payload(
        at=_MICROSECONDS,
        optional=_MICROSECONDS,
        nested=_Nested(at=_MICROSECONDS),
        listed=[_MICROSECONDS],
        mapped={"k": _MICROSECONDS},
    )
    dumped = TypeAdapter(_Payload).dump_python(payload, mode="json")
    emitted = [
        dumped["at"],
        dumped["optional"],
        dumped["nested"]["at"],
        dumped["listed"][0],
        dumped["mapped"]["k"],
    ]
    assert all(RFC3339_MS.match(value) for value in emitted), emitted


def test_jsonable_encoder_matches_the_response_model_path() -> None:
    """The other path: a handler with no ``response_model`` is serialised by
    ``jsonable_encoder``, which has its own encoder table and never consults
    pydantic. If only one path were fixed, the wire format would depend on
    whether a route happened to declare a response model."""
    encoded = jsonable_encoder({"at": _MICROSECONDS, "whole": _MICROSECONDS.replace(microsecond=0)})
    assert encoded == {"at": "2026-05-14T21:59:10.586Z", "whole": "2026-05-14T21:59:10.000Z"}


def test_a_datetime_subclass_takes_the_same_path() -> None:
    """``jsonable_encoder`` falls back to a first-match ``isinstance`` scan for
    anything that is not exactly ``datetime`` — and ``datetime`` is a subclass
    of ``date``, whose stock entry is plain ``isoformat``. Registered upstream
    ahead of ``datetime``, that entry would claim a subclass (freezegun's
    ``FakeDatetime``, a driver's own) and serialise it in the old format."""

    class _Subclass(datetime):
        pass

    value = _Subclass(2026, 5, 14, 21, 59, 10, 586198, tzinfo=UTC)
    assert jsonable_encoder({"at": value}) == {"at": "2026-05-14T21:59:10.586Z"}


def test_a_plain_date_is_left_alone() -> None:
    """Only ``datetime`` moves. A ``date`` has no time to be precise about,
    and rewriting its encoder would change ``format: date`` fields."""
    assert jsonable_encoder({"on": _MICROSECONDS.date()}) == {"on": "2026-05-14"}


def test_installing_twice_does_not_wrap_the_wrapper() -> None:
    """``install()`` runs from a package ``__init__``, so a second call is a
    plausible accident — and a wrapper wrapping a wrapper would keep working
    while doing the whole schema build twice per model. The guard asks the
    patch target whether it is already ours rather than tracking a flag of its
    own, so it holds even if this module is imported twice under two names."""
    from pydantic_core import core_schema  # noqa: PLC0415 — the patch target

    before = core_schema.datetime_schema
    install()
    assert core_schema.datetime_schema is before
    assert to_rfc3339_ms(_MICROSECONDS) == "2026-05-14T21:59:10.586Z"


def test_python_mode_dumps_keep_full_precision() -> None:
    """The serialiser is JSON-only. Internal callers that ``model_dump()`` in
    python mode still get real datetimes at microsecond precision, so nothing
    that compares or does arithmetic on them changes behaviour."""
    dumped = TypeAdapter(_Payload).dump_python(
        _Payload(at=_MICROSECONDS, nested=_Nested(at=_MICROSECONDS))
    )
    assert dumped["at"] == _MICROSECONDS


def test_millisecond_output_still_validates() -> None:
    """Round trip: what we emit has to be something we accept back."""
    assert _Nested.model_validate({"at": to_rfc3339_ms(_MICROSECONDS)}).at == _MICROSECONDS.replace(
        microsecond=586000
    )


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_the_document_still_describes_a_date_time_string(
    mode: Literal["validation", "serialization"],
) -> None:
    """The serialiser must not leak into the schema: ``format: date-time`` is
    what a generator reads to emit a date decoder rather than a plain string.

    ``serialization`` is the mode that matters and the one that broke: FastAPI
    publishes response models in it, and declaring the serialiser's return
    type (the obvious ``return_schema=str_schema()``) silently rewrote every
    ``created_at`` in the document to a bare string — trading the six-digit
    decode failure for a client that never parses a date at all.
    """
    assert _Nested.model_json_schema(mode=mode)["properties"]["at"] == {
        "format": "date-time",
        "title": "At",
        "type": "string",
    }


def test_the_served_document_keeps_date_time_on_timestamp_properties() -> None:
    """The same guard at document level, where a client actually reads it —
    on a plain timestamp and on one that was ``datetime | None`` and went
    through the #907 nullable collapse."""
    schemas = app.openapi()["components"]["schemas"]
    assert schemas["AddressSetResponse"]["properties"]["created_at"] == {
        "type": "string",
        "format": "date-time",
        "title": "Created At",
    }
    assert schemas["APITokenRow"]["properties"]["expires_at"] == {
        "type": "string",
        "format": "date-time",
        "title": "Expires At",
    }


def test_hand_formatted_timestamp_fields_are_declared_as_timestamps() -> None:
    """Three response models carried their timestamps as ``str`` and filled
    them with ``isoformat()``, so they published as bare strings with no
    ``format: date-time`` — a generated client read the audit trail's
    timestamp as text — and they went out in the six-digit ``+00:00`` shape
    every other timestamp in the API had just stopped using (#907)."""
    schemas = app.openapi()["components"]["schemas"]
    declared = {
        "AuditLogResponse": ["timestamp"],
        "SessionRow": ["created_at", "last_seen_at", "expires_at"],
        "app__api__v1__users__router__UserResponse": [
            "last_login_at",
            "failed_login_locked_until",
        ],
    }
    for name, properties in declared.items():
        for prop in properties:
            schema = schemas[name]["properties"][prop]
            assert schema.get("format") == "date-time", f"{name}.{prop}: {schema}"


# ── end to end, through a real endpoint ───────────────────────────────────


@pytest.mark.asyncio
async def test_list_spaces_serialises_timestamps_for_a_generated_client(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """``list_spaces_api_v1_ipam_spaces_get`` is the operation named in the
    issue's decoding failure — a 200 OK a generated client could not read."""
    user = User(
        username="ts-probe",
        email="ts-probe@example.com",
        display_name="ts-probe",
        hashed_password=hash_password("password123"),
        auth_source="local",
        is_superadmin=True,
    )
    user.groups = []  # mark loaded — is_effective_superadmin walks .groups
    db_session.add(user)
    await db_session.flush()
    headers = {"Authorization": f"Bearer {create_access_token(str(user.id))}"}
    await db_session.commit()

    created = await client.post(
        "/api/v1/ipam/spaces", headers=headers, json={"name": "ts-probe-space"}
    )
    assert created.status_code in (200, 201), created.text
    assert RFC3339_MS.match(created.json()["created_at"]), created.json()["created_at"]

    listed = await client.get("/api/v1/ipam/spaces", headers=headers)
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    rows = rows["items"] if isinstance(rows, dict) else rows
    stamps = [row["created_at"] for row in rows] + [row["modified_at"] for row in rows]
    assert stamps, "no rows to check — the fixture did not create a space"
    assert all(RFC3339_MS.match(stamp) for stamp in stamps), stamps
