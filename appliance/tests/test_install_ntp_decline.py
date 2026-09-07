"""Blanking the installer's Time source means no time source (#1002).

Removing the installer's own ``sources.d`` file never stopped chrony:
Debian's ``/etc/chrony/chrony.conf`` carries its own ``pool`` directive and
``sourcedir`` is additive, so an appliance whose operator declined a time
source kept synchronising against the public Debian pool — while the Confirm
screen, the field menu, ``docs/PRIVACY.md`` and the cloud-init README all said
"none".  ``PRIVACY.md`` is normative for non-negotiable #17, so that was a
false claim about an outbound connection.

Two mechanisms, and both are needed, because they cover different windows:

* the installer comments Debian's source lines out of ``chrony.conf`` — this
  covers the first boot, before the control plane has pushed anything;
* firstboot forwards an explicit "declined" sentinel so
  ``platform_settings.ntp_pool_servers`` seeds EMPTY — this covers everything
  afterwards, because the #154 runner replaces ``chrony.conf`` wholesale and
  would otherwise put the pool straight back.

The Python half lives in ``backend/tests/test_ntp_initial_seed.py``, including
the cross-boundary check that the two spellings of the sentinel agree.

Asserted on the RENDERED config rather than on the installer's log line, which
is what #1002 asked for: every one of the four wrong surfaces was prose.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_install_ntp_decline.py -v
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).parent.parent / "mkosi.extra" / "usr" / "local" / "bin"
INSTALL = BIN / "spatium-install"
FIRSTBOOT = BIN / "spatiumddi-firstboot"

# Debian stable's stock /etc/chrony/chrony.conf, verified against
# chrony 4.6.1 in debian:stable-slim while #1002 was written.
STOCK_CHRONY_CONF = """\
# Welcome to the chrony configuration file.
# See chrony.conf(5) for more information about usable directives.

# Include configuration files found in /etc/chrony/conf.d.
confdir /etc/chrony/conf.d

# Use Debian vendor zone.
pool 2.debian.pool.ntp.org iburst

# Use time sources from DHCP.
sourcedir /run/chrony-dhcp

# Use NTP sources found in /etc/chrony/sources.d.
sourcedir /etc/chrony/sources.d

# This directive specify the location of the file containing ID/key pairs.
keyfile /etc/chrony/chrony.keys

