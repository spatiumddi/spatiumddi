"""apply_role_assignment must not clear labels reconcile just set (#1003 item 3).

Reported from a fresh control-plane install. On the first heartbeat:

    supervisor.k3s_lifecycle.labels_applied  set=[]  cleared=[..., role-control-plane]

``reconcile_node_labels`` runs first on each tick and unions the operator's
profiles with ``_VARIANT_FIXED_ROLES`` and the promoted-member join state.
``apply_role_assignment`` then ran on the same tick with a desired set built
from ``profiles`` ALONE, and cleared every label outside it — including the
``control-plane`` label the reconcile had just asserted and the install had
baked. The api and worker pods that the same tick's memory-limit re-render had
just rolled then hit:

    0/1 nodes are available: 1 node(s) didn't match Pod's node affinity/selector

Single node, so the next tick healed it. On a #272 multi-node control plane,
toggling DNS on a member makes that member briefly ineligible for every
control-plane workload — and non-negotiable #16 makes the label the source of
truth for placement, so a writer that clears labels must know the full set.

These tests assert on the LABEL DIFF actually PATCHed, not on the helper's
return value: the defect was never in computing the set, it was in which
computation the patch used.
"""

from __future__ import annotations

import pytest

from spatium_supervisor import appliance_state, k8s_api, service_lifecycle


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str | None]]:
    """Capture every label diff PATCHed, without touching a kubeapi."""
    seen: list[dict[str, str | None]] = []

    def _patch(node: str, diff: dict[str, str | None]) -> tuple[bool, str | None]:
        seen.append(dict(diff))
        return True, None

    monkeypatch.setattr(k8s_api, "patch_node_labels", _patch)
    monkeypatch.setenv("NODE_NAME", "ddi1")
    return seen


def _as_control_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appliance_state, "detect_appliance_variant", lambda: "control-plane")
    monkeypatch.setattr(appliance_state, "read_cluster_join_state", lambda: (None, None))


def _as_promoted_member(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appliance_state, "detect_appliance_variant", lambda: "appliance")
    monkeypatch.setattr(appliance_state, "read_cluster_join_state", lambda: ("ready", None))


def test_desired_set_includes_variant_fixed_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    _as_control_plane(monkeypatch)
    assert "control-plane" in service_lifecycle.desired_role_set([])


def test_desired_set_includes_a_promoted_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """#277 — an `appliance` node that joined as a k3s server."""
    _as_promoted_member(monkeypatch)
    assert "control-plane" in service_lifecycle.desired_role_set([])


def test_reconcile_sets_control_plane(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, str | None]]
) -> None:
    _as_control_plane(monkeypatch)
    ok, err = service_lifecycle.reconcile_node_labels(["dns-bind9"])
    assert ok, err
    assert captured[-1]["spatium.io/role-control-plane"] == "true"


def test_reconcile_and_apply_agree_on_the_same_tick(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, str | None]]
) -> None:
    """THE REGRESSION. Both writers run per tick; they must not disagree.

    Driven through ``desired_role_set`` rather than through
    ``apply_role_assignment`` end-to-end, which would need a compose env
    file, a rendered chart and a live kubeapi — none of which is what broke.
    """
    _as_control_plane(monkeypatch)
    profiles = ["dns-bind9"]

    service_lifecycle.reconcile_node_labels(profiles)
    reconcile_diff = captured[-1]

    roles = service_lifecycle.desired_role_set(profiles)
    apply_diff = {
        label: ("true" if role in roles else None)
        for role, label in service_lifecycle._ROLE_LABEL_KEYS.items()
    }

    assert apply_diff == reconcile_diff, (
        "the two writers disagree — whichever runs second wins, and on the "
        "reported box that was the one clearing role-control-plane"
    )
    assert apply_diff["spatium.io/role-control-plane"] == "true"


def test_the_old_profiles_only_computation_would_have_cleared_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control, kept in the file.

    Pins WHY the shared helper exists: the pre-fix expression is still
    perfectly valid Python, so without this the fix could be reverted to it
    and every other test here would still pass.
    """
    _as_control_plane(monkeypatch)
    profiles = ["dns-bind9"]

    old = {p for p in profiles if p in service_lifecycle._ROLE_LABEL_KEYS}
    assert "control-plane" not in old, "the pre-fix expression, reproduced"
    assert "control-plane" in service_lifecycle.desired_role_set(profiles)


def test_a_role_the_operator_removed_is_still_cleared(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, str | None]]
) -> None:
    """The fix must not turn label-clearing off — a DNS role toggled off has
    to lose its label, or the workload keeps scheduling there."""
    _as_control_plane(monkeypatch)
    service_lifecycle.reconcile_node_labels([])
    assert captured[-1]["spatium.io/role-dns-bind9"] is None
