"""#59 — packet-capture validator + argv-builder unit tests.

Pure functions, no DB / no tcpdump. The headline control is the BPF
filter: it must admit real BPF (byte-offset + bitmask idioms) while
rejecting shell metacharacters, and the built argv must carry the filter
as a single trailing element (never shell-interpolated).
"""

from __future__ import annotations

import pytest

from app.services.pcap.runner import (
    HARD_MAX_BYTES,
    HARD_MAX_DURATION_S,
    HARD_MAX_PACKETS,
    PcapArgError,
    build_pcap_argv,
    clamp_caps,
    validate_bpf_filter,
    validate_interface,
)

# ── BPF filter ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "good",
    [
        "",
        "port 53",
        "host 10.0.0.1 and tcp",
        "port 67 or port 68",
        "tcp[tcpflags] & tcp-syn != 0",
        "ip[6:2] & 0x1fff != 0",
        "ether[0] & 1 = 1",
        "vlan 100 and host 10.0.0.1",
        "not port 22",
        "proto 112",
    ],
)
def test_validate_bpf_accepts_legal_expressions(good: str) -> None:
    # Empty → None; otherwise round-trips trimmed.
    out = validate_bpf_filter(good)
    assert out == (good or None)


@pytest.mark.parametrize(
    "bad",
    [
        "port 80; rm -rf /",
        "host $(whoami)",
        "host `id`",
        'host "x"',
        "host 'x'",
        "a\nb",
        "host {1}",
        "port 80 # comment",
        "port 80 \\ esc",
    ],
)
def test_validate_bpf_rejects_shell_metacharacters(bad: str) -> None:
    # `;` `$` backtick quotes newline `{}` `#` `\` are rejected. Note `&`
    # and `|` are NOT rejected — they're legal BPF bitwise operators and
    # harmless because the filter is a single non-shell argv element (a
    # bogus `&& reboot` just fails tcpdump's BPF parser, never a shell).
    with pytest.raises(PcapArgError):
        validate_bpf_filter(bad)


def test_validate_bpf_rejects_overlong() -> None:
    with pytest.raises(PcapArgError):
        validate_bpf_filter("a" * 1025)


# ── interface ───────────────────────────────────────────────────────


def test_validate_interface_defaults_to_any() -> None:
    assert validate_interface(None, available=["any", "eth0"]) == "any"
    assert validate_interface("  ", available=["any", "eth0"]) == "any"


def test_validate_interface_accepts_enumerated() -> None:
    assert validate_interface("eth0", available=["any", "eth0"]) == "eth0"


def test_validate_interface_rejects_unknown() -> None:
    with pytest.raises(PcapArgError):
        validate_interface("eth9", available=["any", "eth0"])


def test_validate_interface_rejects_bad_charset() -> None:
    with pytest.raises(PcapArgError):
        validate_interface("eth0; rm", available=["any", "eth0", "eth0; rm"])


# ── caps ────────────────────────────────────────────────────────────


def test_clamp_caps_requires_a_stop_condition() -> None:
    with pytest.raises(PcapArgError):
        clamp_caps(max_packets=None, max_duration_s=None, max_bytes=None, snaplen=256)


def test_clamp_caps_clamps_to_hard_maxima() -> None:
    mp, md, mb, sl = clamp_caps(
        max_packets=99_999_999,
        max_duration_s=99_999,
        max_bytes=10 * HARD_MAX_BYTES,
        snaplen=999_999,
    )
    assert mp == HARD_MAX_PACKETS
    assert md == HARD_MAX_DURATION_S
    assert mb == HARD_MAX_BYTES
    assert sl == 65535


def test_clamp_caps_passes_through_reasonable_values() -> None:
    mp, md, mb, sl = clamp_caps(max_packets=100, max_duration_s=30, max_bytes=1024, snaplen=256)
    assert (mp, md, mb, sl) == (100, 30, 1024, 256)


# ── argv ────────────────────────────────────────────────────────────


def test_build_argv_filter_is_single_trailing_element() -> None:
    argv = build_pcap_argv(
        interface="eth0",
        bpf_filter="port 53 or port 67",
        snaplen=256,
        promiscuous=False,
        max_packets=100,
        output_path="/tmp/x.pcap",
    )
    # The whole filter is ONE element (not split on spaces) and it's last.
    assert argv[-1] == "port 53 or port 67"
    assert argv[0] == "tcpdump"
    assert "-w" in argv and "/tmp/x.pcap" in argv
    assert "-p" in argv  # non-promiscuous opt-out
    assert argv[argv.index("-c") + 1] == "100"


def test_build_argv_omits_filter_when_empty() -> None:
    argv = build_pcap_argv(
        interface="any",
        bpf_filter=None,
        snaplen=0,
        promiscuous=True,
        max_packets=None,
        output_path="/tmp/x.pcap",
    )
    assert "-p" not in argv  # promiscuous requested → no opt-out flag
    assert "-c" not in argv
    # Last element is the output path (no trailing filter).
    assert argv[-2] == "-w"
    assert argv[-1] == "/tmp/x.pcap"
