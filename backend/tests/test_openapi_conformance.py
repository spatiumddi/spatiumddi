"""The published OpenAPI document must describe the responses we actually send.

The assertions here are regression guards for defects the per-commit
conformance fuzz found on live appliances but nothing in CI could see: the fuzz
validates real responses against this document, so a document that lies fails
dozens of unrelated endpoints at once and reads as flake.

No database, no client — the document is generated from the route table alone.
"""

from __future__ import annotations

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
