"""Interface MTU — the three doors and the one rule (#1017).

The appliance could not set an interface MTU anywhere: not in the
installer, not in the preseed schema, not in STATE, not in the
host-config plane.  The only route was ``nmtui``, and per #1016 an edit
there is reverted at the next boot.

The case for it is **not** jumbo frames.  SpatiumDDI's own traffic is
small UDP and small JSON, and raising the MTU on a DHCP-served segment is
actively hazardous because PXE ROMs are 1500.  The gap is the other
direction: an appliance reached over a tunnel (WireGuard, IPsec, GRE,
PPPoE) or sitting on a reduced-MTU provider underlay needs an MTU *below*
1500, and the failure mode is nasty — ping and small requests work, large
TCP hangs, and it reads as an application fault.

Three doors write this value and each one can refuse it: the installer
wizard, ``spatium-preseed-parse``, and ``spatium-etc-render`` as the last
thing between a hand-edited STATE and NetworkManager.  The tests here
pin the properties that would be wrong in a way nobody would notice:

* **one rule at all three doors** — a value the parser accepts and the
  renderer then drops is a setting the operator was told took effect and
  did not, which is the failure this whole feature exists to prevent;
* **the 1280 refusal** — below it IPv6 cannot run on the link at all
  (RFC 8200), so a static v6 address there is refused rather than warned
  about.  With v6 on its RA / SLAAC default nothing is configured to
  break and a 1200-byte tunnel is allowed, which is the case the feature
  is FOR;
* **"no keyfile, no MTU"** — for DHCP with no pinned port etc-render
  writes no keyfile at all, so an MTU there would be stored, displayed,
  and reach nothing;
* **the status sidecar cannot out-claim the keyfile** — it is what the
  fleet-consistency check upstream compares, so a sidecar reporting a
  value the keyfile does not carry would report two disagreeing nodes as
  matching.

These drive the REAL scripts rather than re-deriving their logic: the
renderer runs end to end against a temp STATE, and the wizard's
validator is sliced out and executed.  A structural check would pass
against an inverted comparison.

HOW TO RUN (from the repo root):
    python3 -m pytest appliance/tests/test_network_mtu.py -v

Needs ``sh`` and ``python3`` (3.10+ for the parser).  No root, no
NetworkManager, no Docker.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from _installer_source import BIN, CODE, extract_fn

ETC_RENDER = BIN / "spatium-etc-render"
PARSER = BIN / "spatium-preseed-parse"
ADOPT = BIN / "spatium-network-adopt"


# ── the renderer, run for real ────────────────────────────────────────


def _run_render(tmp_path: Path, config: str) -> dict[str, object]:
    """Run ``spatium-etc-render`` against a temp STATE and report what it did.

    Paths are rebased into ``tmp_path`` with sed rather than by
    parameterising the script, so what runs is the shipped text — a
    rewritten copy would let a change to the real paths pass here
    forever.
    """
    root = tmp_path / "root"
    state = root / "var/lib/spatium-state"
    state.mkdir(parents=True)
    (state / "spatium-config.yaml").write_text(config + "\n", encoding="utf-8")
    (root / "etc").mkdir(parents=True)

    src = ETC_RENDER.read_text(encoding="utf-8")
    src = re.sub(r"^STATE_DIR=.*$", f"STATE_DIR={state}", src, count=1, flags=re.M)
    src = re.sub(r"^LOG=.*$", f"LOG={root}/render.log", src, count=1, flags=re.M)
    src = src.replace("/etc/NetworkManager", f"{root}/etc/NetworkManager")
    src = src.replace("/etc/spatiumddi", f"{root}/etc/spatiumddi")
    script = tmp_path / "render.sh"
    script.write_text(src, encoding="utf-8")

    subprocess.run(["sh", str(script)], capture_output=True, text=True, check=False)

    conns = root / "etc/NetworkManager/system-connections"
    keyfiles = sorted(p.name for p in conns.glob("*.nmconnection")) if conns.is_dir() else []
    keyfile_text = "\n".join(
        (conns / name).read_text(encoding="utf-8") for name in keyfiles
    )
    mtu_in_keyfile: str | None = None
    for line in keyfile_text.splitlines():
        if line.startswith("mtu="):
            mtu_in_keyfile = line.split("=", 1)[1]

    sidecar: dict[str, str] = {}
    status = root / "etc/spatiumddi/network-status"
    if status.exists():
        for line in status.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                sidecar[key] = value

    return {
        "keyfiles": keyfiles,
        "keyfile_text": keyfile_text,
        "mtu": mtu_in_keyfile,
        "sidecar": sidecar,
        "log": (root / "render.log").read_text(encoding="utf-8")
        if (root / "render.log").exists()
        else "",
    }


_STATIC = """network_mode: static
network_interface: eth0
network_ip: 10.0.0.5
network_prefix: 24
network_gateway: 10.0.0.1"""


def test_a_configured_mtu_reaches_the_static_keyfile(tmp_path):
    got = _run_render(tmp_path, _STATIC + "\nnetwork_mtu: 1400")
    assert got["mtu"] == "1400"
    assert "[ethernet]\nmtu=1400" in got["keyfile_text"]


def test_a_configured_mtu_reaches_the_pinned_dhcp_keyfile(tmp_path):
    got = _run_render(
        tmp_path, "network_mode: dhcp\nnetwork_interface: eth0\nnetwork_mtu: 1400"
    )
    assert got["keyfiles"] == ["10-spatium-dhcp.nmconnection"]
    assert got["mtu"] == "1400"


def test_no_mtu_configured_writes_no_mtu_line(tmp_path):
    """The normal case on every appliance installed to date. The
    ``[ethernet]`` section stays present and empty, exactly as before —
    the keyfile must not change shape for an install that set nothing.
    """
    got = _run_render(tmp_path, _STATIC)
    assert got["mtu"] is None
    assert "[ethernet]" in got["keyfile_text"]
    assert got["sidecar"]["MTU_APPLIED"] == "default"


@pytest.mark.parametrize(
    "bad",
    [
        "99999",
        "0",
        "42",
        "abc",
        "1400abc",
        "-1500",
        "1.5",
        # Longer than a signed 64-bit integer. The shell's ``[ -lt ]``
        # does NOT fail safe here: dash and bash both print "Illegal
        # number" and exit non-zero, which reads as "not out of range"
        # and fell through to writing the value — so a 20-digit MTU
        # reached the keyfile, NetworkManager rejected the profile, and
        # every surface reported the MTU as applied. ``set -e`` does not
        # catch it because the test sits in an ``if`` condition.
        "99999999999999999999",
        "1" * 40,
    ],
)
def test_a_value_the_renderer_cannot_trust_is_dropped_not_written(tmp_path, bad):
    """STATE is a hand-editable flat file on a partition the operator can
    mount, so this is the last check before NetworkManager. An
    unparseable ``mtu=`` risks NM rejecting the profile outright, and a
    box that comes up with NO network is far worse than one at the
    default MTU — so the value is dropped and the reason logged.
    """
    got = _run_render(tmp_path, _STATIC + f"\nnetwork_mtu: {bad}")
    assert got["mtu"] is None
    assert got["keyfiles"] == ["10-spatium-static.nmconnection"], "the profile must survive"
    assert got["sidecar"]["MTU_APPLIED"] == "dropped"
    assert "network_mtu" in got["log"]


def test_below_1280_is_dropped_when_a_static_ipv6_address_is_pinned(tmp_path):
    """RFC 8200 makes 1280 the IPv6 minimum link MTU. Both doors refuse
    this combination outright, so reaching the renderer means STATE was
    hand-edited — and it is the MTU that gets dropped rather than the
    address, because dropping the address would take the appliance off
    the network the operator reaches it on.
    """
    got = _run_render(
        tmp_path,
        _STATIC + "\nnetwork6_mode: static\nnetwork6_ip: 2001:db8::5\nnetwork_mtu: 1200",
    )
    assert got["mtu"] is None
    assert "address1=2001:db8::5" in got["keyfile_text"], "the v6 address must survive"
    assert "1280" in got["log"]


def test_below_1280_is_allowed_without_a_static_ipv6_address(tmp_path):
    """The case the feature exists for: a 1200-byte tunnel. IPv6 on its
    RA / SLAAC default has no configured address to break and no explicit
    intent to honour, so refusing here would refuse the whole point.
    """
    got = _run_render(tmp_path, _STATIC + "\nnetwork_mtu: 1200")
    assert got["mtu"] == "1200"


def test_1280_exactly_is_allowed_alongside_static_ipv6(tmp_path):
    got = _run_render(
        tmp_path,
        _STATIC + "\nnetwork6_mode: static\nnetwork6_ip: 2001:db8::5\nnetwork_mtu: 1280",
    )
    assert got["mtu"] == "1280"


# ── the status sidecar ────────────────────────────────────────────────


def test_the_sidecar_never_claims_an_mtu_the_keyfile_does_not_carry(tmp_path):
    """DHCP with no pinned port writes NO keyfile — NetworkManager uses
    its own auto profile — so nothing is in force however STATE was
    edited.

    This is the property the fleet-consistency check upstream rests on.
    A sidecar reporting the REQUESTED value here would have the control
    plane agree that a node running 9000 and a node running the link
    default match, which is the mixed-MTU black hole the check exists to
    catch, reported as healthy.
    """
    got = _run_render(tmp_path, "network_mode: dhcp\nnetwork_mtu: 9000")
    assert got["keyfiles"] == []
    assert got["sidecar"]["MTU"] == ""
    assert got["sidecar"]["MTU_APPLIED"] == "n/a"
    # The operator's value is still reported, or the mismatch would not
    # be diagnosable from the fleet surfaces.
    assert got["sidecar"]["MTU_REQUESTED"] == "9000"


def test_the_sidecar_reports_n_a_when_a_static_stanza_is_incomplete(tmp_path):
    """etc-render skips the keyfile when a static install names no
    interface. The sidecar keys off whether a keyfile was WRITTEN rather
    than re-testing ``mode = static``, which would report an MTU in force
    on a box whose profile was never rendered.
    """
    got = _run_render(tmp_path, "network_mode: static\nnetwork_ip: 10.0.0.5\nnetwork_mtu: 1400")
    assert got["keyfiles"] == []
    assert got["sidecar"]["MTU_APPLIED"] == "n/a"
    assert got["sidecar"]["MTU"] == ""


def test_the_sidecar_and_the_keyfile_agree_on_every_applied_value(tmp_path):
    got = _run_render(tmp_path, _STATIC + "\nnetwork_mtu: 1400")
    assert got["sidecar"]["MTU"] == got["mtu"] == "1400"
    assert got["sidecar"]["MTU_APPLIED"] == "applied"
    assert got["sidecar"]["INTERFACE"] == "eth0"


# ── the installer's validator, executed ───────────────────────────────


def _mtu_error(mtu: str, v6_mode: str = "auto", v6_ip: str = "") -> str | None:
    """Run the wizard's ``_mtu_error``; None when it accepts."""
    script = "\n".join(
        [
            "set -uo pipefail",
            extract_fn("_mtu_error"),
            f'NET_MTU="{mtu}"; NET6_MODE="{v6_mode}"; NET6_IP="{v6_ip}"',
            "_mtu_error",
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    return None if proc.returncode == 0 else proc.stdout.strip()


@pytest.mark.parametrize("good", ["", "576", "1280", "1400", "1500", "9000"])
def test_the_wizard_accepts_a_usable_mtu(good):
    assert _mtu_error(good) is None


@pytest.mark.parametrize("bad", ["575", "9001", "0", "abc", "14.5", "-1", "+1400"])
def test_the_wizard_refuses_an_unusable_mtu(bad):
    assert _mtu_error(bad) is not None


def test_the_wizard_refuses_below_1280_alongside_static_ipv6():
    err = _mtu_error("1200", "static", "2001:db8::5")
    assert err is not None
    assert "1280" in err and "RFC 8200" in err


def test_the_wizard_allows_below_1280_when_ipv6_is_automatic():
    assert _mtu_error("1200", "auto", "") is None


def test_the_wizard_allows_below_1280_when_static_ipv6_has_no_address():
    """``network6_mode: static`` with no address renders ``method=auto``
    — the renderer's own fallback — so there is no v6 address to break
    and refusing would contradict what actually gets written.
    """
    assert _mtu_error("1200", "static", "") is None


# ── the three doors agree ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,acceptable",
    [("1400", True), ("576", True), ("9000", True), ("575", False), ("9001", False), ("0", False)],
)
def test_the_wizard_and_the_renderer_reach_the_same_verdict(tmp_path, value, acceptable):
    """One rule, three doors. A value one accepts and another drops is a
    setting the operator was told took effect and did not — the exact
    failure this feature exists to prevent, reproduced inside it.
    """
    wizard_ok = _mtu_error(value) is None
    rendered = _run_render(tmp_path, _STATIC + f"\nnetwork_mtu: {value}")
    render_ok = rendered["mtu"] is not None
    assert wizard_ok is render_ok is acceptable


# ── structural: the wiring that has no runtime surface here ───────────


def test_the_installer_writes_the_mtu_into_state():
    assert 'network_mtu: "$NET_MTU"' in CODE


def test_the_installer_exports_the_mtu_in_the_answers_file():
    """#995 item 29's export must round-trip the value, or an identical
    reinstall from the exported answers comes up at the default MTU —
    silently, and on the setting whose absence is hardest to spot.
    """
    assert "mtu: %s" in CODE


def test_any_port_dhcp_is_not_offered_an_mtu():
    """The prompt is gated on a pinned interface. Offering it on
    any-port DHCP would accept a value, show it on the Confirm screen,
    store it in STATE and have it reach nothing.
    """
    fn = extract_fn("ask_network")
    assert '[ "$NET_MODE" = "dhcp" ] && [ -n "$NET_INTERFACE" ]' in fn


def test_the_preseed_parser_refuses_an_mtu_with_no_pinned_interface():
    parser = PARSER.read_text(encoding="utf-8")
    assert "network.mtu requires network.interface" in parser


def test_the_adopt_tool_models_the_mtu():
    """#1016 shipped with ``ethernet.mtu`` as its headline UNadoptable
    setting. It must now be in ``_MANAGED_KEYS`` *and* projected, or it
    falls between the two lists: filtered out of the "will revert" panel
    while nothing offers to adopt it.
    """
    adopt = ADOPT.read_text(encoding="utf-8")
    assert '("ethernet", "mtu")' in adopt
    assert 'out["network_mtu"]' in adopt


def test_the_shipped_preseed_examples_mention_the_mtu():
    """An absent key is a legal preseed that silently falls through to
    the default, so a capability missing from the examples is one a
    headless install cannot use and nothing reports.
    """
    examples = BIN.parents[3] / "cloud-init"
    for name in (
        "spatium-preseed-control-plane.yaml.example",
        "spatium-preseed-appliance.yaml.example",
    ):
        assert "mtu" in (examples / name).read_text(encoding="utf-8"), name
