"""Blocklist templates, profiles, and the SafeSearch data (issue #878).

The catalog's value is that its rewrite targets are right, so most of
these are structural invariants that survive new entries, plus hardcoded
regressions for the traps that motivated the feature.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dns import DNSBlockList, DNSBlockListEntry
from app.services.dns.blocklist_templates import (
    TemplateGroupConflict,
    all_profiles,
    all_templates,
    catalog_source_ids,
    profile_for,
    template_entries,
    template_for,
)
from app.services.dns_blocklist import parse_feed


async def _make_superadmin(db: AsyncSession, username: str = "bltsuper") -> tuple[User, str]:
    user = User(
        username=username,
        email=f"{username}@example.com",
        display_name=username,
        hashed_password=hash_password("password123"),
        auth_source="local",
        is_superadmin=True,
    )
    user.groups = []
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


# ── Catalogue invariants ────────────────────────────────────────────────


def test_templates_are_structurally_sound() -> None:
    templates = all_templates()
    assert templates, "catalog must ship at least one template"

    ids = [t.id for t in templates]
    assert len(ids) == len(set(ids))

    for t in templates:
        assert t.groups, f"{t.id}: a template with no groups can never be applied"
        gids = [g.id for g in t.groups]
        assert len(gids) == len(set(gids)), f"{t.id}: duplicate group ids"
        assert t.default_group_ids, f"{t.id}: no group is on by default"
        for g in t.groups:
            assert g.domains, f"{t.id}/{g.id}: no domains"
            # Targets are hostnames here, and they must be bare names —
            # they go into an RPZ CNAME right-hand side.
            assert "/" not in g.target and " " not in g.target
            assert "." in g.target
            for d in g.domains:
                assert d == d.lower().strip(), f"{g.id}: {d!r} not normalised"
                assert "." in d and not d.startswith("*")


def test_no_group_rewrites_its_own_target() -> None:
    """A group covering its own target is a CNAME loop.

    BIND follows the rewrite, matches the RPZ again, and SERVFAILs. It
    is the one way this data can take a search engine off the air rather
    than merely filtering it.
    """
    for t in all_templates():
        for g in t.groups:
            assert g.target not in g.domains, f"{t.id}/{g.id} rewrites {g.target} to itself"
            # …and no other group may capture it either.
            for other in t.groups:
                assert (
                    g.target not in other.domains
                ), f"{t.id}/{other.id} captures {t.id}/{g.id}'s target {g.target}"


def test_declared_conflicts_match_actual_domain_overlap() -> None:
    """``conflicts_with`` is shipped data the UI trusts to grey out a
    pair. Re-derive it from the domains so the declaration cannot drift
    away from what ``template_entries`` actually refuses."""
    for t in all_templates():
        for g in t.groups:
            expected = {
                o.id
                for o in t.groups
                if o.id != g.id and o.target != g.target and (set(o.domains) & set(g.domains))
            }
            assert (
                set(g.conflicts_with) == expected
            ), f"{t.id}/{g.id}: declares {sorted(g.conflicts_with)}, overlaps {sorted(expected)}"


def test_default_groups_never_conflict() -> None:
    """The defaults must apply as-is, or the picker's initial state is a
    configuration the API rejects."""
    for t in all_templates():
        assert template_entries(t, list(t.default_group_ids))


def test_profiles_reference_things_that_exist() -> None:
    source_ids = catalog_source_ids()
    template_ids = {t.id for t in all_templates()}
    assert all_profiles(), "catalog must ship at least one profile"
    for p in all_profiles():
        assert p.source_ids or p.template_ids, f"{p.id}: applies nothing"
        for sid in p.source_ids:
            assert sid in source_ids, f"{p.id} references unknown source {sid}"
        for tid in p.template_ids:
            assert tid in template_ids, f"{p.id} references unknown template {tid}"


# ── SafeSearch specifics ────────────────────────────────────────────────


def test_safesearch_entries_are_never_wildcards() -> None:
    """A wildcard on a rewritten name would also match the rewrite
    target's own subdomain and loop; Google's docs additionally warn
    against rewriting any YouTube hostname beyond the five they name."""
    t = template_for("safesearch")
    assert t is not None
    # Every group, one from each mutually-exclusive pair — so this covers
    # the optional groups (Yandex, YouTube Moderate) the defaults skip.
    excluded: set[str] = set()
    widest: list[str] = []
    for g in t.groups:
        if g.id in excluded:
            continue
        widest.append(g.id)
        excluded.update(g.conflicts_with)

    entries = template_entries(t, widest)
    assert entries
    for e in entries:
        assert e.is_wildcard is False, e.domain
        assert e.entry_type == "redirect"
        assert e.target


def test_google_covers_country_domains_not_just_dot_com() -> None:
    """A www.google.com-only rule is bypassed by typing google.de."""
    t = template_for("safesearch")
    assert t is not None
    google = t.group("google")
    assert google is not None
    assert google.target == "forcesafesearch.google.com"
    for d in ("www.google.com", "www.google.co.uk", "www.google.de", "www.google.ca"):
        assert d in google.domains
    assert len(google.domains) > 100


def test_youtube_covers_exactly_the_documented_hostnames() -> None:
    """Google names five and warns that rewriting others breaks playback."""
    documented = {
        "www.youtube.com",
        "m.youtube.com",
        "youtubei.googleapis.com",
        "youtube.googleapis.com",
        "www.youtube-nocookie.com",
    }
    t = template_for("safesearch")
    assert t is not None
    for gid, target in (
        ("youtube-strict", "restrict.youtube.com"),
        ("youtube-moderate", "restrictmoderate.youtube.com"),
    ):
        g = t.group(gid)
        assert g is not None
        assert set(g.domains) == documented, gid
        assert g.target == target
    # Named explicitly by Google as must-NOT-rewrite.
    every_domain = {d for g in t.groups for d in g.domains}
    for forbidden in ("youtube.com", "youtu.be", "s.ytimg.com", "googleapis.com"):
        assert forbidden not in every_domain


def test_bing_includes_the_copilot_sidebar_entry_point() -> None:
    """Without edgeservices.bing.com the Edge sidebar answers unfiltered,
    which looks like the filter simply not working."""
    t = template_for("safesearch")
    assert t is not None
    bing = t.group("bing")
    assert bing is not None
    assert set(bing.domains) == {"www.bing.com", "edgeservices.bing.com"}


def test_selecting_both_youtube_modes_is_refused() -> None:
    t = template_for("safesearch")
    assert t is not None
    with pytest.raises(TemplateGroupConflict) as e:
        template_entries(t, ["youtube-strict", "youtube-moderate"])
    # Asserted on the exception's fields, not by searching its prose: an
    # operator needs to know WHICH domain collided and between which two
    # targets, and equality on those pins it exactly where a substring
    # check would pass on a half-written message.
    assert e.value.domain in {
        "www.youtube.com",
        "m.youtube.com",
        "youtubei.googleapis.com",
        "youtube.googleapis.com",
        "www.youtube-nocookie.com",
    }
    assert set(e.value.targets) == {
        "restrict.youtube.com",
        "restrictmoderate.youtube.com",
    }


def test_unknown_group_is_reported_by_name() -> None:
    t = template_for("safesearch")
    assert t is not None
    with pytest.raises(ValueError, match="nope"):
        template_entries(t, ["nope"])


def test_empty_selection_is_honoured_not_defaulted() -> None:
    """``None`` means defaults; ``[]`` means the caller asked for none.
    Silently substituting defaults would create a list they did not ask
    for."""
    t = template_for("safesearch")
    assert t is not None
    assert template_entries(t, []) == []
    assert template_entries(t, None)


# ── Feed parsing ────────────────────────────────────────────────────────


def test_wildcard_feed_syntax_is_stripped_not_stored() -> None:
    """OISD's ``domainswild`` and Hagezi's ``wildcard/`` publish
    ``*.example.com``. Stored literally, the RPZ rule matches subdomains
    only and the apex keeps resolving — the opposite of what the feed
    means."""
    out = parse_feed("*.ads.example.com\nplain.example.com\n", "domains")
    assert out == ["ads.example.com", "plain.example.com"]
    # Same domain in both spellings dedupes to one entry.
    assert parse_feed("*.a.example\na.example\n", "domains") == ["a.example"]


# ── API surface ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_catalog_returns_templates_and_profiles(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_superadmin(db_session)
    await db_session.commit()
    resp = await client.get(
        "/api/v1/dns/blocklists/catalog",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["templates"]) == len(all_templates())
    assert len(body["profiles"]) == len(all_profiles())

    ss = next(t for t in body["templates"] if t["id"] == "safesearch")
    google = next(g for g in ss["groups"] if g["id"] == "google")
    # Counts, not the domains themselves — 269 names would bloat every
    # picker render for nothing.
    assert google["domain_count"] > 100
    assert "domains" not in google
    assert google["target"] == "forcesafesearch.google.com"


@pytest.mark.asyncio
async def test_from_template_creates_a_populated_manual_list(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_superadmin(db_session, "blttmpl")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/dns/blocklists/from-template",
        headers=headers,
        json={"template_id": "safesearch", "group_ids": ["bing", "duckduckgo"]},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    list_id = body["id"]
    assert body["source_type"] == "manual"
    assert body["feed_url"] is None
    # Nothing to refresh, so the scheduled sweep must not pick it up.
    assert body["update_interval_hours"] == 0
    assert body["entry_count"] == 5  # 2 Bing + 3 DuckDuckGo

    entries = (
        await client.get(f"/api/v1/dns/blocklists/{list_id}/entries", headers=headers)
    ).json()
    by_domain = {e["domain"]: e for e in entries["items"]}
    assert by_domain["www.bing.com"]["target"] == "strict.bing.com"
    assert by_domain["www.bing.com"]["entry_type"] == "redirect"
    assert by_domain["www.bing.com"]["is_wildcard"] is False
    # source="manual", not "feed": the refresh task deletes feed-sourced
    # rows that are absent from a fetch, and there is no fetch here.
    assert all(e["source"] == "manual" for e in entries["items"])


@pytest.mark.asyncio
async def test_from_template_rejects_conflicting_groups(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_superadmin(db_session, "bltconf")
    await db_session.commit()
    resp = await client.post(
        "/api/v1/dns/blocklists/from-template",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "template_id": "safesearch",
            "group_ids": ["youtube-strict", "youtube-moderate"],
        },
    )
    assert resp.status_code == 422, resp.text
    # The 422 must explain the collision rather than say "invalid": the
    # operator picked two groups that look independent in the picker, and
    # the detail is the only place the mutual exclusion is stated.
    detail = resp.json()["detail"]
    assert detail.startswith("Groups selected for 'SafeSearch enforcement' disagree about ")
    assert detail.endswith("Pick one of them.")
    # …and nothing was created by the rejected call.
    listed = await client.get(
        "/api/v1/dns/blocklists",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listed.json() == []


@pytest.mark.asyncio
async def test_from_template_rejects_empty_and_unknown(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_superadmin(db_session, "bltempty")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    empty = await client.post(
        "/api/v1/dns/blocklists/from-template",
        headers=headers,
        json={"template_id": "safesearch", "group_ids": []},
    )
    assert empty.status_code == 400, empty.text

    unknown = await client.post(
        "/api/v1/dns/blocklists/from-template",
        headers=headers,
        json={"template_id": "does-not-exist"},
    )
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_apply_profile_creates_everything_then_skips_on_replay(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_superadmin(db_session, "bltprof")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    profile = profile_for("family")
    assert profile is not None
    expected = len(profile.source_ids) + len(profile.template_ids)

    first = await client.post(
        "/api/v1/dns/blocklists/apply-profile",
        headers=headers,
        json={"profile_id": "family"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["created"] == expected
    assert first.json()["skipped"] == 0

    # The template list is populated inline; the feed lists are created
    # empty and filled by the refresh task (not run here).
    rows = (await client.get("/api/v1/dns/blocklists", headers=headers)).json()
    by_name = {r["name"]: r for r in rows}
    ss = by_name["SafeSearch enforcement"]
    assert ss["source_type"] == "manual"
    assert ss["entry_count"] > 0
    assert by_name["Hagezi NSFW"]["source_type"] == "url"

    # Re-applying is safe and reports what it skipped, so a profile that
    # gains a source in a later release can be re-run for just that one.
    second = await client.post(
        "/api/v1/dns/blocklists/apply-profile",
        headers=headers,
        json={"profile_id": "family"},
    )
    assert second.status_code == 200
    assert second.json()["created"] == 0
    assert second.json()["skipped"] == expected
    assert all(i["status"] == "skipped_existing" for i in second.json()["items"])

    # …and nothing was duplicated.
    total = await db_session.scalar(select(func.count()).select_from(DNSBlockList))
    assert total == expected


@pytest.mark.asyncio
async def test_applied_profile_assigns_to_nothing(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Auto-scoping would filter the server VLAN along with the kids' one.
    Assignment stays a deliberate second step."""
    _, token = await _make_superadmin(db_session, "bltscope")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    await client.post(
        "/api/v1/dns/blocklists/apply-profile",
        headers=headers,
        json={"profile_id": "family"},
    )
    rows = (await client.get("/api/v1/dns/blocklists", headers=headers)).json()
    assert rows
    for r in rows:
        assert r["applied_group_ids"] == []
        assert r["applied_view_ids"] == []


