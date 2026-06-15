"""Shape guards for integration-mirror reads (#430 / #426).

A read-only integration mirror (Kubernetes / Docker / Proxmox / Cloud /
UniFi / OPNsense / Tailscale) pulls the upstream inventory and then runs an
*absence-delete* pass: any mirrored IPAM/DNS row whose source object is no
longer present gets removed. That makes the parse step safety-critical.

A 200 response whose body is the **wrong shape** — a proxy/gateway error
page served 200, an auth-downgrade landing page, an envelope change, a
``data: null`` from a momentarily-confused upstream — must be treated as a
*fetch failure*: raise the integration's typed ``*ClientError`` so the
reconciler aborts and keeps the last-known mirror rows. The dangerous
anti-pattern (the #426 defect class) is collapsing such a body to ``[]`` /
zero items, which looks like "everything was deleted upstream" and purges
the entire mirror while reporting ``ok=True``.

A *legitimately empty* result — the documented envelope present, just no
rows (an empty cluster ``{"items": []}``, an empty tailnet
``{"devices": []}``) — stays valid and returns an empty list.

Each client passes its own error factory so the raised exception is the
type its reconciler already catches; no new catch wiring is required.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def require_list(value: Any, *, make_error: Callable[[str], Exception], context: str) -> list[Any]:
    """Return ``value`` if it is a list, else raise ``make_error(...)``.

    Use when the upstream returns a bare JSON array on success (Docker
    ``/networks``, Proxmox ``data`` for list endpoints).
    """
    if not isinstance(value, list):
        raise make_error(
            f"{context}: expected a JSON array but got {type(value).__name__} "
            f"— treating as a degraded read, not zero items"
        )
    return value


def require_keyed_list(
    body: Any, key: str, *, make_error: Callable[[str], Exception], context: str
) -> list[Any]:
    """Return ``body[key]`` if it is a list, else raise ``make_error(...)``.

    Use when the upstream wraps the collection in an object under a known
    key (Kubernetes ``{"items": [...]}``, Tailscale ``{"devices": [...]}``).
    A missing key, a non-dict body, or a non-list value are all degraded
    reads. An explicitly-empty list (``{"items": []}``) is legitimate.
    """
    if not isinstance(body, dict) or key not in body:
        raise make_error(
            f"{context}: response is not an object with a '{key}' key "
            f"({type(body).__name__}) — treating as a degraded read, not zero items"
        )
    return require_list(body[key], make_error=make_error, context=f"{context}.{key}")
