"""Validation for DNS view names and address-match-lists (issue #876).

View scoping shipped with #24 but was only reachable through the API, so
the values in ``DNSView.match_clients`` were whatever an operator's script
put there. #876 puts a form in front of it, which makes validation
load-bearing rather than nice-to-have — both fields are interpolated
**verbatim** into ``named.conf``:

    view "{{ view.name }}" {
        match-clients { {% for c in view.match_clients %}{{ c }}; {% endfor %}};

Two ways that goes wrong, and neither is hypothetical:

* **A typo takes the group offline.** BIND rejects a malformed
  ``match-clients`` element, and the agent runs ``named-checkconf`` before
  swapping config in — so one bad CIDR doesn't corrupt one view, it stops
  the whole group's config converging, silently, until somebody reads the
  agent log. Same blast radius as the malformed-RPZ-zone bugs in #878.
* **The view name is also a directory name.** The agent writes RPZ zone
  files to ``/var/cache/bind/rpz/<view name>/`` and zone files to
  ``zones/<view name>/``, so a name containing ``/`` or ``..`` is a path
  traversal, and one containing a quote or brace escapes the ``view "…"``
  statement it is embedded in.

So this module is the gate. It rejects rather than sanitises: silently
rewriting an operator's ACL into something that "works" would be worse
than telling them it is wrong.
"""

from __future__ import annotations

import ipaddress
import re

__all__ = [
    "BUILTIN_ACLS",
    "RESERVED_VIEW_NAMES",
    "ViewValidationError",
    "validate_address_match_list",
    "validate_view_name",
]


class ViewValidationError(ValueError):
    """Carries the offending element so the caller can name it in the 422."""

    def __init__(self, message: str, *, field: str, value: str | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.value = value


# BIND's built-in address-match-list names. `none` and `any` are the two
# an operator actually reaches for; `localhost` / `localnets` are the
# server's own addresses and directly-attached networks.
BUILTIN_ACLS = frozenset({"any", "none", "localhost", "localnets"})

# ``_default`` is the view BIND uses when a client matches nothing and
# ``_bind`` is the built-in CHAOS view — declaring either is an error at
# config-parse time, which is exactly the failure this module exists to
# catch before it reaches a server.
RESERVED_VIEW_NAMES = frozenset({"_default", "_bind"})

# The per-view RPZ zone the agent renders is named
# ``spatium-blocklist-<view>.rpz.`` (see ``services/dns/agent_config.py``),
# and the first label of that name is a DNS label — capped at 63 **octets**
# by RFC 1035. ``spatium-blocklist-`` is 18 of them, so a view name longer
# than 45 renders a zone BIND refuses to load ("label too long"), taking
# the whole group's config down with it. That is the failure this module
# exists to catch, so the cap is enforced here rather than discovered
# later.
_RPZ_ZONE_PREFIX = "spatium-blocklist-"
MAX_VIEW_NAME_LEN = 63 - len(_RPZ_ZONE_PREFIX)

# Conservative: letters, digits, and the three separators that are safe
# both as a BIND identifier and as a single path segment. Deliberately no
# dot — a dotted view name is legal to BIND but reads as a hostname in the
# agent's ``zones/<view>/<zone>`` layout, and the ambiguity buys nothing.
_VIEW_NAME_RE = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9_-]{{0,{MAX_VIEW_NAME_LEN - 1}}}$")

# An ACL name defined via `DNSAcl`. A dot is allowed here (unlike a view
# name, which is also a path segment) because ``DNSAcl.name`` is free text
# up to 255 chars and BIND takes a dotted unquoted string happily — a name
# the operator has already created must not be rejected as "not a valid
# ACL name" when the real objection would be that it isn't defined. The
# character class stays injection-safe: no quote, brace, semicolon or
# whitespace can reach the rendered ``match-clients { … };``.
_ACL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")

_KEY_RE = re.compile(r"^key\s+([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")


def validate_view_name(name: str, *, field: str = "name") -> str:
    """Return the name, or raise :class:`ViewValidationError`.

    The name ends up in three places — the ``view "…"`` statement, a
    directory under the agent's cache, and the per-view RPZ zone path — so
    it is held to the strictest of the three.
    """
    candidate = (name or "").strip()
    if not candidate:
        raise ViewValidationError("View name is required.", field=field, value=name)
    if candidate.lower() in RESERVED_VIEW_NAMES:
        raise ViewValidationError(
            f"'{candidate}' is reserved by BIND and cannot be used as a view name.",
            field=field,
            value=candidate,
        )
    if not _VIEW_NAME_RE.match(candidate):
        raise ViewValidationError(
            "A view name must start with a letter or digit and contain only "
            f"letters, digits, hyphens and underscores (max {MAX_VIEW_NAME_LEN} "
            "characters). It is used as a BIND view name, as a directory name "
            "on the DNS agent, and as a label in the per-view blocking-list "
            "zone name.",
            field=field,
            value=candidate,
        )
    return candidate


