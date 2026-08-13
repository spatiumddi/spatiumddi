"""#856 — the agent dropped DHCP options whose canonical name it didn't know.

The control plane stores option 6 under the canonical name ``dns-servers``
(``STANDARD_OPTION_NAMES`` in ``backend/app/drivers/dhcp/base.py``) and ships
that key in the bundle. The agent's v4 renderer looked it up only as
``dns_servers`` / ``domain-name-servers``, so the lookup missed and option 6
never reached ``kea-dhcp4.conf`` — silently, because a missing key is
indistinguishable from an unset option. ``routers`` survived only because it
happens to be spelled identically on both sides.

These tests pin the canonical vocabulary as the contract, so a future option
cannot be added on one side alone and quietly go missing on the other.
"""

from __future__ import annotations

import pytest

from spatium_dhcp_agent.render_kea import (
    _KEA_OPTION_NAMES_V4,
    _collect_option_defs,
    _options_from_mapping,
    render,
)


def _by_name(entries: list[dict]) -> dict[str, str]:
    return {e["name"]: e["data"] for e in entries}


# ── The reported bug ────────────────────────────────────────────────────────


def test_canonical_dns_servers_renders_option_6() -> None:
    """The exact #856 report: option 6 stored canonically must reach Kea."""
    out = _by_name(
        _options_from_mapping(
            {"routers": ["192.168.20.254"], "dns-servers": ["192.168.20.1"]}
        )
    )
    assert out["domain-name-servers"] == "192.168.20.1"
    assert out["routers"] == "192.168.20.254"


def test_option_6_carries_code_and_space() -> None:
    """The reporter's expected shape — Kea name plus explicit code 6."""
    entry = next(
        e
        for e in _options_from_mapping({"dns-servers": ["192.168.20.1"]})
        if e["name"] == "domain-name-servers"
    )
    assert entry["code"] == 6
    assert entry["space"] == "dhcp4"


def test_canonical_bootfile_name_renders() -> None:
    """Option 67 had the identical defect — canonical ``bootfile-name`` vs the
    agent's ``boot_file`` / ``boot-file-name``."""
    out = _by_name(_options_from_mapping({"bootfile-name": "pxelinux.0"}))
    assert out["boot-file-name"] == "pxelinux.0"


@pytest.mark.parametrize(
    ("canonical", "kea_name", "value"),
    [
        ("broadcast-address", "broadcast-address", "192.168.20.255"),
        ("mtu", "interface-mtu", 1500),
        ("time-offset", "time-offset", 0),
        ("tftp-server-address", "tftp-server-address", ["192.168.20.5"]),
    ],
)
def test_previously_unhandled_options_render(
    canonical: str, kea_name: str, value: object
) -> None:
    """Four options the v4 renderer omitted entirely."""
    assert kea_name in _by_name(_options_from_mapping({canonical: value}))


def test_time_offset_zero_is_emitted() -> None:
    """``0`` is a real time-offset (UTC), not an unset option — the falsy
    filter must reject only None / "" / []."""
    assert "time-offset" in _by_name(_options_from_mapping({"time-offset": 0}))


# ── Contract between the two renderers ──────────────────────────────────────


def test_every_canonical_backend_option_is_supported() -> None:
    """The agent's v4 table must cover the control plane's whole vocabulary.

    This is the assertion that would have caught #856 at author time.
    ``STANDARD_OPTION_NAMES`` is duplicated here rather than imported because
    the agent is a separate package that cannot import the backend.
    """
    backend_canonical = {
        "routers",
        "dns-servers",
        "domain-name",
        "broadcast-address",
        "ntp-servers",
        "tftp-server-name",
        "bootfile-name",
        "tftp-server-address",
        "domain-search",
        "mtu",
        "time-offset",
    }
    assert backend_canonical == set(_KEA_OPTION_NAMES_V4)


# ── Backward compatibility + determinism ────────────────────────────────────


@pytest.mark.parametrize(
    ("legacy", "kea_name"),
    [
        ("dns_servers", "domain-name-servers"),
        ("domain-name-servers", "domain-name-servers"),
        ("gateway", "routers"),
        ("boot_file", "boot-file-name"),
        ("ntp_servers", "ntp-servers"),
        ("domain_name", "domain-name"),
    ],
)
def test_legacy_spellings_still_accepted(legacy: str, kea_name: str) -> None:
    """Older bundles and hand-written fixtures must keep working."""
    assert kea_name in _by_name(_options_from_mapping({legacy: "192.0.2.1"}))


def test_canonical_key_wins_over_alias() -> None:
    """Both spellings present must not resolve by dict ordering."""
    out = _by_name(
        _options_from_mapping({"dns-servers": ["1.1.1.1"], "dns_servers": ["9.9.9.9"]})
    )
    assert out["domain-name-servers"] == "1.1.1.1"


