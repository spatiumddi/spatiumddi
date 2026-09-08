"""Storage-redundancy classification (#999 Part A).

The supervisor reports raw md-array + multipath state inside its
``cluster_health`` dict (``read_storage_health`` in
``agent/supervisor/spatium_supervisor/appliance_state.py``). This module
turns that reading into findings, and it is the ONLY place that
decision is made — the ``appliance_storage_degraded`` alert matcher, the
``find_appliance_storage`` copilot tool and the API schema all call it,
so none of them can disagree about whether an array is in trouble.

**Severity keys off redundancy remaining, never off the state string.**
``2 of 3`` in a three-way mirror and ``1 of 2`` in a pair both report
``degraded``, and only the second one is an emergency: it has no copy
left to lose. Collapsing them into one severity would either page for a
condition that can wait until morning or fail to page for one that
cannot.

**A routine scrub or resync on an intact array is deliberately NOT a
finding.** The issue asked for it as "informational, auto-clears", and
that would be right if ``info`` were quiet — it is not. Alert delivery
filters a target's ``min_severity`` against ``payload["result"]``, a
key alert payloads do not carry (see ``audit_forward._target_accepts``),
and the column defaults to NULL anyway, so an ``info`` event notifies
exactly like a critical one. Debian runs ``checkarray`` monthly by
cron, so an ``info`` finding here would mail every operator with an
array, every month, about their array working correctly — which is how
an alarm gets muted before the night it matters. The scrub IS surfaced,
on all three screens, with its progress; it is just not an event.

The rebuild that DOES matter never reaches here as ``syncing`` anyway:
an array with a member out of sync reports ``degraded``, and is
classified on redundancy like any other degraded array.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Severity ranking, worst first — used to pick one severity for an
# appliance carrying several findings.
_SEVERITY_ORDER = ("critical", "warning")


@dataclass(frozen=True)
class StorageFinding:
    """One thing worth saying about a node's storage redundancy."""

    #: ``critical`` or ``warning``.
    severity: str
    #: ``md`` or ``multipath`` — which subsystem this is about.
    kind: str
    #: The array or map name (``md0`` / ``mpatha``).
    name: str
    #: Operator-facing sentence, complete on its own.
    detail: str


def _md_findings(array: dict[str, Any]) -> list[StorageFinding]:
    name = str(array.get("name") or "?")
    level = str(array.get("level") or "unknown")
    state = str(array.get("state") or "unknown")
    in_sync = int(array.get("members_in_sync") or 0)
    expected = array.get("members_expected")
    remaining = int(array.get("redundancy_remaining") or 0)
    counts = f"{in_sync} of {expected} members in sync"

    if state == "unknown":
        # The array is assembled but its member count could not be read,
        # so no redundancy statement can be made about it. That is worth
        # saying out loud on a box whose whole point is a mirror: sysfs
        # failing on an assembled array is not a normal condition, and
        # the alternative is a screen that quietly stops mentioning it.
        return [
            StorageFinding(
                "warning",
                "md",
                name,
                f"{level} array {name} reports no member count — its redundancy "
                "cannot be determined. Check `mdadm --detail /dev/" + name + "` "
                "on the node.",
            )
        ]
    if state == "failed":
        # An array that failed to ASSEMBLE reports no member count at
        # all, so the counts sentence would read "0 of None members in
        # sync … below the None member(s)" — on the most serious alert
        # the feature raises.
        minimum = array.get("min_working_members")
        if expected is None:
            detail = (
                f"{level} array {name} has FAILED — it did not assemble, and "
                "reports no member count. It is serving no data."
            )
        else:
            detail = (
                f"{level} array {name} has FAILED — {counts}. It is below the "
                f"{minimum} member(s) it needs to serve data at all."
            )
        return [StorageFinding("critical", "md", name, detail)]
    if state == "degraded":
        # remaining == 0 means the array is running on the last copy it
        # has: one more loss and the data is gone.
        severity = "critical" if remaining <= 0 else "warning"
        tail = (
            "no redundancy remains — a second failure loses the data"
            if remaining <= 0
            else f"{remaining} further member loss(es) survivable"
        )
        return [
            StorageFinding(
                severity,
                "md",
                name,
                f"{level} array {name} is DEGRADED — {counts}; {tail}.",
            )
        ]
    # ``syncing`` (a scrub or resync on an intact array) is not a
    # finding — see the module docstring.
    return []


