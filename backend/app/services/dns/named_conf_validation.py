"""Validation for the free-text that reaches ``named.conf``.

Views and their address-match-lists came first (#876); named ACLs joined
when the agent learned to render them (#899). Both are operator-supplied
strings interpolated **verbatim** into the config an agent applies, so
they share one gate.

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
    "AclCycleError",
    "ViewValidationError",
    "is_name_reference",
    "order_acls_for_render",
    "validate_acl_name",
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
    allow_unknown_names: bool = False,
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
        if not allow_unknown_names and key_name not in known_keys:
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
                f"'{body}' is not a valid CIDR prefix ({exc}).",
                field=field,
                value=element,
            ) from exc
        return
    try:
        ipaddress.ip_address(body)
    except ValueError:
        pass
    else:
        return

    # A named ACL. It has to be one this group actually defines, because
    # the rendered ``match-clients { office; };`` is an undefined symbol
    # otherwise — ``named-checkconf`` fails, the agent declines the whole
    # bundle, and the group stops converging entirely rather than just this
    # one statement.
    #
    # Before #899 this branch rejected every ACL name, because the agent
    # emitted no ``acl {}`` definitions at all and so a reference could
    # never resolve. Now that it does, an ACL the operator defined is a
    # first-class element here.
    if not _ACL_NAME_RE.match(body):
        raise ViewValidationError(
            f"'{body}' is not an IP address, a CIDR prefix, an ACL name, or "
            f"one of {', '.join(sorted(BUILTIN_ACLS))}.",
            field=field,
            value=element,
        )
    if not allow_unknown_names and body not in known_acls:
        known = ", ".join(sorted(known_acls)) if known_acls else "none defined"
        raise ViewValidationError(
            f"'{body}' is not an ACL defined in this server group "
            f"(available: {known}). Create it on the ACLs tab first, or use "
            f"an address or CIDR prefix here.",
            field=field,
            value=element,
        )


def validate_address_match_list(
    elements: list[str] | None,
    *,
    field: str,
    known_acls: frozenset[str] = frozenset(),
    known_keys: frozenset[str] = frozenset(),
    allow_unknown_names: bool = False,
) -> list[str]:
    """Validate every element of a BIND address-match-list.

    ``allow_unknown_names`` checks syntax only, accepting a bare name
    without asking whether it resolves. The bundle builder uses it to
    decide whether a legacy row is *renderable*; resolution is a separate
    question it answers with its own known-set.

    ``known_keys`` is the set of TSIG key names the group ships to its
    agents; a ``key <name>`` element naming one that doesn't exist renders a
    ``named.conf`` BIND will not load, so it is rejected here rather than
    discovered when the agent's ``named-checkconf`` refuses the bundle.

    ``known_acls`` is the set of ``DNSAcl`` names defined on the group. A
    name outside it is the same undefined-symbol failure, so it is rejected
    here too.

    Returns the list with each element stripped, so trailing whitespace
    from a paste doesn't reach the template.
    """
    if elements is None:
        return []
    cleaned: list[str] = []
    for element in elements:
        _validate_element(element, field, known_acls, known_keys, allow_unknown_names)
        cleaned.append((element or "").strip())
    return cleaned


# ── named ACLs (issue #899) ───────────────────────────────────────────────


class AclCycleError(ViewValidationError):
    """An ACL references itself, directly or through other ACLs.

    BIND has no way to resolve that, so it is a config error — and since a
    config error fails the whole bundle, catching it at write time is the
    difference between a 422 and a server group that stops converging.
    """


def validate_acl_name(name: str, *, field: str = "name") -> str:
    """Return the ACL name, or raise.

    Looser than a view name: an ACL is never a directory or a DNS label, so
    it only has to be a safe BIND identifier. Dots are allowed because
    ``guest.wifi`` is a name operators reach for and BIND takes it unquoted.
    """
    candidate = (name or "").strip()
    if not candidate:
        raise ViewValidationError("ACL name is required.", field=field, value=name)
    if candidate.lower() in BUILTIN_ACLS:
        raise ViewValidationError(
            f"'{candidate}' is a built-in BIND address-match-list name and "
            f"cannot be redefined as an ACL.",
            field=field,
            value=candidate,
        )
    if not _ACL_NAME_RE.match(candidate):
        raise ViewValidationError(
            "An ACL name must start with a letter or digit and contain only "
            "letters, digits, dots, hyphens and underscores (max 255 "
            "characters). It is rendered into named.conf as "
            'acl "<name>" { … };',
            field=field,
            value=candidate,
        )
    return candidate


def validate_acl_entries(
    entries: list[str] | None,
    *,
    field: str = "entries",
    known_acls: frozenset[str] = frozenset(),
    known_keys: frozenset[str] = frozenset(),
) -> list[str]:
    """Validate the values of one ACL's entries.

    An ACL body is an address-match-list like any other, so this is the
    same gate a view's ``match_clients`` goes through — including the
    ability to reference another ACL by name, which BIND supports and the
    model has always documented.

    Note ``known_acls`` should NOT contain the ACL being edited: a
    self-reference is a cycle, and :func:`order_acls_for_render` would
    refuse to order it anyway. Rejecting it here names the problem while
    the operator is still looking at the form.
    """
    return validate_address_match_list(
        entries, field=field, known_acls=known_acls, known_keys=known_keys
    )


def is_name_reference(value: str) -> str | None:
    """The ACL name an address-match element refers to, or ``None``.

    Only a bare identifier is a reference — an address, a CIDR prefix, a
    built-in (``any`` / ``none`` / …) or a ``key <name>`` never is. Shared
    by the ordering pass and the bundle builder so the two cannot disagree
    about what counts as an edge.
    """
    body = (value or "").strip()
    if body.startswith("!"):
        body = body[1:].strip()
    if not body or body.lower() in BUILTIN_ACLS or _KEY_RE.match(body):
        return None
    if "/" in body:
        return None
    try:
        ipaddress.ip_address(body)
    except ValueError:
        pass
    else:
        return None
    return body if _ACL_NAME_RE.match(body) else None


def order_acls_for_render(acls: list[dict]) -> list[dict]:
    """Order ACLs so every definition precedes its first reference.

    BIND resolves ``acl`` statements top-down: referencing one that is
    declared later in the file is an error, not a forward declaration. The
    operator's own ordering carries no such guarantee — the ACLs tab sorts
    by name — so the bundle emits them sorted by dependency instead, and
    the agent renders the list as given.

    ``acls`` is a list of ``{"name": str, "entries": [{"value", "negate"}]}``
    dicts. Returns the same dicts, reordered.

    Raises :class:`AclCycleError` if the references form a loop. That check
    lives here rather than only at write time because an ACL can also be
    orphaned into a cycle by *deleting* a different one, and a bundle that
    cannot be ordered must fail loudly rather than ship a config the agent
    will reject.
    """
    by_name = {a["name"]: a for a in acls}

    def refs(acl: dict) -> list[str]:
        out = []
        for entry in acl.get("entries") or []:
            target = is_name_reference(str(entry.get("value", "")))
            if target in by_name:
                out.append(str(target))
        return out

    ordered: list[dict] = []
    # 0 = unvisited, 1 = on the current DFS path, 2 = emitted.
    state: dict[str, int] = {}

    def visit(name: str, path: list[str]) -> None:
        mark = state.get(name, 0)
        if mark == 2:
            return
        if mark == 1:
            loop = " → ".join([*path[path.index(name) :], name])
            raise AclCycleError(
                f"ACL '{name}' references itself through {loop}. BIND cannot "
                f"resolve a circular ACL, and the whole server group's config "
                f"would be refused.",
                field="entries",
                value=name,
            )
        state[name] = 1
        for dep in refs(by_name[name]):
            visit(dep, [*path, name])
        state[name] = 2
        ordered.append(by_name[name])

    # Sorted for determinism: the same set of ACLs must render byte-identical
    # every time, or the bundle's sha256 ETag churns and every agent re-pulls
    # a config that did not change.
    for name in sorted(by_name):
        visit(name, [])
    return ordered
