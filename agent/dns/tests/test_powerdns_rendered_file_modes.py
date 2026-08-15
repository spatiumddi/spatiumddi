"""Rendered PowerDNS files that embed a credential are 0600 (issue #869).

The driver already took care to write its *standalone* secret files at 0600
via ``os.open`` + ``O_NOFOLLOW`` — the API key store (#249 / #253) and the
DoT/DoH listener key (#50). What it then did was hand the same secrets back
out at the umask default by rendering them into config with ``write_text``:

* ``pdns.conf`` embeds ``api-key=``, which grants zone CRUD + DNSSEC over
  the pdns REST API;
* ``zones.json`` embeds ``update_tsig_keys``, whole TSIG key dicts including
  ``secret`` — the material that authorises dynamic updates.

CodeQL flagged only the first (alert 89). These tests pin both, plus the
negative case, so the sweep can't quietly regress: ``dnsdist-rules.conf``
carries cert *paths* rather than key material and is deliberately left at
the default mode, which is only correct for as long as it stays secret-free.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from spatium_dns_agent.admin_pusher import redact_secrets
from spatium_dns_agent.drivers.powerdns import PowerDNSDriver
from spatium_dns_agent.secure_io import write_private

_TSIG_SECRET = "c3VwZXItc2VjcmV0LXRzaWcta2V5LW1hdGVyaWFs"


def _bundle() -> dict:
    """A bundle whose zone carries a TSIG-keyed dynamic-update ACL, so the
    rendered ``zones.json`` actually contains key material to protect."""
    return {
        "options": {},
        "tsig_keys": [
            {
                "name": "ddns-key",
                "algorithm": "hmac-sha256",
                "secret": _TSIG_SECRET,
            }
        ],
        "zones": [
            {
                "name": "example.com",
                "type": "primary",
                "dynamic_update_enabled": True,
                "update_acl": [{"match_kind": "tsig_key", "tsig_key_name": "ddns-key"}],
                "records": [],
            }
        ],
    }


def _mode(path: Path) -> str:
    return oct(path.stat().st_mode & 0o777)


def test_rendered_pdns_conf_is_private(tmp_path: Path) -> None:
    drv = PowerDNSDriver(state_dir=tmp_path)
    drv.render(_bundle())

    conf = tmp_path / "rendered.new" / "pdns.conf"
    # Guard the guard: if the key ever stops being inlined this test would
    # otherwise keep passing while asserting nothing.
    assert "api-key=" in conf.read_text()
    assert _mode(conf) == "0o600"


def test_rendered_zones_json_is_private(tmp_path: Path) -> None:
    drv = PowerDNSDriver(state_dir=tmp_path)
    drv.render(_bundle())

    zones = tmp_path / "rendered.new" / "zones.json"
    payload = json.loads(zones.read_text())
    # The TSIG secret really does reach this file — that is the whole reason
    # it needs the same treatment as pdns.conf.
    assert _TSIG_SECRET in json.dumps(payload)
    assert _mode(zones) == "0o600"


def test_dnsdist_rules_carry_no_secret(tmp_path: Path) -> None:
    """The one rendered file deliberately left at the default mode.

    It is written with ``write_text`` because it holds cert *paths*, not key
    material. If a future change puts a credential in here, this fails and
    the file needs ``write_private`` too.
    """
    drv = PowerDNSDriver(state_dir=tmp_path)
    bundle = _bundle()
    bundle["options"] = {"dnsdist_enabled": True, "dnsdist_max_qps_per_client": 50}
    drv.render(bundle)

    rules = tmp_path / "dnsdist-rules.conf"
    # Asserted, not `if rules.exists()`: guarding the body behind a condition
    # the test itself sets up means the whole check silently disappears if
    # rendering ever stops producing the file — the same vacuity the two
    # tests above deliberately avoid.
    assert rules.exists(), "dnsdist rules should render when dnsdist_enabled"
    body = rules.read_text()
    assert _TSIG_SECRET not in body
    assert "api-key" not in body


def test_render_hardens_files_left_by_a_pre_fix_build(tmp_path: Path) -> None:
    """Upgrading the agent must re-mode what is already on disk (#869).

    The live ``rendered/`` tree survives the upgrade until the next
    structural render replaces it, and lingers as ``rendered.prev/`` for one
    cycle after that — so without remediation a still-valid API key stays
    world-readable for two renders' worth of uptime.
    """
    for tree in ("rendered", "rendered.prev"):
        d = tmp_path / tree
        d.mkdir(parents=True)
        # Exactly what the old code produced: write_text at the umask default.
        (d / "pdns.conf").write_text("api-key=stale-but-still-live\n")
        (d / "zones.json").write_text("[]")
        for name in ("pdns.conf", "zones.json"):
            os.chmod(d / name, 0o644)

    PowerDNSDriver(state_dir=tmp_path).render(_bundle())

    for tree in ("rendered", "rendered.prev"):
        for name in ("pdns.conf", "zones.json"):
            assert _mode(tmp_path / tree / name) == "0o600", f"{tree}/{name}"


def test_harden_is_a_noop_on_a_clean_install(tmp_path: Path) -> None:
    """No pre-existing trees — remediation must not invent files or raise."""
    PowerDNSDriver(state_dir=tmp_path).render(_bundle())
    assert not (tmp_path / "rendered.prev").exists()


# ── Redaction before the snapshot leaves the appliance ──────────────────


def test_redaction_masks_api_key_and_tsig_secret() -> None:
    """0600 on disk does nothing for the snapshot pusher, which uploads
    these files to the control plane and serves them back over the API."""
    conf = redact_secrets(f"api=yes\napi-key={_TSIG_SECRET}\nwebserver=yes\n")
    assert _TSIG_SECRET not in conf
    # The setting NAME survives — "is an api-key configured?" is usually the
    # question the operator opened the snapshot to answer.
    assert "api-key=" in conf
    assert "api=yes" in conf and "webserver=yes" in conf

    zones = redact_secrets(json.dumps({"update_tsig_keys": [{"secret": _TSIG_SECRET}]}))
    assert _TSIG_SECRET not in zones
    assert "update_tsig_keys" in zones

    bind_style = redact_secrets(f'key "k" {{ secret "{_TSIG_SECRET}"; }};')
    assert _TSIG_SECRET not in bind_style


def test_redaction_leaves_ordinary_config_alone() -> None:
    body = "local-port=53\nlog-dns-queries=yes\n"
    assert redact_secrets(body) == body


def test_pushed_snapshot_carries_no_secret(tmp_path: Path) -> None:
    """End-to-end over the real render output, through the walk that feeds
    the uploader — the property that actually matters."""
    from spatium_dns_agent.admin_pusher import _walk_rendered

    drv = PowerDNSDriver(state_dir=tmp_path)
    drv.render(_bundle())
    (tmp_path / "rendered.new").rename(tmp_path / "rendered")

    files = dict(_walk_rendered(tmp_path / "rendered"))
    assert files, "expected the render to produce pushable files"
    blob = json.dumps(files)
    assert _TSIG_SECRET not in blob
    # The api-key is generated, so assert on the file rather than a literal.
    live_key = (tmp_path / "pdns-api.key").read_text().strip()
    assert live_key and live_key not in blob


# ── write_private itself ────────────────────────────────────────────────


def test_write_private_completes_a_short_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.write`` may write less than it was given; the file must still
    end up complete.

    The pre-fix code took the single return value on faith, so a short write
    truncated the file silently — and a truncated ``zones.json`` doesn't
    raise anywhere downstream, it just trips a log line in
    ``swap_and_reload`` while the structural etag advances, so the zone
    state is never applied and never retried.
    """
    real_write = os.write
    payload = "x" * 4096

    def _short(fd: int, data: bytes) -> int:
        return real_write(fd, data[: max(1, len(data) // 2)])

    monkeypatch.setattr(os, "write", _short)
    write_private(tmp_path / "secret", payload)
    monkeypatch.undo()

    assert (tmp_path / "secret").read_text() == payload


def test_write_private_raises_when_the_write_stalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write making no progress raises rather than looping forever.

    Callers' error handling assumes a failed write propagates — the sync
    loop relies on the exception to avoid advancing its etag past a config
    that never landed on disk.
    """
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    with pytest.raises(OSError, match="short write"):
        write_private(tmp_path / "secret", "payload")


def test_write_private_is_atomic_and_private(tmp_path: Path) -> None:
    dest = tmp_path / "secret"
    write_private(dest, "first")
    write_private(dest, "second")
    assert dest.read_text() == "second"
    assert _mode(dest) == "0o600"
    # The tmp sibling must not survive a successful write.
    assert not (tmp_path / "secret.new").exists()
