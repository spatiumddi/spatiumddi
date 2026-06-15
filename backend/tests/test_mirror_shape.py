"""#430 — shared shape guards for integration-mirror reads.

``require_list`` / ``require_keyed_list`` turn a wrong-shape 200 (proxy
error page, envelope change, ``data: null``) into a raised error so the
reconciler aborts and keeps last-known rows, instead of collapsing to zero
items and triggering the absence-delete mass-purge (the #426 defect class).
A legitimately-empty result stays valid and returns an empty list.
"""

from __future__ import annotations

import pytest

from app.services._mirror_shape import require_keyed_list, require_list


class _Boom(Exception):
    pass


def _err(msg: str) -> _Boom:
    return _Boom(msg)


# ── require_list ──────────────────────────────────────────────────────


def test_require_list_passes_through_a_list() -> None:
    assert require_list([1, 2], make_error=_err, context="x") == [1, 2]


def test_require_list_allows_empty_list() -> None:
    # A genuinely-empty collection is legitimate — must NOT raise.
    assert require_list([], make_error=_err, context="x") == []


@pytest.mark.parametrize("bad", [{}, {"a": 1}, None, "string", 0, 3.5])
def test_require_list_raises_on_non_list(bad: object) -> None:
    with pytest.raises(_Boom):
        require_list(bad, make_error=_err, context="ctx")


def test_require_list_raises_the_factory_type_with_context() -> None:
    with pytest.raises(_Boom, match="ctx"):
        require_list({}, make_error=_err, context="ctx")


# ── require_keyed_list ────────────────────────────────────────────────


def test_require_keyed_list_returns_the_keyed_list() -> None:
    assert require_keyed_list({"items": [1]}, "items", make_error=_err, context="x") == [1]


def test_require_keyed_list_allows_empty_keyed_list() -> None:
    # {"items": []} — a real empty cluster / tailnet — is legitimate.
    assert require_keyed_list({"items": []}, "items", make_error=_err, context="x") == []


@pytest.mark.parametrize(
    "bad",
    [
        {},  # key missing entirely (degraded)
        {"items": None},  # null instead of list
        {"items": {}},  # object instead of list
        [],  # not even a dict
        None,
        "string",
    ],
)
def test_require_keyed_list_raises_on_bad_shape(bad: object) -> None:
    with pytest.raises(_Boom):
        require_keyed_list(bad, "items", make_error=_err, context="ctx")
