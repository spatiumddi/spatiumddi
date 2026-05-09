"""Tests for the DNS configuration importer (issue #128).

Two layers:

* **Parser layer** — ``parse_bind9_archive`` against synthetic
  tarballs / zips. Pure-Python; no DB dependency.
* **Commit layer** — preview + commit endpoints exercised through
  the FastAPI test client, with a real Postgres-backed session so
  the per-zone savepoint pattern + audit-log chain are covered
  end-to-end.

Phase 1 ships only BIND9; Phase 2 + 3 will add windows_dns + powerdns
parser tests under the same module.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from collections.abc import Iterable

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.dns import DNSRecord, DNSServerGroup, DNSZone
from app.services.dns_import import (
    ImportSourceError,
    parse_bind9_archive,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _build_tar(members: Iterable[tuple[str, str]]) -> bytes:
    """Build a .tar.gz archive in memory from ``(path, content)`` pairs."""
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tf:
        for path, content in members:
            data = content.encode("utf-8")
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return bio.getvalue()


def _build_zip(members: Iterable[tuple[str, str]]) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in members:
            zf.writestr(path, content)
    return bio.getvalue()


_BASIC_NAMED = """
options { directory "/var/named"; };
zone "example.com" {
    type master;
    file "example.com.zone";
};
"""

_BASIC_ZONE = """\
$TTL 3600
@   IN  SOA ns1.example.com. admin.example.com. (
        2026050901 3600 1800 1209600 3600 )
    IN  NS  ns1.example.com.
www IN  A   192.0.2.10
mail IN  MX  10 mail.example.com.
foo IN  CNAME example.com.
"""


async def _make_admin(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username="dnsimport-admin",
        email="dnsimport-admin@example.com",
        display_name="dnsimport-admin",
        hashed_password=hash_password("password123"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    token = create_access_token(str(user.id))
    return user, token


async def _make_group(db: AsyncSession, name: str = "test-grp") -> DNSServerGroup:
    g = DNSServerGroup(name=name)
    db.add(g)
    await db.flush()
    return g


# ── Parser layer ─────────────────────────────────────────────────────


def test_parse_basic_zone() -> None:
    archive = _build_tar(
        [
            ("etc/named.conf", _BASIC_NAMED),
            ("var/named/example.com.zone", _BASIC_ZONE),
        ]
    )
    preview = parse_bind9_archive(archive)
    assert preview.source == "bind9"
    assert len(preview.zones) == 1
    z = preview.zones[0]
    assert z.name == "example.com."
    assert z.zone_type == "primary"
    assert z.kind == "forward"
    assert z.soa is not None
    assert z.soa.serial == 2026050901
    assert {r.record_type for r in z.records} == {"NS", "A", "MX", "CNAME"}
    mx = next(r for r in z.records if r.record_type == "MX")
    assert mx.priority == 10
    assert mx.value == "mail.example.com."


def test_parse_zip_archive() -> None:
    """ZIP works the same as tar.gz — same payload, different framing."""
    archive = _build_zip(
        [
            ("etc/named.conf", _BASIC_NAMED),
            ("var/named/example.com.zone", _BASIC_ZONE),
        ]
    )
    preview = parse_bind9_archive(archive)
    assert len(preview.zones) == 1
    assert preview.zones[0].name == "example.com."


def test_reverse_zone_classification() -> None:
    """in-addr.arpa / ip6.arpa zones flag kind=reverse."""
    rev_named = """
zone "10.in-addr.arpa" { type master; file "10.zone"; };
zone "8.b.d.0.1.0.0.2.ip6.arpa" { type master; file "v6.zone"; };
"""
    zone = """\
$TTL 3600
@ IN SOA ns1. admin. (1 3600 1800 1209600 3600)
@ IN NS ns1.
"""
    archive = _build_tar(
        [
            ("named.conf", rev_named),
            ("10.zone", zone),
            ("v6.zone", zone),
        ]
    )
    preview = parse_bind9_archive(archive)
    kinds = {z.name: z.kind for z in preview.zones}
    assert kinds["10.in-addr.arpa."] == "reverse"
    assert kinds["8.b.d.0.1.0.0.2.ip6.arpa."] == "reverse"


def test_view_block_parsed_with_view_name() -> None:
    """Zones nested inside view {} blocks carry the view tag and
    the preview emits a multi-view warning."""
    named = """
