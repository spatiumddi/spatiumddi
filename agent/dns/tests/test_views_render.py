"""DNS Views — split-horizon BIND9 rendering on the agent (issue #24).

The agent receives a long-poll bundle whose zones are pre-expanded
per view (each zone copy tagged with ``view_name`` and carrying only
that view's records) and renders ``named.conf`` with one ``view { … }``
block per view, plus per-view zone files so an identical zone name in
two views doesn't clobber files.

These tests exercise ``Bind9Driver.render`` against hand-built bundles
shaped exactly like ``app.services.dns.agent_config.build_config_bundle``
emits.
"""

from __future__ import annotations

from pathlib import Path

from spatium_dns_agent.drivers.bind9 import Bind9Driver


def _zone(name: str, view_name: str | None, records: list[dict]) -> dict:
    return {
        "id": name,
        "name": name,
        "type": "primary",
        "ttl": 3600,
        "forwarders": [],
        "forward_only": True,
        "view_name": view_name,
        "records": records,
    }


def _split_horizon_bundle() -> dict:
    # Same zone "example.com." served two ways: internal clients see the
    # RFC 1918 address, external clients see the public one; the MX is
    # shared (view_name folded to NULL on the control plane → present in
    # both view copies).
    internal_recs = [
        {
            "name": "www",
            "type": "A",
            "ttl": 300,
            "value": "10.0.0.1",
            "priority": None,
            "weight": None,
            "port": None,
        },
        {
            "name": "@",
            "type": "MX",
            "ttl": 3600,
            "value": "mail.example.com.",
            "priority": 10,
            "weight": None,
            "port": None,
        },
    ]
    external_recs = [
        {
            "name": "www",
            "type": "A",
            "ttl": 300,
            "value": "203.0.113.10",
            "priority": None,
            "weight": None,
            "port": None,
        },
        {
            "name": "@",
            "type": "MX",
            "ttl": 3600,
            "value": "mail.example.com.",
            "priority": 10,
            "weight": None,
            "port": None,
        },
    ]
    return {
        "options": {"recursion_enabled": True, "allow_query": ["any"]},
        "views": [
            {
                "id": "v1",
                "name": "internal",
                "match_clients": ["10.0.0.0/8"],
                "match_destinations": [],
                "recursion": True,
                "order": 0,
            },
            {
                "id": "v2",
                "name": "external",
                "match_clients": ["any"],
                "match_destinations": [],
                "recursion": False,
                "order": 1,
            },
        ],
        "zones": [
            _zone("example.com.", "internal", internal_recs),
            _zone("example.com.", "external", external_recs),
        ],
        "tsig_keys": [],
        "blocklists": [],
    }


def test_render_wraps_zones_in_view_blocks(tmp_path: Path) -> None:
    drv = Bind9Driver(state_dir=tmp_path)
    drv.render(_split_horizon_bundle())
    conf = (tmp_path / "rendered.new" / "named.conf").read_text()

    # One view block per view, with the right match-clients + recursion.
    assert 'view "internal" {' in conf
    assert 'view "external" {' in conf
    assert "match-clients { 10.0.0.0/8; };" in conf
    # internal recursion yes, external recursion no. Internal (order 0)
    # is emitted before external (order 1), so the internal block is the
    # text between the two view headers.
    internal_block = conf.split('view "internal" {', 1)[1].split(
        'view "external" {', 1
    )[0]
    external_block = conf.split('view "external" {', 1)[1]
    assert "recursion yes;" in internal_block
    assert "recursion no;" in external_block

    # The zone appears inside BOTH view blocks, each pointing at a
    # per-view zone file (no clobber).
    assert "zones/internal/example.com.db" in conf
    assert "zones/external/example.com.db" in conf


def test_per_view_zone_files_hold_view_scoped_records(tmp_path: Path) -> None:
    drv = Bind9Driver(state_dir=tmp_path)
    drv.render(_split_horizon_bundle())
    zdir = tmp_path / "rendered.new" / "zones"

    internal_zone = (zdir / "internal" / "example.com.db").read_text()
    external_zone = (zdir / "external" / "example.com.db").read_text()

    # Split horizon: the same name resolves differently per view.
    assert "www 300 IN A 10.0.0.1" in internal_zone
    assert "10.0.0.1" not in external_zone
    assert "www 300 IN A 203.0.113.10" in external_zone
    assert "203.0.113.10" not in internal_zone
    # Shared record present in both.
    assert "mail.example.com." in internal_zone
    assert "mail.example.com." in external_zone


