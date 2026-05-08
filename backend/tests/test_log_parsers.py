"""Unit tests for the BIND9 + Kea log line parsers.

Pure-function tests — no DB, no fixtures. Each test exercises one
real-world line shape we want to make sure stays parseable.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.logs.bind9_parser import parse_query_line
from app.services.logs.kea_parser import parse_kea_line
from app.services.logs.pdns_parser import parse_query_line as parse_pdns_query_line

# ── BIND9 query log ───────────────────────────────────────────────────


def test_bind9_classic_query_line_v4() -> None:
    line = (
        "25-Apr-2026 16:30:01.123 client @0x7f8b1c001234 192.0.2.5#54321 "
        "(example.com): query: example.com IN A +E(0)K (10.0.0.1)"
    )
    parsed = parse_query_line(line)
    assert parsed is not None
    assert parsed.client_ip == "192.0.2.5"
    assert parsed.client_port == 54321
    assert parsed.qname == "example.com"
    assert parsed.qclass == "IN"
    assert parsed.qtype == "A"
    assert parsed.flags == "+E(0)K"
    assert parsed.view is None
    assert parsed.ts.year == 2026
    assert parsed.ts.month == 4
    assert parsed.ts.day == 25
    assert parsed.ts.tzinfo is not None
    assert parsed.raw == line


def test_bind9_classic_query_line_v6() -> None:
    line = (
        "01-Jan-2026 00:00:00.000 client @0x... 2001:db8::dead#34567 "
        "(foo.bar): query: foo.bar IN AAAA + (2001:db8::1)"
    )
    parsed = parse_query_line(line)
    assert parsed is not None
    assert parsed.client_ip == "2001:db8::dead"
    assert parsed.client_port == 34567
    assert parsed.qname == "foo.bar"
    assert parsed.qtype == "AAAA"


def test_bind9_query_with_view() -> None:
    # Real BIND9 with views renders ``view <name>:`` (no parens) in
    # place of the bare ``:``. We tolerate both shapes.
    line = (
        "25-Apr-2026 16:30:02.000 client @0x... 192.0.2.5#10000 "
        "(internal.example) view internal: query: internal.example IN A + (10.0.0.1)"
    )
    parsed = parse_query_line(line)
    assert parsed is not None
    assert parsed.view == "internal"
    assert parsed.qname == "internal.example"


def test_bind9_no_timestamp_uses_fallback() -> None:
    line = "client @0x... 192.0.2.5#1234 (foo): query: foo IN A + (10.0.0.1)"
    fallback = datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC)
    parsed = parse_query_line(line, fallback_ts=fallback)
    assert parsed is not None
    assert parsed.ts == fallback
    assert parsed.qname == "foo"


def test_bind9_unparseable_line_preserves_raw() -> None:
    line = "25-Apr-2026 16:30:01.123 some random bind log we don't recognise"
    parsed = parse_query_line(line)
    assert parsed is not None
    assert parsed.qname is None
    assert parsed.client_ip is None
    assert parsed.raw == line
    # Timestamp still extracted.
    assert parsed.ts.year == 2026


def test_bind9_empty_line_returns_none() -> None:
    assert parse_query_line("") is None
    assert parse_query_line("   \n") is None


def test_bind9_iso_timestamp_variant() -> None:
    line = (
        "2026-04-25T16:30:01.500Z client @0x... 10.1.2.3#5555 "
        "(host.lan): query: host.lan IN A + (10.0.0.1)"
    )
    parsed = parse_query_line(line)
    assert parsed is not None
    assert parsed.ts.year == 2026
    assert parsed.qname == "host.lan"


# ── Kea DHCPv4 ─────────────────────────────────────────────────────────


def test_kea_lease_alloc_line() -> None:
    line = (
        "2026-04-25 16:30:01.123 INFO  [kea-dhcp4.leases/12345.139] "
        "DHCP4_LEASE_ALLOC [hwtype=1 aa:bb:cc:dd:ee:ff], cid=[no info], "
        "tid=0x12345678: lease 192.0.2.10 has been allocated for 3600 seconds"
    )
    parsed = parse_kea_line(line)
    assert parsed is not None
    assert parsed.severity == "INFO"
    assert parsed.code == "DHCP4_LEASE_ALLOC"
    assert parsed.mac_address == "aa:bb:cc:dd:ee:ff"
    assert parsed.ip_address == "192.0.2.10"
    assert parsed.transaction_id == "12345678"
    assert parsed.ts.year == 2026


def test_kea_dhcpdiscover_line() -> None:
    line = (
        "2026-04-25 16:31:00.000 DEBUG [kea-dhcp4.packets/12345.140] "
        "DHCP4_PACKET_PROCESS_STARTED tid=0xdeadbeef: started processing of packet from "
        "client [hwtype=1 11:22:33:44:55:66]"
    )
    parsed = parse_kea_line(line)
    assert parsed is not None
    assert parsed.severity == "DEBUG"
    assert parsed.code == "DHCP4_PACKET_PROCESS_STARTED"
    assert parsed.mac_address == "11:22:33:44:55:66"
    assert parsed.ip_address is None
    assert parsed.transaction_id == "deadbeef"


def test_kea_decline_line() -> None:
    line = (
        "2026-04-25 16:32:00.000 WARN  [kea-dhcp4.leases/12345.141] "
        "DHCP4_LEASE_DECLINE [hwtype=1 aa:bb:cc:11:22:33], "
        "tid=0x42: address 192.0.2.99 declined"
    )
    parsed = parse_kea_line(line)
    assert parsed is not None
    assert parsed.severity == "WARN"
    assert parsed.code == "DHCP4_LEASE_DECLINE"
    assert parsed.ip_address == "192.0.2.99"


def test_kea_unparseable_line_preserves_raw() -> None:
    line = "totally unstructured kea log line"
    parsed = parse_kea_line(line)
    assert parsed is not None
    assert parsed.code is None
    assert parsed.severity is None
    assert parsed.raw == line


def test_kea_empty_line_returns_none() -> None:
    assert parse_kea_line("") is None
    assert parse_kea_line("\n") is None


def test_kea_uses_fallback_when_no_match() -> None:
    line = "fragment with no header"
    fallback = datetime(2026, 4, 25, 0, 0, 0, tzinfo=UTC)
    parsed = parse_kea_line(line, fallback_ts=fallback)
    assert parsed is not None
    assert parsed.ts == fallback


# ── PowerDNS query log ────────────────────────────────────────────────


def test_pdns_v4_with_port() -> None:
    line = (
        "May 08 02:11:22 Remote 192.0.2.5:54321 wants 'www.example.com|A', "
        "do = 0, bufsize = 4096: 1 RR(s)"
    )
    parsed = parse_pdns_query_line(line)
    assert parsed is not None
    assert parsed.client_ip == "192.0.2.5"
    assert parsed.client_port == 54321
    assert parsed.qname == "www.example.com"
    assert parsed.qtype == "A"
    assert parsed.qclass == "IN"


def test_pdns_v4_no_port() -> None:
    line = (
        "May 08 02:11:22 Remote 127.0.0.1 wants 'foo.example.com|AAAA', "
        "do = 1, bufsize = 4096: packetcache HIT"
    )
    parsed = parse_pdns_query_line(line)
    assert parsed is not None
    assert parsed.client_ip == "127.0.0.1"
    assert parsed.client_port is None
    assert parsed.qname == "foo.example.com"
    assert parsed.qtype == "AAAA"


def test_pdns_v6_bracketed_with_port() -> None:
    line = (
        "May 08 02:11:22 Remote [2001:db8::1]:54321 wants 'v6.example|MX', "
        "do = 0, bufsize = 4096: 2 RR(s)"
    )
    parsed = parse_pdns_query_line(line)
    assert parsed is not None
    assert parsed.client_ip == "2001:db8::1"
    assert parsed.client_port == 54321
    assert parsed.qname == "v6.example"
    assert parsed.qtype == "MX"


def test_pdns_v6_bracketed_no_port() -> None:
    line = (
        "May 08 02:11:22 Remote [2001:db8::1] wants 'v6no.example|TXT', "
        "do = 0, bufsize = 4096: 0 RR(s)"
    )
    parsed = parse_pdns_query_line(line)
    assert parsed is not None
    assert parsed.client_ip == "2001:db8::1"
    assert parsed.client_port is None


def test_pdns_v6_bare_no_port() -> None:
    # Some pdns builds emit IPv6 sources without brackets when no
    # port follows. ``2001:db8::1`` must not be misparsed as
    # ``2001:db8:`` + port=1.
    line = (
        "May 08 02:11:22 Remote 2001:db8::1 wants 'bare-v6.example|TXT', "
        "do = 0, bufsize = 4096: 0 RR(s)"
    )
    parsed = parse_pdns_query_line(line)
    assert parsed is not None
    assert parsed.client_ip == "2001:db8::1"
    assert parsed.client_port is None
    assert parsed.qname == "bare-v6.example"


def test_pdns_chaos_probe_is_dropped() -> None:
    # CHAOS-class server-id probes are noise — must be dropped at
    # parse time so they never hit the DB.
    for qname in ("id.server", "version.bind", "version.server", "hostname.bind"):
        line = (
            f"May 08 02:11:22 Remote 127.0.0.1 wants '{qname}|TXT', "
            "do = 0, bufsize = 4096: 1 RR(s)"
        )
        assert parse_pdns_query_line(line) is None, f"expected drop for {qname}"


def test_pdns_banner_preserves_raw() -> None:
    # Non-query line (no ``wants '`` separator) returns a raw-only
    # ParsedQueryLine so the storage filter can drop it.
    line = "May 08 02:11:22 [main] Auth-only powerdns server starting"
    parsed = parse_pdns_query_line(line)
    assert parsed is not None
    assert parsed.qname is None
    assert parsed.client_ip is None
    assert parsed.raw == line


def test_pdns_empty_line_returns_none() -> None:
    assert parse_pdns_query_line("") is None
    assert parse_pdns_query_line("   \n") is None


def test_pdns_redos_regression_linear_time() -> None:
    # CodeQL alert #40 (py/polynomial-redos): the previous parser
    # combined ``[^\]]+``, an alternation fallback, and a lazy
    # ``[0-9a-fA-F.:]+?`` that ``re.search`` could retry from every
    # starting position. On adversarial input shaped like
    # ``Remote [\\\\…\\\\`` the regex engine took O(n²) time.
    # The new parser splits on the hard `` wants '`` separator and
    # walks the head with a token split, so a pathological input
    # is handled in linear time.
    import time

    adversary = "Remote [" + "\\" * 4000
    t0 = time.perf_counter()
    for _ in range(500):
        parse_pdns_query_line(adversary)
    elapsed = time.perf_counter() - t0
    # 500 iterations of a 4 KB line should finish in well under a
    # second on any reasonable CPU. The pre-fix code took multiple
    # seconds. Pin a generous bound so this test isn't flaky on
    # slow CI runners while still catching any future regression
    # that reintroduces the polynomial backtracking.
    assert elapsed < 2.0, f"adversarial parse took {elapsed:.2f}s — possible ReDoS regression"
