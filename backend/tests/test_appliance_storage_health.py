"""Storage-redundancy classification + surfaces (#999 Part A).

The supervisor reads md / multipath state out of the host's sysfs and
folds it into its ``cluster_health`` JSONB; ``evaluate_storage`` turns
that reading into findings, and it is the ONLY place that decision is
made — the ``appliance_storage_degraded`` alert matcher, the
``find_appliance_storage`` copilot tool, the Cluster screen's node chip
and the Fleet drilldown all derive from it.

The property under test throughout is that **severity comes from
redundancy remaining, not from the state string**. ``2 of 3`` in a
three-way mirror and ``1 of 2`` in a pair both report ``degraded``; only
the second one has nothing left to lose, and collapsing them would
either page for something that can wait or fail to page for something
that cannot.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appliance import APPLIANCE_STATE_APPROVED, Appliance
from app.models.auth import User
from app.services.appliance.storage_health import (
    evaluate_storage,
    has_storage_report,
    worst_severity,
)


def _array(**over: object) -> dict:
    base = {
        "name": "md0",
        "level": "raid1",
        "state": "clean",
        "array_state": "clean",
        "members_expected": 2,
        "members_in_sync": 2,
        "members_faulty": 0,
        "spares": 0,
        "redundancy_remaining": 1,
        "min_working_members": 1,
        "members": [],
    }
    base.update(over)
    return base


def _mpath(**over: object) -> dict:
    base = {
        "name": "mpatha",
        "dm_device": "dm-0",
        "uuid": "mpath-abc",
        "paths_total": 4,
        "paths_faulted": 0,
        "paths": [],
    }
    base.update(over)
    return base


# ── the classifier ──────────────────────────────────────────────────


def test_healthy_array_has_no_findings() -> None:
    assert evaluate_storage({"md_arrays": [_array()], "multipath_maps": []}) == []


def test_degraded_pair_is_critical() -> None:
    """``1 of 2``: the array is running on its last copy."""
    findings = evaluate_storage(
        {"md_arrays": [_array(state="degraded", members_in_sync=1, redundancy_remaining=0)]}
    )
    assert [f.severity for f in findings] == ["critical"]
    assert "no redundancy remains" in findings[0].detail


def test_degraded_three_way_mirror_is_only_a_warning() -> None:
    """``2 of 3``: reduced, but a further failure is survivable.

    Same ``state`` string as the case above and a different night.
    """
    findings = evaluate_storage(
        {
            "md_arrays": [
                _array(
                    state="degraded",
                    members_expected=3,
                    members_in_sync=2,
                    redundancy_remaining=1,
                )
            ]
        }
    )
    assert [f.severity for f in findings] == ["warning"]
    assert "2 of 3" in findings[0].detail


def test_failed_array_is_critical() -> None:
    findings = evaluate_storage(
        {"md_arrays": [_array(state="failed", members_in_sync=0, redundancy_remaining=0)]}
    )
    assert [f.severity for f in findings] == ["critical"]
    assert "FAILED" in findings[0].detail


def test_scrub_on_intact_array_is_not_an_event() -> None:
    """Routine maintenance is SHOWN on every screen and is deliberately
    not an alert.

    The issue asked for it as "informational, auto-clears", which would
    be right if ``info`` were quiet. It is not: delivery filters
    ``min_severity`` against ``payload["result"]``, a key alert payloads
    never carry, and the column defaults to NULL — so an ``info`` event
    notifies exactly like a critical one. Debian runs ``checkarray``
    monthly, so this would mail every operator with an array, every
    month, about their array working correctly.
    """
    assert (
        evaluate_storage(
            {"md_arrays": [_array(state="syncing", sync={"action": "check", "percent": 42.0})]}
        )
        == []
    )


def test_unknown_member_count_is_a_warning_not_silence() -> None:
    """An assembled array that will not report its member count cannot
    be called healthy, and must not be quietly dropped either."""
    findings = evaluate_storage(
        {
            "md_arrays": [
                _array(
                    state="unknown",
                    members_expected=None,
                    redundancy_remaining=None,
                    min_working_members=None,
                )
            ]
        }
    )
    assert [f.severity for f in findings] == ["warning"]
    assert "redundancy cannot be determined" in findings[0].detail


def test_a_single_path_map_is_not_alarmed_on() -> None:
    """#999 called this critical; it cannot be, and the reason is a
    missing baseline.

    Nothing here knows whether the map ever had more than one path, and
    the installer explicitly permits installing to a single-path LUN. A
    count-based alarm on those appliances is critical forever, turns the
    console verdict red forever, and no action can clear it — which is
    how an alarm gets muted before the night it matters. The count is
    still on every screen for an operator who knows what it should be.
    """
    assert evaluate_storage({"multipath_maps": [_mpath(paths_total=1)]}) == []


def test_a_map_with_no_paths_at_all_is_critical() -> None:
    """Unambiguous, and needs no baseline: the LUN is gone."""
    findings = evaluate_storage({"multipath_maps": [_mpath(paths_total=0)]})
    assert [f.severity for f in findings] == ["critical"]
    assert "no paths at all" in findings[0].detail


def test_multipath_partial_fault_is_a_warning() -> None:
    findings = evaluate_storage({"multipath_maps": [_mpath(paths_total=4, paths_faulted=2)]})
    assert [f.severity for f in findings] == ["warning"]
    assert "2 of 4" in findings[0].detail


def test_healthy_multipath_has_no_findings() -> None:
    assert evaluate_storage({"multipath_maps": [_mpath()]}) == []


def test_findings_sort_worst_first() -> None:
    findings = evaluate_storage(
        {
            "md_arrays": [
                _array(
                    name="md1",
                    state="degraded",
                    members_expected=3,
                    members_in_sync=2,
                    redundancy_remaining=1,
                ),
                _array(name="md0", state="degraded", members_in_sync=1, redundancy_remaining=0),
            ],
            "multipath_maps": [_mpath(paths_total=4, paths_faulted=1)],
        }
    )
    assert [f.severity for f in findings] == ["critical", "warning", "warning"]
    assert worst_severity(findings) == "critical"


def test_a_quiet_multipath_map_is_not_a_clean_bill_of_health() -> None:
    """The absence of a multipath finding says nothing.

    The only per-path signal readable without the device-mapper ioctl is
    the path's SCSI ``device/state``, which stays ``running`` for the
    commonest failure there is — multipathd's checker marking a path
    failed while the device is still present. So this map has silently
    lost half its paths and produces nothing, which is exactly why the
    UI renders a finding-less map NEUTRAL rather than green.
    """
    assert evaluate_storage({"multipath_maps": [_mpath(paths_total=2)]}) == []


def test_missing_reading_is_not_a_finding() -> None:
    """A supervisor too old to collect storage reports nothing at all.

    That is UNKNOWN. Firing would be a guess; treating it as healthy
    would be a lie — so it is simply not a match, and
    ``has_storage_report`` is what a surface uses to tell the two apart.
    """
    assert evaluate_storage(None) == []
    assert worst_severity([]) is None
    assert has_storage_report({"kubeapi_ready": True}) is False
    assert has_storage_report({"storage": {"md_arrays": []}}) is True


# ── the alert matcher ───────────────────────────────────────────────


async def _superadmin(db: AsyncSession) -> User:
    """A superadmin, so ``_superadmin_gate``'s ``is_effective_superadmin``
    short-circuits on the flag and never lazy-loads ``user.groups``."""
    u = User(
        username=f"sa-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="SA",
        hashed_password="x",
        auth_source="local",
        is_active=True,
        is_superadmin=True,
        force_password_change=False,
    )
    db.add(u)
    await db.flush()
    return u


async def _appliance(db: AsyncSession, hostname: str, storage: dict | None) -> Appliance:
    der = os.urandom(32)
    ch: dict = {"kubeapi_ready": True}
    if storage is not None:
        ch["storage"] = storage
    row = Appliance(
        id=uuid.uuid4(),
        hostname=hostname,
        public_key_der=der,
        public_key_fingerprint=hashlib.sha256(der).hexdigest(),
        state=APPLIANCE_STATE_APPROVED,
        deployment_kind="appliance",
        appliance_variant="control-plane",
        session_token_hash=hashlib.sha256(der).hexdigest(),
        cluster_health=ch,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.mark.asyncio
async def test_matcher_fires_per_appliance_with_worst_severity(
    db_session: AsyncSession,
) -> None:
    from app.models.alerts import AlertRule
    from app.services.alerts import (
        RULE_TYPE_APPLIANCE_STORAGE_DEGRADED,
        _matching_appliance_storage_subjects,
    )

    bad = await _appliance(
        db_session,
        "ddi-degraded",
        {
            "md_supported": True,
            "md_arrays": [_array(state="degraded", members_in_sync=1, redundancy_remaining=0)],
            "multipath_maps": [_mpath(paths_total=4, paths_faulted=1)],
        },
    )
    # Healthy, no-arrays and never-reported appliances must all be silent.
    await _appliance(db_session, "ddi-healthy", {"md_supported": True, "md_arrays": [_array()]})
    await _appliance(db_session, "ddi-plain", {"md_supported": False, "md_arrays": []})
    await _appliance(db_session, "ddi-old", None)
    await db_session.flush()

    rule = AlertRule(
        name="Appliance storage redundancy degraded (test)",
        rule_type=RULE_TYPE_APPLIANCE_STORAGE_DEGRADED,
        severity="warning",
        enabled=True,
    )
    matches = await _matching_appliance_storage_subjects(db_session, rule)
    assert len(matches) == 1
    subject_id, display, message, severity = matches[0]
    assert subject_id == str(bad.id)
    assert display == "ddi-degraded"
    # One event per appliance, naming every finding worst-first.
    assert severity == "critical"
    assert "DEGRADED" in message and "Multipath map mpatha" in message


@pytest.mark.asyncio
async def test_matcher_skips_revoked_appliances(db_session: AsyncSession) -> None:
    """A decommissioned box's array is not an operational problem."""
    from app.models.alerts import AlertRule
    from app.services.alerts import (
        RULE_TYPE_APPLIANCE_STORAGE_DEGRADED,
        _matching_appliance_storage_subjects,
    )

    row = await _appliance(
        db_session,
        "ddi-gone",
        {
            "md_supported": True,
            "md_arrays": [_array(state="degraded", members_in_sync=1, redundancy_remaining=0)],
        },
    )
    row.revoked_at = datetime.now(UTC)
    await db_session.flush()

    rule = AlertRule(
        name="Appliance storage redundancy degraded (test)",
        rule_type=RULE_TYPE_APPLIANCE_STORAGE_DEGRADED,
        severity="warning",
        enabled=True,
    )
    assert await _matching_appliance_storage_subjects(db_session, rule) == []