view "internal" {
    zone "corp.example.com" { type master; file "corp.zone"; };
};
"""
    zone = """\
$TTL 3600
@ IN SOA ns1.corp. admin.corp. (1 3600 1800 1209600 3600)
@ IN NS ns1.corp.
"""
    archive = _build_tar([("named.conf", named), ("corp.zone", zone)])
    preview = parse_bind9_archive(archive)
    assert len(preview.zones) == 1
    assert preview.zones[0].name == "corp.example.com."
    assert preview.zones[0].view_name == "internal"
    assert any("view" in w.lower() for w in preview.warnings)


def test_dnssec_records_stripped_with_warning() -> None:
    """DNSSEC rdtypes (NSEC, DS, ...) get stripped on import + a
    warning surfaces in the per-zone parse_warnings.

    We use NSEC + DS here (rather than DNSKEY + RRSIG) because
    dnspython needs valid base64 / signature data to *parse* the
    record at all — fake placeholder data fails the whole zone.
    NSEC + DS have simpler text formats that dnspython accepts with
    arbitrary-but-syntactically-valid content; the strip-and-warn
    pipeline downstream is the same regardless of which DNSSEC type
    triggered it.
    """
    named = 'zone "ex.com" { type master; file "ex.zone"; };'
    zone = """\
$TTL 3600
@ IN SOA ns1. admin. (1 3600 1800 1209600 3600)
@ IN NS ns1.
@ IN A 192.0.2.1
@ 3600 IN NSEC ns1.ex.com. A NS RRSIG NSEC
@ 3600 IN DS 12345 8 2 0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF
"""
    archive = _build_tar([("named.conf", named), ("ex.zone", zone)])
    preview = parse_bind9_archive(archive)
    z = preview.zones[0]
    types = {r.record_type for r in z.records}
    assert "NSEC" not in types
    assert "DS" not in types
    assert "A" in types
    assert "NS" in types
    # Skipped histogram populated for the operator-facing preview.
    assert z.skipped_record_types == {"NSEC": 1, "DS": 1}
    assert any("DNSSEC" in w or "stripped" in w for w in z.parse_warnings)


def test_missing_named_conf_raises() -> None:
    archive = _build_tar([("var/named/some.zone", _BASIC_ZONE)])
    with pytest.raises(ImportSourceError, match="No named.conf"):
        parse_bind9_archive(archive)


def test_no_zones_raises() -> None:
    archive = _build_tar([("etc/named.conf", "options { listen-on { 127.0.0.1; }; };")])
    with pytest.raises(ImportSourceError, match="no zone declarations"):
        parse_bind9_archive(archive)


def test_path_traversal_rejected() -> None:
    """``../../etc/passwd`` member must be rejected before unpack writes."""
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tf:
        info = tarfile.TarInfo("../../etc/passwd")
        info.size = 5
        tf.addfile(info, io.BytesIO(b"hello"))
    with pytest.raises(ImportSourceError, match="escape"):
        parse_bind9_archive(bio.getvalue())


def test_empty_payload_raises() -> None:
    with pytest.raises(ImportSourceError, match="Empty"):
        parse_bind9_archive(b"")


def test_zone_file_missing_partial_import() -> None:
    """A zone whose file is missing imports the metadata but warns."""
    named = """
zone "ghost.com" { type master; file "ghost.zone"; };
zone "ok.com" { type master; file "ok.zone"; };
"""
    ok_zone = """\
$TTL 3600
@ IN SOA ns1.ok. admin.ok. (1 3600 1800 1209600 3600)
@ IN NS ns1.ok.
"""
    # ghost.zone NOT included
    archive = _build_tar([("named.conf", named), ("ok.zone", ok_zone)])
    preview = parse_bind9_archive(archive)
    by_name = {z.name: z for z in preview.zones}
    assert "ghost.com." in by_name
    assert "ok.com." in by_name
    # ghost has zero records but a warning explaining why
    ghost = by_name["ghost.com."]
    assert len(ghost.records) == 0
    assert any("not found" in w for w in ghost.parse_warnings)
    # ok has its full record set
    assert any(r.record_type == "NS" for r in by_name["ok.com."].records)


def test_comment_styles_stripped() -> None:
    """``//`` line, ``#`` line, and ``/* … */`` block comments all
    strip cleanly so a comment can't accidentally become part of a
    quoted name."""
    named = """
