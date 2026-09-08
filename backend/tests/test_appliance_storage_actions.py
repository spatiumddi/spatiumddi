"""md / multipath management actions (#999 Part B).

The validation layer decides what an operator is ALLOWED TO ASK FOR; the
host runner decides what is safe to do at the moment of the action. These
cover the first half — the second is shell and is covered by
``appliance/tests``.

The property under test is #999's rule about which gate to use: a refusal
beats an acknowledgement when the operator cannot inspect the consequence
afterwards. Adding a member erases a disk the operator picked and the
array then reports a rebuild, so that is CONFIRMED; removing the last
in-sync member leaves no array to look at, so that is REFUSED (by the
runner, which is the only place that can count members at the moment of
the action).
"""

from __future__ import annotations

import pytest

from app.services.appliance.storage_actions import (
    ACTIONS,
    DESTRUCTIVE,
    ActionRefused,
    summarize,
    validate_action,
)


def test_unknown_action_is_refused() -> None:
    with pytest.raises(ActionRefused, match="unknown storage action"):
        validate_action("rm_rf", array="/dev/md/root_a", device=None, confirm=None)


def test_scrub_needs_an_array() -> None:
    with pytest.raises(ActionRefused, match="needs an array"):
        validate_action("scrub_start", array=None, device=None, confirm=None)


def test_scrub_start_needs_no_device_or_confirmation() -> None:
    """A consistency scrub reads both copies and rewrites nothing, so it
    is not destructive and must not demand a typed confirmation."""
    params = validate_action("scrub_start", array="/dev/md/root_a", device=None, confirm=None)
    assert params == {"action": "scrub_start", "array": "/dev/md/root_a"}
    assert "scrub_start" not in DESTRUCTIVE


@pytest.mark.parametrize("action", sorted(DESTRUCTIVE))
def test_destructive_actions_demand_the_device_typed_back(action: str) -> None:
    """Not a generic "yes": a typed confirmation that is not the thing
    being destroyed is a click-through with extra steps."""
    with pytest.raises(ActionRefused, match="destructive"):
        validate_action(action, array="/dev/md/root_a", device="/dev/sdb4", confirm="yes")
    with pytest.raises(ActionRefused, match="destructive"):
        validate_action(action, array="/dev/md/root_a", device="/dev/sdb4", confirm=None)
    # The device path exactly — and only that — is accepted.
    params = validate_action(
        action, array="/dev/md/root_a", device="/dev/sdb4", confirm="/dev/sdb4"
    )
    assert params["device"] == "/dev/sdb4"


def test_a_confirmation_for_a_different_device_is_refused() -> None:
    """The case a generic confirmation cannot catch: the operator meant
    one disk and typed the other."""
    with pytest.raises(ActionRefused):
        validate_action(
            "add_member",
            array="/dev/md/root_a",
            device="/dev/sdb4",
            confirm="/dev/sdc4",
        )


@pytest.mark.parametrize(
    "array",
    ["/dev/sda1", "md0", "/dev/md/root_a; rm -rf /", "/etc/passwd", ""],
)
def test_array_paths_are_allowlisted(array: str) -> None:
    """The values reach argv on the host. An allowlist, not escaping —
    escaping is how one of these becomes a second argument."""
    with pytest.raises(ActionRefused):
        validate_action("scrub_start", array=array, device=None, confirm=None)


@pytest.mark.parametrize("device", ["/dev/sdb4 --force", "sdb4", "$(reboot)", "/dev/../etc/passwd"])
def test_device_paths_are_allowlisted(device: str) -> None:
    for bad in (device,):
        with pytest.raises(ActionRefused):
            validate_action("fail_member", array="/dev/md/root_a", device=bad, confirm=bad)


def test_both_md_array_spellings_are_accepted() -> None:
    """The installer creates NAMED arrays (/dev/md/root_a), but an
    operator-built one is usually /dev/md0."""
    for array in ("/dev/md0", "/dev/md127", "/dev/md/root_a", "/dev/md/var"):
        validate_action("scrub_start", array=array, device=None, confirm=None)


def test_mpath_topology_needs_nothing() -> None:
    assert validate_action("mpath_topology", array=None, device=None, confirm=None) == {
        "action": "mpath_topology"
    }


def test_every_action_has_a_summary() -> None:
    """The summary is what the audit row and the operator's confirmation
    both read, so a new action without one would be recorded as the
    fallback sentence for a different operation."""
    seen = {summarize(a, "/dev/md/root_a", "/dev/sdb4") for a in ACTIONS}
    assert len(seen) == len(ACTIONS)


def test_the_add_summary_says_it_erases() -> None:
    text = summarize("add_member", "/dev/md/root_a", "/dev/sdb4")
    assert "ERASES /dev/sdb4" in text
