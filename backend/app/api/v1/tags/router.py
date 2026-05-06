"""Tag autocomplete endpoints (issue #104 phase 2).

Powers the typeahead behind the upcoming UI tag-chip — the
frontend hits ``GET /tags/keys`` while the operator is typing a
key, and ``GET /tags/values?key=env`` once they've entered a key
and are picking the value side.

## Cross-table aggregation

Both endpoints UNION ALL across every model that carries a JSONB
``tags`` column:

* IPAM — ``ip_space``, ``ip_block``, ``subnet``, ``ip_address``
* Network modeling — ``asn``, ``vrf``, ``network_device``,
  ``domain``, ``circuit``, ``network_service``, ``overlay_network``

The list is intentionally hardcoded here (not derived) so a future
``tags`` column on a new model is an explicit one-line opt-in
rather than silently expanding the autocomplete surface — autocomplete
discoverability has compliance implications (operators see "what
tag keys exist on this platform"), so an explicit list is safer.

The same set is what :func:`app.services.tags.apply_tag_filter`'s
callers wire into their ``tag=`` query params, so the autocomplete
options always match what the filter can actually act on.

## Postgres mechanics

* ``jsonb_object_keys(tags)`` unrolls each row's keys to one row
  per key — set-returning function, runs against the JSONB GIN
  index on each table's ``tags`` column.
* ``tags ->> 'key'`` extracts the value as text (numbers /
  booleans collapse to their printable form; nested objects come
  back as JSON-stringified text — which is fine for autocomplete
  display but operators won't be able to match on nested
  structures from the UI chip; that limitation is by design for
  v1).
* Outer ``DISTINCT`` + ``ORDER BY`` produces a stable list. No
  pagination — autocomplete is single-shot, capped via ``limit``.

## Authorization

Only requires authentication. Tag *names* themselves are not
sensitive metadata — every operator who can list any tagged
resource already sees the tag values inline on each row, so
exposing the union of keys / values doesn't leak anything new.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.api.deps import DB, CurrentUser

router = APIRouter()


# Hardcoded — see module docstring for why this isn't derived.
# Order is alphabetical for readable EXPLAIN plans + matches the
# alphabetised router.include_router list.
_TAGGED_TABLES: tuple[str, ...] = (
    "asn",
    "circuit",
    "domain",
    "ip_address",
    "ip_block",
    "ip_space",
    "network_device",
    "network_service",
    "overlay_network",
    "subnet",
    "vrf",
)


# Build the UNION-ALL fragments at import time — they're constant.
# ``tags`` is JSONB-typed and non-nullable on every model
# (default=dict on the column), but `jsonb_object_keys(NULL)`
# raises, so the IS NOT NULL guard costs nothing and keeps the
# query safe if a future migration ever loosens the column.
_KEYS_UNION_SQL = " UNION ALL ".join(
    f"SELECT jsonb_object_keys(tags) AS key FROM {t} WHERE tags IS NOT NULL" for t in _TAGGED_TABLES
)

_VALUES_UNION_SQL = " UNION ALL ".join(
    f"SELECT tags ->> :key AS value FROM {t} WHERE tags ? :key" for t in _TAGGED_TABLES
)


class TagKeysResponse(BaseModel):
    keys: list[str] = Field(description="Sorted distinct list of tag keys.")


class TagValuesResponse(BaseModel):
    key: str
    values: list[str] = Field(description="Sorted distinct list of values for the requested key.")


@router.get("/keys", response_model=TagKeysResponse)
async def list_tag_keys(
    db: DB,
    _: CurrentUser,
    prefix: str | None = Query(
        None,
        description=(
            "Case-insensitive substring filter on the key name. Pass "
            "the operator's in-flight typeahead chars; empty / null "
            "returns the full set."
        ),
        max_length=200,
    ),
    limit: int = Query(200, ge=1, le=1000),
) -> TagKeysResponse:
    sql = f"SELECT DISTINCT key FROM ({_KEYS_UNION_SQL}) AS sub"
    params: dict[str, object] = {"limit": limit}
    if prefix:
        sql += " WHERE key ILIKE :prefix"
        params["prefix"] = f"%{prefix}%"
    sql += " ORDER BY key LIMIT :limit"
    rows = (await db.execute(text(sql), params)).all()
    return TagKeysResponse(keys=[r.key for r in rows])


@router.get("/values", response_model=TagValuesResponse)
async def list_tag_values(
    db: DB,
    _: CurrentUser,
    key: str = Query(
        ...,
        min_length=1,
        max_length=255,
        description="Tag key whose values should be returned.",
    ),
    prefix: str | None = Query(
        None,
        description="Case-insensitive substring filter on the value side.",
        max_length=200,
    ),
    limit: int = Query(200, ge=1, le=1000),
) -> TagValuesResponse:
    # Outer DISTINCT lets duplicates from multiple tables collapse.
    # ``WHERE value IS NOT NULL`` drops rows where the key is
    # explicitly stored as JSON null — the frontend chip can't
    # represent "match the JSON null" anyway, so excluding them
    # keeps the autocomplete tidy.
    sql = f"SELECT DISTINCT value FROM ({_VALUES_UNION_SQL}) AS sub WHERE value IS NOT NULL"
    params: dict[str, object] = {"key": key, "limit": limit}
    if prefix:
        sql += " AND value ILIKE :prefix"
        params["prefix"] = f"%{prefix}%"
    sql += " ORDER BY value LIMIT :limit"
    rows = (await db.execute(text(sql), params)).all()
    return TagValuesResponse(key=key, values=[r.value for r in rows])
