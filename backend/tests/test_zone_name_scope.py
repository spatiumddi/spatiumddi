"""Zone name scope — TLD classification + the TLD registry (#986).

Three things are worth pinning here, in rough order of how badly they hurt
when wrong:

1. **The classification table.** Getting ``example.com`` or ``home.arpa``
   wrong is a wrong label on a screen. Getting the *ordering* wrong is
   worse: reverse zones would read as "reserved" and ``example.com`` as
   "public", which is the opposite of the advice the pill exists to give.
2. **The refresh guard.** A truncated download that got stored would
   relabel every public zone in the estate as "undelegated" in one action.
   The negative controls below are the point of the whole test file.
3. **The bundled file.** A botched release-prep regeneration should fail
   CI, not ship a registry with three TLDs in it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dns import TLDRegistrySnapshot
from app.services.dns import tld_registry
from app.services.dns.name_scope import classify_zone_name
from app.services.dns.tld_registry import (
    MIN_TLDS,
    TldPayloadError,
    TldRegistry,
    load_bundled,
    load_special_use,
    parse_tld_payload,
    resolve_effective,
)


async def _make_user(
    db: AsyncSession, *, superadmin: bool = True, username: str = "tldadmin"
) -> tuple[User, str]:
    user = User(
        username=username,
        email=f"{username}@example.com",
        display_name=username,
        hashed_password=hash_password("password123"),
        is_superadmin=superadmin,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


def _payload(version: str, tlds: list[str]) -> str:
    body = "\n".join(t.upper() for t in tlds)
    return f"# Version {version}, Last Updated Sat Sep  5 07:07:01 2026 UTC\n{body}\n"


def _full_payload(version: str = "2030010100", extra: list[str] | None = None) -> str:
    """A payload that passes the guard: MIN_TLDS entries plus the sentinels."""
    filler = [f"x{i:05d}" for i in range(MIN_TLDS)]
    return _payload(version, ["com", "net", "org", "arpa", *filler, *(extra or [])])


# ── Classification ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # The table straight out of the issue.
        ("corp.example.com.", "reserved"),
        ("ad.contoso.local.", "reserved"),
        ("lab.", "undelegated"),
        ("acme.lan.", "undelegated"),
        ("foo.internal.", "reserved"),
        ("home.arpa.", "reserved"),
        ("10.in-addr.arpa.", "reverse"),
        ("8.b.d.0.1.0.0.2.ip6.arpa.", "reverse"),
        ("xn--e1afmkfd.xn--p1ai.", "public"),
        ("example.arpa.", "public"),
        (".", "public"),
        # Ordering: reserved beats public even though .com is delegated,
        # and reverse beats both even though .arpa is delegated.
        ("example.com", "reserved"),
        ("notexample.com.", "public"),
        ("arpa.", "public"),
        # Label-wise suffix matching, not string endswith.
        ("mylocal.", "undelegated"),
        ("notinternal.", "undelegated"),
        # Case and trailing dot are normalised.
        ("MAIL.", "reserved"),
        ("X.Corp", "reserved"),
        # The three SSAC deliberately did NOT protect.
        ("x.lan.", "undelegated"),
        ("x.intranet.", "undelegated"),
        ("x.private.", "undelegated"),
    ],
)
def test_classification_table(name: str, expected: str) -> None:
    assert classify_zone_name(name).scope == expected


def test_local_flags_mdns_conflict_and_nothing_else_does() -> None:
    """The amber variant is ``.local`` alone. ``.internal`` is the blessed
    alternative and must not be dressed up as a warning."""
    assert classify_zone_name("ad.contoso.local.").mdns_conflict is True
    assert classify_zone_name("hq.internal.").mdns_conflict is False
    assert classify_zone_name("corp.example.com.").mdns_conflict is False


def test_reserved_entries_carry_an_rfc_and_a_reason() -> None:
    scope = classify_zone_name("home.arpa.")
    assert scope.rfc == "RFC 8375"
    assert scope.matched_suffix == "home.arpa"
    assert scope.reason


def test_undelegated_reason_points_at_internal_not_at_lan() -> None:
    """The hint must recommend ``.internal``. It must NOT imply ``.lan`` is
    reserved — SSAC considered that name and did not protect it."""
    reason = classify_zone_name("acme.lan.").reason
    assert ".internal" in reason
    assert classify_zone_name("acme.lan.").matched_suffix is None


def test_longest_special_use_suffix_wins() -> None:
    """``home.arpa`` is two labels; a one-label entry must not shadow it.

    Guards the branch directly: a one-label ``arpa`` special-use entry does
    not exist today, so the assertion is about the comparison, driven with
    a name that matches two entries of different lengths.
    """
    assert classify_zone_name("www.example.com.").matched_suffix == "example.com"


def test_effective_tld_set_is_honoured() -> None:
    """Classification uses the list it is handed, not just the bundled one —
    this is what makes an operator refresh actually change anything."""
    assert classify_zone_name("shop.zzznew").scope == "undelegated"
    assert classify_zone_name("shop.zzznew", tlds=frozenset({"zzznew"})).scope == "public"


def test_special_use_is_not_overridable_by_the_tld_list() -> None:
    """A TLD list that (absurdly) contained ``local`` must not turn a
    ``.local`` zone public: the special-use table is checked first and is
    never sourced from the download."""
    scope = classify_zone_name("ad.contoso.local", tlds=frozenset({"local"}))
    assert scope.scope == "reserved"


# ── Bundled data file ───────────────────────────────────────────────────────


def test_bundled_file_is_plausible() -> None:
    """A botched ``scripts/refresh_iana_tlds.py`` run fails here rather than
    shipping."""
    bundled = load_bundled()
    assert bundled.count >= 1400, f"only {bundled.count} TLDs bundled"
    assert bundled.version.isdigit() and len(bundled.version) == 10
    assert bundled.fetched_at is not None
    for sentinel in ("com", "net", "org", "arpa", "xn--p1ai"):
        assert sentinel in bundled.tlds


def test_bundled_special_use_table_is_intact() -> None:
    suffixes = {e["suffix"] for e in load_special_use()}
    # Every entry the issue enumerates. A regeneration that dropped these
    # would silently reclassify reserved zones as public.
    assert {
        "local",
        "localhost",
        "test",
        "example",
        "invalid",
        "onion",
        "alt",
        "internal",
        "home.arpa",
        "example.com",
        "example.net",
        "example.org",
        "corp",
        "home",
        "mail",
    } <= suffixes
    # And the three that must stay OUT of it.
    assert not ({"lan", "intranet", "private"} & suffixes)


# ── Payload guard ───────────────────────────────────────────────────────────


def test_parse_accepts_a_real_shaped_payload() -> None:
    version, tlds = parse_tld_payload(_full_payload("2026090500"))
    assert version == "2026090500"
    assert "com" in tlds
    # Stored lowercased and de-duplicated.
    assert tlds == sorted(set(tlds))


def test_parse_rejects_a_truncated_payload() -> None:
    with pytest.raises(TldPayloadError, match="at least"):
        parse_tld_payload(_payload("2026090500", ["com", "net", "org"]))


def test_parse_rejects_a_payload_missing_a_sentinel_tld() -> None:
    filler = [f"x{i:05d}" for i in range(MIN_TLDS + 10)]
    with pytest.raises(TldPayloadError, match="sentinel"):
        parse_tld_payload(_payload("2026090500", ["net", "org", "arpa", *filler]))


def test_parse_rejects_a_payload_with_no_version_header() -> None:
    body = "\n".join(t.upper() for t in ["com", "net", "org", "arpa"])
    with pytest.raises(TldPayloadError, match="Version"):
        parse_tld_payload(body)


# ── Snapshot preference ─────────────────────────────────────────────────────


def _snapshot(version: str) -> TldRegistry:
    return TldRegistry(
        tlds=frozenset({"com", "zzznew"}),
        version=version,
        fetched_at=datetime.now(UTC),
        source="x",
        origin="snapshot",
    )


def test_newer_snapshot_wins() -> None:
    bundled = load_bundled()
    assert resolve_effective(bundled, _snapshot("2999010100")).origin == "snapshot"


def test_older_snapshot_loses() -> None:
    """A year-old stored copy must not survive an upgrade that ships a newer
    bundled list — otherwise upgrading would silently lose TLDs."""
    bundled = load_bundled()
    assert resolve_effective(bundled, _snapshot("2000010100")).origin == "bundled"


def test_equal_version_prefers_bundled() -> None:
    bundled = load_bundled()
    assert resolve_effective(bundled, _snapshot(bundled.version)).origin == "bundled"


def test_unparseable_snapshot_version_never_outranks_a_numeric_one() -> None:
    bundled = load_bundled()
    assert resolve_effective(bundled, _snapshot("garbage")).origin == "bundled"


def test_empty_snapshot_is_ignored() -> None:
    bundled = load_bundled()
    empty = TldRegistry(
        tlds=frozenset(),
        version="2999010100",
        fetched_at=None,
        source="x",
        origin="snapshot",
    )
    assert resolve_effective(bundled, empty).origin == "bundled"


# ── API: registry read + refresh ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_tld_registry_reports_the_bundled_list(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session)
    resp = await client.get(
        "/api/v1/dns/tld-registry", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["origin"] == "bundled"
    assert body["count"] >= 1400
    assert body["snapshot_version"] is None


@pytest.mark.asyncio
async def test_refresh_stores_a_snapshot_and_audits(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, token = await _make_user(db_session)

    async def _fake() -> tuple[str, list[str]]:
        return "2999010100", ["com", "net", "org", "arpa", "zzznew"]

    monkeypatch.setattr("app.api.v1.dns.router.fetch_remote_payload", _fake)
    resp = await client.post(
        "/api/v1/dns/tld-registry/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["origin"] == "snapshot"
    assert body["version"] == "2999010100"

    row = (await db_session.execute(select(TLDRegistrySnapshot))).scalar_one()
    assert row.id == 1
    assert "zzznew" in row.tlds

    from app.models.audit import AuditLog

    audit = (
        (await db_session.execute(select(AuditLog).where(AuditLog.resource_type == "tld_registry")))
        .scalars()
        .all()
    )
    assert len(audit) == 1
    assert audit[0].action == "refresh"


@pytest.mark.asyncio
async def test_an_older_payload_is_stored_but_not_preferred(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refresh that comes back older than the bundled list still stores —
    it is a valid list — but must not take effect, or an upgrade would lose
    TLDs to whatever was cached before it. The card reports both so the
    operator is told why the button appeared to do nothing."""
    _, token = await _make_user(db_session, username="tldold")

    async def _old() -> tuple[str, list[str]]:
        return parse_tld_payload(_full_payload("2000010100"))

    monkeypatch.setattr("app.api.v1.dns.router.fetch_remote_payload", _old)
    resp = await client.post(
        "/api/v1/dns/tld-registry/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["origin"] == "bundled"
    assert body["snapshot_version"] == "2000010100"
    assert body["version"] == body["bundled_version"]

    row = (await db_session.execute(select(TLDRegistrySnapshot))).scalar_one()
    assert row.version == "2000010100"


@pytest.mark.asyncio
async def test_refresh_is_superadmin_only(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, token = await _make_user(db_session, superadmin=False, username="tldviewer")

    async def _fake() -> tuple[str, list[str]]:  # pragma: no cover - must not run
        raise AssertionError("a non-superadmin reached the outbound fetch")

    monkeypatch.setattr("app.api.v1.dns.router.fetch_remote_payload", _fake)
    resp = await client.post(
        "/api/v1/dns/tld-registry/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_a_bad_download_502s_and_leaves_the_previous_snapshot(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control this whole feature turns on. Storing a truncated
    payload would relabel every public zone in the estate at once."""
    _, token = await _make_user(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    good = _full_payload("2999010100", extra=["zzzkeep"])

    async def _good() -> tuple[str, list[str]]:
        return parse_tld_payload(good)

    monkeypatch.setattr("app.api.v1.dns.router.fetch_remote_payload", _good)
    assert (
        await client.post("/api/v1/dns/tld-registry/refresh", headers=headers)
    ).status_code == 200

    for bad in (
        _payload("2999010200", ["com", "net", "org"]),  # truncated
        _payload("2999010200", [f"x{i:05d}" for i in range(MIN_TLDS + 10)]),  # no com
    ):

        async def _bad(payload: str = bad) -> tuple[str, list[str]]:
            return parse_tld_payload(payload)

        monkeypatch.setattr("app.api.v1.dns.router.fetch_remote_payload", _bad)
        resp = await client.post("/api/v1/dns/tld-registry/refresh", headers=headers)
        assert resp.status_code == 502, resp.text
        assert "still in effect" in resp.json()["detail"]

    # The good snapshot survived both failures untouched.
    tld_registry.invalidate_effective_cache()
    await db_session.rollback()
    row = (await db_session.execute(select(TLDRegistrySnapshot))).scalar_one()
    assert row.version == "2999010100"
    assert "zzzkeep" in row.tlds


@pytest.mark.asyncio
async def test_an_unreachable_registry_502s_rather_than_500ing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.dns.tld_registry import TldFetchError

    _, token = await _make_user(db_session)

    async def _dead() -> tuple[str, list[str]]:
        raise TldFetchError("could not reach data.iana.org: connection refused")

    monkeypatch.setattr("app.api.v1.dns.router.fetch_remote_payload", _dead)
    resp = await client.post(
        "/api/v1/dns/tld-registry/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 502
    assert "Nothing was stored" in resp.json()["detail"]


# ── API: classify ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_endpoint_does_not_validate_the_name(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """It is called on every keystroke-burst while the operator types, so a
    half-finished name must classify rather than 422."""
    _, token = await _make_user(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    for partial in ("exa", "example.c", "acme.lan"):
        resp = await client.get(
            "/api/v1/dns/tld-registry/classify",
            params={"name": partial},
            headers=headers,
        )
        assert resp.status_code == 200, (partial, resp.text)
    resp = await client.get(
        "/api/v1/dns/tld-registry/classify",
        params={"name": "ad.contoso.local"},
        headers=headers,
    )
    assert resp.json()["scope"] == "reserved"
    assert resp.json()["mdns_conflict"] is True


# ── Serialisation on the zone list ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_zone_list_carries_every_scope(client: AsyncClient, db_session: AsyncSession) -> None:
    from app.models.dns import DNSServerGroup

    _, token = await _make_user(db_session, username="tldzones")
    headers = {"Authorization": f"Bearer {token}"}
    group = DNSServerGroup(name="scope-test")
    db_session.add(group)
    await db_session.flush()

    wanted = {
        "corp.example.com.": "reserved",
        "acme.lan.": "undelegated",
        "shop.io.": "public",
        "10.in-addr.arpa.": "reverse",
    }
    for name in wanted:
        resp = await client.post(
            f"/api/v1/dns/groups/{group.id}/zones",
            headers=headers,
            json={
                "name": name,
                "zone_type": "primary",
                "primary_ns": f"ns1.{name}",
                "admin_email": f"admin.{name}",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["name_scope"] == wanted[name]

    resp = await client.get(f"/api/v1/dns/groups/{group.id}/zones", headers=headers)
    assert resp.status_code == 200
    got = {z["name"]: z["name_scope"] for z in resp.json()}
    assert got == wanted

    detail = next(z for z in resp.json() if z["name"] == "acme.lan.")["name_scope_detail"]
    assert detail["scope"] == "undelegated"
    assert ".internal" in detail["reason"]


@pytest.mark.asyncio
async def test_a_stored_snapshot_changes_how_a_zone_is_classified(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end proof that the refresh is load-bearing rather than
    decorative: a zone reads "undelegated", IANA delegates the TLD, and after
    a refresh the same zone reads "public" with no edit to the zone."""
    from app.models.dns import DNSServerGroup

    _, token = await _make_user(db_session, username="tldsnap")
    headers = {"Authorization": f"Bearer {token}"}
    group = DNSServerGroup(name="snap-test")
    db_session.add(group)
    await db_session.flush()

    resp = await client.post(
        f"/api/v1/dns/groups/{group.id}/zones",
        headers=headers,
        json={
            "name": "shop.zzznew.",
            "zone_type": "primary",
            "primary_ns": "ns1.shop.zzznew.",
            "admin_email": "admin.shop.zzznew.",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["name_scope"] == "undelegated"

    async def _fake() -> tuple[str, list[str]]:
        return parse_tld_payload(_full_payload("2999010100", extra=["zzznew"]))

    monkeypatch.setattr("app.api.v1.dns.router.fetch_remote_payload", _fake)
    assert (
        await client.post("/api/v1/dns/tld-registry/refresh", headers=headers)
    ).status_code == 200

    resp = await client.get(f"/api/v1/dns/groups/{group.id}/zones", headers=headers)
    assert resp.json()[0]["name_scope"] == "public"


# ── MCP tool ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_list_zones_reports_and_filters_by_scope(
    db_session: AsyncSession,
) -> None:
    from app.models.dns import DNSServerGroup, DNSZone
    from app.services.ai.tools.dns import ListZonesArgs, list_dns_zones

    user, _ = await _make_user(db_session, username="tldmcp")
    group = DNSServerGroup(name="mcp-scope")
    db_session.add(group)
    await db_session.flush()
    for name in ("acme.lan.", "shop.io."):
        db_session.add(
            DNSZone(
                group_id=group.id,
                name=name,
                zone_type="primary",
                kind="forward",
                primary_ns=f"ns1.{name}",
                admin_email=f"admin.{name}",
            )
        )
    await db_session.flush()

    rows = await list_dns_zones(db_session, user, ListZonesArgs(group_id=str(group.id)))
    assert {r["name"]: r["name_scope"] for r in rows} == {
        "acme.lan.": "undelegated",
        "shop.io.": "public",
    }

    filtered = await list_dns_zones(
        db_session,
        user,
        ListZonesArgs(group_id=str(group.id), name_scope="undelegated"),
    )
    assert [r["name"] for r in filtered] == ["acme.lan."]


# ── Domain RDAP skip ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rdap_is_skipped_for_a_name_with_no_registry(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``.lan`` domain can never have RDAP data. Attempting the lookup and
    reporting "unreachable" is wrong twice over: it is not an outage, and it
    would send an internal-only name to a public registry on every tick."""
    from app.models.domain import Domain
    from app.services.domain_refresh import refresh_one_domain

    async def _must_not_run(name: str) -> None:  # pragma: no cover - guard
        raise AssertionError(f"RDAP was queried for {name}")

    async def _absent(name: str) -> str:
        return "absent"

    monkeypatch.setattr("app.services.domain_refresh.lookup_domain", _must_not_run)
    # ``.lan`` is ``undelegated``, which now defers to the live bootstrap —
    # stub it so the test makes no network call and does not depend on
    # whether the runner has one.
    monkeypatch.setattr("app.services.domain_refresh.rdap_service_state", _absent)

    d = Domain(name="corp.lan")
    db_session.add(d)
    await db_session.flush()

    result = await refresh_one_domain(d, interval_hours=24)
    assert d.whois_state == "n/a"
    assert result.rdap_reachable is False
    assert result.skipped_reason is not None
    assert "delegated" in result.skipped_reason
    # The attempt is still stamped so the beat sweep paces itself instead of
    # re-selecting the row every tick.
    assert d.whois_last_checked_at is not None
    assert d.next_check_at is not None


@pytest.mark.asyncio
async def test_an_undelegated_name_defers_to_the_live_bootstrap(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression the two-stage gate exists to prevent.

    Our TLD list is a snapshot. A TLD delegated since it was cut
    classifies ``undelegated`` here while RDAP would answer perfectly
    well — and skipping it would freeze ``expires_at`` forever, with
    ``domain_expiring`` alerts sitting on data that never refreshes. So
    ``undelegated`` asks the LIVE bootstrap, which is authoritative.
    """
    from app.models.domain import Domain
    from app.services.domain_refresh import refresh_one_domain

    called: list[str] = []

    async def _fake_lookup(name: str) -> dict[str, object]:
        called.append(name)
        return {"registrar": "New TLD Registrar", "nameservers": [], "raw": {}}

    async def _bootstrap_has_it(name: str) -> str:
        return "available"

    monkeypatch.setattr("app.services.domain_refresh.lookup_domain", _fake_lookup)
    monkeypatch.setattr("app.services.domain_refresh.rdap_service_state", _bootstrap_has_it)

    # Not in the bundled list, so the classifier says undelegated...
    d = Domain(name="example.zzznew")
    db_session.add(d)
    await db_session.flush()
    assert classify_zone_name(d.name).scope == "undelegated"

    result = await refresh_one_domain(d, interval_hours=24)
    # ...but the registry exists, so the lookup happened anyway.
    assert called == ["example.zzznew"]
    assert result.skipped_reason is None
    assert d.whois_state != "n/a"


@pytest.mark.asyncio
async def test_an_unreachable_bootstrap_never_marks_a_domain_n_a(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``unknown`` is not ``absent``. An IANA outage read as "no registry
    exists" would mark the whole estate n/a in a single tick."""
    from app.models.domain import Domain
    from app.services.domain_refresh import refresh_one_domain

    async def _lookup_fails(name: str) -> None:
        return None

    async def _bootstrap_down(name: str) -> str:
        return "unknown"

    monkeypatch.setattr("app.services.domain_refresh.lookup_domain", _lookup_fails)
    monkeypatch.setattr("app.services.domain_refresh.rdap_service_state", _bootstrap_down)

    d = Domain(name="example.zzznew")
    db_session.add(d)
    await db_session.flush()

    result = await refresh_one_domain(d, interval_hours=24)
    assert result.skipped_reason is None
    assert d.whois_state == "unreachable"


@pytest.mark.asyncio
async def test_a_reserved_name_is_settled_without_any_outbound_call(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``reserved`` / ``reverse`` are decided locally — no registry, and
    not even the bootstrap, is consulted."""
    from app.models.domain import Domain
    from app.services.domain_refresh import refresh_one_domain

    async def _must_not_run(name: str) -> object:  # pragma: no cover - guard
        raise AssertionError(f"an outbound call was made for {name}")

    monkeypatch.setattr("app.services.domain_refresh.lookup_domain", _must_not_run)
    monkeypatch.setattr("app.services.domain_refresh.rdap_service_state", _must_not_run)

    d = Domain(name="ad.contoso.local")
    db_session.add(d)
    await db_session.flush()

    result = await refresh_one_domain(d, interval_hours=24)
    assert d.whois_state == "n/a"
    assert result.skipped_reason is not None


@pytest.mark.asyncio
async def test_rdap_service_state_distinguishes_absent_from_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import rdap

    async def _loaded() -> dict[str, str]:
        return {"com": "https://rdap.example/"}

    async def _empty() -> dict[str, str]:
        return {}

    monkeypatch.setattr(rdap, "_get_bootstrap", _loaded)
    assert await rdap.rdap_service_state("foo.com") == "available"
    assert await rdap.rdap_service_state("foo.lan") == "absent"

    monkeypatch.setattr(rdap, "_get_bootstrap", _empty)
    # IANA unreachable — must NOT read as "no registry exists".
    assert await rdap.rdap_service_state("foo.lan") == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "must_contain", "must_not_contain"),
    [
        # example.com IS under .com, a delegated TLD. Saying otherwise is
        # simply false, and it is the message the operator reads.
        ("example.com", "reserved special-use", "not under a delegated"),
        ("10.in-addr.arpa", "reverse-lookup", "not under a delegated"),
        # Only the genuinely-undelegated case gets that sentence.
        ("corp.lan", "not under a delegated", "reserved special-use"),
    ],
)
async def test_the_skip_reason_names_the_right_cause(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    must_contain: str,
    must_not_contain: str,
) -> None:
    from app.models.domain import Domain
    from app.services.domain_refresh import refresh_one_domain

    async def _must_not_run(n: str) -> object:  # pragma: no cover - guard
        raise AssertionError(f"an outbound lookup was made for {n}")

    async def _absent(n: str) -> str:
        return "absent"

    monkeypatch.setattr("app.services.domain_refresh.lookup_domain", _must_not_run)
    monkeypatch.setattr("app.services.domain_refresh.rdap_service_state", _absent)

    d = Domain(name=name)
    db_session.add(d)
    await db_session.flush()

    result = await refresh_one_domain(d, interval_hours=24)
    assert d.whois_state == "n/a"
    assert result.skipped_reason is not None
    assert must_contain in result.skipped_reason
    assert must_not_contain not in result.skipped_reason


@pytest.mark.asyncio
async def test_rdap_still_runs_for_a_public_name(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control for the skip: it must not swallow real lookups."""
    from app.models.domain import Domain
    from app.services.domain_refresh import refresh_one_domain

    called: list[str] = []

    async def _fake(name: str) -> dict[str, object]:
        called.append(name)
        return {"registrar": "Example Registrar", "nameservers": [], "raw": {}}

    monkeypatch.setattr("app.services.domain_refresh.lookup_domain", _fake)

    d = Domain(name="example.io")
    db_session.add(d)
    await db_session.flush()

    result = await refresh_one_domain(d, interval_hours=24)
    assert called == ["example.io"]
    assert result.skipped_reason is None
    assert d.whois_state != "n/a"


@pytest.mark.asyncio
async def test_sweep_counts_a_skip_separately_from_an_unreachable_registry(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name with no registry is not a failed lookup. Folding the two
    together would report every .lan row as an unreachable registry on
    every tick — the exact mislabelling #986 exists to remove."""
    from app.models.domain import Domain
    from app.services.domain_refresh import refresh_one_domain

    async def _lookup_fails(name: str) -> None:
        return None

    async def _absent(name: str) -> str:
        return "absent"

    monkeypatch.setattr("app.services.domain_refresh.lookup_domain", _lookup_fails)
    monkeypatch.setattr("app.services.domain_refresh.rdap_service_state", _absent)

    skipped = Domain(name="corp.lan")
    broken = Domain(name="example.io")
    db_session.add_all([skipped, broken])
    await db_session.flush()

    r_skipped = await refresh_one_domain(skipped, interval_hours=24)
    r_broken = await refresh_one_domain(broken, interval_hours=24)

    # Both report rdap_reachable=False, which is why the counter cannot key
    # off that field alone — skipped_reason is the discriminator.
    assert r_skipped.rdap_reachable is False
    assert r_broken.rdap_reachable is False
    assert r_skipped.skipped_reason is not None
    assert r_broken.skipped_reason is None
    assert skipped.whois_state == "n/a"
    assert broken.whois_state == "unreachable"


@pytest.mark.asyncio
async def test_store_snapshot_does_not_invalidate_the_cache_before_the_commit(
    db_session: AsyncSession,
) -> None:
    """``store_snapshot`` has only flushed; the caller still has to commit.

    Dropping the cache there opens a window where a concurrent request in
    this process misses it, reads the row as it was BEFORE the refresh (the
    write is not visible to another session yet) and re-caches the stale
    list for a further 60 s — so the settings card would report the new
    version while every zone pill, importer preview and MCP read still
    classified against the old one.
    """
    from app.services.dns.tld_registry import effective_registry, store_snapshot

    assert (await effective_registry(db_session)).origin == "bundled"
    await store_snapshot(db_session, "2999010100", ["com", "net", "org", "arpa", "zzznew"])
    assert (
        await effective_registry(db_session)
    ).origin == "bundled", "store_snapshot invalidated the cache before the commit"
    await db_session.rollback()


@pytest.mark.asyncio
async def test_the_cache_is_actually_used_within_the_ttl(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated reads inside the TTL must hit the cache, not the database.

    This is the test that makes ``_cache_loaded_at`` demonstrably
    load-bearing rather than merely asserted to be. Delete the
    ``_cache_loaded_at = now`` line and the timestamp stays at its initial
    ``0.0``, so ``now - 0.0`` exceeds any TTL, every call re-queries, and
    this fails — which is the answer to a static analyser that reports the
    write as an unused global because it cannot see the read happening on
    the *next* invocation.
    """
    from app.services.dns import tld_registry as tr

    calls: list[int] = []
    real_load = tr.load_snapshot

    async def _counting(db: AsyncSession) -> object:
        calls.append(1)
        return await real_load(db)

    monkeypatch.setattr(tr, "load_snapshot", _counting)
    tr.invalidate_effective_cache()

    first = await tr.effective_registry(db_session)
    for _ in range(3):
        assert (await tr.effective_registry(db_session)).version == first.version
    assert calls == [1], f"expected one DB read inside the TTL, got {len(calls)}"

    # …and an explicit invalidation really does force the next read.
    tr.invalidate_effective_cache()
    await tr.effective_registry(db_session)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_the_refresh_endpoint_invalidates_after_committing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: once committed, the very next read must see it —
    including the frontend's own refetch right after the button click."""
    from app.services.dns.tld_registry import effective_registry

    _, token = await _make_user(db_session, username="tldcache")
    # Prime the cache with the bundled list so a stale hit would show.
    assert (await effective_registry(db_session)).origin == "bundled"

    async def _fake() -> tuple[str, list[str]]:
        return parse_tld_payload(_full_payload("2999010200", extra=["zzznew"]))

    monkeypatch.setattr("app.api.v1.dns.router.fetch_remote_payload", _fake)
    resp = await client.post(
        "/api/v1/dns/tld-registry/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert (await effective_registry(db_session)).origin == "snapshot"


# ── The release-prep script shares the product's guard ──────────────────────


def test_refresh_script_uses_the_shared_parser() -> None:
    """``scripts/refresh_iana_tlds.py`` must not grow its own copy of the
    guard. Two validators meant to agree is the #878 bug class; this asserts
    the script resolves to *this* function object."""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "refresh_iana_tlds.py"
    if not script.exists():
        # The dev container mounts backend/ only. CI checks out the whole
        # repo, so this DOES run there — it is not a permanently-green skip.
        pytest.skip(f"repo scripts/ not present at {script}")
    spec = importlib.util.spec_from_file_location("_refresh_iana_tlds_test", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Same source function, not merely a same-named one: the script loads
    # the module by file path (app/__init__ pulls in pydantic, which the
    # script must not need), so it is a distinct function OBJECT compiled
    # from the same lines. Comparing file + first line is what actually
    # proves there is no second copy.
    theirs = mod.parse_tld_payload.__code__
    ours = parse_tld_payload.__code__
    assert theirs.co_filename == ours.co_filename
    assert theirs.co_firstlineno == ours.co_firstlineno
    # And a bad payload really is rejected through that path.
    with pytest.raises(mod.TldPayloadError):
        mod.parse_tld_payload("# Version 1\nCOM\n")


def test_bundled_file_json_shape_is_what_the_script_writes() -> None:
    """Guards the keys the script preserves / replaces."""
    from importlib.resources import files

    payload = json.loads(files("app.data").joinpath("iana_tlds.json").read_text())
    assert set(payload) >= {"source", "version", "fetched_at", "tlds", "special_use"}
    for entry in payload["special_use"]:
        assert entry["suffix"] and entry["rfc"] and entry["reason"]