@pytest.mark.asyncio
async def test_profile_is_audited_per_list(db_session: AsyncSession, client: AsyncClient) -> None:
    from app.models.audit import AuditLog

    _, token = await _make_superadmin(db_session, "bltaudit")
    await db_session.commit()
    await client.post(
        "/api/v1/dns/blocklists/apply-profile",
        headers={"Authorization": f"Bearer {token}"},
        json={"profile_id": "family"},
    )
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.resource_type == "dns_blocklist")
            )
        )
        .scalars()
        .all()
    )
    created = await db_session.scalar(select(func.count()).select_from(DNSBlockList))
    assert len(rows) == created


@pytest.mark.asyncio
async def test_template_entries_survive_a_name_clash(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Two lists cannot share a name, and the create path must say so
    rather than half-creating a list with orphaned entries."""
    _, token = await _make_superadmin(db_session, "bltclash")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    first = await client.post(
        "/api/v1/dns/blocklists/from-template",
        headers=headers,
        json={"template_id": "safesearch", "name": "SS"},
    )
    assert first.status_code == 201

    clash = await client.post(
        "/api/v1/dns/blocklists/from-template",
        headers=headers,
        json={"template_id": "safesearch", "name": "SS"},
    )
    assert clash.status_code == 409

    lists = await db_session.scalar(select(func.count()).select_from(DNSBlockList))
    assert lists == 1
    entries = await db_session.scalar(select(func.count()).select_from(DNSBlockListEntry))
    assert entries == first.json()["entry_count"]
