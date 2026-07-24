"""DoT / DoH + encrypted upstream forwarding rendering (issue #50).

Exercises the pure BIND9 helpers and the PowerDNS/dnsdist rules renderer
against the ``options`` dict shape ``app.services.dns.agent_config`` ships.

The through-line of these tests: **every path degrades to Do53 rather than
to a daemon that won't start.** A listener whose cert has gone away renders
nothing at all, because emitting a ``tls`` block pointing at a missing PEM
makes named refuse to load — taking plain DNS down with it.

The rendered grammar itself was verified against a real BIND 9.20.23 via
``named-checkconf`` (and dnsdist via ``--check-config``) while developing;
these tests lock in the shape so a refactor can't silently drift from it.
"""

from __future__ import annotations

from pathlib import Path

from spatium_dns_agent.drivers.bind9 import (
    TLS_CERT_FILENAME,
    TLS_DIR_NAME,
    TLS_KEY_FILENAME,
    _format_forwarder,
    _render_encrypted_listeners,
    _render_tls_statements,
)
from spatium_dns_agent.drivers.powerdns import render_dnsdist_conf

STATE = Path("/var/lib/spatium-dns-agent")


# ── BIND9: inbound listeners ────────────────────────────────────────────


def test_default_is_noop() -> None:
    """An install that never opted in must render byte-identical config."""
    assert _render_tls_statements({}, STATE, True) == ""
    assert _render_encrypted_listeners({}, True) == ""


def test_dot_listener() -> None:
    opts = {"dot_enabled": True, "dot_port": 853}
    stmts = _render_tls_statements(opts, STATE, True)
    assert "tls spatium-local-tls {" in stmts
    assert 'cert-file "/var/lib/spatium-dns-agent/tls/listener.crt";' in stmts
    assert 'key-file "/var/lib/spatium-dns-agent/tls/listener.key";' in stmts
    # No http statement — DoH is off.
    assert "http spatium-local-http" not in stmts

    listeners = _render_encrypted_listeners(opts, True)
    assert "listen-on port 853 tls spatium-local-tls { any; };" in listeners
    assert "listen-on-v6 port 853 tls spatium-local-tls { any; };" in listeners
    assert "http" not in listeners


def test_doh_listener_carries_both_tls_and_http() -> None:
    opts = {"doh_enabled": True, "doh_port": 8443, "doh_path": "/dns-query"}
    stmts = _render_tls_statements(opts, STATE, True)
    assert 'endpoints { "/dns-query"; };' in stmts

    listeners = _render_encrypted_listeners(opts, True)
    # BIND needs BOTH clauses on a DoH listener — tls terminates, http routes.
    assert (
        "listen-on port 8443 tls spatium-local-tls http spatium-local-http { any; };"
        in listeners
    )


def test_custom_doh_path_is_honoured() -> None:
    opts = {"doh_enabled": True, "doh_path": "/resolve"}
    assert 'endpoints { "/resolve"; };' in _render_tls_statements(opts, STATE, True)


def test_listener_skipped_when_cert_missing() -> None:
    """``tls_certificate_id`` is ON DELETE SET NULL, so the cert can vanish
    while the enable flags stay on. Rendering a listener then would point
    ``cert-file`` at a non-existent PEM and named would refuse to start —
    losing Do53 too. Degrade instead."""
    opts = {"dot_enabled": True, "doh_enabled": True}
    assert _render_tls_statements(opts, STATE, False) == ""
    assert _render_encrypted_listeners(opts, False) == ""


def test_listeners_are_additive_not_replacing() -> None:
    """Nothing here should ever emit the plain listen-on pair; those stay in
    the skeleton so Do53 clients are unaffected by enabling DoT/DoH."""
    out = _render_encrypted_listeners({"dot_enabled": True}, True)
    assert "listen-on { any; };" not in out


# ── BIND9: outbound forwarding ──────────────────────────────────────────


def test_forwarder_do53_plain() -> None:
    assert _format_forwarder("1.1.1.1", {}) == "1.1.1.1"


def test_forwarder_do53_with_explicit_port() -> None:
    """``ip@port`` is the control-plane wire shape. It used to be emitted
    verbatim, rendering an unloadable ``192.0.2.2@5353;`` token."""
    assert _format_forwarder("192.0.2.2@5353", {}) == "192.0.2.2 port 5353"


def test_forwarder_over_tls_defaults_to_853() -> None:
    """RFC 7858 port — an operator flipping the transport shouldn't have to
    re-type every forwarder to avoid a TLS handshake against a Do53 port."""
    opts = {"forward_transport": "tls"}
    assert _format_forwarder("1.1.1.1", opts) == "1.1.1.1 port 853 tls spatium-upstream-tls"


def test_forwarder_over_tls_respects_explicit_port() -> None:
    opts = {"forward_transport": "tls"}
    assert _format_forwarder("9.9.9.9@8853", opts) == (
        "9.9.9.9 port 8853 tls spatium-upstream-tls"
    )


def test_upstream_tls_statement_strict() -> None:
    opts = {
        "forward_transport": "tls",
        "forward_tls_verify": True,
        "forward_tls_hostname": "cloudflare-dns.com",
    }
    stmts = _render_tls_statements(opts, STATE, False)
    assert "tls spatium-upstream-tls {" in stmts
    assert 'ca-file "/etc/ssl/certs/ca-certificates.crt";' in stmts
    assert 'remote-hostname "cloudflare-dns.com";' in stmts


