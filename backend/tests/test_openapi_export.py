"""The exported OpenAPI contract (issue #903).

The native app lives in its own repo, so this document is the contract
BETWEEN two repos rather than a file a client reads off the working tree.
That changes what can go wrong: a defect here does not break this server, it
breaks a generated client somewhere else, one release later, with no local
symptom.

Two properties carry the whole thing and neither is visible in a diff:

* ``info.version`` has to be the real version. It was hardcoded ``"0.1.0"``
  from the first release until #903, so a client generated against any tag
  would have been stamped with a version that never changes — which defeats
  pinning, the entire reason for publishing the artifact.
* The document has to come from ``app.openapi()``, which ``create_app()``
  replaces with a wrapper that widens ``HTTPValidationError.detail`` to admit
  the string form ~270 handlers actually return. Swapping in
  ``fastapi.openapi.utils.get_openapi`` would drop that without touching a
  call site, and generated clients would then reject a large share of this
  server's real 4xx bodies.

The end-to-end run is a subprocess because ``settings`` is a module-level
singleton built at import time: this test session has already imported
``app.main``, so setting ``VERSION`` in-process would be a no-op and an
in-process assertion would prove nothing about the shipped path.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from app.config import settings
from app.main import app

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_EXPORTER = _REPO_ROOT / "scripts" / "export_openapi.py"

# The dev container copies only ``backend/`` into the image, so the subprocess
# tests skip there and run for real in CI, which tests from a full checkout.
# Same convention as test_spatium_console.py.
_needs_exporter = pytest.mark.skipif(
    not _EXPORTER.exists(),
    reason="exporter script not present in this checkout",
)


# Each run is a full app import (~15 s), so identical invocations are shared.
# Keyed on the env overrides, which are the only thing that varies the output.
_CACHE: dict[tuple[tuple[str, str], ...], str] = {}


def _run_raw(env_overrides: dict[str, str]) -> str:
    key = tuple(sorted(env_overrides.items()))
    if key not in _CACHE:
        # No ``cwd``: the script locates ``backend/`` relative to itself, which
        # is what lets a client repo invoke it by absolute path. Pinning a cwd
        # here would test an invocation nobody uses and hide a regression in
        # the one everybody does.
        proc = subprocess.run(
            [sys.executable, str(_EXPORTER)],
            capture_output=True,
            text=True,
            env={**os.environ, **env_overrides},
            timeout=300,
        )
        assert proc.returncode == 0, f"exporter failed:\n{proc.stderr[-3000:]}"
        _CACHE[key] = proc.stdout
    return _CACHE[key]


def _run(env_overrides: dict[str, str]) -> dict:
    return json.loads(_run_raw(env_overrides))


# ── the served document ───────────────────────────────────────────────────


def test_create_app_survives_an_empty_version_setting() -> None:
    """An empty ``VERSION`` must degrade, not kill the container.

    ``create_app()`` passed a literal until #903, which made a falsy
    ``settings.version`` harmless. Reading the real value introduced a way to
    crash at import instead: pydantic-settings honours an empty ``VERSION``
    env var, and FastAPI asserts on a falsy version inside ``__init__`` —
    before logging is configured, so the operator gets a bare AssertionError
    and a crashlooping api container for what is a typo in ``.env``.

    Called directly rather than through a subprocess because ``create_app()``
    reads ``settings.version`` at call time; only the module-level ``app`` is
    fixed at import.
    """
    from app.main import create_app  # noqa: PLC0415 — see docstring

    original = settings.version
    try:
        settings.version = ""
        assert create_app().openapi()["info"]["version"] == "dev"
    finally:
        settings.version = original


def test_served_version_is_the_running_version_not_a_literal() -> None:
    """``create_app()`` passed ``version="0.1.0"`` literally while
    ``settings.version`` carried the real one, so every deployment
    misreported itself at ``/api/docs`` and ``/api/openapi.json``."""
    assert app.openapi()["info"]["version"] == settings.version
    assert app.openapi()["info"]["version"] != "0.1.0"


# ── the exported artifact ─────────────────────────────────────────────────


@_needs_exporter
def test_export_stamps_the_requested_version() -> None:
    doc = _run({"VERSION": "2026.01.02-3"})
    assert doc["info"]["version"] == "2026.01.02-3"


@_needs_exporter
def test_export_does_not_inherit_operator_branding() -> None:
    """``info.title`` is ``settings.app_title``, which operators can rebrand
    (#886/#888). Exporting on a branded install must not publish that
    install's name as the name of the public API."""
    doc = _run({"VERSION": "2026.01.02-3", "APP_TITLE": "Acme DDI"})
    assert doc["info"]["title"] == "SpatiumDDI"


@_needs_exporter
def test_export_goes_through_the_validation_detail_wrapper() -> None:
    """The regression guard for a future refactor reaching for
    ``get_openapi``: it would produce a document that looks complete and is
    wrong only in the shape of every hand-raised 422."""
    doc = _run({"VERSION": "2026.01.02-3"})
    detail = doc["components"]["schemas"]["HTTPValidationError"]["properties"]["detail"]
    arms = detail.get("anyOf")
    assert arms, f"detail is not a union — the export bypassed app.openapi(): {detail}"
    assert {"type": "string"} in arms


@_needs_exporter
def test_empty_version_env_falls_back_instead_of_crashing() -> None:
    """An EMPTY ``VERSION`` is not an absent one.

    pydantic-settings honours the empty string, so ``settings.version``
    becomes ``""`` rather than defaulting, and FastAPI asserts on a falsy
    version ("A version must be provided for OpenAPI") — which ``create_app()``
    guards with ``settings.version or "dev"``, but the exporter must stamp the
    right value itself rather than lean on that. Easy to hit for free: an
    undefined Make variable still expands to ``-e VERSION=``, and so does an
    unset ``GITHUB_REF_NAME``.
    """
    doc = _run({"VERSION": ""})
    assert doc["info"]["version"] == "dev"


@_needs_exporter
def test_export_is_byte_reproducible() -> None:
    """The release attaches this artifact and the acceptance criterion is
    that it matches a local run at the same tag. Insertion-order-dependent
    output would make that comparison meaningless."""
    # Deliberately bypasses the shared cache — comparing a cached string to
    # itself would assert nothing.
    _CACHE.clear()
    first = _run_raw({"VERSION": "2026.01.02-3"})
    _CACHE.clear()
    second = _run_raw({"VERSION": "2026.01.02-3"})
    assert first == second, "export is not byte-reproducible at a fixed version"


@_needs_exporter
def test_export_is_structurally_usable_by_a_generator() -> None:
    """Cheap proxy for "a generator consumes it without hand-editing": every
    operation needs an ``operationId``, which is what generators name methods
    from — a missing one makes them invent a name off the path and produces
    an unstable client API across releases."""
    doc = _run({"VERSION": "2026.01.02-3"})
    methods = ("get", "post", "put", "patch", "delete")
    missing = [
        f"{method.upper()} {path}"
        for path, item in doc["paths"].items()
        for method, op in item.items()
        if method in methods and not op.get("operationId")
    ]
    assert not missing, f"operations without operationId: {missing[:10]}"
