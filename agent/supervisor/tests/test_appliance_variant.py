"""Tests for #272 Phase 1 — appliance-variant detection + variant-
aware node-label reconciliation.

The supervisor reads its installer-role variant from
``/etc/spatiumddi-host/role-config:ROLE`` and uses it to
(a) report ``appliance_variant`` in the heartbeat payload so the
Fleet UI can split rows into Control plane vs Service agents, and
(b) merge the variant's fixed role set into the per-tick label
reconciliation. After #272 there are two variants — ``control-plane``
(forces the control-plane label) and ``appliance`` (nothing fixed);
DNS/DHCP are operator-toggleable on every variant. Legacy pre-#272
strings (full-stack / frontend-core / application) are normalised to
the two canonical variants.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spatium_supervisor import appliance_state, service_lifecycle


@pytest.mark.parametrize(
    "role_value,expected",
    [
        ("control-plane", "control-plane"),
        ("appliance", "appliance"),
        # Legacy pre-#272 strings normalise to the two canonical
        # variants (full-stack + frontend-core were both control
        # planes; application was the data-plane node).
        ("full-stack", "control-plane"),
        ("frontend-core", "control-plane"),
        ("application", "appliance"),
        # Quoted values — installer wizard writes both forms; the
        # parser strips one layer of surrounding quotes.
        ('"control-plane"', "control-plane"),
        ("'application'", "appliance"),
        # Unknown value — refuse rather than report a bogus variant
        # the control plane wouldn't know how to categorise.
        ("control-cluster-member", None),
        ("", None),
    ],
)
def test_detect_appliance_variant_parses_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, role_value: str, expected: str | None
) -> None:
    role_config = tmp_path / "role-config"
    role_config.write_text(
        f"ROLE={role_value}\nCONTROL_PLANE_URL=https://x\nBOOTSTRAP_PAIRING_CODE=12345678\n"
    )
    monkeypatch.setattr(appliance_state, "_HOST_ROLE_CONFIG", role_config)

    assert appliance_state.detect_appliance_variant() == expected


def test_detect_appliance_variant_missing_file_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Pointing at a non-existent path is the supervisor's signal that
    # it's running on docker / k8s (no bind mount). The heartbeat
    # handler interprets None as "supervisor didn't ship the field"
    # and leaves the persisted column alone.
    monkeypatch.setattr(appliance_state, "_HOST_ROLE_CONFIG", tmp_path / "absent")
    assert appliance_state.detect_appliance_variant() is None


def test_variant_fixed_roles_table_covers_every_installer_variant() -> None:
    # Coupling test — every variant the installer wizard offers must
    # have an entry in _VARIANT_FIXED_ROLES so the reconciler has a
    # defined behaviour. Two canonical variants after #272.
    assert set(service_lifecycle._VARIANT_FIXED_ROLES.keys()) == {
        "control-plane",
        "appliance",
    }


def test_variant_fixed_roles_subset_of_label_keys() -> None:
    # Every fixed-role value must also be a key the supervisor knows
    # how to apply as a label. Without this, a typo in the fixed-set
    # ("dns-bin9") would silently no-op at reconcile time.
    every_role = set()
    for roles in service_lifecycle._VARIANT_FIXED_ROLES.values():
        every_role.update(roles)
    assert every_role.issubset(service_lifecycle._ROLE_LABEL_KEYS.keys())


def test_control_plane_forces_only_control_plane() -> None:
    # #272: the control-plane variant FORCES only the control-plane
    # label. DNS/DHCP are NOT forced (and NOT auto-assigned at install
    # either) — the operator enables them per node via the Fleet
    # toggle, so they must never appear in the forced set.
    assert service_lifecycle._VARIANT_FIXED_ROLES["control-plane"] == frozenset({"control-plane"})


def test_appliance_has_no_fixed_roles() -> None:
    # Appliance nodes inherit roles only from the operator's
    # heartbeat-response role assignment — nothing fixed.
    assert service_lifecycle._VARIANT_FIXED_ROLES["appliance"] == frozenset()


def test_dns_dhcp_are_never_force_asserted() -> None:
    # #272 Phase 7b (items 4/5): DNS/DHCP must be operator-toggleable on
    # every variant, so neither role may appear in ANY variant's forced
    # set — otherwise the reconciler re-adds the label every tick and
    # the operator can't shed (full-stack) or the role picker is moot.
    for variant, forced in service_lifecycle._VARIANT_FIXED_ROLES.items():
        assert "dns-bind9" not in forced, variant
        assert "dns-powerdns" not in forced, variant
        assert "dhcp" not in forced, variant
