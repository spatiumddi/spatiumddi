"""Unit tests for the appliance SNMP renderer (issue #153).

Pure-Python — the renderer is deterministic over a ``PlatformSettings``
ORM row but doesn't actually touch the DB. We synthesise the row
in-memory and assert on the rendered config bytes.

Covers:

* ``render_snmpd_conf`` shape for v2c (rocommunity per source CIDR,
  v4/v6 distinction, sysContact / sysLocation quoting, agentAddress)
* v3 emits createUser + rouser at the right access level for
  noauth / auth / priv (the three USM security levels)
* Same settings → same bytes (determinism, so ETag math stays stable)
* ``snmp_bundle`` short-circuits to empty when ``snmp_enabled=False``
  but always returns the same key set so consumers can compute hashes
  without None-handling
* Empty community + empty sources both render *something* but not a
  working config — operator should see comments explaining why
"""

from __future__ import annotations

from app.core.crypto import encrypt_str
from app.models.settings import PlatformSettings
from app.services.appliance.snmp import (
    render_snmpd_conf,
    snmp_bundle,
)


def _bare_settings() -> PlatformSettings:
    """Build a minimal PlatformSettings with SNMP off and no users.

    SQLAlchemy default= kwargs fire only on flush, so set every column
    we care about explicitly. The renderer doesn't touch the DB so
    this is purely a struct."""
    s = PlatformSettings(id=1)
    s.snmp_enabled = False
    s.snmp_version = "v2c"
    s.snmp_community_encrypted = None
    s.snmp_v3_users = []
    s.snmp_allowed_sources = []
    s.snmp_sys_contact = ""
    s.snmp_sys_location = ""
    return s


def test_v2c_renders_rocommunity_for_each_source() -> None:
    s = _bare_settings()
    s.snmp_enabled = True
    s.snmp_version = "v2c"
    s.snmp_community_encrypted = encrypt_str("s3cret")
    s.snmp_allowed_sources = ["10.0.0.0/8", "192.168.1.0/24", "fd00::/8"]
    s.snmp_sys_contact = "ops@example.com"
    s.snmp_sys_location = "Datacenter A, Rack 12"

    text = render_snmpd_conf(s)
    assert "rocommunity s3cret 10.0.0.0/8" in text
    assert "rocommunity s3cret 192.168.1.0/24" in text
    # IPv6 source uses the v6-specific directive
    assert "rocommunity6 s3cret fd00::/8" in text
    # sysContact / sysLocation get double-quoted
    assert 'sysContact     "ops@example.com"' in text
    assert 'sysLocation    "Datacenter A, Rack 12"' in text
    # Standard agent listen line
    assert "agentAddress udp:161,udp6:[::1]:161" in text


def test_v2c_empty_community_emits_no_rocommunity_lines() -> None:
    s = _bare_settings()
    s.snmp_enabled = True
    s.snmp_version = "v2c"
    s.snmp_allowed_sources = ["10.0.0.0/8"]

    text = render_snmpd_conf(s)
    assert "rocommunity" not in text
    # But the operator gets a comment explaining why
    assert "no community configured" in text.lower()


def test_v2c_empty_sources_emits_no_rocommunity_lines() -> None:
    s = _bare_settings()
    s.snmp_enabled = True
    s.snmp_version = "v2c"
    s.snmp_community_encrypted = encrypt_str("public")
    s.snmp_allowed_sources = []

    text = render_snmpd_conf(s)
    assert "rocommunity public" not in text
    assert "no allowed" in text.lower()


