"""Adversarial input must not escape a handler as a 5xx.

Each case below was a live, reproducible unhandled exception on an
endpoint whose OpenAPI declares 422. They are grouped in one file
because they are one defect class, not four unrelated bugs: a value the
schema admits reaches code that cannot take it.

The ``Content-Disposition`` cases assert on the header VALUE rather
than on a status code. The ASGI transport these tests run over does not
encode headers the way a real server does, so a CR/LF that uvicorn
refuses (``RuntimeError: Invalid HTTP header value.``, connection
dropped with no response at all) sails through httpx and the test would
pass on a broken header. Asserting the header is latin-1-encodable and
control-character-free is what actually reproduces the production gate.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.content_disposition import content_disposition, slugify_filename_part
from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.services.appliance.tls import CSRSubject, TLSValidationError, generate_csr_and_key

# Values that broke one of the three handlers. Kept in one list so a new
# adversarial shape is added to every case at once.
HOSTILE = [
    pytest.param("9ᓴ", id="non-latin1"),
    pytest.param("a\nb", id="lf"),
    pytest.param("a\rb", id="cr"),
    pytest.param('a"; x="', id="quote-breakout"),
    pytest.param("a" * 500, id="very-long"),
    pytest.param("東京", id="all-non-ascii"),
]


async def _superadmin(db: AsyncSession) -> str:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


def _assert_sendable(header: str) -> None:
    """The three ways a Content-Disposition value breaks in production."""
    # 1. Starlette encodes response headers as latin-1.
    header.encode("latin-1")
    # 2. uvicorn's writer refuses a value containing CR or LF.
    assert "\r" not in header and "\n" not in header
    # 3. The quoted-string must not be closed early by an embedded quote,
    #    which would inject a second parameter into the header.
    assert header.count('"') == 2


# ── The header builder itself ────────────────────────────────────────


@pytest.mark.parametrize("value", HOSTILE)
def test_content_disposition_is_sendable(value: str) -> None:
    _assert_sendable(content_disposition(f"report-{value}.pdf"))


def test_content_disposition_keeps_the_original_name_in_filename_star() -> None:
    header = content_disposition("café-東京.pdf")
    # Percent-encoded UTF-8, so the unicode survives for a client that
    # reads RFC 6266 while the latin-1 ``filename`` stays ASCII.
    assert "filename*=UTF-8''caf%C3%A9-%E6%9D%B1%E4%BA%AC.pdf" in header
    assert 'filename="caf-.pdf"' in header


def test_content_disposition_never_emits_an_empty_filename() -> None:
    assert 'filename="download"' in content_disposition("東京")


def test_slugify_filename_part_matches_the_pdf_exporter_contract() -> None:
    # Behaviour test_ipam_pdf_export.py already pins, asserted here too so
    # the shared implementation can't drift away from its first caller.
    assert slugify_filename_part("AV multicast (543demo)") == "AV-multicast-543demo"
    assert slugify_filename_part("東京") == "export"
    assert slugify_filename_part("") == "export"
    assert slugify_filename_part("---") == "export"
    assert '"' not in slugify_filename_part('evil"; filename="pwned.pdf')
    assert len(slugify_filename_part("x" * 500)) <= 60


# ── GET /conformity/export.pdf ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("framework", HOSTILE)
async def test_conformity_export_pdf_survives_hostile_framework(
    client: AsyncClient, db_session: AsyncSession, framework: str
) -> None:
    token = await _superadmin(db_session)
    await db_session.commit()
    r = await client.get(
        "/api/v1/conformity/export.pdf",
        params={"framework": framework},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    _assert_sendable(r.headers["content-disposition"])


@pytest.mark.asyncio
async def test_conformity_export_pdf_normal_filename_unchanged(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _superadmin(db_session)
    await db_session.commit()
    r = await client.get(
        "/api/v1/conformity/export.pdf",
        params={"framework": "PCI DSS"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert 'filename="spatiumddi-conformity-pci-dss-' in r.headers["content-disposition"]


# ── POST /ipam/addresses/bulk-edit ───────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [[], ["admin"], {"a": 1}, 5, True])
async def test_bulk_edit_non_string_role_is_422(
    client: AsyncClient, db_session: AsyncSession, role: object
) -> None:
    # ``v not in IP_ROLES`` hashed the raw value against a frozenset; an
    # unhashable list raised TypeError out of the mode="before" validator.
    token = await _superadmin(db_session)
    await db_session.commit()
    r = await client.post(
        "/api/v1/ipam/addresses/bulk-edit",
        headers={"Authorization": f"Bearer {token}"},
        json={"address_ids": [str(uuid.uuid4())], "changes": {"role": role}},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_bulk_edit_unknown_string_role_still_names_the_valid_set(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _superadmin(db_session)
    await db_session.commit()
    r = await client.post(
        "/api/v1/ipam/addresses/bulk-edit",
        headers={"Authorization": f"Bearer {token}"},
        json={"address_ids": [str(uuid.uuid4())], "changes": {"role": "nope"}},
    )
    assert r.status_code == 422
    assert "role must be one of" in r.text


# ── POST /vrfs ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("targets", [5, True, 1.5, {"a": "b"}])
async def test_vrf_non_list_targets_is_422(
    client: AsyncClient, db_session: AsyncSession, targets: object
) -> None:
    # ``list(v)`` raised TypeError on a non-iterable; a dict quietly became
    # its key list, which is a route target the operator never sent.
    token = await _superadmin(db_session)
    await db_session.commit()
    r = await client.post(
        "/api/v1/vrfs",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": f"vrf-{uuid.uuid4().hex[:6]}", "import_targets": targets},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_vrf_comma_string_targets_still_accepted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # The liberal wire format the UI form sends must survive the guard.
    token = await _superadmin(db_session)
    await db_session.commit()
    r = await client.post(
        "/api/v1/vrfs",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": f"vrf-{uuid.uuid4().hex[:6]}", "import_targets": "65000:1, 65000:2"},
    )
    assert r.status_code in (200, 201), r.text
    assert r.json()["import_targets"] == ["65000:1", "65000:2"]


# ── POST /appliance/tls/csr ──────────────────────────────────────────


def test_csr_common_name_over_64_bytes_is_a_validation_error() -> None:
    with pytest.raises(TLSValidationError):
        generate_csr_and_key(CSRSubject(common_name="a" * 80), [], "rsa-2048")


def test_csr_multibyte_common_name_within_the_char_limit_is_a_validation_error() -> None:
    # 30 characters — inside the schema's 255-char bound — but 90 bytes,
    # which is what X.509 actually counts.
    with pytest.raises(TLSValidationError):
        generate_csr_and_key(CSRSubject(common_name="你" * 30), [], "rsa-2048")


def test_csr_non_ascii_san_is_a_validation_error() -> None:
    with pytest.raises(TLSValidationError):
        generate_csr_and_key(CSRSubject(common_name="ok.test"), ["你好.test"], "rsa-2048")


def test_csr_valid_subject_and_sans_still_produce_a_csr() -> None:
    csr_pem, key_pem = generate_csr_and_key(
        CSRSubject(common_name="ok.test", country="US", organization="Acme"),
        ["a.example.com", "192.168.1.10"],
        "rsa-2048",
    )
    assert "BEGIN CERTIFICATE REQUEST" in csr_pem
    assert "BEGIN PRIVATE KEY" in key_pem
