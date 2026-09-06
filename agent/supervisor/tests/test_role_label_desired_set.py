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

import inspect

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

    Asserts on the SHARED writer, and separately that neither caller builds
    its own diff — because the first version of this test re-implemented
    ``apply_role_assignment``'s diff in the test body, and a review proved
    that reverting the fix to the exact pre-#1003 expression left all 339
    supervisor tests green. A test that reimplements the code it guards is
    testing the reimplementation.
    """
    _as_control_plane(monkeypatch)
    profiles = ["dns-bind9"]

    service_lifecycle.reconcile_node_labels(profiles)
    reconcile_diff = captured[-1]

    assert service_lifecycle.role_label_diff(profiles) == reconcile_diff
    assert reconcile_diff["spatium.io/role-control-plane"] == "true"


def test_neither_writer_builds_its_own_diff() -> None:
    """The structural half, and the one that actually catches the revert.

    ``apply_role_assignment`` clearing a label it computed from ``profiles``
    alone IS the bug. Both writers must go through ``role_label_diff``, and
    neither may contain the profiles-only comprehension.
    """
    for fn in (
        service_lifecycle.apply_role_assignment,
        service_lifecycle.reconcile_node_labels,
    ):
        # Comments stripped: apply_role_assignment's own comment QUOTES the
        # pre-#1003 expression to explain the bug, so a raw substring test
        # matches the explanation and reports the defect as still present.
        # Third time that trap has fired on this branch.
        src = "\n".join(
            ln
            for ln in inspect.getsource(fn).splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert "role_label_diff(profiles)" in src, f"{fn.__name__} builds its own diff"
        assert "{p for p in profiles" not in src, (
            f"{fn.__name__} contains the pre-#1003 profiles-only set comprehension"
        )


def test_a_role_the_operator_removed_is_still_cleared(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, str | None]]
) -> None:
    """The fix must not turn label-clearing off — a DNS role toggled off has
    to lose its label, or the workload keeps scheduling there."""
    _as_control_plane(monkeypatch)
    service_lifecycle.reconcile_node_labels([])
    assert captured[-1]["spatium.io/role-dns-bind9"] is None
