"""Unit tests for the Proxmox client's parse helpers.

The HTTP layer is covered by ``test_proxmox_reconcile`` via a fake
client; these tests exercise the pure-function parsers that hydrate
``_ProxmoxNicDef`` / ``_ProxmoxGuest`` from PVE config strings.
"""

from __future__ import annotations

from app.services.proxmox.client import (
    _agent_flag_from_config,
    _cidr_from_sdn_id,
    _nics_from_lxc_config,
    _nics_from_qemu_config,
    _normalise_mac,
    _parse_ipconfig_gw,
    _parse_ipconfig_string,
    _parse_nic_string,
)

# ── _parse_nic_string ────────────────────────────────────────────────


def test_parse_vm_nic_virtio_with_tag() -> None:
    nic = _parse_nic_string("net0", "virtio=BC:24:11:E8:4A:3F,bridge=vmbr0,tag=10,firewall=1")
    assert nic.slot == "net0"
    assert nic.mac == "BC:24:11:E8:4A:3F"
    assert nic.bridge == "vmbr0"
    assert nic.vlan_tag == 10
    assert nic.static_cidr is None


def test_parse_vm_nic_e1000_no_tag() -> None:
    nic = _parse_nic_string("net1", "e1000=AA:BB:CC:DD:EE:FF,bridge=vmbr1")
    assert nic.mac == "AA:BB:CC:DD:EE:FF"
    assert nic.bridge == "vmbr1"
    assert nic.vlan_tag is None


def test_parse_lxc_nic_with_inline_static_ip() -> None:
    nic = _parse_nic_string(
        "net0",
        "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:E8:4A:3F,ip=10.0.0.5/24,gw=10.0.0.1",
    )
    assert nic.mac == "BC:24:11:E8:4A:3F"
    assert nic.bridge == "vmbr0"
    assert nic.static_cidr == "10.0.0.5/24"
    assert nic.static_gateway == "10.0.0.1"


def test_parse_lxc_nic_dhcp() -> None:
    nic = _parse_nic_string(
        "net0",
        "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:E8:4A:3F,ip=dhcp",
    )
    # "dhcp" is not a useful static CIDR — dropped to None so the
    # reconciler falls back to runtime IPs only.
    assert nic.static_cidr is None


def test_parse_nic_missing_hwaddr_preserves_other_fields() -> None:
    nic = _parse_nic_string("net0", "bridge=vmbr0,tag=50")
    assert nic.mac is None
    assert nic.bridge == "vmbr0"
    assert nic.vlan_tag == 50


# ── _parse_ipconfig_string ───────────────────────────────────────────


def test_parse_ipconfig_basic() -> None:
    assert _parse_ipconfig_string("ip=10.0.0.5/24,gw=10.0.0.1") == "10.0.0.5/24"


def test_parse_ipconfig_dhcp_returns_none() -> None:
    assert _parse_ipconfig_string("ip=dhcp") is None


def test_parse_ipconfig_empty_returns_none() -> None:
    assert _parse_ipconfig_string("gw=10.0.0.1") is None


# ── _parse_ipconfig_gw ───────────────────────────────────────────────


def test_parse_ipconfig_gw_basic() -> None:
    assert _parse_ipconfig_gw("ip=10.0.0.5/24,gw=10.0.0.1") == "10.0.0.1"


def test_parse_ipconfig_gw_missing_returns_none() -> None:
    assert _parse_ipconfig_gw("ip=10.0.0.5/24") is None
    assert _parse_ipconfig_gw("ip=dhcp") is None


# ── _agent_flag_from_config ──────────────────────────────────────────


def test_agent_flag_bare_1_enabled() -> None:
    assert _agent_flag_from_config("1") is True


def test_agent_flag_bare_0_disabled() -> None:
    assert _agent_flag_from_config("0") is False


def test_agent_flag_option_form_enabled() -> None:
    assert _agent_flag_from_config("enabled=1,type=virtio,freeze-fs-on-backup=1") is True


def test_agent_flag_option_form_disabled() -> None:
    assert _agent_flag_from_config("enabled=0,type=virtio") is False


def test_agent_flag_missing() -> None:
    assert _agent_flag_from_config(None) is False
    assert _agent_flag_from_config("") is False


# ── _nics_from_qemu_config ───────────────────────────────────────────


def test_qemu_config_pairs_ipconfig_with_matching_net_slot() -> None:
    cfg = {
        "net0": "virtio=BC:24:11:11:11:11,bridge=vmbr0",
        "net1": "virtio=BC:24:11:22:22:22,bridge=vmbr1",
        "ipconfig0": "ip=10.0.0.5/24,gw=10.0.0.1",
        "ipconfig1": "ip=dhcp",
    }
    nics = _nics_from_qemu_config(cfg)
    assert len(nics) == 2
    assert nics[0].slot == "net0"
    assert nics[0].static_cidr == "10.0.0.5/24"
    assert nics[0].static_gateway == "10.0.0.1"
    assert nics[1].slot == "net1"
    assert nics[1].static_cidr is None
    assert nics[1].static_gateway is None


def test_qemu_config_skips_empty_slots() -> None:
    cfg = {
        "net0": "virtio=AA:AA:AA:AA:AA:AA,bridge=vmbr0",
        # No net1 — skipped.
        "net2": "virtio=CC:CC:CC:CC:CC:CC,bridge=vmbr0",
    }
    nics = _nics_from_qemu_config(cfg)
    assert [n.slot for n in nics] == ["net0", "net2"]


# ── _nics_from_lxc_config ────────────────────────────────────────────


def test_lxc_config_inline_static_ip_on_netn() -> None:
    cfg = {
        "net0": "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:FF,ip=10.0.0.10/24",
    }
    nics = _nics_from_lxc_config(cfg)
    assert len(nics) == 1
    assert nics[0].mac == "AA:BB:CC:DD:EE:FF"
    assert nics[0].static_cidr == "10.0.0.10/24"


# ── _normalise_mac ───────────────────────────────────────────────────


def test_normalise_mac_lowercases_and_canonicalises_separator() -> None:
    assert _normalise_mac("BC:24:11:E8:4A:3F") == "bc:24:11:e8:4a:3f"
    assert _normalise_mac("BC-24-11-E8-4A-3F") == "bc:24:11:e8:4a:3f"
    assert _normalise_mac("  bc:24:11:e8:4a:3f  ") == "bc:24:11:e8:4a:3f"


# ── _cidr_from_sdn_id ────────────────────────────────────────────────


def test_cidr_from_sdn_id_v4() -> None:
    assert _cidr_from_sdn_id("localnetwork-10.0.0.0-24") == "10.0.0.0/24"


def test_cidr_from_sdn_id_with_hyphen_zone() -> None:
    # Zone names can contain hyphens; we split from the right so only
    # the last two ``-``-delimited tokens are consumed as net + prefix.
    assert _cidr_from_sdn_id("my-zone-192.168.10.0-24") == "192.168.10.0/24"


def test_cidr_from_sdn_id_garbage_returns_none() -> None:
    assert _cidr_from_sdn_id("") is None
    assert _cidr_from_sdn_id("no-prefix") is None
    assert _cidr_from_sdn_id("zone-10.0.0.0-notanint") is None