def _validate_element(
    element: str,
    field: str,
    known_acls: frozenset[str],
    known_keys: frozenset[str],
) -> None:
    raw = (element or "").strip()
    if not raw:
        raise ViewValidationError(
            "Empty entry — remove it or replace it with an address, CIDR or ACL name.",
            field=field,
            value=element,
        )

    # A leading '!' negates the element ("everything except this"). BIND
    # allows whitespace after it; normalise before checking the rest.
    body = raw[1:].strip() if raw.startswith("!") else raw
    if not body:
        raise ViewValidationError(
            "'!' must be followed by an address, CIDR or ACL name.",
            field=field,
            value=element,
        )

    lowered = body.lower()
    if lowered in BUILTIN_ACLS:
        return

    # ``key <name>`` matches queries signed with that TSIG key. The key has
    # to be one the group actually ships to the agent — an undefined key
    # name is an undefined symbol in named.conf, exactly as an undefined
    # ACL name is, and fails the whole bundle rather than the one view.
    key_match = _KEY_RE.match(body)
    if key_match:
        key_name = key_match.group(1)
        if key_name not in known_keys:
            known = ", ".join(sorted(known_keys)) if known_keys else "none defined"
            raise ViewValidationError(
                f"'{key_name}' is not a TSIG key defined in this server group "
                f"(available: {known}). Define it on the TSIG keys tab first.",
                field=field,
                value=element,
            )
        return

    # An address or prefix. ``strict=False`` so 10.0.0.5/24 is accepted the
    # way BIND accepts it, rather than rejected for having host bits set.
    if "/" in body:
        try:
            ipaddress.ip_network(body, strict=False)
        except ValueError as exc:
            raise ViewValidationError(
                f"'{body}' is not a valid CIDR prefix ({exc}).", field=field, value=element
            ) from exc
        return
    try:
        ipaddress.ip_address(body)
    except ValueError:
        pass
    else:
        return

    # Anything left would have to be a named ACL — and BIND9 config as
    # actually shipped has none.
    #
    # A `DNSAcl` row is stored, listed and editable on the ACLs tab, and the
    # bundle even carries an ``acls`` block — but only as ``{id, name}``,
    # and the agent's BIND9 renderer never reads it or emits an ``acl {};``
    # stanza. (The control-plane template that *does* render one has no
    # production caller.) So ``match-clients { office; };`` reaches a server
    # with ``office`` undefined, ``named-checkconf`` fails, and the agent
    # declines the whole bundle — the group stops converging entirely, not
    # just this view.
    #
    # Rejecting here is therefore not a restriction, it is the accurate
    # answer: it converts a silent group-wide outage into a 422 that says
    # what to type instead. Lift this the moment the agent renders ACL
    # blocks — https://github.com/spatiumddi/spatiumddi/issues/899.
    if _ACL_NAME_RE.match(body) and body in known_acls:
        raise ViewValidationError(
            f"'{body}' is a named ACL, and the DNS agent does not yet render "
            f"ACL definitions into named.conf — a view referencing one would "
            f"stop the whole server group's config from applying. Use the "
            f"addresses or CIDR prefixes directly here instead.",
            field=field,
            value=element,
        )
    raise ViewValidationError(
        f"'{body}' is not an IP address, a CIDR prefix, or one of "
        f"{', '.join(sorted(BUILTIN_ACLS))}.",
        field=field,
        value=element,
    )


def validate_address_match_list(
    elements: list[str] | None,
    *,
    field: str,
    known_acls: frozenset[str] = frozenset(),
    known_keys: frozenset[str] = frozenset(),
) -> list[str]:
    """Validate every element of a BIND address-match-list.

    ``known_keys`` is the set of TSIG key names the group ships to its
    agents; a ``key <name>`` element naming one that doesn't exist renders a
    ``named.conf`` BIND will not load, so it is rejected here rather than
    discovered when the agent's ``named-checkconf`` refuses the bundle.

    ``known_acls`` is used only to tell an operator who typed a *real* ACL
    name why it isn't accepted — see :func:`_validate_element`. Named ACLs
    are rejected outright today because the agent never renders ``acl {}``
    definitions.

    Returns the list with each element stripped, so trailing whitespace
    from a paste doesn't reach the template.
    """
    if elements is None:
        return []
    cleaned: list[str] = []
    for element in elements:
        _validate_element(element, field, known_acls, known_keys)
        cleaned.append((element or "").strip())
    return cleaned
