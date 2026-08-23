"""A client-supplied dangling reference is a 4xx, not a 500 (#922).

``POST /api/v1/dhcp/servers`` with a well-formed but nonexistent
``server_group_id`` answered an unhandled 500: #861's global handler maps
unique violations (23505) to 409 and re-raises every other integrity error,
because a NOT NULL / CHECK / foreign-key violation generally means *our* code
is wrong and a 4xx would both misattribute it and hide it from the
conformance fuzz's no-5xx assertion.

Foreign keys are the one arm where that is only half true. A reference the
CLIENT sent is an ordinary client error; the same violation on a value the
SERVER computed is exactly the bug #861 was protecting. So the discriminator
is whether the offending value — which Postgres names in ``DETAIL`` — appears
in what the request actually carried, and the unit tests below pin both
directions of that, including the case that must still reach the 500 path.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.integrity_errors import (
    classify_foreign_key_violation,
    client_supplied_values,
    parse_foreign_key_detail,
)
from app.core.security import create_access_token, hash_password
from app.models.auth import User

_MISSING = 'Key (server_group_id)=(abf64806-1111-2222-3333-444455556666) is not present in table "dhcp_server_group".'  # noqa: E501
_REFERENCED = 'Key (id)=(11111111-1111-4111-8111-111111111111) is still referenced from table "dhcp_scope".'  # noqa: E501


async def _superadmin_token(db: AsyncSession) -> str:
    user = User(
        username=f"fk-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="FK Probe",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


def test_parses_a_missing_reference() -> None:
    violation = parse_foreign_key_detail(_MISSING)
    assert violation is not None
    assert violation.columns == ("server_group_id",)
    assert violation.values == ("abf64806-1111-2222-3333-444455556666",)
    assert violation.table == "dhcp_server_group"
    assert violation.dangling is True


def test_parses_a_still_referenced_row() -> None:
    violation = parse_foreign_key_detail(_REFERENCED)
    assert violation is not None
    assert violation.dangling is False
    assert violation.table == "dhcp_scope"


def test_unparseable_detail_returns_none() -> None:
    """None means re-raise, so an unfamiliar message can never be downgraded."""
    assert parse_foreign_key_detail("some driver we have never seen") is None
    assert parse_foreign_key_detail("") is None


def test_client_supplied_values_walks_nested_json() -> None:
    body = b'{"a": {"b": ["deep-value", 7]}, "c": "top"}'
    supplied = client_supplied_values(body, {"pid": "path-value"}, "q=query-value&empty=")
    assert {"deep-value", "7", "top", "path-value", "query-value"} <= supplied


def test_booleans_and_null_are_not_treated_as_supplied_values() -> None:
    """``str(True)`` is "True", which could collide with a real string value."""
    supplied = client_supplied_values(b'{"flag": true, "gone": null}', {}, "")
    assert "True" not in supplied
    assert "None" not in supplied


def test_dangling_reference_from_the_body_is_422() -> None:
    body = b'{"name": "x", "server_group_id": "abf64806-1111-2222-3333-444455556666"}'
    assert classify_foreign_key_violation(_MISSING, body, {}, "") == (
        422,
        "server_group_id references a dhcp_server_group row that does not exist.",
    )


def test_still_referenced_row_from_the_path_is_409() -> None:
    result = classify_foreign_key_violation(
        _REFERENCED, None, {"server_id": "11111111-1111-4111-8111-111111111111"}, ""
    )
    assert result is not None
    assert result[0] == 409


def test_value_the_request_never_carried_still_reaches_the_500_path() -> None:
    """The guarantee #861 asked for: a SERVER-side dangling reference stays a 500.

    Returning None here is what makes the handler re-raise. If this ever
    starts returning a 4xx, a genuine server bug becomes invisible to the
    no-5xx assertion that is the only thing watching for it.
    """
    assert classify_foreign_key_violation(_MISSING, b'{"name": "unrelated"}', {}, "") is None
    assert classify_foreign_key_violation(_MISSING, None, {}, "") is None


def test_partially_client_supplied_composite_key_is_not_downgraded() -> None:
    """Half a composite key from the server is still the server's bug."""
    detail = 'Key (a, b)=(from-client, from-server) is not present in table "t".'
    assert classify_foreign_key_violation(detail, b'{"a": "from-client"}', {}, "") is None


@pytest.mark.asyncio
async def test_post_dhcp_server_with_missing_group_is_not_a_500(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The reported request, end to end."""
    token = await _superadmin_token(db_session)
    resp = await client.post(
        "/api/v1/dhcp/servers",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": f"fk-probe-{uuid.uuid4().hex[:6]}",
            "driver": "kea",
            "host": "10.99.99.99",
            "server_group_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code < 500, resp.text
    assert resp.status_code == 422, resp.text
    assert "does not exist" in resp.json()["detail"]
