"""The installer's Time source answer survives to the platform row (#1003 item 2).

The wizard asked, the installer wrote a chrony sources file, and the #154
control-plane plane deleted it three minutes into the first boot and pushed
``pool.ntp.org`` — because nothing carried the answer to
``platform_settings.ntp_pool_servers``. Only visible when the answer differed
from the default, which on the reporting VM it did not.
"""

from __future__ import annotations

import importlib


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


def test_blank_answer_does_not_produce_an_empty_list(monkeypatch):
    """chronyd with zero sources is a clock nothing corrects.

    A blank Time source means "no INSTALLER sources" (#1002), not "no time
    source at all" — so the platform default still applies here.
    """
    assert _default(monkeypatch, "   ") == ["pool.ntp.org"]


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
    """A row created outside the ORM cannot know an installer answer."""
    from app.models.settings import PlatformSettings

    col = PlatformSettings.__table__.c.ntp_pool_servers
    assert "pool.ntp.org" in str(col.server_default.arg)
