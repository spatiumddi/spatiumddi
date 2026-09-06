"""The role-chart values the supervisor PATCHes into its HelmChart (#992).

``_build_values`` is one half of a pair. The appliance chart is installed
TWICE per appliance — ``spatium-bootstrap`` (firstboot: the supervisor
DaemonSet + the CNPG operator) and ``spatiumddi-appliance`` (this, the role
DaemonSets) — and the PriorityClasses it renders are CLUSTER-SCOPED. Helm
stamps ``meta.helm.sh/release-name`` on everything it creates and refuses an
install WHOLE when it meets an object owned by another release, so exactly
one of the two may render them.

#988 shipped with both rendering them. Every fresh install after it had its
role chart refused with ``invalid ownership metadata`` and therefore NO role
DaemonSet on the cluster at all — invisibly, because the k3s helm-controller
job carries ``backoffLimit: 1000`` and simply retried forever.

The other half of the pair is
``.github/scripts/charts-render-check.sh``, which renders both release
shapes and fails on any cluster-scoped object appearing in both. That
script mirrors the flags asserted here; this test is what stops the mirror
drifting, so keep the two in step.
"""

from __future__ import annotations

from spatium_supervisor.service_lifecycle import _build_values


def _values(profiles: list[str] | None = None) -> dict:
    return _build_values(profiles or ["dns-bind9"], {"CONTROL_PLANE_URL": "https://cp.example"})


def test_supervisor_release_never_renders_the_priority_classes() -> None:
    """create: false — bootstrap owns them, and it installs first."""
    assert _values()["priorityClasses"]["create"] is False


def test_supervisor_release_asserts_the_classes_are_external() -> None:
    """external: true keeps the chart's own guard satisfied.

    Without it the guard asks the apiserver, and an answer of "absent" here
    means only that bootstrap has not finished yet — never that these values
    are wrong. Failing the render on that would trade #992's permanent
    failure for a transient one, on a job that retries forever either way.
    """
    assert _values()["priorityClasses"]["external"] is True


def test_the_priority_class_block_is_role_independent() -> None:
    """Release ownership is not a per-role decision.

    A node assigned no roles at all still installs this release, and would
    still collide with bootstrap over the classes if the block were gated on
    a profile being present.
    """
    for profiles in ([], ["dns-bind9"], ["dhcp"], ["dns-powerdns", "dhcp", "looking-glass"]):
        assert _values(profiles)["priorityClasses"] == {"create": False, "external": True}


def test_the_supervisor_daemonset_stays_owned_by_bootstrap() -> None:
    """Guards the other half of the two-release split.

    ``supervisor.enabled: false`` is what keeps this release from also
    claiming the supervisor's ClusterRole / ClusterRoleBinding — the same
    cluster-scoped collision as the PriorityClasses, one values flip away.
    """
    assert _values()["supervisor"]["enabled"] is False
