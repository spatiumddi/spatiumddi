"""Self-service request catalog (issue #696).

The catalog is the allow-list of things a low-privilege user may *ask* for.
Each entry maps a portal-facing ``kind`` onto an existing
:class:`~app.services.ai.operations.Operation` — the same registry the
Copilot's propose→apply flow and #62's approve spine already use.

That indirection is the point. The issue asks that approving a request run
"the same service-layer functions the REST handlers do — so bulk-allocate,
the race-safe next-free carve, and DNS sync all behave identically whether a
human or an approval triggered them". Operations already are those calls, and
``approve_change_request`` already re-previews and dispatches ``apply()``
under the approver. So the portal contributes a *catalog* and a *submit*
path; it contributes no provisioning code of its own, and cannot drift from
the REST behaviour because there is nothing to drift.

**Why a catalog rather than "any operation".** The submit endpoint
deliberately does NOT require the caller to hold the operation's
``required_permission`` — a requester who could do it themselves would not be
requesting. That makes the set of reachable operations a security boundary:
without an allow-list, a requester could queue *any* registered operation
(``factory_reset``, ``toggle_firewall_policy``, ``grant_temporary_access``)
and wait for an approver to rubber-stamp it. The catalog keeps the portal to
provisioning verbs that are additive and reviewable.

**Auto-approve (#696 phase 2) is designed for, not built.** ``RequestKind``
carries no auto-approve fields on purpose; the seam is that a future
``auto_approve_rules`` table would be consulted in ``submit_request`` right
after validation, and on a match would call the same ``approve_change_request``
spine under a system identity. Nothing here needs to change to allow that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.services.ai.operations import Operation, get_operation


@dataclass(frozen=True)
class RequestKind:
    """One thing a requester may ask for."""

    # Portal-facing stable id, used in the API and the UI.
    kind: str
    label: str
    # Registry name of the Operation that previews + provisions it.
    operation: str
    # Short operator-facing description of what approving will do.
    description: str
    # Grouping for the request form ("ipam" / "dns" / "dhcp").
    category: str


# The four kinds #696 names: IP / subnet / DNS / DHCP.
#
# Every operation here is additive (it creates a row) and previewable, so an
# approver sees exactly what will happen before it happens. Deliberately
# excluded: anything destructive, anything that changes the security posture
# (firewall / RBAC / appliance lifecycle), and anything with no meaningful
# preview. Adding a kind is a security decision — see the module docstring.
REQUEST_KINDS: Final[tuple[RequestKind, ...]] = (
    RequestKind(
        kind="subnet",
        label="New subnet",
        operation="allocate_subnet",
        description=(
            "Carve a new subnet of the requested size out of a parent block. "
            "Approving runs the same race-safe next-free carve the IPAM API uses."
        ),
        category="ipam",
    ),
    RequestKind(
        kind="ip_address",
        label="IP address",
        operation="create_ip_address",
        description=(
            "Reserve an IP in an existing subnet. Approving allocates it through "
            "the same path as a manual allocation, including DNS sync."
        ),
        category="ipam",
    ),
    RequestKind(
        kind="dns_record",
        label="DNS record",
        operation="create_dns_record",
        description=(
            "Create a record in an existing zone. Approving enqueues the record "
            "op so the change reaches the DNS servers exactly as a manual edit would."
        ),
        category="dns",
    ),
    RequestKind(
        kind="dhcp_reservation",
        label="DHCP reservation",
        operation="create_dhcp_static",
        description=(
            "Pin a MAC to a fixed address in a DHCP scope. Approving writes the "
            "reservation through the driver like any other static assignment."
        ),
        category="dhcp",
    ),
)

_BY_KIND: Final[dict[str, RequestKind]] = {k.kind: k for k in REQUEST_KINDS}


def get_kind(kind: str) -> RequestKind | None:
    return _BY_KIND.get(kind)


def all_kinds() -> tuple[RequestKind, ...]:
    return REQUEST_KINDS


def resolve_operation(kind: str) -> tuple[RequestKind, Operation] | None:
    """Resolve a portal ``kind`` to its catalog entry + registered Operation.

    Returns ``None`` when the kind is unknown OR its operation is not
    registered — the latter would be a packaging bug (a catalog entry naming
    an operation that no longer exists), and callers surface it as a 400
    rather than crashing, so one stale entry cannot take down the portal.
    """
    entry = _BY_KIND.get(kind)
    if entry is None:
        return None
    op = get_operation(entry.operation)
    if op is None:
        return None
    return entry, op


__all__ = [
    "REQUEST_KINDS",
    "RequestKind",
    "all_kinds",
    "get_kind",
    "resolve_operation",
]
