"""The installer's Time source answer survives to the platform row (#1003 item 2).

The wizard asked, the installer wrote a chrony sources file, and the #154
control-plane plane deleted it three minutes into the first boot and pushed
``pool.ntp.org`` — because nothing carried the answer to
``platform_settings.ntp_pool_servers``. Only visible when the answer differed
from the default, which on the reporting VM it did not.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# The dev container copies only ``backend/`` into the image, so the
# cross-boundary check below skips there and runs for real in CI, which tests
# from a full checkout. Same convention as test_spatium_console.py.
_FIRSTBOOT = (
    Path(__file__).resolve().parents[2]
    / "appliance"
    / "mkosi.extra"
    / "usr"
    / "local"
    / "bin"
    / "spatiumddi-firstboot"
)


def _default(monkeypatch, value: str) -> list[str]:
    from app import config as cfg

    monkeypatch.setattr(cfg.settings, "initial_ntp_servers", value, raising=False)
    mod = importlib.import_module("app.models.settings")
    return mod._initial_ntp_servers()


def test_no_installer_answer_keeps_the_pool(monkeypatch):
    assert _default(monkeypatch, "") == ["pool.ntp.org"]


def test_single_server_is_seeded(monkeypatch):
    assert _default(monkeypatch, "ntp.corp.internal") == ["ntp.corp.internal"]


def test_several_servers_split_on_whitespace(monkeypatch):
    """The prompt collects them space-separated and STATE stores that verbatim."""
    assert _default(monkeypatch, "ntp1.corp ntp2.corp") == ["ntp1.corp", "ntp2.corp"]


def test_an_absent_answer_still_keeps_the_pool(monkeypatch):
    """Empty means "nobody was asked", and must keep the platform default.

    Three real situations produce it and none of them is an answer: an
    installer that predates the Time source screen (#995 item 16), a fully
    unattended run, and a STATE volume that was not mounted when firstboot
    read it (mounted ``nofail``, so this is a routine race).
    """
    assert _default(monkeypatch, "   ") == ["pool.ntp.org"]


def test_an_explicit_decline_seeds_an_empty_list(monkeypatch):
    """#1002 — the operator cleared the field, so there is no time source.

    This is the half that makes the installer's own suppression stick: it
    comments Debian's ``pool`` line out of ``chrony.conf``, and then the #154
    plane replaces that file wholesale from these settings a few minutes into
    the first boot. Falling back to the pool here would put the public pool
    back, which is the bug seen from the other side.
    """
    from app.models.settings import NTP_EXPLICITLY_NONE

    assert _default(monkeypatch, NTP_EXPLICITLY_NONE) == []


def test_the_sentinel_is_not_a_legal_server_name():
    """It has to be unspellable, or an operator could type it by accident.

    The installer and the preseed linter both accept ``[A-Za-z0-9._:-]`` in a
    server name; anything outside that set can never arrive from a real
    answer. A sentinel like ``none`` would be a perfectly good hostname.
    """
    import re

    from app.models.settings import NTP_EXPLICITLY_NONE

    assert re.fullmatch(r"[A-Za-z0-9._:-]+", NTP_EXPLICITLY_NONE) is None


def test_an_empty_list_renders_a_chrony_conf_with_no_sources():
    """The end state the decline is asking for, asserted on the RENDERED body.

    #1002 asked for a test on the rendered config rather than on a log line,
    because every one of the four surfaces that was wrong about this was
    prose. Note there is no ``sourcedir`` either: the control plane's
    chrony.conf does not inherit Debian's, so the DHCP-supplied servers the
    installer suppressed do not come back through it.
    """
    from app.models.settings import PlatformSettings
    from app.services.appliance.ntp import render_chrony_conf

    body = render_chrony_conf(
        PlatformSettings(
            ntp_source_mode="pool",
            ntp_pool_servers=[],
            ntp_custom_servers=[],
            ntp_allow_clients=False,
        )
    )
    directives = [
        ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")
    ]
    assert not [d for d in directives if d.split()[0] in ("pool", "server", "peer")], directives
    assert not [d for d in directives if d.split()[0] in ("sourcedir", "confdir")], directives
    # ...and it is still a valid config, not an empty file.
    assert "driftfile" in body and "makestep" in body


@pytest.mark.skipif(
    not _FIRSTBOOT.exists(),
    reason="spatiumddi-firstboot not present in this checkout",
)
def test_firstboot_and_the_api_agree_on_the_sentinel():
    """Cross-boundary constant — a bash literal and a Python one.

    firstboot writes ``INITIAL_NTP_SERVERS`` and this module reads it, so the
    two spellings are one contract with nothing but this test between them.
    A drifted sentinel does not fail loudly: the api would read an unknown
    string, ``split()`` it into a one-element list, and seed a "server" named
    ``!none`` — which resolves to nothing, so the appliance reports
    unsynchronised and looks exactly like the decline working.
    """
    from app.models.settings import NTP_EXPLICITLY_NONE

    src = _FIRSTBOOT.read_text(encoding="utf-8")
    assert f'NTP_EXPLICITLY_NONE="{NTP_EXPLICITLY_NONE}"' in src, (
        "spatiumddi-firstboot does not spell the sentinel the way "
        f"app.models.settings does ({NTP_EXPLICITLY_NONE!r})"
    )


def test_the_column_uses_the_callable(monkeypatch):
    """Guards the WIRING, not just the helper — a default that is not attached
    to the column is a helper nothing calls.

    Asserted by invoking the column's default rather than by identity:
    SQLAlchemy wraps a zero-argument callable to adapt its signature, so
    ``col.default.arg is _initial_ntp_servers`` is False even when correctly
    wired. Invoking it also proves the wrapper passes through, which identity
    would not.
    """
    from app import config as cfg
    from app.models.settings import PlatformSettings

    monkeypatch.setattr(cfg.settings, "initial_ntp_servers", "ntp.corp.internal")
    col = PlatformSettings.__table__.c.ntp_pool_servers
    assert col.default is not None and col.default.is_callable
    assert col.default.arg(None) == ["ntp.corp.internal"]


def test_server_default_stays_the_plain_pool():
    """A row created outside the ORM cannot know an installer answer.

    Compared exactly rather than with ``"pool.ntp.org" in ...``: a substring
    test would also pass for ``evil-pool.ntp.org.attacker.test``, which is
    why CodeQL flags that shape (py/incomplete-url-substring-sanitization)
    — and here the exact form is simply the better assertion, since a
    changed DDL default is precisely what this guards.
    """
    from app.models.settings import PlatformSettings

    col = PlatformSettings.__table__.c.ntp_pool_servers
    assert str(col.server_default.arg) == "'[\"pool.ntp.org\"]'::jsonb"