@pytest.mark.asyncio
async def test_a_reading_that_disappears_holds_its_open_event(
    db_session: AsyncSession,
) -> None:
    """``evaluate_all`` resolves every open event whose subject is absent
    from a pass, so "not a match" means "recovered".

    An A/B slot rollback to a pre-#999 supervisor drops the ``storage``
    key, which would announce a recovery that did not happen on a node
    whose mirror is still one disk from data loss. The appliance is
    re-matched at its EXISTING severity instead — a no-op for the
    caller (it never downgrades and never re-delivers on an unchanged
    severity) that keeps the event standing.
    """
    from app.models.alerts import AlertEvent, AlertRule
    from app.services.alerts import (
        RULE_TYPE_APPLIANCE_STORAGE_DEGRADED,
        _matching_appliance_storage_subjects,
    )

    # The supervisor has been rolled back: heartbeating, no storage key.
    rolled_back = await _appliance(db_session, "ddi-rolled-back", None)
    # ...and one that simply never reported, which must stay silent.
    await _appliance(db_session, "ddi-never", None)

    rule = AlertRule(
        name="Appliance storage redundancy degraded (test)",
        rule_type=RULE_TYPE_APPLIANCE_STORAGE_DEGRADED,
        severity="warning",
        enabled=True,
    )
    db_session.add(rule)
    await db_session.flush()
    db_session.add(
        AlertEvent(
            rule_id=rule.id,
            subject_type="appliance",
            subject_id=str(rolled_back.id),
            subject_display=rolled_back.hostname,
            severity="critical",
            message="Storage on ddi-rolled-back: raid1 array md0 is DEGRADED.",
            fired_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    matches = await _matching_appliance_storage_subjects(db_session, rule)
    assert len(matches) == 1
    subject_id, display, message, severity = matches[0]
    assert subject_id == str(rolled_back.id)
    assert display == "ddi-rolled-back"
    # Re-matched at the SAME severity, so the caller's escalation compare
    # (`new > existing`) is false and nothing is re-delivered.
    assert severity == "critical"
    assert "can no longer be read" in message
    assert "unknown is not recovered" in message


@pytest.mark.asyncio
async def test_copilot_tool_reports_appliances_it_could_not_look_at(
    db_session: AsyncSession,
) -> None:
    """With the default ``degraded_only=true``, an appliance missing from
    the list is not necessarily healthy — it may simply never have
    reported storage. That absence is the exact place a degraded array
    hides, so it is reported explicitly rather than left to be inferred.
    """
    from app.services.ai.tools.appliance import (
        FindApplianceStorageArgs,
        find_appliance_storage,
    )

    await _appliance(db_session, "ddi-healthy", {"md_supported": True, "md_arrays": [_array()]})
    await _appliance(db_session, "ddi-old", None)
    admin = await _superadmin(db_session)
    await db_session.flush()

    res = await find_appliance_storage(db_session, admin, FindApplianceStorageArgs())
    # Nothing is degraded, so the list is empty...
    assert res["count"] == 0
    # ...but one box was never looked at, and the caller is told so.
    assert res["not_reporting"] == ["ddi-old"]
    assert res["not_reporting_count"] == 1


@pytest.mark.asyncio
async def test_rule_type_is_registered() -> None:
    """An unregistered rule_type is rejected by the rules API, so a rule
    the seeder creates would be uneditable."""
    from app.services.alerts import RULE_TYPE_APPLIANCE_STORAGE_DEGRADED, RULE_TYPES

    assert RULE_TYPE_APPLIANCE_STORAGE_DEGRADED in RULE_TYPES
