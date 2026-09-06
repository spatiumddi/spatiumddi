"""The shipped preseed examples mention every key the parser accepts.

#549 gives the appliance a headless install; #995 added seven new options to
it (root-password choice, keymap, NTP, SSH keys + password-SSH toggle, a
DHCP-mode interface pin, static IPv6) and #1003 wired the NTP one through to
the control plane.

Every one of those worked. **None of them appeared in the two
``.example`` files an operator actually copies** — so in practice a headless
install could not use any of #995's new capabilities, and nothing failed to
say so: an absent key is a legal preseed that silently falls through to the
interactive prompt, which on an unattended run means the default.

That is the failure this guards: not a broken parser, but a documented
surface that quietly stops matching the code. The parser is the source of
truth; the examples must at least MENTION every key it reads, commented out
if it is optional.

Keys that are genuinely inapplicable to a role are listed in
``_NOT_APPLICABLE`` with the reason. That list is the only escape hatch, and
it is deliberately in this file rather than inferred — adding to it is a
sentence somebody has to write.
"""

from __future__ import annotations

import re

import pytest

from _installer_source import BIN

_PARSER = (BIN / "spatium-preseed-parse").read_text(encoding="utf-8")
_EXAMPLES = BIN.parents[3] / "cloud-init"  # appliance/cloud-init

# Keys read from each mapping level, straight out of the parser.
_TOP = sorted(set(re.findall(r'ps\.get\("([a-z_0-9]+)"', _PARSER)))
_NET = sorted(set(re.findall(r'net\.get\("([a-z_0-9]+)"', _PARSER)))
_K3S = sorted(set(re.findall(r'k3s\.get\("([a-z_0-9]+)"', _PARSER)))

_NOT_APPLICABLE = {
    # An Additional node joins an existing control plane; the first node
    # has nothing to point at and no code to redeem.
    "control-plane": {"control_plane_url", "pairing_code"},
    # Nothing role-specific is missing from the appliance example.
    "appliance": set(),
}


def _example(role: str) -> str:
    return (_EXAMPLES / f"spatium-preseed-{role}.yaml.example").read_text(encoding="utf-8")


def test_the_parser_key_lists_are_not_empty() -> None:
    """The extraction is regex-based, so an empty list is a broken test
    reporting a clean pass — the exact shape this file exists to catch."""
    assert len(_TOP) >= 10, f"only found {_TOP} top-level keys — extraction broken?"
    assert len(_NET) >= 8, f"only found {_NET} network keys — extraction broken?"
    assert _K3S, "no k3s keys found — extraction broken?"


@pytest.mark.parametrize("role", ["control-plane", "appliance"])
def test_every_parser_key_is_mentioned(role: str) -> None:
    text = _example(role)
    missing = [
        k
        for k in (_TOP + _NET + _K3S)
        if k not in _NOT_APPLICABLE[role]
        and not re.search(rf"^\s*#?\s*{re.escape(k)}\s*:", text, re.M)
    ]
    assert not missing, (
        f"{role}.yaml.example never mentions {missing}. An operator copying "
        f"this file cannot use those options, and omitting a key is a LEGAL "
        f"preseed that silently falls through to the default. Add it "
        f"(commented out is fine) or list it in _NOT_APPLICABLE with a reason."
    )


@pytest.mark.parametrize("role", ["control-plane", "appliance"])
def test_examples_are_valid_yaml_with_a_spatium_preseed_block(role: str) -> None:
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_example(role))
    assert "spatium_preseed" in doc
    assert doc["spatium_preseed"].get("role")


def test_not_applicable_entries_really_are_absent() -> None:
    """Stops the escape hatch rotting into a list of keys that ARE there."""
    for role, keys in _NOT_APPLICABLE.items():
        text = _example(role)
        for k in keys:
            assert not re.search(rf"^\s*#?\s*{re.escape(k)}\s*:", text, re.M), (
                f"{k} is listed as not-applicable to {role} but appears in its "
                f"example — drop it from _NOT_APPLICABLE"
            )


def test_the_995_options_are_all_present_in_both() -> None:
    """Named explicitly, because these are the ones that were missing and a
    generic sweep would let them go missing again one at a time."""
    for role in ("control-plane", "appliance"):
        text = _example(role)
        for k in (
            "set_root_password",
            "keymap",
            "ntp_servers",
            "ssh_authorized_keys",
            "ssh_disable_password",
            "ip6",
            "prefix6",
            "gateway6",
        ):
            assert re.search(rf"^\s*#?\s*{re.escape(k)}\s*:", text, re.M), (
                f"{k} (a #995 option) is absent from {role}.yaml.example"
            )
