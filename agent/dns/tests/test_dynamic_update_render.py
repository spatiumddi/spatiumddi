"""BIND9 agent dynamic-update ACL rendering + AXFR parsing (issue #641).

Covers the two pure helpers behind the feature: ``_render_allow_update``
(builds the coarse ``allow-update`` clause, always keeping the group
loopback grant) and ``parse_axfr`` (turns ``dig +noall +answer AXFR``
output into record dicts, dropping daemon-owned RRs).
"""

from __future__ import annotations

from spatium_dns_agent.drivers.bind9 import _render_allow_update
from spatium_dns_agent.ingest import _saw_soa, parse_axfr


def test_allow_update_group_key_only_when_disabled() -> None:
    # Dynamic updates off ⇒ just the internal loopback grant.
    zone = {"dynamic_update_enabled": False, "update_acl": []}
    out = _render_allow_update(zone, "spatium-loop.")
    assert out.strip() == 'allow-update { key "spatium-loop."; };'


def test_allow_update_no_clause_without_group_key_or_grants() -> None:
    zone = {"dynamic_update_enabled": False, "update_acl": []}
    assert _render_allow_update(zone, None) == ""


def test_allow_update_mixes_ip_and_tsig_and_keeps_group_key() -> None:
    zone = {
        "dynamic_update_enabled": True,
        "update_acl": [
            {"action": "grant", "match_kind": "ip", "ip_cidr": "10.0.0.0/24"},
            {
                "action": "grant",
                "match_kind": "tsig_key",
                "tsig_key_name": "dc01-ddns.",
            },
        ],
    }
    out = _render_allow_update(zone, "spatium-loop.")
    assert 'key "spatium-loop.";' in out
    assert "10.0.0.0/24;" in out
    assert 'key "dc01-ddns.";' in out
    assert out.startswith("allow-update { ") and out.rstrip().endswith("};")


def test_allow_update_skips_deny_and_name_scoped_entries() -> None:
    # deny + name-scoped are the P2 update-policy path; the coarse render
    # must not emit them (belt-and-braces — the control plane blocks them).
    zone = {
        "dynamic_update_enabled": True,
        "update_acl": [
            {"action": "deny", "match_kind": "ip", "ip_cidr": "10.9.9.0/24"},
            {
                "action": "grant",
                "match_kind": "tsig_key",
                "tsig_key_name": "scoped.",
                "name_scope": "subdomain",
            },
            {"action": "grant", "match_kind": "ip", "ip_cidr": "192.0.2.0/24"},
        ],
    }
    out = _render_allow_update(zone, None)
    assert "10.9.9.0/24" not in out  # deny dropped
    assert "scoped." not in out  # name-scoped dropped
    assert "192.0.2.0/24;" in out  # plain grant kept


def test_allow_update_dedupes_group_key() -> None:
    zone = {
        "dynamic_update_enabled": True,
        "update_acl": [
            {
                "action": "grant",
                "match_kind": "tsig_key",
                "tsig_key_name": "spatium-loop.",
            }
        ],
    }
    out = _render_allow_update(zone, "spatium-loop.")
    assert out.count('key "spatium-loop.";') == 1


def test_parse_axfr_drops_soa_apex_ns_and_splits_mx_srv() -> None:
    sample = (
        "example.com.\t3600\tIN\tSOA\tns1.example.com. admin.example.com. 1 2 3 4 5\n"
        "example.com.\t3600\tIN\tNS\tns1.example.com.\n"
        "dc01.example.com.\t1200\tIN\tA\t10.0.0.5\n"
        "_ldap._tcp.example.com.\t3600\tIN\tSRV\t0 100 389 dc01.example.com.\n"
        "example.com.\t3600\tIN\tMX\t10 mx1.example.com.\n"
    )
    recs = parse_axfr(sample, "example.com.")
    by_name = {(r["name"], r["record_type"]): r for r in recs}
    # SOA + apex NS dropped.
    assert ("@", "SOA") not in by_name
    assert ("@", "NS") not in by_name
    assert by_name[("dc01", "A")]["value"] == "10.0.0.5"
    srv = by_name[("_ldap._tcp", "SRV")]
    assert (srv["priority"], srv["weight"], srv["port"]) == (0, 100, 389)
    assert srv["value"] == "dc01.example.com."
    mx = (
        by_name[("example.com", "MX")]
        if ("example.com", "MX") in by_name
        else by_name[("@", "MX")]
    )
    assert mx["priority"] == 10 and mx["value"] == "mx1.example.com."


def test_saw_soa_detects_successful_transfer() -> None:
    # A real (even minimal) AXFR carries the apex SOA.
    good = "example.com.\t3600\tIN\tSOA\tns1. admin. 1 2 3 4 5\n"
    assert _saw_soa(good) is True


def test_saw_soa_false_on_failed_transfer_output() -> None:
    # dig +noall suppresses the "; Transfer failed." comment, so a refused
    # transfer yields empty/near-empty output with no SOA — must read as
    # failure so the worker skips (never ships an empty set that would wipe
    # every external mirror).
    assert _saw_soa("") is False
    assert _saw_soa("\n  \n") is False
