"""md / multipath management actions (#999 Part B).

Part A watches storage redundancy; this is the half that lets an
operator do something about it — fail and remove a member, add a
replacement, start or cancel a scrub, reinstate a downed multipath path,
read the live topology.

**Where the work happens.** Every one of these needs a root binary
(``mdadm``, ``multipathd``) that the supervisor container should not be
executing directly, so the supervisor writes a request file and the
host-side ``spatiumddi-storage-action`` runner does the work — the same
trigger-file plane the snmp / chrony / ssh reload runners use, but
imperative rather than convergent. The control plane reaches the
supervisor over the ``agent_cmd`` request/response channel so the
operator gets the outcome in the response rather than having to wait for
a heartbeat and go looking.

**The safety rules live here AND on the host**, deliberately, and they
are not the same rules:

* This module decides what an operator is *allowed to ask for* — it is
  where the destructive-action confirmation is enforced, because this is
  the side that knows who the operator is.
* The runner decides what is *safe to do right now* — it re-counts the
  array's in-sync members from the kernel at the moment of the action,
  because the control plane's view is up to one heartbeat old and a
  member can have failed since. A check made only here would be a check
  made against stale data.

Removing the last working member, or the member the bootloader lives on,
is REFUSED rather than confirmed. #999's rule: a refusal beats an
acknowledgement when the operator cannot inspect the consequence
afterwards — there is no array left to look at in the first case, and in
the second the array stays green while the machine silently stops being
bootable.
"""

from __future__ import annotations

import re
from typing import Any, Literal

#: Actions the control plane will dispatch. An action outside this set is
#: rejected before it reaches the supervisor — the runner has its own
#: allowlist too, so an unknown action is refused twice.
ACTIONS = (
    "scrub_start",
    "scrub_cancel",
    "fail_member",
    "remove_member",
    "add_member",
    "mpath_reinstate",
    "mpath_topology",
)

ActionName = Literal[
    "scrub_start",
    "scrub_cancel",
    "fail_member",
    "remove_member",
    "add_member",
    "mpath_reinstate",
    "mpath_topology",
]

#: Actions that destroy something. Each needs the operator to type the
#: thing back, the way the installer's wipe and the #935 zone move do.
DESTRUCTIVE = frozenset({"fail_member", "remove_member", "add_member"})

#: Which actions need which operands. Checked here so a malformed request
#: is a 422 naming the missing field rather than a runner refusal
#: surfacing as a failed action.
_REQUIRES_ARRAY = frozenset(
    {"scrub_start", "scrub_cancel", "fail_member", "remove_member", "add_member"}
)
_REQUIRES_DEVICE = frozenset({"fail_member", "remove_member", "add_member", "mpath_reinstate"})

# Same shapes the runner's allowlist enforces. Duplicated on purpose:
# this one produces a good error message for the operator, the runner's
# one is the security boundary and must not depend on us having run.
#
# ``.`` and ``/`` both have to be in the device class (``/dev/md/root_a``
# needs the slash, and a dm map name can carry a dot), and that ALONE
# admits ``/dev/../etc/passwd``. So a traversal is rejected by a separate
# component check that runs FIRST — the #995 timezone lesson: a shape
# rule before anything touches a path, rather than one character class
# carrying two jobs. Found by the test for this function, not by review.
_ARRAY_RE = re.compile(r"/dev/md/?[A-Za-z0-9_.-]{1,32}")
_DEVICE_RE = re.compile(r"/dev/[A-Za-z0-9/_.-]{1,64}")


def _has_traversal(path: str) -> bool:
    return any(part in ("..", ".") for part in path.split("/"))


class ActionRefused(ValueError):
    """The request is not one we will dispatch. Router → 422."""


def validate_action(
    action: str,
    *,
    array: str | None,
    device: str | None,
    confirm: str | None,
) -> dict[str, Any]:
    """Validate an operator request and return the supervisor params.

    Raises :class:`ActionRefused` with an operator-facing message.
    """
    if action not in ACTIONS:
        raise ActionRefused(f"unknown storage action: {action!r}")

    if action in _REQUIRES_ARRAY:
        if not array:
            raise ActionRefused(f"{action} needs an array (e.g. /dev/md/root_a)")
        if _has_traversal(array) or not _ARRAY_RE.fullmatch(array):
            raise ActionRefused(f"{array!r} is not an md array path (/dev/mdN or /dev/md/<name>)")
    if action in _REQUIRES_DEVICE:
        if not device:
            raise ActionRefused(f"{action} needs a device (e.g. /dev/sdb1)")
        if _has_traversal(device) or not _DEVICE_RE.fullmatch(device):
            raise ActionRefused(f"{device!r} is not a device path")

    if action in DESTRUCTIVE:
        # The operator types the DEVICE back, not a generic "yes". A
        # typed confirmation that is not the thing being destroyed is a
        # click-through with extra steps — the #935 rule, and the same
        # one the installer's wipe applies to a disk.
        if (confirm or "").strip() != (device or ""):
            raise ActionRefused(
                f"this action is destructive; confirm by sending the device "
                f"path {device!r} in the 'confirm' field"
            )

    params: dict[str, Any] = {"action": action}
    if array:
        params["array"] = array
    if device:
        params["device"] = device
    return params


def summarize(action: str, array: str | None, device: str | None) -> str:
    """One line for the audit row + the operator's confirmation."""
    if action == "scrub_start":
        return f"start a consistency scrub on {array}"
    if action == "scrub_cancel":
        return f"cancel the running scrub on {array}"
    if action == "fail_member":
        return f"mark {device} failed in {array}"
    if action == "remove_member":
        return f"remove {device} from {array}"
    if action == "add_member":
        return f"add {device} to {array} (this ERASES {device})"
    if action == "mpath_reinstate":
        return f"reinstate multipath path {device}"
    return "read the multipath topology"
