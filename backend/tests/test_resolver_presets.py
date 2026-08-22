"""Resolver-preset catalogue + forwarder conflict detection (issue #877).

The catalogue's whole value is that its hostnames are right, so these
tests lean on structural invariants that stay true as entries are added,
plus a few hardcoded regressions for the traps that motivated the feature.
"""

from __future__ import annotations

import ipaddress

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.services.dns.resolver_presets import (
    all_presets,
    encrypted_only_presets,
    find_forwarder_conflict,
    preset_for_address,
)


async def _make_superadmin(db: AsyncSession, username: str = "rpsuper") -> tuple[User, str]:
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


def test_catalogue_is_structurally_sound() -> None:
    presets = all_presets()
    assert presets, "catalogue must not be empty"

    ids = [p.id for p in presets]
    assert len(ids) == len(set(ids)), "preset ids must be unique"

    for p in presets:
        assert p.ipv4, f"{p.id}: needs at least one IPv4 address"
        assert p.tls_hostname, f"{p.id}: needs a TLS hostname"
        # A bare hostname, not a URL or an IP — it goes into BIND's
        # remote-hostname, which validates a name.
        assert "/" not in p.tls_hostname and ":" not in p.tls_hostname
        assert "." in p.tls_hostname
        assert p.blocking_method in {"none", "nxdomain", "refused", "forged_address"}
        for addr in (*p.ipv4, *p.ipv6):
            ipaddress.ip_address(addr)  # raises if malformed
        for addr in p.ipv4:
            assert ipaddress.ip_address(addr).version == 4, f"{p.id}: {addr} in ipv4"
        for addr in p.ipv6:
            assert ipaddress.ip_address(addr).version == 6, f"{p.id}: {addr} in ipv6"


def test_no_address_belongs_to_two_presets() -> None:
    """Address -> preset must be unambiguous, or the conflict check and the
    UI's "which preset is this?" lookup would both be guessing."""
    seen: dict[str, str] = {}
    for p in all_presets():
        for addr in (*p.ipv4, *p.ipv6):
            # Compare by VALUE: two spellings of one IPv6 address in
            # different presets would be a real ambiguity a string
            # compare would wave through.
            key = str(ipaddress.ip_address(addr))
            assert key not in seen, f"{addr} claimed by both {seen.get(key)} and {p.id}"
            seen[key] = p.id


def test_every_address_resolves_back_to_its_preset() -> None:
    for p in all_presets():
        for addr in (*p.ipv4, *p.ipv6):
            assert preset_for_address(addr) is not None
            assert preset_for_address(addr).id == p.id  # type: ignore[union-attr]


def test_a_presets_own_addresses_never_conflict() -> None:
    """Every preset must be usable as-is — if its own address set tripped
    the conflict check, the picker would produce a config the API rejects."""
    for p in all_presets():
        assert find_forwarder_conflict([*p.ipv4, *p.ipv6]) is None, p.id


# ── Conflict detection ──────────────────────────────────────────────────


def test_mixing_two_upstreams_conflicts() -> None:
    conflict = find_forwarder_conflict(["1.1.1.1", "8.8.8.8"])
    assert conflict is not None
    # Exact tuple, not membership: this also pins that the conflict reports
    # exactly two names and that they come back sorted, which membership
    # checks would let drift. (It also keeps CodeQL from reading
    # ``"host.example" in x`` as URL-substring sanitisation — here ``x`` is
    # a tuple, so ``in`` is element equality, but the shapes look alike.)
    assert conflict.hostnames == ("cloudflare-dns.com", "dns.google")


def test_mixing_variants_of_one_brand_conflicts() -> None:
    """The trap this feature exists for: same brand, adjacent addresses,
    different certificate names — so it looks deliberate and still breaks."""
    conflict = find_forwarder_conflict(["1.1.1.1", "1.1.1.3"])
    assert conflict is not None
    assert conflict.preset_ids == ("cloudflare", "cloudflare-family")

    quad9 = find_forwarder_conflict(["9.9.9.9", "9.9.9.10"])
    assert quad9 is not None, "Quad9's variants also differ (dns. vs dns10.)"


def test_an_upstreams_own_pair_does_not_conflict() -> None:
    assert find_forwarder_conflict(["1.1.1.1", "1.0.0.1"]) is None
    assert find_forwarder_conflict(["9.9.9.9", "149.112.112.112"]) is None


def test_unrecognised_addresses_carry_no_opinion() -> None:
    """An internal resolver must stay configurable — we cannot know its
    certificate, so silence is the only correct answer."""
    assert find_forwarder_conflict(["10.0.0.1", "192.168.1.1"]) is None
    # …and one known address beside unknown ones is still not a conflict.
    assert find_forwarder_conflict(["1.1.1.1", "10.0.0.1"]) is None


def test_port_suffix_and_ipv6_are_understood() -> None:
    assert preset_for_address("1.1.1.1@853") is not None
    assert find_forwarder_conflict(["1.1.1.1@853", "8.8.8.8@853"]) is not None
    assert preset_for_address("2606:4700:4700::1111") is not None
    # IPv6 contains colons but no @ — the port split must not mangle it.
    assert preset_for_address("2620:fe::fe").id == "quad9"  # type: ignore[union-attr]


def test_encrypted_only_upstreams_are_flagged() -> None:
    flagged = encrypted_only_presets(["194.242.2.4"])
    assert [p.id for p in flagged] == ["mullvad-base"]
    assert encrypted_only_presets(["1.1.1.1"]) == ()