def test_upstream_tls_statement_opportunistic() -> None:
    """Verification off = encrypt without authenticating. The block must
    still be non-empty (BIND rejects ``tls x { };``) — the protocol floor
    carries it."""
    opts = {"forward_transport": "tls", "forward_tls_verify": False}
    stmts = _render_tls_statements(opts, STATE, False)
    assert "tls spatium-upstream-tls {" in stmts
    assert "ca-file" not in stmts
    assert "remote-hostname" not in stmts
    assert "protocols { TLSv1.2; TLSv1.3; };" in stmts


def test_upstream_verify_without_hostname_does_not_render_ca_file() -> None:
    """The API refuses this combination, but the agent must not invent a
    half-strict config if one ever reaches it — there is no name to match,
    so claiming validation would be a lie."""
    opts = {"forward_transport": "tls", "forward_tls_verify": True}
    stmts = _render_tls_statements(opts, STATE, False)
    assert "remote-hostname" not in stmts
    assert "ca-file" not in stmts


def test_upstream_statement_absent_on_do53() -> None:
    assert "spatium-upstream-tls" not in _render_tls_statements(
        {"forwarders": ["1.1.1.1"]}, STATE, False
    )


# ── PowerDNS / dnsdist front ────────────────────────────────────────────


def test_dnsdist_noop_when_nothing_configured() -> None:
    assert render_dnsdist_conf({}) == ""


def test_dnsdist_dot_and_doh() -> None:
    out = render_dnsdist_conf(
        {"dot_enabled": True, "dot_port": 853, "doh_enabled": True, "doh_port": 8443},
        has_cert=True,
    )
    # Paths must be dnsdist-side (its own container mounts the agent state
    # dir at /agent-state), not the agent's own state_dir.
    assert 'addTLSLocal("0.0.0.0:853", "/agent-state/tls/listener.crt"' in out
    assert 'addTLSLocal("[::]:853"' in out
    assert 'addDOHLocal("0.0.0.0:8443"' in out
    assert '{"/dns-query"}' in out


def test_dnsdist_listeners_render_without_rate_limiting() -> None:
    """DoT/DoH must not require ``dnsdist_enabled`` (the rate-limit knob) —
    they're independent reasons to configure the front."""
    out = render_dnsdist_conf({"dot_enabled": True}, has_cert=True)
    assert "addTLSLocal" in out
    assert "MaxQPSIPRule" not in out


def test_dnsdist_rate_limit_still_renders_alone() -> None:
    out = render_dnsdist_conf(
        {"dnsdist_enabled": True, "dnsdist_max_qps_per_client": 50}, has_cert=False
    )
    assert "addAction(MaxQPSIPRule(50), TCAction())" in out
    assert "addTLSLocal" not in out


def test_dnsdist_skips_listeners_without_cert() -> None:
    assert render_dnsdist_conf({"dot_enabled": True, "doh_enabled": True}) == ""


def test_dnsdist_no_upstream_transport_handling() -> None:
    """PowerDNS Authoritative doesn't recurse, so ``forward_transport`` is a
    BIND9-only concept — the only hop dnsdist makes is to the local pdns
    backend, where encryption buys nothing."""
    out = render_dnsdist_conf(
        {"forward_transport": "tls", "dnsdist_enabled": True}, has_cert=True
    )
    assert "spatium-upstream-tls" not in out


# ── Stale key removal (code-review #3) ──────────────────────────────────


def test_listener_cert_removed_when_disabled(tmp_path: Path) -> None:
    """Disabling the listener (or deleting the cert row) must take the
    PRIVATE KEY off disk, not just stop referencing it.

    A key that outlives the feature that put it there ends up in backups
    and hostPath snapshots long after anyone remembers it exists. The
    PowerDNS driver already did this; BIND9 didn't.
    """
    from spatium_dns_agent.drivers.bind9 import Bind9Driver

    drv = Bind9Driver.__new__(Bind9Driver)
    drv.state_dir = tmp_path  # type: ignore[attr-defined]

    drv._write_listener_cert({"cert_pem": "CERT", "key_pem": "KEY"})
    crt = tmp_path / TLS_DIR_NAME / TLS_CERT_FILENAME
    key = tmp_path / TLS_DIR_NAME / TLS_KEY_FILENAME
    assert crt.read_text() == "CERT"
    assert key.read_text() == "KEY"
    # 0600 — the key must never be group/world readable.
    assert oct(key.stat().st_mode & 0o777) == "0o600"

    drv._write_listener_cert(None)
    assert not crt.exists()
    assert not key.exists()


def test_listener_cert_removal_is_idempotent(tmp_path: Path) -> None:
    """Every render calls this; a group that never enabled a listener must
    not blow up on the missing directory."""
    from spatium_dns_agent.drivers.bind9 import Bind9Driver

    drv = Bind9Driver.__new__(Bind9Driver)
    drv.state_dir = tmp_path  # type: ignore[attr-defined]
    drv._write_listener_cert(None)
    drv._write_listener_cert(None)


def test_powerdns_listener_cert_removed_when_disabled(tmp_path: Path) -> None:
    from spatium_dns_agent.drivers.powerdns import PowerDNSDriver

    drv = PowerDNSDriver.__new__(PowerDNSDriver)
    drv.state_dir = tmp_path  # type: ignore[attr-defined]
    drv._write_listener_cert({"cert_pem": "C", "key_pem": "K"})
    key = tmp_path / TLS_DIR_NAME / TLS_KEY_FILENAME
    assert key.exists()
    drv._write_listener_cert(None)
    assert not key.exists()