def test_v3_emits_createuser_and_rouser_at_correct_level() -> None:
    s = _bare_settings()
    s.snmp_enabled = True
    s.snmp_version = "v3"
    s.snmp_v3_users = [
        # noAuthNoPriv user
        {
            "username": "noauth_user",
            "auth_protocol": "none",
            "auth_pass_enc": None,
            "priv_protocol": "none",
            "priv_pass_enc": None,
        },
        # auth-only user
        {
            "username": "auth_user",
            "auth_protocol": "SHA",
            "auth_pass_enc": encrypt_str("auth-pass").decode("ascii"),
            "priv_protocol": "none",
            "priv_pass_enc": None,
        },
        # auth + priv user
        {
            "username": "priv_user",
            "auth_protocol": "SHA",
            "auth_pass_enc": encrypt_str("auth-pass-2").decode("ascii"),
            "priv_protocol": "AES",
            "priv_pass_enc": encrypt_str("priv-pass").decode("ascii"),
        },
    ]

    text = render_snmpd_conf(s)
    # noauth user — just a createUser with no protocols + rouser noauth
    assert "createUser noauth_user" in text
    assert "rouser noauth_user noauth .1" in text
    # auth user — createUser with SHA + pass + rouser auth
    assert 'createUser auth_user SHA "auth-pass"' in text
    assert "rouser auth_user auth .1" in text
    # priv user — createUser with both protocols + pass + rouser priv
    assert 'createUser priv_user SHA "auth-pass-2" AES "priv-pass"' in text
    assert "rouser priv_user priv .1" in text


def test_v3_empty_users_emits_no_createuser_lines() -> None:
    s = _bare_settings()
    s.snmp_enabled = True
    s.snmp_version = "v3"
    s.snmp_v3_users = []

    text = render_snmpd_conf(s)
    assert "createUser" not in text
    assert "no users configured" in text.lower()


def test_renderer_is_deterministic() -> None:
    """Same inputs → same bytes. Critical because the agent's
    config-hash idempotency check depends on byte-stable output."""
    s = _bare_settings()
    s.snmp_enabled = True
    s.snmp_version = "v2c"
    s.snmp_community_encrypted = encrypt_str("public")
    s.snmp_allowed_sources = ["10.0.0.0/8", "192.168.0.0/16"]
    s.snmp_sys_contact = "ops@example.com"

    a = render_snmpd_conf(s)
    b = render_snmpd_conf(s)
    assert a == b


def test_snmp_bundle_short_circuits_when_disabled() -> None:
    s = _bare_settings()
    s.snmp_enabled = False
    s.snmp_version = "v2c"
    s.snmp_community_encrypted = encrypt_str("ignored")

    bundle = snmp_bundle(s)
    assert bundle == {"enabled": False, "config_hash": "", "snmpd_conf": ""}


def test_snmp_bundle_has_stable_keys() -> None:
    """Both enabled and disabled paths return the same key set so
    downstream consumers (agent comparator, ConfigBundle TypedDict)
    don't need to None-guard."""
    on = _bare_settings()
    on.snmp_enabled = True
    on.snmp_version = "v2c"
    on.snmp_community_encrypted = encrypt_str("on")
    on.snmp_allowed_sources = ["10.0.0.0/8"]

    off = _bare_settings()

    on_bundle = snmp_bundle(on)
    off_bundle = snmp_bundle(off)
    assert set(on_bundle.keys()) == set(off_bundle.keys())
    assert on_bundle["enabled"] is True
    assert off_bundle["enabled"] is False
    # Hash is 64 hex chars when enabled, empty when not
    assert len(on_bundle["config_hash"]) == 64
    assert off_bundle["config_hash"] == ""


def test_sys_contact_with_quote_is_escaped() -> None:
    """Defensive — the quote routine has to escape ``"`` to avoid
    blowing up snmpd's parser when an operator pastes an awkward
    contact string."""
    s = _bare_settings()
    s.snmp_enabled = True
    s.snmp_version = "v2c"
    s.snmp_community_encrypted = encrypt_str("c")
    s.snmp_allowed_sources = ["10.0.0.0/8"]
    s.snmp_sys_contact = 'someone "with quotes"'

    text = render_snmpd_conf(s)
    # The quoted form should escape the embedded quotes; snmpd reads
    # a single double-quoted string.
    assert r'"someone \"with quotes\""' in text


def test_disabled_master_toggle_renders_no_config() -> None:
    """The bundle path is what real consumers use; the renderer is
    a stable pure function. When the master toggle is off, the
    bundle's snmpd_conf body is empty (the runner's ``disabled``
    branch stubs the file out)."""
    s = _bare_settings()
    s.snmp_enabled = False
    s.snmp_version = "v2c"
    s.snmp_community_encrypted = encrypt_str("ignored")
    s.snmp_allowed_sources = ["10.0.0.0/8"]

    bundle = snmp_bundle(s)
    assert bundle["snmpd_conf"] == ""
