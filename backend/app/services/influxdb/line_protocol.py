"""InfluxDB line-protocol rendering (issue #889).

Pure formatting — no I/O, no ORM imports — so the escaping rules can be
unit-tested directly. The rules differ per position and getting them
wrong corrupts the series silently rather than erroring, which is why
they live in one place:

* **measurement** — escape ``,`` and space.
* **tag key, tag value, field key** — escape ``,``, ``=`` and space.
* **string field value** — wrap in double quotes, escape ``"`` and ``\\``.
* newlines are illegal anywhere; they terminate the point.

Integers are suffixed ``i`` (InfluxDB's integer field type); floats are
written bare. Booleans render as ``true`` / ``false``.

Empty tag values are dropped rather than written: InfluxDB treats a tag
with an empty value as absent anyway, and emitting ``tag=`` produces a
parse error on some server versions.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

_MEASUREMENT_ESCAPES = {",": r"\,", " ": r"\ "}
_KEY_ESCAPES = {",": r"\,", "=": r"\=", " ": r"\ "}


def _apply(value: str, table: Mapping[str, str]) -> str:
    # Newlines and carriage returns can't be escaped in line protocol —
    # they end the point — so they are replaced with a space before the
    # positional escaping runs (which then escapes that space).
    cleaned = value.replace("\n", " ").replace("\r", " ")
    return "".join(table.get(ch, ch) for ch in cleaned)


def escape_measurement(value: str) -> str:
    return _apply(value, _MEASUREMENT_ESCAPES)


def escape_key(value: str) -> str:
    """Escape a tag key, tag value, or field key."""
    return _apply(value, _KEY_ESCAPES)


def format_string_field(value: str) -> str:
    cleaned = value.replace("\n", " ").replace("\r", " ")
    escaped = cleaned.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def format_field_value(value: int | float | bool | str) -> str:
    # bool before int — bool is an int subclass, and writing ``1i`` where
    # the series already holds booleans is a field-type conflict InfluxDB
    # rejects for the whole batch.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        return repr(value)
    return format_string_field(str(value))


@dataclass(frozen=True)
class Point:
    measurement: str
    tags: Mapping[str, str]
    fields: Mapping[str, int | float | bool | str]
    # Epoch seconds. The writer always requests ``precision=s``; every
    # source here is either a 60 s agent bucket or a point-in-time gauge,
    # so sub-second resolution would be false precision.
    timestamp: int


def render_point(point: Point) -> str:
    """Render one point. Raises ``ValueError`` if it carries no fields.

    A field-less point is a line-protocol parse error, and the server
    rejects the *entire* batch it arrives in — so it is caught here
    rather than shipped.
    """
    if not point.fields:
        raise ValueError(f"point {point.measurement!r} has no fields")
    head = escape_measurement(point.measurement)
    tag_parts = [
        f"{escape_key(k)}={escape_key(v)}" for k, v in sorted(point.tags.items()) if v != ""
    ]
    if tag_parts:
        head = head + "," + ",".join(tag_parts)
    field_parts = [
        f"{escape_key(k)}={format_field_value(v)}" for k, v in sorted(point.fields.items())
    ]
    return f"{head} {','.join(field_parts)} {int(point.timestamp)}"


def render_batch(points: Iterable[Point]) -> str:
    return "\n".join(render_point(p) for p in points)


__all__ = [
    "Point",
    "escape_key",
    "escape_measurement",
    "format_field_value",
    "format_string_field",
    "render_batch",
    "render_point",
]
