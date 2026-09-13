"""nmtui edits vs the STATE render (#1016).

``spatium-etc-render`` rewrites the two managed NetworkManager keyfiles
from ``spatium-config.yaml`` on every boot, ordered
``Before=NetworkManager.service``, so NM never sees an operator's nmtui
edit to them.  The change works, is verified working, and reverts on a
reboot that may be weeks later — often a slot upgrade, which supplies a
much more plausible suspect.  ``appliance/mkosi.conf`` meanwhile
advertises NetworkManager on the strength of everything being "editable
via nmtui", which was true for exactly one boot.

``spatium-network-adopt`` closes it in the two ways the issue settled on:
report the drift, and adopt back what STATE can model.  The tests here
pin the parts that would be wrong in a way nobody would notice:

* **the projection from keyfile to STATE keys** — a wrong mapping adopts
  a value into the wrong field, and the next boot renders something the
  operator never typed;
* **which keyfile is managed** — DHCP with no pinned interface owns
  nothing, and claiming otherwise would warn on the one install shape
  that does not have the problem;
* **the unadoptable list** — a setting STATE cannot store reverts
  whatever the operator does, and silently adopting "4 of 5" while a
  static route stays revertible is worse than adopting nothing;
* **the MTU**, which #1017 moved from that list into STATE as
  ``network_mtu``, and which is the one adoptable key whose ABSENCE
  carries meaning — NetworkManager omits a property at its default, so
  clearing an MTU in nmtui and never having had one arrive here
  identically;
* **the write** — etc-render's reader takes the FIRST match for a key, so
  appending a duplicate would look like a successful adopt and change
  nothing at all.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_network_adopt.py -v

No Docker, no NetworkManager, no root required.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import sys

import pytest

from _installer_source import BIN  # noqa: E402 - sibling module

TOOL = BIN / "spatium-network-adopt"
CONSOLE = BIN / "spatium-console"
ETC_RENDER = BIN / "spatium-etc-render"


def _load():
    """Import the extensionless script as a module.

    ``spec_from_file_location`` alone returns a spec with no loader for a
    file without a recognised suffix, so the loader is named explicitly.
    """
    loader = importlib.machinery.SourceFileLoader("spatium_network_adopt", str(TOOL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


sna = _load()


STATIC_KEYFILE = """[connection]
id=spatium-static
uuid=5a71c0de-0000-4000-8000-000000000001
type=ethernet
interface-name=eth0
autoconnect=true
autoconnect-priority=100

[ethernet]

[ipv4]
method=manual
address1=10.0.0.9/24,10.0.0.1
dns=10.0.0.1;9.9.9.9;