def test_per_view_allow_query_acl_is_enforced(tmp_path: Path) -> None:
    """#430 — a view's allow_query / allow_query_cache render into its block.

    Previously these round-tripped through the API but were never emitted,
    so a view-scoped query ACL silently never took effect."""
    bundle = _split_horizon_bundle()
    # internal: restrict queries to the RFC 1918 client range + cache to it.
    bundle["views"][0]["allow_query"] = ["10.0.0.0/8", "localhost"]
    bundle["views"][0]["allow_query_cache"] = ["10.0.0.0/8"]
    # external: leave both unset → inherit server-options allow-query.
    bundle["views"][1]["allow_query"] = None
    bundle["views"][1]["allow_query_cache"] = None

    drv = Bind9Driver(state_dir=tmp_path)
    drv.render(bundle)
    conf = (tmp_path / "rendered.new" / "named.conf").read_text()

    internal_block = conf.split('view "internal" {', 1)[1].split(
        'view "external" {', 1
    )[0]
    external_block = conf.split('view "external" {', 1)[1]

    assert "allow-query { 10.0.0.0/8; localhost; };" in internal_block
    assert "allow-query-cache { 10.0.0.0/8; };" in internal_block
    # The unset view emits neither line (inherits server options).
    assert "allow-query {" not in external_block
    assert "allow-query-cache {" not in external_block


def test_no_views_keeps_flat_render(tmp_path: Path) -> None:
    # Backward-compat: a bundle with no views renders flat (no view {}
    # blocks, zone file at zones/<name>.db).
    drv = Bind9Driver(state_dir=tmp_path)
    drv.render(
        {
            "options": {"recursion_enabled": True, "allow_query": ["any"]},
            "views": [],
            "zones": [
                _zone(
                    "flat.example.",
                    None,
                    [
                        {
                            "name": "a",
                            "type": "A",
                            "ttl": 300,
                            "value": "192.0.2.1",
                            "priority": None,
                            "weight": None,
                            "port": None,
                        }
                    ],
                )
            ],
            "tsig_keys": [],
            "blocklists": [],
        }
    )
    conf = (tmp_path / "rendered.new" / "named.conf").read_text()
    assert 'view "' not in conf
    assert 'zone "flat.example." {' in conf
    assert (tmp_path / "rendered.new" / "zones" / "flat.example.db").exists()


def test_global_zone_renders_into_every_view(tmp_path: Path) -> None:
    # A zone with view_name set per the control-plane "global zone →
    # every view" expansion: here we simulate it by emitting the same
    # zone for both views with identical records.
    recs = [
        {
            "name": "shared",
            "type": "A",
            "ttl": 300,
            "value": "198.51.100.5",
            "priority": None,
            "weight": None,
            "port": None,
        }
    ]
    bundle = {
        "options": {"recursion_enabled": True, "allow_query": ["any"]},
        "views": [
            {
                "id": "v1",
                "name": "internal",
                "match_clients": ["10.0.0.0/8"],
                "match_destinations": [],
                "recursion": True,
                "order": 0,
            },
            {
                "id": "v2",
                "name": "external",
                "match_clients": ["any"],
                "match_destinations": [],
                "recursion": True,
                "order": 1,
            },
        ],
        "zones": [
            _zone("global.example.", "internal", recs),
            _zone("global.example.", "external", recs),
        ],
        "tsig_keys": [],
        "blocklists": [],
    }
    drv = Bind9Driver(state_dir=tmp_path)
    drv.render(bundle)
    zdir = tmp_path / "rendered.new" / "zones"
    assert (zdir / "internal" / "global.example.db").exists()
    assert (zdir / "external" / "global.example.db").exists()
    for v in ("internal", "external"):
        assert (
            "shared 300 IN A 198.51.100.5"
            in (zdir / v / "global.example.db").read_text()
        )