def test_ipv6_is_matched_by_value_not_by_spelling() -> None:
    """One IPv6 address has many textual forms, and an operator may paste
    an expanded one. A string compare would miss it and every guard built
    on the index would fail OPEN — the SERVFAIL this feature refuses."""
    assert preset_for_address("2606:4700:4700:0:0:0:0:1111").id == "cloudflare"  # type: ignore[union-attr]
    assert preset_for_address("2606:4700:4700::1111").id == "cloudflare"  # type: ignore[union-attr]
    assert preset_for_address("2620:FE:0:0:0:0:0:FE").id == "quad9"  # type: ignore[union-attr]

    # …so the conflict and encrypted-only guards still fire on them.
    assert (
        find_forwarder_conflict(["2606:4700:4700:0:0:0:0:1111", "2001:4860:4860:0:0:0:0:8888"])
        is not None
    )
    assert [p.id for p in encrypted_only_presets(["2a07:e340:0:0:0:0:0:4"])] == ["mullvad-base"]


# ── API surface ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_presets_endpoint_returns_catalogue(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_superadmin(db_session)
    await db_session.commit()
    resp = await client.get(
        "/api/v1/dns/forwarder-presets",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"]
    assert len(body["presets"]) == len(all_presets())
    first = body["presets"][0]
    assert {"id", "name", "provider", "ipv4", "tls_hostname", "filtering"} <= set(first)


@pytest.mark.asyncio
async def test_conflicting_forwarders_rejected_on_dot(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_superadmin(db_session, "rpconflict")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    group = await client.post(
        "/api/v1/dns/groups",
        headers=headers,
        json={"name": "preset-conflict", "driver": "bind9"},
    )
    assert group.status_code in (200, 201), group.text
    gid = group.json()["id"]

    resp = await client.put(
        f"/api/v1/dns/groups/{gid}/options",
        headers=headers,
        json={
            "forwarders": ["1.1.1.1", "1.1.1.3"],
            "forward_transport": "tls",
            "forward_tls_verify": True,
            "forward_tls_hostname": "cloudflare-dns.com",
        },
    )
    assert resp.status_code == 422, resp.text
    assert "certificate names" in resp.text

    # The same set is accepted on do53, where no certificate is checked and
    # the mix is odd but functional — the refusal must be about validation,
    # not taste.
    ok = await client.put(
        f"/api/v1/dns/groups/{gid}/options",
        headers=headers,
        json={"forwarders": ["1.1.1.1", "1.1.1.3"], "forward_transport": "do53"},
    )
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_manual_forwarders_are_never_constrained_by_the_catalogue(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The presets are a convenience, not a whitelist.

    An operator pointing at their own resolvers — with their own TLS
    hostname, on DoT with verification on — must save exactly as before
    this feature existed. Nothing about an unrecognised address may be
    treated as suspicious, because we cannot know anything about it.
    """
    _, token = await _make_superadmin(db_session, "rpmanual")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    group = await client.post(
        "/api/v1/dns/groups",
        headers=headers,
        json={"name": "preset-manual", "driver": "bind9"},
    )
    gid = group.json()["id"]

    # Wholly private upstreams, a hostname in no catalogue, DoT + verify on.
    resp = await client.put(
        f"/api/v1/dns/groups/{gid}/options",
        headers=headers,
        json={
            "forwarders": ["10.20.30.40", "10.20.30.41@8853"],
            "forward_transport": "tls",
            "forward_tls_verify": True,
            "forward_tls_hostname": "resolver.corp.internal",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["forwarders"] == ["10.20.30.40", "10.20.30.41@8853"]
    assert resp.json()["forward_tls_hostname"] == "resolver.corp.internal"

    # A catalogued address alongside private ones is still fine — one
    # recognised upstream cannot conflict with itself, and the unknown
    # ones contribute no opinion.
    mixed = await client.put(
        f"/api/v1/dns/groups/{gid}/options",
        headers=headers,
        json={
            "forwarders": ["1.1.1.1", "10.20.30.40"],
            "forward_transport": "tls",
            "forward_tls_verify": True,
            "forward_tls_hostname": "resolver.corp.internal",
        },
    )
    assert mixed.status_code == 200, mixed.text

    # And plain do53 with private upstreams, the most common setup of all.
    plain = await client.put(
        f"/api/v1/dns/groups/{gid}/options",
        headers=headers,
        json={"forwarders": ["192.168.1.53"], "forward_transport": "do53"},
    )
    assert plain.status_code == 200, plain.text


@pytest.mark.asyncio
async def test_encrypted_only_upstream_rejected_on_do53(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_superadmin(db_session, "rpmullvad")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    group = await client.post(
        "/api/v1/dns/groups",
        headers=headers,
        json={"name": "preset-encrypted-only", "driver": "bind9"},
    )
    gid = group.json()["id"]

    resp = await client.put(
        f"/api/v1/dns/groups/{gid}/options",
        headers=headers,
        json={"forwarders": ["194.242.2.4"], "forward_transport": "do53"},
    )
    assert resp.status_code == 422, resp.text
    assert "REFUSED" in resp.text

    ok = await client.put(
        f"/api/v1/dns/groups/{gid}/options",
        headers=headers,
        json={
            "forwarders": ["194.242.2.4"],
            "forward_transport": "tls",
            "forward_tls_verify": True,
            "forward_tls_hostname": "base.dns.mullvad.net",
        },
    )
    assert ok.status_code == 200, ok.text
