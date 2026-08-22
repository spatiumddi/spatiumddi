"""Built-in blocklist templates and profiles (issue #878).

The curated catalog already shipped *feeds* — remote URLs the refresh
task fetches on a cadence. Two things a content filter needs cannot be
expressed that way:

**Templates** are entry sets we ship inline instead of fetching. The
SafeSearch enforcement template is the motivating case: it is not a
blocklist at all but a set of RPZ *rewrites* pointing each search
engine at the provider's own filtered endpoint (``www.google.com`` ->
``forcesafesearch.google.com``). There is no upstream feed to
subscribe to — the mapping is published as documentation by each
provider — and the entries must never be replaced by a feed refresh,
so they land as ``source="manual"`` rows on a ``source_type="manual"``
list.

**Profiles** are compositions: a named set of feed ids plus template
ids applied in one action, so "make this network family-safe" is one
click rather than six.

Both live in ``dns_blocklist_catalog.json`` next to the feeds, because
they are the same kind of thing to an operator — something you pick
from a list and apply — and keeping one file means one thing to update
when a provider changes an endpoint.

A note on why the SafeSearch entries are exact-match, never wildcards:
Google's own Workspace documentation warns against rewriting any
YouTube hostname beyond the five it names, and a wildcard would also
catch the rewrite *target* — ``*.youtube.com`` would match
``restrict.youtube.com``, and BIND would resolve a CNAME loop into
SERVFAIL. :func:`template_entries` therefore emits ``is_wildcard=False``
and there is no knob to change it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "dns_blocklist_catalog.json"


@dataclass(frozen=True)
class TemplateGroup:
    """One provider's rewrite set within a template.

    Every domain in a group shares a single ``target``, which is what
    makes the catalog compact: 194 Google country domains are one
    target and a list of names, not 194 repeated pairs.
    """

    id: str
    name: str
    target: str
    domains: tuple[str, ...]
    default: bool
    note: str | None
    # Sibling groups this one cannot be combined with, because they cover
    # a shared domain with a different target (Strict vs Moderate
    # YouTube). Declared in the catalog so a picker can grey the pair out
    # instead of guessing from names; ``template_entries`` still detects
    # the overlap independently, so a missing declaration is a worse
    # error message, never a bad config.
    conflicts_with: tuple[str, ...]


@dataclass(frozen=True)
class BlocklistTemplate:
    id: str
    name: str
    description: str
    category: str
    block_mode: str
    groups: tuple[TemplateGroup, ...]

    def group(self, group_id: str) -> TemplateGroup | None:
        return next((g for g in self.groups if g.id == group_id), None)

    @property
    def default_group_ids(self) -> tuple[str, ...]:
        return tuple(g.id for g in self.groups if g.default)


@dataclass(frozen=True)
class BlocklistProfile:
    id: str
    name: str
    description: str
    source_ids: tuple[str, ...]
    template_ids: tuple[str, ...]
    note: str | None


@dataclass(frozen=True)
class TemplateEntry:
    """One rendered row, ready to become a ``DNSBlockListEntry``."""

    domain: str
    entry_type: str
    target: str
    is_wildcard: bool
    reason: str


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    with open(_CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def all_templates() -> tuple[BlocklistTemplate, ...]:
    return tuple(
        BlocklistTemplate(
            id=t["id"],
            name=t["name"],
            description=t["description"],
            category=t["category"],
            block_mode=t.get("block_mode", "nxdomain"),
            groups=tuple(
                TemplateGroup(
                    id=g["id"],
                    name=g["name"],
                    target=g["target"],
                    domains=tuple(g["domains"]),
                    default=bool(g.get("default", False)),
                    note=g.get("note") or None,
                    conflicts_with=tuple(g.get("conflicts_with", ())),
                )
                for g in t.get("groups", ())
            ),
        )
        for t in _catalog().get("templates", ())
    )


@lru_cache(maxsize=1)
def all_profiles() -> tuple[BlocklistProfile, ...]:
    return tuple(
        BlocklistProfile(
            id=p["id"],
            name=p["name"],
            description=p["description"],
            source_ids=tuple(p.get("source_ids", ())),
            template_ids=tuple(p.get("template_ids", ())),
            note=p.get("note") or None,
        )
        for p in _catalog().get("profiles", ())
    )


@lru_cache(maxsize=1)
def catalog_source_ids() -> frozenset[str]:
    """Ids of the *feed* entries in the same catalog file.

    The router owns the feed surface, so this deliberately returns ids
    only, not the entries: it exists so a profile's ``source_ids`` can be
    checked against something without either module importing the
    other's internals.
    """
    return frozenset(s["id"] for s in _catalog().get("sources", ()))


def template_for(template_id: str) -> BlocklistTemplate | None:
    return next((t for t in all_templates() if t.id == template_id), None)


def profile_for(profile_id: str) -> BlocklistProfile | None:
    return next((p for p in all_profiles() if p.id == profile_id), None)


class TemplateGroupConflict(ValueError):
    """Two selected groups rewrite the same domain to different targets.

    Real case: the Strict and Moderate YouTube groups cover the same
    five hostnames. Silently letting one win would produce a filter
    whose strength depends on dict ordering, and the
    ``(list_id, domain)`` unique constraint would reject the second
    insert anyway — so this is caught up front with an explanation
    instead of surfacing as an integrity error.
    """


def template_entries(
    template: BlocklistTemplate, group_ids: list[str] | None = None
) -> list[TemplateEntry]:
    """Render the selected groups into entry rows.

    ``group_ids=None`` selects the template's defaults. An explicit
    empty list is honoured as "nothing", not silently swapped for the
    defaults — the caller asked for no groups.
    """
    selected = template.default_group_ids if group_ids is None else tuple(group_ids)

    unknown = [gid for gid in selected if template.group(gid) is None]
    if unknown:
        raise ValueError(
            f"Unknown group(s) for template '{template.id}': {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(g.id for g in template.groups)}"
        )

    by_domain: dict[str, TemplateEntry] = {}
    for gid in selected:
        group = template.group(gid)
        assert group is not None  # guarded above
        for domain in group.domains:
            key = domain.lower().rstrip(".")
            prior = by_domain.get(key)
            if prior is not None and prior.target != group.target:
                raise TemplateGroupConflict(
                    f"Groups selected for '{template.name}' disagree about "
                    f"{key}: one rewrites it to {prior.target}, another to "
                    f"{group.target}. Pick one of them."
                )
            by_domain[key] = TemplateEntry(
                domain=key,
                entry_type="redirect",
                target=group.target,
                # Never a wildcard — see the module docstring.
                is_wildcard=False,
                reason=f"{template.name} — {group.name}",
            )

    return sorted(by_domain.values(), key=lambda e: e.domain)
