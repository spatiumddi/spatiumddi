"""firstboot carries the installer's Time source to the api (#1003 item 2).

The wizard asks for a time source, ``spatium-install`` writes
``/etc/chrony/sources.d/spatium-install.sources``, and roughly three minutes
into the first boot the #154 control-plane chrony plane DELETES that file and
installs the platform default — because nothing ever told the platform what
the operator typed. On the reporting VM the answer happened to equal the
default, so it was invisible; an air-gapped site that entered
``ntp.corp.internal`` gets a correct clock for three minutes and a chronyd
reaching for ``pool.ntp.org`` forever, with "unsynchronized" as the only
symptom.

The value's other half — turning the env var into the initial
``platform_settings.ntp_pool_servers`` — is tested in
``backend/tests/test_ntp_initial_seed.py``. This file guards the plumbing,
which is where it can silently come apart: a var written to ``.env`` but
never read back, or read back but never put on the api container.

HOW TO RUN (from the repo root):
    python3 -m pytest appliance/tests/test_firstboot_ntp_seed.py -v
"""

from __future__ import annotations

import subprocess

from _installer_source import BIN

FIRSTBOOT = (BIN / "spatiumddi-firstboot").read_text(encoding="utf-8")
INSTALLER = (BIN / "spatium-install").read_text(encoding="utf-8")


def test_installer_still_records_the_answer_on_state() -> None:
    """The whole chain starts here; without it firstboot reads nothing."""
    assert 'ntp_servers: "$NTP_SERVERS"' in INSTALLER


def test_firstboot_reads_it_off_state() -> None:
    assert "_state_cfg()" in FIRSTBOOT
    assert "INITIAL_NTP_SERVERS=$(_state_cfg ntp_servers)" in FIRSTBOOT


def test_firstboot_reads_it_back_on_later_boots() -> None:
    """``.env`` is written once; every later boot re-reads it, like the
    agent keys. Without this the var is empty on boot two onward and the
    api would fall back to the platform default after any restart."""
    assert "INITIAL_NTP_SERVERS_VAL=$(_env_get INITIAL_NTP_SERVERS)" in FIRSTBOOT


def test_the_value_reaches_the_api_container() -> None:
    """Written and read back but never placed on the pod is the failure
    this whole chain is prone to."""
    assert "- name: INITIAL_NTP_SERVERS" in FIRSTBOOT
    assert 'value: "${INITIAL_NTP_SERVERS_VAL}"' in FIRSTBOOT


def test_state_reader_parses_the_flat_config() -> None:
    """Execute it, because the sed quoting is the part that breaks.

    The reader is a transcription of ``spatium-etc-render``'s ``get_cfg``,
    and the single-quote escaping differs between a heredoc and a shell
    function body — a detail no structural check would notice.
    """
    body = FIRSTBOOT[FIRSTBOOT.index("_state_cfg() {") :]
    body = body[: body.index("\n}\n") + 3]
    script = f"""
        STATE_CONFIG=$1
        {body}
        _state_cfg ntp_servers
    """
    import tempfile
    import pathlib

    with tempfile.TemporaryDirectory() as d:
        cfg = pathlib.Path(d) / "spatium-config.yaml"
        cfg.write_text(
            'hostname: "ddi1"\n'
            'ntp_servers: "ntp1.corp.internal ntp2.corp.internal"\n'
            'keymap: "fr"\n'
        )
        out = subprocess.run(
            ["bash", "-c", script, "_", str(cfg)],
            capture_output=True,
            text=True,
            check=False,
        )
    assert out.stdout.strip() == "ntp1.corp.internal ntp2.corp.internal", out


def test_state_reader_is_quiet_when_state_is_absent() -> None:
    """A compose / k8s install has no STATE partition at all."""
    body = FIRSTBOOT[FIRSTBOOT.index("_state_cfg() {") :]
    body = body[: body.index("\n}\n") + 3]
    out = subprocess.run(
        ["bash", "-c", f"STATE_CONFIG=/nonexistent\n{body}\n_state_cfg ntp_servers"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.stdout.strip() == ""
    assert out.returncode == 0