def test_emission_order_is_independent_of_input_order() -> None:
    """A stable file avoids a spurious rewrite + Kea reload on every poll."""
    a = _options_from_mapping({"dns-servers": ["a"], "routers": ["b"]})
    b = _options_from_mapping({"routers": ["b"], "dns-servers": ["a"]})
    assert a == b


def test_two_aliases_of_one_option_resolve_deterministically() -> None:
    """Alias-vs-alias must not resolve by input dict ordering either.

    ``dns_servers`` and ``domain-name-servers`` both collapse onto
    ``dns-servers``; whichever wins, it must be the SAME one whatever order
    the bundle happens to serialise them in, or two agents in a group render
    different configs from the same bundle.
    """
    both = {"dns_servers": ["9.9.9.9"], "domain-name-servers": ["1.1.1.1"]}
    assert _options_from_mapping(both) == _options_from_mapping(
        dict(reversed(list(both.items())))
    )


def test_unknown_option_is_ignored_not_emitted() -> None:
    """An unrecognised key must not reach Kea, which would reject the config."""
    assert _options_from_mapping({"totally-made-up": "x"}) == []


def test_dropped_option_is_logged_not_silent(capsys: pytest.CaptureFixture[str]) -> None:
    """#856 was invisible because a dropped key looks like an unset option.

    Custom / vendor options (``code:NN`` from the Kea + ISC importers, and the
    codes the UI's custom-options accordion offers beyond the canonical set)
    are still dropped — emitting a name Kea has no definition for would fail
    the WHOLE config — but the operator must at least get a log line.
    """
    assert _options_from_mapping({"code:43": "0102", "time-servers": ["1.2.3.4"]}) == []
    out = capsys.readouterr().out
    assert out.count("kea_option_dropped_unsupported") == 2


def test_supported_and_structural_keys_do_not_warn(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``lease_time`` rides in ``global_options`` and ``option_data`` is the
    raw pass-through list — neither is a dropped option."""
    _options_from_mapping(
        {
            "lease_time": 3600,
            "dns-servers": ["1.1.1.1"],
            "dns_servers": ["9.9.9.9"],
            "option_data": [{"name": "domain-name", "data": "ex.com"}],
        }
    )
    assert "kea_option_dropped_unsupported" not in capsys.readouterr().out


# ── option-def for non-standard options ─────────────────────────────────────


def test_option_150_ships_its_definition() -> None:
    """Kea has no built-in definition for option 150 and fails the WHOLE
    config without one, so emitting it must ship the definition too."""
    defs = _collect_option_defs(
        {
            "subnet4": [
                {
                    "option-data": _options_from_mapping(
                        {"tftp-server-address": ["192.0.2.5"]}
                    )
                }
            ]
        }
    )
    assert defs == [
        {
            "name": "tftp-server-address",
            "code": 150,
            "space": "dhcp4",
            "type": "ipv4-address",
            "array": True,
        }
    ]


def test_no_option_def_when_unused() -> None:
    defs = _collect_option_defs(
        {
            "subnet4": [
                {"option-data": _options_from_mapping({"dns-servers": ["1.1.1.1"]})}
            ]
        }
    )
    assert defs == []


def test_option_def_found_in_nested_pool_and_reservation() -> None:
    """The walk must reach every option-data producer, not just the subnet."""
    entry = _options_from_mapping({"tftp-server-address": ["192.0.2.5"]})
    for doc in (
        {"subnet4": [{"pools": [{"option-data": entry}]}]},
        {"subnet4": [{"reservations": [{"option-data": entry}]}]},
        {"client-classes": [{"option-data": entry}]},
        {"option-data": entry},
    ):
        assert len(_collect_option_defs(doc)) == 1


def test_full_render_emits_option_6_and_option_def() -> None:
    """End to end through ``render()``, mirroring the reported scope."""
    doc = render(
        {
            "server": {"dhcp_socket_type": "raw"},
            "global_options": {"lease_time": 3600},
            "scopes": [
                {
                    "subnet_cidr": "192.168.20.0/24",
                    "address_family": "ipv4",
                    "is_active": True,
                    "lease_time": 3600,
                    "options": {
                        "routers": ["192.168.20.254"],
                        "dns-servers": ["192.168.20.1"],
                        "tftp-server-address": ["192.168.20.5"],
                    },
                    "pools": [
                        {
                            "start_ip": "192.168.20.100",
                            "end_ip": "192.168.20.200",
                            "pool_type": "dynamic",
                        }
                    ],
                    "statics": [],
                }
            ],
        }
    )["Dhcp4"]
    opts = _by_name(doc["subnet4"][0]["option-data"])
    assert opts["domain-name-servers"] == "192.168.20.1"
    assert opts["routers"] == "192.168.20.254"
    assert doc["option-def"][0]["code"] == 150