// line comment
# hash comment
/* block comment with "fake-zone-name" inside */
zone "real.com" { type master; file "real.zone"; };
"""
    zone = """\
$TTL 3600
@ IN SOA ns1.real. admin.real. (1 3600 1800 1209600 3600)
@ IN NS ns1.real.
"""
    archive = _build_tar([("named.conf", named), ("real.zone", zone)])
    preview = parse_bind9_archive(archive)
    names = {z.name for z in preview.zones}
    assert names == {"real.com."}


# ── Commit layer (HTTP integration) ──────────────────────────────────


@pytest.mark.asyncio
async def test_preview_endpoint_returns_canonical_shape(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_admin(db_session)
    group = await _make_group(db_session)
    await db_session.commit()

    archive = _build_tar(
        [
            ("etc/named.conf", _BASIC_NAMED),
            ("var/named/example.com.zone", _BASIC_ZONE),
        ]
    )
    resp = await client.post(
        "/api/v1/dns/import/bind9/preview",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("bind.tar.gz", archive, "application/gzip")},
        data={"target_group_id": str(group.id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "bind9"
    assert len(body["zones"]) == 1
    assert body["zones"][0]["name"] == "example.com."
    assert body["total_records"] == 4  # NS + A + MX + CNAME
    assert body["conflicts"] == []  # nothing exists in this group yet


@pytest.mark.asyncio
async def test_commit_creates_zone_with_provenance(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_admin(db_session)
    group = await _make_group(db_session)
    await db_session.commit()

    archive = _build_tar(
        [
            ("etc/named.conf", _BASIC_NAMED),
            ("var/named/example.com.zone", _BASIC_ZONE),
        ]
    )
    preview_resp = await client.post(
        "/api/v1/dns/import/bind9/preview",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("bind.tar.gz", archive, "application/gzip")},
        data={"target_group_id": str(group.id)},
    )
    assert preview_resp.status_code == 200
    plan = preview_resp.json()

    commit_resp = await client.post(
        "/api/v1/dns/import/bind9/commit",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "target_group_id": str(group.id),
            "plan": plan,
            "conflict_actions": {},
        },
    )
    assert commit_resp.status_code == 200, commit_resp.text
    body = commit_resp.json()
    assert body["total_zones_created"] == 1
    assert body["total_records_created"] == 4
    assert body["zones"][0]["action_taken"] == "created"

    # Provenance columns + audit log row landed.
    zone = (
        await db_session.execute(select(DNSZone).where(DNSZone.name == "example.com."))
    ).scalar_one()
    assert zone.import_source == "bind9"
    assert zone.imported_at is not None
    assert zone.last_serial == 2026050901
    records = (
        (await db_session.execute(select(DNSRecord).where(DNSRecord.zone_id == zone.id)))
        .scalars()
        .all()
    )
    assert len(records) == 4
    assert all(r.import_source == "bind9" for r in records)
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.resource_type == "dns_zone",
                AuditLog.resource_id == str(zone.id),
            )
        )
    ).scalar_one()
    assert audit.action == "create"
    assert audit.new_value is not None
    assert audit.new_value.get("import_source") == "bind9"
    assert audit.new_value.get("records_created") == 4


@pytest.mark.asyncio
async def test_commit_skip_on_conflict_default(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Re-running the same import without operator-supplied actions
    should skip the conflicting zone (default = skip)."""
    _, token = await _make_admin(db_session)
    group = await _make_group(db_session)
    await db_session.commit()

    archive = _build_tar(
        [
            ("etc/named.conf", _BASIC_NAMED),
            ("var/named/example.com.zone", _BASIC_ZONE),
        ]
    )

    async def _run() -> dict:
        preview_resp = await client.post(
            "/api/v1/dns/import/bind9/preview",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("bind.tar.gz", archive, "application/gzip")},
            data={"target_group_id": str(group.id)},
        )
        plan = preview_resp.json()
        commit_resp = await client.post(
            "/api/v1/dns/import/bind9/commit",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "target_group_id": str(group.id),
                "plan": plan,
                "conflict_actions": {},
            },
        )
        return commit_resp.json()

    first = await _run()
    assert first["total_zones_created"] == 1
    second = await _run()
    assert second["total_zones_skipped"] == 1
    assert second["total_zones_created"] == 0
    assert second["zones"][0]["action_taken"] == "skipped"


