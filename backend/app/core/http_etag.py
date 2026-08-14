"""RFC 9110 entity-tag formatting for the agent long-poll (#862).

The DNS / DHCP / looking-glass agent ``/config`` endpoints are conditional
long-polls: the server hands back an ETag, the agent echoes it as
``If-None-Match``, and the request parks on a held 304. That contract ran
on a validator that was not a valid entity-tag at all.

Two problems, one root cause.

**The value was unquoted.** RFC 9110 §8.8.3 defines
``entity-tag = [ weak ] opaque-tag`` where ``opaque-tag`` is a
*quoted-string*, so a bare ``sha256:<hex>`` is malformed. Nothing in our own
stack objected, which is exactly why it survived — the breakage only appears
once a conformant intermediary handles it.

**It was strong, so nginx deleted it.** A strong validator asserts
byte-for-byte identity of the representation, so nginx's gzip filter drops
it rather than lie about a body it just transformed; only weak validators
survive the compression pass. The appliance and compose nginx both gzip
``application/json``, so any client sending ``Accept-Encoding: gzip`` — the
default for python-requests, and so for third-party agents — got a 200 with
no ETag at all and nothing to condition the next poll on. Verified live:
identity → ``sha256:…``, gzip → header absent, and the same request with a
weak tag → header preserved through the compression.

A weak tag is also the semantically correct answer: gzipped and identity are
different representations of the same payload, and "semantically equivalent"
is precisely what ``W/`` means.

The bare ``sha256:<hex>`` stays the *internal* value — it is what the DB
columns, the bundle body's own ``etag`` field, ``structural_etag`` and the
agents' on-disk cache all hold. Only the wire representation changes, and
:func:`etag_matches` accepts every spelling, so an agent still sending the
old bare form matches on its very first poll after upgrade rather than
re-downloading a full bundle.
"""

from __future__ import annotations

__all__ = ["etag_matches", "format_etag"]


def format_etag(value: str) -> str:
    """Return ``value`` as a weak entity-tag suitable for the ETag header.

    Idempotent: a value already carrying the ``W/`` prefix is returned
    unchanged, so a caller that formats twice can't produce ``W/"W/"…""``.
    """
    if value.startswith(("W/", "w/")):
        return value
    return f'W/"{value}"'


def _unwrap(tag: str) -> str:
    """Strip the weak prefix and surrounding quotes from one entity-tag.

    Tolerates the bare, unquoted form we used to emit — that is the whole
    point, since agents cached it and will send it back after an upgrade.
    """
    tag = tag.strip()
    if tag[:2] in ("W/", "w/"):
        tag = tag[2:]
    if len(tag) >= 2 and tag.startswith('"') and tag.endswith('"'):
        tag = tag[1:-1]
    return tag


def etag_matches(if_none_match: str | None, current: str) -> bool:
    """True when an ``If-None-Match`` header names ``current``.

    ``current`` is the internal bare value. Comparison is weak (RFC 9110
    §13.1.2 mandates the weak function for ``If-None-Match``), which is what
    lets a gzipped and an identity response share one tag.

    Handles the three shapes that reach us: the legacy bare value from an
    agent that has not re-fetched since upgrading, the quoted and weak forms
    from any conformant client, and a comma-separated list. ``*`` matches
    anything, per spec — it means "if any representation exists".
    """
    if not if_none_match:
        return False
    raw = if_none_match.strip()
    if raw == "*":
        return True
    # A comma inside an opaque-tag would be legal but we mint the value
    # ourselves and it is always ``sha256:<hex>``, so a plain split is safe.
    return any(_unwrap(part) == current for part in raw.split(",") if part.strip())
