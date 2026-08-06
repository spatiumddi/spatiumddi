"""The published OpenAPI document must describe the responses we actually send.

The assertions here are regression guards for defects the per-commit
conformance fuzz found on live appliances but nothing in CI could see: the fuzz
validates real responses against this document, so a document that lies fails
dozens of unrelated endpoints at once and reads as flake.

No database, no client — the document is generated from the route table alone.
"""

from __future__ import annotations

from app.api.pagination import MAX_PAGE
from app.main import app


def _schema() -> dict:
    return app.openapi()


def test_validation_detail_allows_the_string_form_we_actually_return() -> None:
    """422 bodies come in two shapes and the document has to admit both.

    FastAPI auto-declares ``HTTPValidationError.detail`` as an array, which is
    right for its own request validation. But 270 call sites across
    ``app/api/v1`` raise ``HTTPException(status_code=422, detail="...")`` for
    semantic checks a signature cannot express, and those serialise to
    ``{"detail": "<string>"}``.

    Live example that failed ``response_schema_conformance`` before this guard:
    ``GET /api/v1/new-devices/sightings?classification=null`` returns
    ``422 {"detail":"invalid classification"}``.
    """
    detail = _schema()["components"]["schemas"]["HTTPValidationError"]["properties"]["detail"]
    arms = detail.get("anyOf")
    assert arms, f"detail is not a union, so hand-raised 422s violate it: {detail}"
    assert {"type": "string"} in arms, f"string 422 detail not admitted: {arms}"
    assert any("array" in str(a) for a in arms), f"array 422 detail lost: {arms}"


def test_every_page_parameter_declares_an_upper_bound() -> None:
    """``page`` bounded like ``page_size``, or the offset overflows the bigint.

    The offset is ``(page - 1) * page_size`` and SQL ``OFFSET`` is a bigint, so
    an unbounded ``page`` is a 500 one query parameter away. Measured live at
    the default page size: page 92233720368547759 -> 200, page
    92233720368547760 -> 500, which is exactly ``2**63-1``.

    Asserted over the document rather than the source so it also covers list
    endpoints that build their own offset instead of calling ``paginate()`` —
    which is most of them.
    """
    unbounded = []
    for path, item in _schema()["paths"].items():
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            for param in op.get("parameters") or []:
                if param.get("name") != "page" or param.get("in") != "query":
                    continue
                schema = param.get("schema") or {}
                arms = schema.get("anyOf") or [schema]
                numeric = [a for a in arms if a.get("type") == "integer"]
                # Not every `page` is a page NUMBER: GET /api/v1/saved-views
                # takes `page: str`, the UI page a preset belongs to. Only an
                # integer page reaches an SQL OFFSET, so only that one needs a
                # ceiling — skip the rest rather than demanding `maximum` on a
                # string.
                if not numeric:
                    continue
                if not any("maximum" in a for a in numeric):
                    unbounded.append(f"{method.upper()} {path}")
    assert not unbounded, (
        "page query parameters with no maximum — each one is an overflow 500:\n  "
        + "\n  ".join(sorted(unbounded))
    )
    assert MAX_PAGE * 1000 < 2**63 - 1, "MAX_PAGE * MAX_PAGE_SIZE must stay inside a bigint"
