"""Every route that serves a non-JSON body must declare it (#921).

FastAPI infers ``application/json`` for a handler annotated ``-> Response``.
A route that then streams a zip, a PDF, a pcap or an SSE stream publishes a
success response it does not produce, so a generated client cannot decode it
and a strict response validator rejects the 200 — schemathesis reports
"Undocumented Content-Type — Received: application/zip, Documented:
application/json".

Three of these were fixed one at a time as the export.pdf routes (#861),
then ``POST /system/support-bundle`` was reported the same way (#921). This
check found the remaining eleven at once — SSE streams, backup and DNS zone
archives, the SAML metadata document, pod logs, upgrade images and the pcap
download — so the fix is the class, and this is what stops it regrowing.

Detection reads the **handler's source** for the media type it passes and
compares against the **generated OpenAPI document**, not against
``route.response_model``: the document is what a client generator consumes,
and it is the only place the mismatch is visible.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

from fastapi.routing import APIRoute

from app.main import create_app

_MEDIA_TYPE = re.compile(r'media_type\s*=\s*"([^"]+)"')
# Response classes that carry an implicit media type with no ``media_type=``.
_IMPLICIT = {
    "PlainTextResponse": "text/plain",
    "HTMLResponse": "text/html",
}


def _walk(routes: list[Any], prefix: str = "") -> list[tuple[str, APIRoute]]:
    """``(full path, route)`` for every API route.

    FastAPI defers ``include_router`` into a wrapper whose child routes carry
    only their sub-path, so the prefix has to be reassembled here — reading
    ``route.path`` alone yields ``""`` for most of the application and would
    make this check silently pass on everything.
    """
    found: list[tuple[str, APIRoute]] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append((prefix + route.path, route))
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            found += _walk(
                getattr(included, "routes", []), prefix + (getattr(context, "prefix", "") or "")
            )
        elif hasattr(route, "routes"):
            found += _walk(route.routes, prefix)
    return found


def _served_media_types(route: APIRoute) -> set[str]:
    try:
        source = inspect.getsource(route.endpoint)
    except (OSError, TypeError):  # pragma: no cover — C or dynamically built
        return set()
    served = set(_MEDIA_TYPE.findall(source))
    for cls, media_type in _IMPLICIT.items():
        if re.search(rf"\b{cls}\(", source):
            served.add(media_type)
    # A ``media_type=`` built from a variable comes through as the variable's
    # name; only literals ("type/subtype") are checkable.
    return {m for m in served if "/" in m and not m.startswith("application/json")}


def test_non_json_responses_are_declared() -> None:
    app = create_app()
    document = app.openapi()

    undeclared: list[str] = []
    for path, route in _walk(app.routes):
        served = _served_media_types(route)
        if not served:
            continue
        for method in sorted(route.methods or []):
            operation = document["paths"].get(path, {}).get(method.lower())
            if operation is None:
                continue
            declared = set(operation.get("responses", {}).get("200", {}).get("content", {}))
            missing = sorted(m for m in served if m not in declared)
            if missing:
                undeclared.append(
                    f"{method} {path} serves {missing} but declares {sorted(declared)}"
                )

    assert not undeclared, "routes serving an undeclared media type:\n  " + "\n  ".join(
        sorted(set(undeclared))
    )