def _multipath_findings(mp: dict[str, Any]) -> list[StorageFinding]:
    """Findings for one multipath map.

    **The absence of a finding here is not a clean bill of health**, and
    no surface may render it as one. The only per-path signal available
    without the device-mapper ioctl is the path's SCSI ``device/state``,
    which stays ``running`` for the commonest failure there is: the
    multipathd checker marking a path failed while the SCSI device is
    still perfectly present. So a map that has silently lost half its
    paths reports ``paths_faulted: 0`` and produces nothing here.

    That is why ``paths_faulted`` is a positive fault signal only, and
    why every surface renders a finding-less multipath map in a NEUTRAL
    style rather than the green one an md array earns — for md the state
    is actually known. Making dm's own verdict readable is #999 Part B.
    """
    name = str(mp.get("name") or mp.get("dm_device") or "?")
    total = int(mp.get("paths_total") or 0)
    faulted = int(mp.get("paths_faulted") or 0)

    if total == 0:
        # A map with no paths at all is unambiguous: the LUN is gone.
        return [
            StorageFinding(
                "critical",
                "multipath",
                name,
                f"Multipath map {name} has no paths at all — the LUN is " "unreachable.",
            )
        ]
    # A map with exactly ONE path is deliberately NOT alarmed on.
    #
    # #999 called it critical, on the reasoning that a LUN "down to its
    # last path" has no failover left. That reasoning needs a baseline we
    # do not have: nothing here knows whether the map ever had more. The
    # installer explicitly permits installing to a single-path LUN (it
    # flags it in the picker), so on those appliances a count-based alarm
    # is critical forever, drives the console verdict red forever, and
    # cannot be cleared by any action — which is how an alarm gets muted
    # before the night it matters.
    #
    # So the rule is: alarm on a DEFINITE fault, never on the absence of
    # a baseline. The path count is still on every screen, where an
    # operator who knows what it should be can read it.
    if faulted:
        return [
            StorageFinding(
                "warning",
                "multipath",
                name,
                f"Multipath map {name} has {faulted} of {total} path(s) reporting "
                "a SCSI fault (offline / blocked). The LUN is still reachable on "
                "the rest.",
            )
        ]
    return []


def evaluate_storage(storage: dict[str, Any] | None) -> list[StorageFinding]:
    """Classify one node's ``cluster_health["storage"]`` snapshot.

    Returns ``[]`` for a healthy node AND for one that has never
    reported — the caller distinguishes those with ``has_storage_report``
    when it needs to, because "no arrays" and "no reading" must not be
    the same answer on a screen even though neither is a fault.
    """
    if not isinstance(storage, dict):
        return []
    findings: list[StorageFinding] = []
    for array in storage.get("md_arrays") or []:
        if isinstance(array, dict):
            findings.extend(_md_findings(array))
    for mp in storage.get("multipath_maps") or []:
        if isinstance(mp, dict):
            findings.extend(_multipath_findings(mp))
    findings.sort(key=lambda f: (_SEVERITY_ORDER.index(f.severity), f.kind, f.name))
    return findings


def has_storage_report(cluster_health: dict[str, Any] | None) -> bool:
    """Has the supervisor on this node ever reported storage state?

    A supervisor too old to collect it ships no ``storage`` key at all,
    and that is UNKNOWN — never a clean bill of health. Every surface
    renders nothing in that case rather than a green tick.
    """
    return isinstance(cluster_health, dict) and isinstance(cluster_health.get("storage"), dict)


def worst_severity(findings: list[StorageFinding]) -> str | None:
    """The most serious severity among ``findings``, or None when empty."""
    for sev in _SEVERITY_ORDER:
        if any(f.severity == sev for f in findings):
            return sev
    return None