driftfile /var/lib/chrony/chrony.drift
ntsdumpdir /var/lib/chrony
logdir /var/log/chrony
maxupdateskew 100.0
rtcsync
makestep 1 3
leapseclist /usr/share/zoneinfo/leap-seconds.list
"""


def _has_gnu_sed() -> bool:
    """The appliance is Debian, so the shipped ``sed -i -E`` is GNU sed.

    BSD sed (macOS) takes ``-i``'s backup suffix as the next argument, so it
    swallows the ``-E`` — the alternation becomes literal and the substitution
    errors out rather than doing something subtly different. Testing under it
    would measure the wrong program, so these skip instead. CI runs
    ubuntu-latest, where they always execute; a developer on a mac gets an
    explicit skip line rather than a false green.
    """
    sed = shutil.which("sed")
    if not sed:
        return False
    out = subprocess.run([sed, "--version"], capture_output=True, text=True)
    return out.returncode == 0 and "GNU sed" in out.stdout


requires_gnu_sed = pytest.mark.skipif(
    not _has_gnu_sed(),
    reason="needs GNU sed (the appliance is Debian); BSD sed misparses `sed -i -E`",
)


def _extract_function(script: Path, name: str) -> str:
    src = script.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", src, re.S | re.MULTILINE)
    assert m, f"{script.name} no longer defines {name}()"
    return m.group(0)


def _suppress(conf_text: str) -> tuple[str, str]:
    """Run the installer's own ``_suppress_default_ntp_sources`` on a file.

    Extracted from the shipped script and executed under bash, so this tests
    the bytes that ship — including the ``NTP_DECLINED_MARK`` literal, which a
    transcription would silently get right forever.
    """
    src = INSTALL.read_text(encoding="utf-8")
    mark = re.search(r'^NTP_DECLINED_MARK=.*$', src, re.MULTILINE)
    assert mark, "spatium-install no longer defines NTP_DECLINED_MARK"

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        conf = Path(td) / "chrony.conf"
        conf.write_text(conf_text, encoding="utf-8")
        script = f"""
            set -euo pipefail
            log() {{ printf '%s\\n' "$*" >&2; }}
            {mark.group(0)}
            {_extract_function(INSTALL, "_suppress_default_ntp_sources")}
            _suppress_default_ntp_sources "{conf}"
        """
        res = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True
        )
        assert res.returncode == 0, res.stderr
        return conf.read_text(encoding="utf-8"), res.stderr


def _active(conf_text: str) -> list[str]:
    """Directives chrony will actually act on."""
    return [
        ln.strip()
        for ln in conf_text.splitlines()
        if ln.strip() and not ln.strip().startswith(("#", "!", ";"))
    ]


# --------------------------------------------------------------------------
# 1. the installer's on-target chrony.conf
# --------------------------------------------------------------------------
@requires_gnu_sed
def test_the_debian_pool_is_suppressed() -> None:
    """The whole bug: this line is why "none" was false."""
    out, _ = _suppress(STOCK_CHRONY_CONF)
    assert not [d for d in _active(out) if d.startswith("pool ")], _active(out)


@requires_gnu_sed
def test_the_dhcp_sourcedir_is_suppressed() -> None:
    """Deliberate, not collateral.

    The prompt PRE-FILLS the field with the servers the DHCP lease offered and
    shows them as a hint, so clearing it rejects exactly those. Leaving
    ``/run/chrony-dhcp`` live would also make "none" false again the moment
    NetworkManager renewed a lease carrying option 42.
    """
    out, _ = _suppress(STOCK_CHRONY_CONF)
    assert "sourcedir /run/chrony-dhcp" not in _active(out)


@requires_gnu_sed
def test_no_source_directive_of_any_kind_survives() -> None:
    out, _ = _suppress(STOCK_CHRONY_CONF)
    assert not [
        d for d in _active(out) if d.split()[0] in ("pool", "server", "peer")
    ], _active(out)


@requires_gnu_sed
def test_the_way_back_is_left_in_place() -> None:
    """``sources.d`` and ``conf.d`` survive, so an operator can change their
    mind without hand-editing a dpkg conffile."""
    out, _ = _suppress(STOCK_CHRONY_CONF)
    active = _active(out)
    assert "sourcedir /etc/chrony/sources.d" in active
    assert "confdir /etc/chrony/conf.d" in active


@requires_gnu_sed
def test_everything_that_is_not_a_source_is_untouched() -> None:
    out, _ = _suppress(STOCK_CHRONY_CONF)
    for directive in (
        "keyfile /etc/chrony/chrony.keys",
        "driftfile /var/lib/chrony/chrony.drift",
        "ntsdumpdir /var/lib/chrony",
        "rtcsync",
        "makestep 1 3",
        "leapseclist /usr/share/zoneinfo/leap-seconds.list",
    ):
        assert directive in _active(out), directive


@requires_gnu_sed
def test_the_edit_is_marked_and_exactly_reversible() -> None:
    """A dpkg conffile edit has to be legible and undoable.

    Stripping the marker prefix must restore the original file byte for byte —
    that is what makes this a comment rather than a deletion.
    """
    out, _ = _suppress(STOCK_CHRONY_CONF)
    src = INSTALL.read_text(encoding="utf-8")
    mark = re.search(r'^NTP_DECLINED_MARK="(.*)"$', src, re.MULTILINE)
    assert mark, "NTP_DECLINED_MARK is no longer a simple double-quoted literal"
    prefix = mark.group(1)
    assert prefix in out, "nothing was marked"
    restored = "\n".join(
        ln[len(prefix):] if ln.startswith(prefix) else ln for ln in out.splitlines()
    ) + "\n"
    assert restored == STOCK_CHRONY_CONF


@requires_gnu_sed
def test_a_conf_with_no_sources_is_left_alone_and_reported() -> None:
    """Idempotent: running twice must not double-mark."""
    once, _ = _suppress(STOCK_CHRONY_CONF)
    twice, _ = _suppress(once)
    assert twice == once


def test_a_missing_conf_is_survivable() -> None:
    """A clock that keeps syncing is not a broken install, so this warns
    rather than aborting a wipe-and-rsync that has already happened."""
    script = f"""
        set -euo pipefail
        log() {{ printf '%s\\n' "$*" >&2; }}
        NTP_DECLINED_MARK="# x "
        {_extract_function(INSTALL, "_suppress_default_ntp_sources")}
        _suppress_default_ntp_sources "/nonexistent/chrony.conf"
    """
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert res.returncode == 0
    assert "nothing to suppress" in res.stderr


@requires_gnu_sed
def test_a_surviving_source_line_is_reported_loudly() -> None:
    """The failure this is most prone to is a silent one.

    Every surface now says "none", so a suppression that quietly matched
    nothing would leave the operator with a false statement and no signal.
    """
    weird = "pool\tfoo.example iburst\n"  # tab-separated: still a source line
    out, err = _suppress(weird)
    if [d for d in _active(out) if d.split()[0] == "pool"]:
        assert "WARN" in err, err
    else:
        assert "suppressed" in err, err


# --------------------------------------------------------------------------
# 2. the installer's other surfaces
# --------------------------------------------------------------------------
def test_the_installer_no_longer_claims_the_debian_pool_applies() -> None:
    """The four surfaces #1000 corrected were corrected to the OLD behaviour.

    Now that blank really means none, a surface still saying the pool applies
    is wrong again — in the other direction.
    """
    src = INSTALL.read_text(encoding="utf-8")
    for stale in (
        "Debian's default pool still applies",
        "Debian default pool",
        "none given",
    ):
        assert stale not in src, f"stale #1000-era wording still present: {stale!r}"


def test_the_decline_path_calls_the_suppression() -> None:
    """Computed but never invoked is the failure mode this file exists for."""
    src = INSTALL.read_text(encoding="utf-8")
    assert '_suppress_default_ntp_sources "$MOUNT/etc/chrony/chrony.conf"' in src


# --------------------------------------------------------------------------
# 3. firstboot's explicit-decline sentinel
# --------------------------------------------------------------------------
def _seed(state_config: str | None) -> str:
    """Run firstboot's INITIAL_NTP_SERVERS seed block against a STATE config.

    ``None`` means the file does not exist — the ``nofail`` mount race, which
    must stay a retry rather than becoming an answer.
    """
    src = FIRSTBOOT.read_text(encoding="utf-8")
    block = re.search(
        r'^NTP_EXPLICITLY_NONE=.*?^fi$', src, re.S | re.MULTILINE
    )
    assert block, "firstboot no longer has the INITIAL_NTP_SERVERS seed block"

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "spatium-config.yaml"
        if state_config is not None:
            cfg.write_text(state_config, encoding="utf-8")
        env = Path(td) / "env"
        env.write_text("", encoding="utf-8")
        script = f"""
            set -euo pipefail
            STATE_CONFIG="{cfg}"
            ENV_FILE="{env}"
            {_extract_function(FIRSTBOOT, "_state_cfg")}
            {_extract_function(FIRSTBOOT, "_state_cfg_has")}
            INITIAL_NTP_SERVERS_VAL=""
            {block.group(0)}
            printf '%s' "$INITIAL_NTP_SERVERS_VAL"
        """
        res = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        return res.stdout


_SENTINEL = "!none"


def test_a_typed_answer_is_forwarded_verbatim() -> None:
    assert _seed('ntp_servers: "ntp1.corp ntp2.corp"\n') == "ntp1.corp ntp2.corp"


def test_an_empty_answer_becomes_the_explicit_sentinel() -> None:
    """Present and empty is the operator declining — an answer, not a gap."""
    assert _seed('hostname: "ddi1"\nntp_servers: ""\n') == _SENTINEL


@pytest.mark.parametrize(
    "cfg", [None, 'hostname: "ddi1"\n'], ids=["no-state-file", "pre-995-config"]
)
def test_an_absent_key_stays_a_retry(cfg: str | None) -> None:
    """STATE is mounted ``nofail``, so an unmounted volume on one boot is
    routine. Recording "declined" for it would lose what the operator typed
    and hand them the public pool, permanently — the bug from the other side.
    """
    assert _seed(cfg) == ""
