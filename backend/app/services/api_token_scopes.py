"""API token scopes — coarse-grained access vocabulary (issue #74).

Five well-known scopes that compose by union:

* ``read`` — restricts the token to safe-method requests only
  (``GET`` / ``HEAD`` / ``OPTIONS``). Any mutation 401s.
* ``ipam:write`` — allows mutations under ``/api/v1/ipam/*``,
  ``/api/v1/vlans*``, ``/api/v1/vrfs*``, ``/api/v1/network-devices*``.
  Read traffic anywhere is also allowed by ``read`` if combined.
* ``dns:write`` — allows mutations under ``/api/v1/dns/*``,
  ``/api/v1/dns-pools*``.
* ``dhcp:write`` — allows mutations under ``/api/v1/dhcp/*``.
* ``agent`` — allows the agent push surface
  (``/api/v1/dns/agents/*``, ``/api/v1/dhcp/agents/*``) only.
  Used by the BIND9 / Kea sidecar agents that bootstrap with
  PSK + JWT but can also be issued plain API tokens for ops
  testing.

Empty ``scopes`` = no scope restriction (the existing RBAC path
is the only gate). Non-empty = enforced BEFORE RBAC at the auth
layer; a request that doesn't match any of the token's scopes
401s with ``token scope insufficient``. Multiple scopes form a
union (any single match passes).

Why a closed vocabulary? Storing free-form strings would let an
operator scope a token to ``/api/v1/typo`` and silently lock
themselves out — the failure mode is delayed, surfaces as 401,
and gives no hint about the typo. The closed enum lets the create
endpoint reject the typo with 422 at issuance time.
"""

from __future__ import annotations

# Vocabulary — values stored verbatim in ``api_token.scopes``. Any
# new entry needs a matching branch in ``scope_matches_request``.
TOKEN_SCOPE_VOCABULARY: frozenset[str] = frozenset(
    {
        "read",
        "ipam:write",
        "dns:write",
        "dhcp:write",
        "agent",
    }
)

_SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

# Path-prefix vocabulary per scope. Prefixes are matched as
# ``path.startswith(prefix)`` against the request path *with* the
# ``/api/v1`` mount, so the leading ``/api/v1`` is included.
_IPAM_PREFIXES: tuple[str, ...] = (
    "/api/v1/ipam",
    "/api/v1/vlans",
    "/api/v1/vrfs",
    "/api/v1/network-devices",
)
_DNS_PREFIXES: tuple[str, ...] = (
    "/api/v1/dns",
    "/api/v1/dns-pools",
)
_DHCP_PREFIXES: tuple[str, ...] = ("/api/v1/dhcp",)
_AGENT_PREFIXES: tuple[str, ...] = (
    "/api/v1/dns/agents",
    "/api/v1/dhcp/agents",
)


def _path_under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(p) for p in prefixes)


def scope_matches_request(scopes: list[str], method: str, path: str) -> bool:
    """Decide whether a token with these ``scopes`` can serve the
    request. Empty ``scopes`` always passes (no restriction).

    Returns True on first match — scopes union, not intersect.
    """
    if not scopes:
        return True
    method = method.upper()
    is_safe = method in _SAFE_METHODS
    for scope in scopes:
        if scope == "read" and is_safe:
            return True
        if scope == "ipam:write" and _path_under(path, _IPAM_PREFIXES):
            return True
        if scope == "dns:write" and _path_under(path, _DNS_PREFIXES):
            # Agent paths are nested under /api/v1/dns/* — explicitly
            # require the ``agent`` scope for those rather than
            # letting ``dns:write`` cover them too.
            if _path_under(path, _AGENT_PREFIXES):
                continue
            return True
        if scope == "dhcp:write" and _path_under(path, _DHCP_PREFIXES):
            if _path_under(path, _AGENT_PREFIXES):
                continue
            return True
        if scope == "agent" and _path_under(path, _AGENT_PREFIXES):
            return True
    return False


def validate_scopes(scopes: list[str]) -> list[str]:
    """Return the de-duplicated list if every entry is in the
    vocabulary; raise ``ValueError`` otherwise. Used by the
    create / update endpoints + by Pydantic field_validator.
    """
    invalid = [s for s in scopes if s not in TOKEN_SCOPE_VOCABULARY]
    if invalid:
        raise ValueError(
            f"Unknown scope(s): {', '.join(invalid)}. "
            f"Allowed: {', '.join(sorted(TOKEN_SCOPE_VOCABULARY))}"
        )
    # Preserve order but dedupe.
    seen: set[str] = set()
    out: list[str] = []
    for s in scopes:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


__all__ = [
    "TOKEN_SCOPE_VOCABULARY",
    "scope_matches_request",
    "validate_scopes",
]
