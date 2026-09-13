"""NetworkManager must keep the interface configured through a carrier loss (#1084).

Stock NetworkManager deactivates a profile when its link drops (``activated ->
unavailable (reason 'carrier-changed')``) and withdraws the address and the
default route with it. On a cluster member that turns a cable flap into a k3s
crash loop: k3s exits on the lease it can no longer renew, and every 5 s
restart then dies on ``no default routes found in "/proc/net/route"`` until
the link is back (13-15 ``k3s.service`` failures per ~70 s partition on the
nightly QA rigs, measured on nightly-2026.09.12). The appliance's
NetworkManager configuration now ignores the carrier on ethernet devices, so
the address and route survive the flap and k3s waits on etcd instead.

These read the shipped ``conf.d`` file the way NetworkManager does (keyfile
INI): the guard is on the *shape* NetworkManager honours -- ``ignore-carrier``
is a boolean in a ``[device*]`` section scoped by ``match-device``; the
``[main] ignore-carrier=<device list>`` spelling is the deprecated form and a
``*`` in a ``[device]`` section is not a boolean at all.

    python3 -m pytest appliance/tests/test_networkmanager_ignore_carrier.py -v
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONF = REPO / "appliance" / "mkosi.extra" / "etc" / "NetworkManager" / "conf.d" / "10-spatiumddi.conf"

pytestmark = pytest.mark.skipif(not CONF.is_file(), reason="appliance tree not present")


def _parsed() -> configparser.RawConfigParser:
    cp = configparser.RawConfigParser(strict=True)
    cp.optionxform = str  # NetworkManager keys are case-sensitive
    cp.read_string(CONF.read_text(encoding="utf-8"))
    return cp


def _device_sections(cp: configparser.RawConfigParser) -> list[str]:
    return [s for s in cp.sections() if s == "device" or s.startswith("device-")]


def test_an_ethernet_device_section_ignores_the_carrier() -> None:
    cp = _parsed()
    matching = [
        s
        for s in _device_sections(cp)
        if cp.get(s, "ignore-carrier", fallback="").strip().lower() in ("yes", "true", "1")
    ]
    assert matching, (
        "no [device*] section sets ignore-carrier=yes -- a carrier loss deactivates the "
        "profile, withdraws the address and route, and k3s crash-loops on 'no default "
        "routes found' for the whole partition (#1084)"
    )
    # Scoped to ethernet (or unscoped, which NetworkManager reads as every device):
    # a section that only matches some other type would leave enp1s0 unprotected.
    for s in matching:
        spec = cp.get(s, "match-device", fallback="").strip()
        assert spec == "" or "type:ethernet" in spec or spec == "*", (
            f"[{s}] match-device={spec!r} does not cover the ethernet uplink"
        )


def test_the_deprecated_main_spelling_is_not_used() -> None:
    cp = _parsed()
    assert not cp.has_option("main", "ignore-carrier"), (
        "[main] ignore-carrier is the deprecated device-list form; the [device] boolean "
        "overrides it and is what this configuration asserts"
    )
    for s in _device_sections(cp):
        value = cp.get(s, "ignore-carrier", fallback="yes").strip()
        assert value.lower() in ("yes", "no", "true", "false", "1", "0"), (
            f"[{s}] ignore-carrier={value!r} is not a boolean -- NetworkManager would not honour it"
        )


def test_the_resolved_and_keyfile_defaults_survive() -> None:
    """The carrier section is additive: the DNS hand-off to systemd-resolved and the
    keyfile plugin the installer's static-IP override relies on must stay."""
    cp = _parsed()
    assert cp.get("main", "dns") == "systemd-resolved"
    assert cp.get("main", "plugins") == "keyfile"
