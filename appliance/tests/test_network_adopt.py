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
  whatever the operator does, and silently adopting "4 of 5" while the
  MTU stays revertible is worse than adopting nothing;
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


def test_settings_state_cannot_store_are_reported(keyfile):
    """An MTU is the issue's own example, and the nastiest: pings and
    small requests keep working while large TCP hangs, so it does not
    even read as "my network config vanished".
    """
    keyfile.write_text(
        STATIC_KEYFILE.replace("[ethernet]\n", "[ethernet]\nmtu=9000\n"), encoding="utf-8"
    )
    unadoptable = sna._unadoptable(sna.parse_keyfile(keyfile))
    assert "ethernet.mtu" in unadoptable


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
            rendered_keys.add(line.split("=", 1)[0])
    managed_names = {k for _, k in sna._MANAGED_KEYS}
    missing = rendered_keys - managed_names
    assert not missing, f"etc-render renders keys _MANAGED_KEYS does not know: {missing}"
    # And the interesting keys really were found, or an empty set would
    # pass this test forever.
    assert {"interface-name", "method", "address1"} <= rendered_keys


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
