"""Zone-name canonicalisation, shared by every component that reads zone names (#981).

The API canonicalises a zone name on the way IN and reports the canonical
form on the way OUT, so a seeder that compares an API-returned name with
the string it POSTed never matches. ``ZoneCreate.validate_zone_name``
(backend/app/api/v1/dns/router.py) runs the name through
``validate_fqdn`` — which lower-cases every label — and appends the root
dot, so::

    POST {"name": "burst.DDIPG.test"}   →  stored/listed "burst.ddipg.test."

``_get_or_create_zone`` relied on that comparison for its whole 409
fallback, which meant the fallback could never fire: every re-seed of a
manifest naming a fixed zone died with "reported conflict (409) but was
not found in group …" while the zone sat in the list one trailing dot
away. Manifests with a fixed zone name (``gate.yaml``, the sizing
ladders, ddi-pg's burst template) were therefore single-use per
appliance, and ``reverse_zone_shape: per-octet`` brought one colliding
reverse zone per /16 on top.

Both halves of the canonicalisation matter. #981 reports the trailing
dot; the lower-casing is the same bug for any manifest that spells a
zone with a capital, and ``rstrip('.')`` alone would leave that one
live.

It lives in the shared spine rather than next to the seeder because the
seeder was not the only component reading zone names off the API and
comparing them to an undotted string. ``api_mutation_stream`` picked its
target zone with ``not name.endswith(".arpa")``, which is false for
``0.10.in-addr.arpa.`` — and since ``list_zones`` orders by name, that
reverse zone sorts first, so the mutation stream spent whole runs POSTing
``perf-op-*`` A records into a reverse zone. ``synthetic_ui_probe``
carried the same expression. Both now go through ``is_reverse_zone``.

Dependency-free on purpose: the comparison is testable without importing
any component that needs ``httpx``, so it runs under the hermetic
``make perf-test`` runner.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def normalize_zone_name(name: str) -> str:
    """Return the comparison key for a zone name.

    Lower-cased and stripped of the root dot, so a manifest's
    ``Burst.DDIPG.test`` and the API's ``burst.ddipg.test.`` compare
    equal. This is a comparison key, NOT the wire format: the seeder
    still POSTs the manifest's spelling and lets the API canonicalise.
    """
    return name.strip().rstrip(".").lower()


def is_reverse_zone(name: str) -> bool:
    """True for a reverse-lookup zone (``in-addr.arpa`` / ``ip6.arpa``).

    Suffix-matched on the canonical name, so the API's
    ``0.10.in-addr.arpa.`` is recognised. A raw ``endswith(".arpa")``
    against the stored name is always false — the root dot is last.
    """
    canonical = normalize_zone_name(name)
    return canonical == "arpa" or canonical.endswith(".arpa")


def find_existing_zone(zones: Iterable[Mapping[str, Any]], zname: str) -> Mapping[str, Any] | None:
    """Find the zone a 409 on ``zname`` was about, or None.

    ``zones`` is the decoded ``GET /v1/dns/groups/{gid}/zones`` body.

    Only an unviewed zone can be the culprit: the uniqueness constraint
    behind the 409 is ``(group_id, view_id, name)`` and the seeder POSTs
    no ``view_id``, so the conflict is necessarily with the ``view_id IS
    NULL`` row. A same-named zone in a view is a *different* zone —
    returning it would load the run's records behind that view's
    match-clients, where the query generator would never see them — so
    it is not accepted as a match.
    """
    wanted = normalize_zone_name(zname)
    for z in zones:
        if normalize_zone_name(str(z.get("name") or "")) != wanted:
            continue
        # Absent key reads as unviewed, which is also how a server that
        # omitted the field would behave.
        if z.get("view_id") is None:
            return z
    return None
