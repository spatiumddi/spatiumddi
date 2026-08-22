"""RPZ zone-file rendering on the agent (issue #878).

The agent is the live RPZ renderer: the control plane ships an
effective blocklist in the config bundle and the agent writes the zone
file BIND loads. These tests pin the rdata it emits per entry shape,
because a malformed line does not degrade to "that one entry does not
work" — BIND refuses to load the zone, and the whole blocklist stops
being enforced.
"""

from __future__ import annotations

from pathlib import Path

from spatium_dns_agent.drivers.bind9 import Bind9Driver


def _render(tmp_path: Path, entries: list[dict], exceptions: list[str] | None = None) -> list[str]:
    driver = Bind9Driver(state_dir=tmp_path)
    out = tmp_path / "rpz.zone"
    driver._write_rpz_zone_file(
        out,
        {
            "rpz_zone_name": "spatium-blocklist.rpz.",
            "entries": entries,
            "exceptions": exceptions or [],
        },
    )
    return [
        line
        for line in out.read_text().splitlines()
        if line and not line.startswith(("$TTL", "@"))
    ]


def test_block_and_sinkhole_rdata(tmp_path: Path) -> None:
    lines = _render(
        tmp_path,
        [
            {"domain": "ads.example.test", "action": "block", "block_mode": "nxdomain"},
            {"domain": "drop.example.test", "action": "block", "block_mode": "sinkhole"},
        ],
    )
    assert "ads.example.test CNAME ." in lines
    assert "drop.example.test CNAME rpz-drop." in lines


def test_redirect_to_hostname_is_a_cname(tmp_path: Path) -> None:
    """SafeSearch rewrites point at a name, not an address.

    Before #878 this path emitted ``CNAME`` unconditionally, which is
    right here — the regression risk is the opposite direction, so both
    are pinned.
    """
    lines = _render(
        tmp_path,
        [
            {
                "domain": "www.google.com",
                "action": "redirect",
                "block_mode": "nxdomain",
                "target": "forcesafesearch.google.com",
            }
        ],
    )
    assert lines == ["www.google.com CNAME forcesafesearch.google.com."]


def test_redirect_to_an_ip_is_an_address_record(tmp_path: Path) -> None:
    """``CNAME 1.2.3.4.`` is a CNAME to a name that does not exist, so
    the redirect silently resolves to nothing. The model documents the
    target as "the IP/hostname to return instead", so both must work."""
    lines = _render(
        tmp_path,
        [
            {
                "domain": "v4.example.test",
                "action": "redirect",
                "block_mode": "nxdomain",
                "target": "192.0.2.10",
            },
            {
                "domain": "v6.example.test",
                "action": "redirect",
                "block_mode": "nxdomain",
                "target": "2001:db8::1",
            },
        ],
    )
    assert "v4.example.test A 192.0.2.10" in lines
    assert "v6.example.test AAAA 2001:db8::1" in lines


def test_wildcard_covers_the_apex_as_well_as_subdomains(tmp_path: Path) -> None:
    """An RPZ wildcard matches subdomains only, so a `*.x`-only rule
    leaves `x` itself resolving — both lines are required."""
    lines = _render(
        tmp_path,
        [{"domain": "bad.example.test", "action": "block", "block_mode": "nxdomain",
          "is_wildcard": True}],
    )
    assert lines == ["bad.example.test CNAME .", "*.bad.example.test CNAME ."]


def test_wildcard_redirect_keeps_one_rdata_for_both_lines(tmp_path: Path) -> None:
    lines = _render(
        tmp_path,
        [{"domain": "search.example.test", "action": "redirect",
          "block_mode": "nxdomain", "target": "safe.example.test",
          "is_wildcard": True}],
    )
    assert lines == [
        "search.example.test CNAME safe.example.test.",
        "*.search.example.test CNAME safe.example.test.",
    ]


def test_exceptions_render_as_passthru(tmp_path: Path) -> None:
    lines = _render(tmp_path, [], ["allow.example.test"])
    assert lines == [
        "allow.example.test CNAME rpz-passthru.",
        "*.allow.example.test CNAME rpz-passthru.",
    ]


def test_redirect_target_trailing_dot_is_not_doubled(tmp_path: Path) -> None:
    lines = _render(
        tmp_path,
        [{"domain": "a.example.test", "action": "redirect",
          "block_mode": "nxdomain", "target": "safe.example.test."}],
    )
    assert lines == ["a.example.test CNAME safe.example.test."]
