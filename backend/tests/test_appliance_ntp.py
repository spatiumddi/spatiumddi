"""Unit tests for the appliance NTP renderer (issue #154).

Pure-Python — same shape as ``test_appliance_snmp.py``. The renderer
takes a ``PlatformSettings`` ORM row and produces deterministic
chrony.conf text without touching the DB.

Covers:

* Pool mode emits ``pool <host> iburst`` per entry; mixed mode emits
  both pool + unicast lines; servers mode skips the pool block
* Unicast servers carry the iburst / prefer flags only when set
* Determinism (same settings → same bytes — required for the
  agent's config-hash idempotency check)
* ``allow_clients`` emits ``allow <cidr>`` lines; empty list with
  the toggle on produces a warning comment, no allow lines
* ``ntp_bundle`` always returns the same key set
* Source mode + an empty source list together produce a warning
  comment instead of a silent no-time-source config
"""

from __future__ import annotations

from app.models.settings import PlatformSettings
from app.services.appliance.ntp import ntp_bundle, render_chrony_conf


def _bare_settings() -> PlatformSettings:
    s = PlatformSettings(id=1)
    s.ntp_source_mode = "pool"
    s.ntp_pool_servers = ["pool.ntp.org"]
    s.ntp_custom_servers = []
    s.ntp_allow_clients = False
    s.ntp_allow_client_networks = []
    return s


def test_pool_mode_emits_pool_lines() -> None:
    s = _bare_settings()
    s.ntp_source_mode = "pool"
    s.ntp_pool_servers = ["pool.ntp.org", "2.debian.pool.ntp.org"]
    text = render_chrony_conf(s)
    assert "pool pool.ntp.org iburst" in text
    assert "pool 2.debian.pool.ntp.org iburst" in text
    # No unicast server lines in pure-pool mode
    assert "server " not in text or text.count("server ") == 0


def test_servers_mode_emits_server_lines_with_flags() -> None:
    s = _bare_settings()
    s.ntp_source_mode = "servers"
    s.ntp_custom_servers = [
        {"host": "time.internal.example.com", "iburst": True, "prefer": True},
        {"host": "10.0.0.5", "iburst": True, "prefer": False},
        {"host": "10.0.0.6", "iburst": False, "prefer": False},
    ]
    text = render_chrony_conf(s)
    # First server has both flags
    assert "server time.internal.example.com iburst prefer" in text
    # Second has iburst only
    assert "server 10.0.0.5 iburst" in text
    assert "server 10.0.0.5 iburst prefer" not in text
    # Third has neither
    assert "server 10.0.0.6\n" in text
    # No pool lines in pure-servers mode
    assert "pool pool.ntp.org" not in text


def test_mixed_mode_emits_both() -> None:
    s = _bare_settings()
    s.ntp_source_mode = "mixed"
    s.ntp_pool_servers = ["pool.ntp.org"]
    s.ntp_custom_servers = [{"host": "10.0.0.5", "iburst": True, "prefer": False}]
    text = render_chrony_conf(s)
    assert "pool pool.ntp.org iburst" in text
    assert "server 10.0.0.5 iburst" in text


def test_empty_sources_emits_warning() -> None:
    # Pool mode with empty pool list = no time source. Renderer should
    # surface that as a comment rather than silently producing a working-
    # but-untrustworthy daemon config.
    s = _bare_settings()
    s.ntp_source_mode = "pool"
    s.ntp_pool_servers = []
    text = render_chrony_conf(s)
    assert "no time sources configured" in text.lower()
    assert "pool " not in text


def test_allow_clients_emits_allow_lines() -> None:
    s = _bare_settings()
    s.ntp_allow_clients = True
    s.ntp_allow_client_networks = ["10.0.0.0/8", "192.168.0.0/16"]
    text = render_chrony_conf(s)
    assert "allow 10.0.0.0/8" in text
    assert "allow 192.168.0.0/16" in text


def test_allow_clients_with_empty_networks_emits_warning() -> None:
    s = _bare_settings()
    s.ntp_allow_clients = True
    s.ntp_allow_client_networks = []
    text = render_chrony_conf(s)
    # No allow line should be emitted
    assert "allow " not in text
    # But the operator should see a warning so the empty list doesn't
    # silently break their NTP-server-mode intent
    assert "no cidrs configured" in text.lower()


def test_allow_clients_off_omits_allow_block() -> None:
    s = _bare_settings()
    s.ntp_allow_clients = False
    s.ntp_allow_client_networks = ["10.0.0.0/8"]  # Ignored when off
    text = render_chrony_conf(s)
    assert "allow " not in text


def test_renderer_is_deterministic() -> None:
    s = _bare_settings()
    s.ntp_source_mode = "mixed"
    s.ntp_pool_servers = ["pool.ntp.org"]
    s.ntp_custom_servers = [
        {"host": "10.0.0.5", "iburst": True, "prefer": False},
    ]
    s.ntp_allow_clients = True
    s.ntp_allow_client_networks = ["10.0.0.0/8"]
    a = render_chrony_conf(s)
    b = render_chrony_conf(s)
    assert a == b


def test_bundle_has_stable_keys() -> None:
    on = _bare_settings()
    on.ntp_allow_clients = True
    on.ntp_allow_client_networks = ["10.0.0.0/8"]
    off = _bare_settings()
    on_bundle = ntp_bundle(on)
    off_bundle = ntp_bundle(off)
    assert set(on_bundle.keys()) == set(off_bundle.keys())
    # ``enabled`` is True for both (chrony always runs on appliance);
    # ``allow_clients`` reflects the operator's toggle.
    assert on_bundle["enabled"] is True
    assert off_bundle["enabled"] is True
    assert on_bundle["allow_clients"] is True
    assert off_bundle["allow_clients"] is False
    # Hash is always populated (chrony.conf body is never empty)
    assert len(on_bundle["config_hash"]) == 64
    assert len(off_bundle["config_hash"]) == 64


def test_standard_hygiene_directives_always_present() -> None:
    # ``driftfile``, ``makestep``, ``rtcsync`` and ``leapsectz`` are
    # the standard chrony hygiene lines the renderer always emits so
    # the config is self-contained — no stale Debian defaults left in
    # place if the operator pushed a brand-new config.
    s = _bare_settings()
    text = render_chrony_conf(s)
    assert "driftfile /var/lib/chrony/chrony.drift" in text
    assert "makestep 1.0 3" in text
    assert "rtcsync" in text
    assert "leapsectz right/UTC" in text
