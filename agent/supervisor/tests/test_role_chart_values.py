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

from spatium_supervisor.service_lifecycle import _build_values, roles_awaiting_key


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


# ---------------------------------------------------------------------------
# #1062 — an agent DaemonSet is rendered only once its key exists.
#
# Every agent entrypoint refuses an empty key and exits 2, so the old
# ``enabled: True`` on every block made the supervisor's FIRST apply — the idle
# heartbeat before any role or key had arrived — create every agent DaemonSet
# keyless. The role label then landed in milliseconds while the keyed re-render
# took helm-controller's Job ~11 s, and every fresh install scheduled a
# revision-1 pod that could only die (both 2026-09-11 QA formations, and the
# 2026-09-15 base rig). Gating the render on the key makes the first revision
# that exists the keyed one.
# ---------------------------------------------------------------------------
_HEX48 = "0123456789abcdef" * 3
_AGENT_BLOCKS = ("dnsBind9", "dnsPowerdns", "dnsTechnitium", "dhcpKea", "lookingGlass")


def _enabled(values: dict) -> dict[str, bool]:
    return {b: values[b]["enabled"] for b in _AGENT_BLOCKS}


def test_the_idle_first_apply_renders_no_agent_daemonset() -> None:
    """No role, no key: the values the first heartbeat applies must not create
    a DaemonSet that only a later, keyed apply could make runnable."""
    assert _enabled(_build_values([], {})) == dict.fromkeys(_AGENT_BLOCKS, False)


def test_a_role_assigned_without_its_key_is_held_back_not_crash_looped() -> None:
    """The bug's exact window — roles present, keys not yet in the env — and
    the state of a role whose key the control plane never configured."""
    values = _build_values(["dns-bind9", "dhcp"], {"CONTROL_PLANE_URL": "https://cp.example"})
    assert _enabled(values) == dict.fromkeys(_AGENT_BLOCKS, False)
    assert roles_awaiting_key(["dns-bind9", "dhcp"], values) == {"dns-bind9", "dhcp"}


def test_the_first_keyed_render_enables_the_daemonsets_with_their_keys() -> None:
    env = {"DNS_AGENT_KEY": _HEX48, "DHCP_AGENT_KEY": _HEX48, "AGENT_GROUP": "g",
           "DHCP_AGENT_GROUP": "d"}
    values = _build_values(["dns-bind9", "dhcp"], env)
    assert _enabled(values) == {"dnsBind9": True, "dnsPowerdns": True, "dnsTechnitium": True,
                                "dhcpKea": True, "lookingGlass": False}
    for block in ("dnsBind9", "dnsPowerdns", "dnsTechnitium"):
        assert values[block]["agentKey"] == _HEX48
    assert values["dhcpKea"]["agentKey"] == _HEX48
    assert roles_awaiting_key(["dns-bind9", "dhcp"], values) == set()


def test_a_dns_engine_swap_stays_a_label_flip() -> None:
    """One DNS key renders all three engines, so bind9 -> PowerDNS is still a
    node-label change, not a chart upgrade — the Phase 10 wave 2 property."""
    values = _build_values(["dns-bind9"], {"DNS_AGENT_KEY": _HEX48})
    assert values["dnsPowerdns"]["enabled"] is True
    assert values["dnsTechnitium"]["enabled"] is True
    assert values["dhcpKea"]["enabled"] is False  # no DHCP key, no DHCP DaemonSet


def test_the_looking_glass_block_follows_the_same_gate() -> None:
    assert _build_values(["looking-glass"], {})["lookingGlass"]["enabled"] is False
    values = _build_values(["looking-glass"], {"LG_AGENT_KEY": _HEX48})
    assert values["lookingGlass"]["enabled"] is True
    assert values["lookingGlass"]["agentKey"] == _HEX48


def test_removing_a_role_removes_its_daemonset_with_the_key() -> None:
    """role_orchestrator writes a role's key only while the role is assigned,
    so un-assigning DNS drops DNS_AGENT_KEY from the env and, now, the three
    DNS DaemonSets — the same chart upgrade the key's departure always caused,
    minus the keyless DaemonSet it used to leave behind."""
    before = _build_values(["dns-bind9", "dhcp"], {"DNS_AGENT_KEY": _HEX48, "DHCP_AGENT_KEY": _HEX48})
    after = _build_values(["dhcp"], {"DHCP_AGENT_KEY": _HEX48})
    assert before["dnsBind9"]["enabled"] is True and after["dnsBind9"]["enabled"] is False
    assert after["dhcpKea"]["enabled"] is True
    assert roles_awaiting_key(["dhcp"], after) == set()


def test_roles_awaiting_key_ignores_profiles_without_a_chart_block() -> None:
    values = _build_values(["control-plane"], {})
    assert roles_awaiting_key(["control-plane"], values) == set()