[ipv6]
method=auto
"""


@pytest.fixture
def config(tmp_path) -> pathlib.Path:
    path = tmp_path / "spatium-config.yaml"
    path.write_text(
        "hostname: ddi1\n"
        "network_mode: static\n"
        "network_interface: eth0\n"
        "network_ip: 10.0.0.5\n"
        "network_prefix: 24\n"
        "network_gateway: 10.0.0.1\n"
        "network_dns: 10.0.0.1 1.1.1.1\n"
        "admin_user: ops\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def keyfile(tmp_path) -> pathlib.Path:
    path = tmp_path / "10-spatium-static.nmconnection"
    path.write_text(STATIC_KEYFILE, encoding="utf-8")
    return path


# ── keyfile → STATE projection ────────────────────────────────────────


def test_a_static_keyfile_projects_onto_the_state_keys(keyfile):
    live = sna._state_from_keyfile(sna.parse_keyfile(keyfile))
    assert live["network_mode"] == "static"
    assert live["network_interface"] == "eth0"
    assert live["network_ip"] == "10.0.0.9"
    assert live["network_prefix"] == "24"
    assert live["network_gateway"] == "10.0.0.1"
    # NM stores DNS semicolon-separated with a trailing ';'; STATE stores
    # it space-separated, which is the form etc-render translates back.
    assert live["network_dns"] == "10.0.0.1 9.9.9.9"
    assert live["network6_mode"] == "auto"


def test_dns_separator_differences_alone_are_not_drift(config, keyfile):
    """The two sides genuinely store DNS differently. If the projection
    did not normalise, every single check would report drift on a box
    nobody had touched — and an alarm that always fires is one operators
    learn to dismiss.
    """
    keyfile.write_text(
        STATIC_KEYFILE.replace("address1=10.0.0.9/24,10.0.0.1", "address1=10.0.0.5/24,10.0.0.1")
        .replace("dns=10.0.0.1;9.9.9.9;", "dns=10.0.0.1;1.1.1.1;"),
        encoding="utf-8",
    )
    state = sna.read_state(config)
    live = sna._state_from_keyfile(sna.parse_keyfile(keyfile))
    assert sna.drift_for(state, live) == {}


def test_an_unset_ipv6_mode_is_not_drift_against_a_live_auto(config, keyfile):
    """``_render_ipv6_block`` emits ``method=auto`` for anything that is
    not literally ``static``, so an absent ``network6_mode`` and a live
    ``auto`` are the same configuration.

    Without this, every appliance that never set an IPv6 mode — most of
    them — would report drift the moment anyone opened F4, and the
    console would offer to adopt a change nobody made. An alarm that
    always fires is one operators learn to dismiss.
    """
    state = sna.read_state(config)
    assert "network6_mode" not in state
    live = sna._state_from_keyfile(sna.parse_keyfile(keyfile))
    assert live["network6_mode"] == "auto"
    assert "network6_mode" not in sna.drift_for(state, live)


def test_a_real_ipv6_change_is_still_drift(config, keyfile):
    # The negative control for the default above: suppressing the noise
    # must not suppress the signal.
    keyfile.write_text(
        STATIC_KEYFILE.replace(
            "[ipv6]\nmethod=auto", "[ipv6]\nmethod=manual\naddress1=2001:db8::5/64,fe80::1"
        ),
        encoding="utf-8",
    )
    live = sna._state_from_keyfile(sna.parse_keyfile(keyfile))
    drift = sna.drift_for(sna.read_state(config), live)
    assert drift["network6_mode"]["live"] == "static"
    assert drift["network6_ip"]["live"] == "2001:db8::5"


def test_a_dhcp_keyfile_projects_to_dhcp_mode(tmp_path):
    path = tmp_path / "10-spatium-dhcp.nmconnection"
    path.write_text(
        "[connection]\ninterface-name=eno1\n\n[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n",
        encoding="utf-8",
    )
    live = sna._state_from_keyfile(sna.parse_keyfile(path))
    assert live["network_mode"] == "dhcp"
    assert live["network_interface"] == "eno1"
    assert "network_ip" not in live


def test_a_static_ipv6_block_projects_too(tmp_path):
    path = tmp_path / "kf.nmconnection"
    path.write_text(
        "[connection]\ninterface-name=eth0\n\n[ipv4]\nmethod=auto\n\n"
        "[ipv6]\nmethod=manual\naddress1=2001:db8::5/64,fe80::1\n",
        encoding="utf-8",
    )
    live = sna._state_from_keyfile(sna.parse_keyfile(path))
    assert live["network6_mode"] == "static"
    assert live["network6_ip"] == "2001:db8::5"
    assert live["network6_prefix"] == "64"
    # A link-local gateway is the commonest correct answer on IPv6 and
    # must survive the round trip (#995 Phase 3 made the installer accept
    # one; adopting must not throw it away).
    assert live["network6_gateway"] == "fe80::1"


def test_an_address_with_no_gateway_yields_an_empty_gateway(tmp_path):
    path = tmp_path / "kf.nmconnection"
    path.write_text(
        "[connection]\ninterface-name=eth0\n\n[ipv4]\nmethod=manual\naddress1=10.0.0.9/24\n",
        encoding="utf-8",
    )
    live = sna._state_from_keyfile(sna.parse_keyfile(path))
    assert live["network_ip"] == "10.0.0.9"
    assert live["network_gateway"] == ""


def test_the_parser_survives_values_containing_equals_and_semicolons(tmp_path):
    """configparser would be the obvious choice and is the wrong one:
    NM values routinely carry ``;`` and ``=``, and its interpolation
    chokes on a ``%``.
    """
    path = tmp_path / "kf.nmconnection"
    path.write_text(
        "[connection]\nid=my=weird;name\ninterface-name=eth0\n\n"
        "[ipv4]\nmethod=manual\naddress1=10.0.0.9/24,10.0.0.1\ndns=1.1.1.1;8.8.8.8;\n",
        encoding="utf-8",
    )
    parsed = sna.parse_keyfile(path)
    assert parsed["connection"]["id"] == "my=weird;name"
    assert parsed["ipv4"]["dns"] == "1.1.1.1;8.8.8.8;"


# ── which profile is managed ──────────────────────────────────────────


def test_static_mode_manages_the_static_keyfile():
    assert sna.managed_keyfile({"network_mode": "static"}) == sna.STATIC_KEYFILE


def test_pinned_dhcp_manages_the_dhcp_keyfile():
    got = sna.managed_keyfile({"network_mode": "dhcp", "network_interface": "eno1"})
    assert got == sna.DHCP_KEYFILE


def test_unpinned_dhcp_manages_nothing():
    """The one install shape #1016 does NOT bite: with no pinned
    interface etc-render writes no keyfile, NM's own auto profile carries
    the interface, and an nmtui edit to that survives. Warning here would
    be false.
    """
    assert sna.managed_keyfile({"network_mode": "dhcp"}) is None
    assert sna.managed_keyfile({}) is None


# ── unadoptable settings ──────────────────────────────────────────────


def test_an_mtu_is_adoptable_since_1017(keyfile):
    """#1016 shipped with the MTU as its headline UNadoptable setting —
    the nastiest one, because pings and small requests keep working while
    large TCP hangs, so it does not even read as "my network config
    vanished".

    #1017 gave STATE a ``network_mtu`` field and etc-render an
    ``[ethernet] mtu=`` line, so it is now adoptable. This test is the
    inverse of the one it replaces, and it is here rather than deleted
    because the regression it guards is the MTU falling between the two
    lists: filtered out of ``unadoptable`` by ``_MANAGED_KEYS`` while
    nothing projects it into STATE, leaving a setting that is neither
    offered for adoption nor named as reverting.
    """
    keyfile.write_text(
        STATIC_KEYFILE.replace("[ethernet]\n", "[ethernet]\nmtu=1400\n"), encoding="utf-8"
    )
    kf = sna.parse_keyfile(keyfile)
    assert "ethernet.mtu" not in sna._unadoptable(kf)
    assert sna._state_from_keyfile(kf)["network_mtu"] == "1400"


def test_clearing_an_mtu_in_nmtui_is_drift_not_silence(keyfile):
    """The asymmetry that makes the MTU unlike every other adoptable key.

    NetworkManager's keyfile writer omits a property sitting at its
    default, and ``mtu``'s default is 0 — so an operator who clears the
    field in nmtui leaves behind exactly what a box that never had one
    leaves: no line at all. Projected only when present, that clearing
    would be invisible to ``drift_for`` (which walks the LIVE keys), the
    console would report nothing to adopt, and the next boot would
    restore the old value. That is #1016's own failure, reintroduced
    through the setting whose revert is hardest to spot.
    """
    keyfile.write_text(STATIC_KEYFILE, encoding="utf-8")  # no mtu= line
    live = sna._state_from_keyfile(sna.parse_keyfile(keyfile))
    assert live["network_mtu"] == ""
    drift = sna.drift_for({"network_mtu": "9000"}, live)
    assert drift["network_mtu"]["state"] == "9000"
    assert drift["network_mtu"]["live"] == ""


def test_an_mtu_of_zero_means_default_not_a_value(keyfile):
    """NM accepts an explicit ``mtu=0`` and means "default" by it.
    Adopted literally, STATE would carry a 0 that the renderer's 576
    floor then drops on every boot — a value the operator can see in
    the config and can never make take effect.
    """
    keyfile.write_text(
        STATIC_KEYFILE.replace("[ethernet]\n", "[ethernet]\nmtu=0\n"), encoding="utf-8"
    )
    live = sna._state_from_keyfile(sna.parse_keyfile(keyfile))
    assert live["network_mtu"] == ""


def test_an_mtu_the_renderer_refused_is_not_adopted_away(tmp_path):
    """#1017 — the one case where a missing ``mtu=`` is OUR doing.

    etc-render refuses a value it cannot apply (out of range, or below
    1280 with a static IPv6 address) and writes no ``mtu=`` line. To this
    tool that is identical to the operator clearing the field, so the
    drift reads ``state=1200 live=""`` — and adopting it would overwrite
    their configured value with an empty one, from a prompt that said
    "save your nmtui changes". They would lose the setting AND the record
    of what it was.

    Reachable with no hand-editing: set 1200 while IPv6 is on its
    RA/SLAAC default (allowed — nothing to break), later adopt a static
    IPv6 address from nmtui, and the next boot starts refusing the MTU.
    """
    status = tmp_path / "network-status"
    status.write_text(
        "INTERFACE=eth0\nMODE=static\nMTU=\nMTU_REQUESTED=1200\nMTU_APPLIED=dropped\n",
        encoding="utf-8",
    )
    note = sna._mtu_suppression(status)
    assert note is not None
    assert "1200" in note and "REFUSED" in note


def test_an_mtu_with_no_profile_to_live_in_is_not_adopted_away(tmp_path):
    status = tmp_path / "network-status"
    status.write_text(
        "INTERFACE=\nMODE=dhcp\nMTU=\nMTU_REQUESTED=9000\nMTU_APPLIED=n/a\n",
        encoding="utf-8",
    )
    note = sna._mtu_suppression(status)
    assert note is not None
    assert "9000" in note


@pytest.mark.parametrize("applied", ["applied", "default"])
def test_a_normally_rendered_mtu_is_still_adoptable(tmp_path, applied):
    """The complement, and the one that keeps the suppression honest: if
    the renderer applied what it was given, a missing ``mtu=`` really IS
    the operator clearing it, and clearing must stay adoptable.
    """
    status = tmp_path / "network-status"
    status.write_text(
        f"INTERFACE=eth0\nMODE=static\nMTU=1400\nMTU_REQUESTED=1400\nMTU_APPLIED={applied}\n",
        encoding="utf-8",
    )
    assert sna._mtu_suppression(status) is None


def test_compare_marks_a_refused_mtu_unadoptable(keyfile, monkeypatch, tmp_path):
    """Through ``compare()``, not the helper.

    The first cut tested ``_mtu_suppression`` directly, so the wiring
    inside ``compare()`` was never executed: deleting it entirely left
    all the tests green while ``--adopt`` silently overwrote the
    operator's configured MTU with an empty one — the exact scenario the
    wiring exists to stop. This is the same argument the module makes for
    extracting ``drift_for``.
    """
    status = tmp_path / "network-status"
    status.write_text(
        "INTERFACE=eth0\nMODE=static\nMTU=\nMTU_REQUESTED=1200\nMTU_APPLIED=dropped\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sna, "NETWORK_STATUS", status)
    monkeypatch.setattr(sna, "STATIC_KEYFILE", keyfile)
    keyfile.write_text(STATIC_KEYFILE, encoding="utf-8")  # no mtu= line

    report = sna.compare(
        {"network_mode": "static", "network_interface": "eth0", "network_mtu": "1200"}
    )
    entry = report["drift"]["network_mtu"]
    # Still REPORTED as drift — it genuinely is one, and the exit code
    # contract says 10 — but explicitly not adoptable, with the reason.
    assert entry["adoptable"] is False
    assert "REFUSED" in entry["reason"]


def test_compare_refuses_to_adopt_an_mtu_the_renderer_would_drop(
    keyfile, config, monkeypatch, tmp_path
):
    """The other direction: nmtui accepts an MTU the renderer will not.

    500 is legal to NetworkManager (the Ethernet minimum is 68) and below
    SpatiumDDI's 576 floor. Without this, ``--adopt`` was a fourth door
    with no rule: the console printed "Adopted 1 setting(s) into STATE:
    network_mtu = 500" and the next boot dropped it.
    """
    status = tmp_path / "network-status"
    status.write_text("MTU=1400\nMTU_REQUESTED=1400\nMTU_APPLIED=applied\n", encoding="utf-8")
    monkeypatch.setattr(sna, "NETWORK_STATUS", status)
    monkeypatch.setattr(sna, "STATIC_KEYFILE", keyfile)
    keyfile.write_text(
        STATIC_KEYFILE.replace("[ethernet]\n", "[ethernet]\nmtu=500\n"), encoding="utf-8"
    )

    entry = sna.compare(
        {"network_mode": "static", "network_interface": "eth0"}
    )["drift"]["network_mtu"]
    assert entry["live"] == "500"
    assert entry["adoptable"] is False
    assert "576" in entry["reason"]


def test_an_ordinary_mtu_edit_stays_adoptable(keyfile, monkeypatch, tmp_path):
    """The complement, and what keeps the two refusals honest."""
    status = tmp_path / "network-status"
    status.write_text("MTU=1400\nMTU_REQUESTED=1400\nMTU_APPLIED=applied\n", encoding="utf-8")
    monkeypatch.setattr(sna, "NETWORK_STATUS", status)
    monkeypatch.setattr(sna, "STATIC_KEYFILE", keyfile)
    keyfile.write_text(
        STATIC_KEYFILE.replace("[ethernet]\n", "[ethernet]\nmtu=9000\n"), encoding="utf-8"
    )

    entry = sna.compare(
        {"network_mode": "static", "network_interface": "eth0"}
    )["drift"]["network_mtu"]
    assert entry["live"] == "9000"
    assert entry["adoptable"] is True
    assert entry["reason"] == ""


def test_no_sidecar_suppresses_nothing(tmp_path):
    """An older slot, or a non-appliance host. Nothing is known about why
    the line is missing, so nothing is suppressed — which is the
    pre-#1017 behaviour.
    """
    assert sna._mtu_suppression(tmp_path / "absent") is None


def test_an_unchanged_appliance_reports_no_mtu_drift(keyfile):
    """The complement of the two above, and the one that keeps them
    honest: neither STATE nor the keyfile carrying an MTU is the normal
    case on every appliance installed to date, and it must produce no
    drift at all. An alarm that always fires is one operators learn to
    dismiss.
    """
    keyfile.write_text(STATIC_KEYFILE, encoding="utf-8")
    live = sna._state_from_keyfile(sna.parse_keyfile(keyfile))
    assert "network_mtu" not in sna.drift_for({"network_mode": "static"}, live)


def test_a_static_route_is_reported_unadoptable(keyfile):
    keyfile.write_text(
        STATIC_KEYFILE.replace(
            "dns=10.0.0.1;9.9.9.9;", "dns=10.0.0.1;9.9.9.9;\nroute1=192.168.9.0/24,10.0.0.254"
        ),
        encoding="utf-8",
    )
    assert "ipv4.route1" in sna._unadoptable(sna.parse_keyfile(keyfile))


def test_everything_etc_render_writes_is_adoptable(keyfile):
    """The complement, and the one that keeps the list honest: a stock
    rendered keyfile must produce an EMPTY unadoptable list, or the
    console would warn about settings on a box nobody has touched.
    """
    assert sna._unadoptable(sna.parse_keyfile(keyfile)) == []


def test_the_managed_key_set_covers_what_etc_render_actually_renders():
    """Cross-check against the renderer rather than against a memory of
    it: a key added to etc-render's heredocs but not to ``_MANAGED_KEYS``
    would show up as spurious 'unadoptable' on every appliance.
    """
    import re

    render = ETC_RENDER.read_text(encoding="utf-8")
    # Only the heredoc bodies — the surrounding shell is full of
    # ``x=$(...)`` assignments, and a naive scan for "=" picks those up
    # (the first cut of this test reported ``[ -n "$existing_uuid" ] &&
    # net_uuid`` as a rendered key).
    bodies = re.findall(r"cat > \"\$NM_[A-Z_]*KEYFILE\" <<EOF\n(.*?)\nEOF", render, re.S)
    assert bodies, "no keyfile heredocs found — did etc-render change shape?"
    rendered_keys = set()
    for body in bodies:
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("[") or "=" not in line:
                continue
            if line.startswith("$("):
                continue  # a shell substitution emitting its own lines
            # #1017 — a conditional expansion, ``${VAR:+key=value}``, is
            # how an OPTIONAL key is rendered: present when the variable
            # is set, gone entirely when it is not. Resolved to its key
            # rather than skipped, so the guard actually covers those
            # lines. Skipping them would have been the easy fix and
            # would have quietly exempted every optional key anyone adds
            # from here on — which is the class of key most likely to be
            # forgotten in ``_MANAGED_KEYS``.
            m = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z_0-9]*:\+(.*)\}", line)
            if m:
                line = m.group(1)
                if "=" not in line:
                    continue
            rendered_keys.add(line.split("=", 1)[0])
    managed_names = {k for _, k in sna._MANAGED_KEYS}
    missing = rendered_keys - managed_names
    assert not missing, f"etc-render renders keys _MANAGED_KEYS does not know: {missing}"
    # And the interesting keys really were found, or an empty set would
    # pass this test forever. ``mtu`` is in the list because it is
    # rendered through the conditional form above — if the resolution
    # breaks, this catches it rather than letting the key vanish.
    assert {"interface-name", "method", "address1", "mtu"} <= rendered_keys


# ── writing back into STATE ───────────────────────────────────────────


def test_adopting_rewrites_in_place_without_duplicating(config):
    """etc-render's reader takes the FIRST match for a key, so appending
    a second copy would be read as a no-op — the adopt would report
    success and change nothing.
    """
    changed = sna.write_state({"network_ip": "10.0.0.9"}, config)
    assert changed == ["network_ip"]
    body = config.read_text(encoding="utf-8")
    assert body.count("network_ip:") == 1
    assert sna.read_state(config)["network_ip"] == "10.0.0.9"


def test_adopting_preserves_every_other_line(config):
    sna.write_state({"network_ip": "10.0.0.9", "network_dns": "9.9.9.9"}, config)
    state = sna.read_state(config)
    assert state["hostname"] == "ddi1"
    assert state["admin_user"] == "ops"
    assert state["network_gateway"] == "10.0.0.1"


def test_a_key_not_yet_present_is_appended(config):
    sna.write_state({"network6_ip": "2001:db8::5"}, config)
    assert sna.read_state(config)["network6_ip"] == "2001:db8::5"


def test_a_value_needing_quotes_round_trips(config):
    """The flat reader strips one layer of quotes, so a value containing
    a colon must go back in quoted or the next read truncates it.
    """
    sna.write_state({"network6_gateway": "fe80::1"}, config)
    assert sna.read_state(config)["network6_gateway"] == "fe80::1"


def test_writing_to_a_missing_config_is_refused(tmp_path):
    """Creating one would produce a STATE file with only network keys,
    losing the hostname and admin account — worse than refusing.
    """
    with pytest.raises(sna.AdoptError):
        sna.write_state({"network_ip": "10.0.0.9"}, tmp_path / "nope.yaml")


def test_the_write_is_atomic(config):
    """STATE survives a factory reset and is not recoverable from
    anywhere else on the box, so a torn write is unrecoverable.
    """
    import inspect

    body = inspect.getsource(sna.write_state)
    assert "os.replace(tmp, path)" in body, "must rename a temp file over the target"
    # The rename alone is only namespace-atomic. Without an fsync of the
    # file AND of its directory, a power loss just after the console
    # prints "Saved to STATE" can leave a zero-length config on the one
    # partition nothing else on the box backs up.
    assert body.count("os.fsync") == 2, "fsync the temp file and its directory"
    # A fresh inode is created at 0666 & ~umask owned by whoever ran the
    # tool, so a root-only STATE config — which carries the admin
    # username, hostname and control-plane URL — would silently become
    # world-readable after one save.
    assert "os.chmod(tmp" in body and "os.chown(tmp" in body


# ── the console side ──────────────────────────────────────────────────


def test_the_console_warns_before_launching_nmtui():
    """Option (1) from the issue: the cheapest honest fix, and the one
    that covers the settings adopt-back cannot take.
    """
    body = CONSOLE.read_text(encoding="utf-8")
    warn = body.index("def _network_managed_warning")
    do_network = body.index("def do_network")
    call = body.index("_network_managed_warning(console, tty)", do_network)
    nmtui = body.index('["nmtui"]', do_network)
    assert warn < do_network
    assert call < nmtui, "the warning must come BEFORE nmtui, not after"


def test_the_console_offers_adopt_after_nmtui_exits():
    body = CONSOLE.read_text(encoding="utf-8")
    do_network = body.index("def do_network")
    nmtui = body.index('["nmtui"]', do_network)
    offer = body.index("_offer_network_adopt(console, tty)", do_network)
    assert nmtui < offer


def test_the_console_does_not_offer_adopt_when_nmtui_never_ran():
    """The ``OSError`` path returns before the offer — otherwise a box
    that could not open its own TTY would be asked to adopt drift that
    nothing produced.
    """
    body = CONSOLE.read_text(encoding="utf-8")
    do_network = body[body.index("def do_network") :]
    do_network = do_network[: do_network.index("\ndef _fetch_warning_events")]
    failed = do_network.index("Failed to open")
    offer = do_network.index("_offer_network_adopt")
    assert "return" in do_network[failed:offer]


def test_the_tool_is_made_executable_at_install_time():
    """The recurring appliance gap: a host tool that is shipped but never
    chmod'd is silently inert — the console's ``--check`` would fail to
    exec, the report would come back None, and the warning would never
    appear.
    """
    postinst = (pathlib.Path(__file__).resolve().parents[1] / "mkosi.postinst").read_text()
    assert 'chmod 0755 "$BUILDROOT/usr/local/bin/spatium-network-adopt"' in postinst