@pytest.mark.asyncio
async def test_commit_overwrite_replaces_existing(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_admin(db_session)
    group = await _make_group(db_session)
    await db_session.commit()

    archive = _build_tar(
        [
            ("etc/named.conf", _BASIC_NAMED),
            ("var/named/example.com.zone", _BASIC_ZONE),
        ]
    )

    # First import
    preview_resp = await client.post(
        "/api/v1/dns/import/bind9/preview",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("bind.tar.gz", archive, "application/gzip")},
        data={"target_group_id": str(group.id)},
    )
    plan = preview_resp.json()
    await client.post(
        "/api/v1/dns/import/bind9/commit",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "target_group_id": str(group.id),
            "plan": plan,
            "conflict_actions": {},
        },
    )

    # Second import with overwrite action
    preview_resp = await client.post(
        "/api/v1/dns/import/bind9/preview",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("bind.tar.gz", archive, "application/gzip")},
        data={"target_group_id": str(group.id)},
    )
    plan = preview_resp.json()
    assert len(plan["conflicts"]) == 1

    commit_resp = await client.post(
        "/api/v1/dns/import/bind9/commit",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "target_group_id": str(group.id),
            "plan": plan,
            "conflict_actions": {
                "example.com.": {"action": "overwrite", "rename_to": None},
            },
        },
    )
    body = commit_resp.json()
    assert body["total_zones_overwrote"] == 1
    assert body["zones"][0]["action_taken"] == "overwrote"
    assert body["zones"][0]["records_deleted"] == 4


@pytest.mark.asyncio
async def test_commit_rename_action(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_admin(db_session)
    group = await _make_group(db_session)
    await db_session.commit()

    archive = _build_tar(
        [
            ("etc/named.conf", _BASIC_NAMED),
            ("var/named/example.com.zone", _BASIC_ZONE),
        ]
    )

    # First import
    preview_resp = await client.post(
        "/api/v1/dns/import/bind9/preview",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("bind.tar.gz", archive, "application/gzip")},
        data={"target_group_id": str(group.id)},
    )
    plan = preview_resp.json()
    await client.post(
        "/api/v1/dns/import/bind9/commit",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "target_group_id": str(group.id),
            "plan": plan,
            "conflict_actions": {},
        },
    )

    # Re-import with rename
    preview_resp = await client.post(
        "/api/v1/dns/import/bind9/preview",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("bind.tar.gz", archive, "application/gzip")},
        data={"target_group_id": str(group.id)},
    )
    plan = preview_resp.json()
    commit_resp = await client.post(
        "/api/v1/dns/import/bind9/commit",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "target_group_id": str(group.id),
            "plan": plan,
            "conflict_actions": {
                "example.com.": {
                    "action": "rename",
                    "rename_to": "example-imported.com.",
                },
            },
        },
    )
    body = commit_resp.json()
    assert body["total_zones_renamed"] == 1
    # Both zones now exist
    names = {
        z.name
        for z in (await db_session.execute(select(DNSZone).where(DNSZone.group_id == group.id)))
        .scalars()
        .all()
    }
    assert names == {"example.com.", "example-imported.com."}


@pytest.mark.asyncio
async def test_commit_missing_group_returns_422(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    import uuid

    _, token = await _make_admin(db_session)
    await db_session.commit()

    archive = _build_tar(
        [
            ("etc/named.conf", _BASIC_NAMED),
            ("var/named/example.com.zone", _BASIC_ZONE),
        ]
    )
    preview = parse_bind9_archive(archive)
    fake_group = str(uuid.uuid4())
    commit_resp = await client.post(
        "/api/v1/dns/import/bind9/commit",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "target_group_id": fake_group,
            "plan": {
                "source": "bind9",
                "zones": [
                    {
                        "name": z.name,
                        "zone_type": z.zone_type,
                        "kind": z.kind,
                        "soa": z.soa.__dict__ if z.soa else None,
                        "records": [r.__dict__ for r in z.records],
                        "view_name": z.view_name,
                        "forwarders": z.forwarders,
                        "skipped_record_types": z.skipped_record_types,
                        "parse_warnings": z.parse_warnings,
                    }
                    for z in preview.zones
                ],
                "conflicts": [],
                "warnings": preview.warnings,
                "total_records": preview.total_records,
                "record_type_histogram": preview.record_type_histogram,
            },
            "conflict_actions": {},
        },
    )
    assert commit_resp.status_code == 422
    assert "does not exist" in commit_resp.json()["detail"]
