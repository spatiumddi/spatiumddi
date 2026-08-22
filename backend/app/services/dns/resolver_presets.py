"""Curated catalogue of public upstream resolvers (issue #877).

Configuring forwarders is a free-text field, and since DoT upstream
forwarding landed (#50) it has carried a trap: with ``forward_transport
= tls`` and verification on, BIND validates the upstream certificate
against ONE group-level ``remote-hostname``, and a mismatch fails closed
(SERVFAIL) rather than degrading to plaintext. So an operator who knows
"Quad9 is 9.9.9.9" but not "…and its DoT name is dns.quad9.net" gets a
group that resolves nothing. These presets carry the address pair and
the hostname together so the two cannot be filled in inconsistently.

The unit here is the PRESET, not the provider brand, because the brands
publish several filtering variants on adjacent addresses with DIFFERENT
certificate names:

    1.1.1.1  -> cloudflare-dns.com
    1.1.1.2  -> security.cloudflare-dns.com
    1.1.1.3  -> family.cloudflare-dns.com
    9.9.9.9  -> dns.quad9.net
    9.9.9.10 -> dns10.quad9.net

Mixing two Cloudflare addresses therefore breaks in exactly the same way
as mixing Cloudflare with Google — one of them cannot match the single
hostname. :func:`find_forwarder_conflict` is what catches that.

Values are verified against each provider's own documentation at the
version stamped in the JSON. They are deliberately a small, checkable
set: a preset carrying a stale hostname is worse than no preset at all,
because it fails closed on every query.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "dns_resolver_presets.json"


@dataclass(frozen=True)
class ResolverPreset:
    """One selectable upstream: an address set plus the name its
    certificate presents."""

    id: str
    name: str
    provider: str
    description: str
    ipv4: tuple[str, ...]
    ipv6: tuple[str, ...]
    tls_hostname: str
    # What the provider filters by default, as a short operator-facing
    # label ("none", "malware", "malware + adult content", …). Shown in
    # the picker so nobody selects a filtering resolver by accident, or
    # expects filtering from one that does none.
    filtering: str
    # How a blocked name is answered: none | nxdomain | refused |
    # forged_address. Load-bearing, not trivia — an upstream that answers
    # a forged 0.0.0.0 for a blocked *signed* name produces bogus data,
    # so a downstream resolver doing DNSSEC validation turns the block
    # into SERVFAIL. Same fail-closed symptom this whole feature exists
    # to prevent, arriving from the other direction.
    blocking_method: str
    # True for upstreams that refuse plaintext 53 (Mullvad). Selecting
    # one without an encrypted transport breaks every query, so the API
    # refuses that combination outright.
    requires_encrypted: bool
    homepage: str
    # Operator-facing caveat, or None. Where a value is unusual or
    # contested this records why, so the next person editing the
    # catalogue does not "fix" a deliberate choice.
    notes: str | None


@dataclass(frozen=True)
class ForwarderConflict:
    """A forwarder set that cannot work with a single TLS hostname."""

    preset_ids: tuple[str, ...]
    hostnames: tuple[str, ...]
    message: str


@lru_cache(maxsize=1)
def _load() -> dict:
    with open(_CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def catalog_version() -> str:
    return str(_load().get("version", ""))


@lru_cache(maxsize=1)
def all_presets() -> tuple[ResolverPreset, ...]:
    return tuple(
        ResolverPreset(
            id=p["id"],
            name=p["name"],
            provider=p["provider"],
            description=p["description"],
            ipv4=tuple(p.get("ipv4", ())),
            ipv6=tuple(p.get("ipv6", ())),
            tls_hostname=p["tls_hostname"],
            filtering=p["filtering"],
            blocking_method=p["blocking_method"],
            requires_encrypted=bool(p.get("requires_encrypted", False)),
            homepage=p.get("homepage", ""),
            notes=p.get("notes") or None,
        )
        for p in _load()["presets"]
    )


def _normalise(addr: str) -> str:
    """Canonical form of an address, for comparison against the catalogue.

    IPv6 has many textual spellings of one address — ``2606:4700:4700::1111``
    and ``2606:4700:4700:0:0:0:0:1111`` are the same host — so a raw string
    compare would miss a catalogued upstream and every guard built on this
    index would fail OPEN, which is precisely the SERVFAIL the conflict
    check exists to refuse. Anything unparseable (a hostname, a typo) falls
    back to the lowercased text: it will simply not be in the catalogue,
    which is the correct "no opinion" answer.
    """
    text = addr.strip().lower()
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return text


@lru_cache(maxsize=1)
def _address_index() -> dict[str, ResolverPreset]:
    """Map every catalogued address, in canonical form, to its preset."""
    index: dict[str, ResolverPreset] = {}
    for preset in all_presets():
        for addr in (*preset.ipv4, *preset.ipv6):
            index[_normalise(addr)] = preset
    return index


def _bare_address(forwarder: str) -> str:
    """Strip the optional ``@port`` suffix the wire shape allows.

    Only the LAST ``@`` is treated as the separator so a bracketed or
    bare IPv6 literal (which contains colons, not ``@``) survives intact.
    """
    return forwarder.rsplit("@", 1)[0].strip().lower()


def preset_for_address(forwarder: str) -> ResolverPreset | None:
    """Return the preset a forwarder belongs to, or None if unrecognised."""
    return _address_index().get(_normalise(_bare_address(forwarder)))


def find_forwarder_conflict(forwarders: list[str]) -> ForwarderConflict | None:
    """Detect a forwarder set spanning two presets with different TLS names.

    Returns None when the set is fine *or* when we simply cannot tell —
    unrecognised addresses (an internal resolver, a provider we do not
    catalogue) carry no opinion, because refusing to configure a private
    upstream would be far worse than the mistake we are preventing.

    Only conflicting HOSTNAMES count. Two presets that happen to share a
    certificate name are not a conflict, and a caller comparing preset
    ids alone would produce a false positive there.
    """
    seen: dict[str, ResolverPreset] = {}
    for entry in forwarders:
        preset = preset_for_address(entry)
        if preset is not None:
            seen[preset.id] = preset

    hostnames = {p.tls_hostname for p in seen.values()}
    if len(hostnames) < 2:
        return None

    ordered = tuple(sorted(seen))
    names = tuple(sorted(hostnames))
    detail = ", ".join(
        f"{p.name} ({p.tls_hostname})" for p in sorted(seen.values(), key=lambda x: x.id)
    )
    return ForwarderConflict(
        preset_ids=ordered,
        hostnames=names,
        message=(
            f"These forwarders belong to upstreams that present different TLS "
            f"certificate names: {detail}. Only one hostname applies to the whole "
            f"group, so at least one of them would fail verification and return "
            f"SERVFAIL. Use one group per upstream, or pick addresses that share "
            f"a certificate name."
        ),
    )


def encrypted_only_presets(forwarders: list[str]) -> tuple[ResolverPreset, ...]:
    """Catalogued upstreams among ``forwarders`` that refuse plaintext 53.

    Mullvad publishes its resolvers for DoT/DoH only and answers REFUSED
    over port 53, so pointing a do53 group at one fails every query. That
    is worth refusing at config time rather than discovering in
    production.
    """
    found: dict[str, ResolverPreset] = {}
    for entry in forwarders:
        preset = preset_for_address(entry)
        if preset is not None and preset.requires_encrypted:
            found[preset.id] = preset
    return tuple(found.values())
