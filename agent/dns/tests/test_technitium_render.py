"""Technitium agent driver unit tests. No live daemon required.

Two layers here:

* Pure helpers (param builders, name qualification, SVCB parsing) —
  straight assertions.
* The API-driven paths (``apply_record_op``, ``_reconcile_zones``,
  ``_call``'s auth recovery). Those are the ones carrying the real
  semantics — rrset REPLACE-vs-append, TTL convergence, stale-token
  re-provisioning — so they are exercised here against a fake
  ``_request`` rather than left to the manual docker-compose run. The
  wire shapes they assert against (``{"status": "ok"|"error"|
  "invalid-token"}`` at HTTP 200, ``rData`` on record GETs) were
  confirmed empirically against a real ``technitium/dns-server``
  container — see the driver module docstring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from spatium_dns_agent.drivers.technitium import (
    TechnitiumDriver,
    _normalize_rdata,
    _qualified_name,
    _record_params,
    _svcb_params,
    _tsig_key_names,
    _zone_options_payload,
)


class _FakeResponse:
    """Stand-in for ``httpx.Response`` — the driver only ever calls
    ``.json()`` on it."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


def _install_fake_request(
    driver: TechnitiumDriver,
    responder: Any,
) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Replace ``_request`` with a recorder. Returns the call log as
    ``(token, method, path, params)`` tuples."""
    calls: list[tuple[str, str, str, dict[str, Any]]] = []

    def _fake(token: str, method: str, path: str, params: dict[str, Any]):
        calls.append((token, method, path, dict(params)))
        return _FakeResponse(responder(path, params, len(calls)))

    driver._request = _fake  # type: ignore[method-assign]
    return calls


def _seed_token(driver: TechnitiumDriver, token: str = "tok-1") -> None:
    driver._write_secret(driver._api_token_path(), token)


def test_qualified_name_apex() -> None:
    assert _qualified_name("example.com", "@") == "example.com"
    assert _qualified_name("example.com", "") == "example.com"
    assert _qualified_name("example.com.", "example.com.") == "example.com"


def test_qualified_name_subdomain() -> None:
    assert _qualified_name("example.com", "www") == "www.example.com"
    assert _qualified_name("example.com", "_sip._tcp") == "_sip._tcp.example.com"


def test_record_params_a_aaaa() -> None:
    assert _record_params("A", "10.0.0.1", {}) == {"ipAddress": "10.0.0.1"}
    assert _record_params("AAAA", "2001:db8::1", {}) == {"ipAddress": "2001:db8::1"}


def test_record_params_cname_dname_ns_ptr() -> None:
    assert _record_params("CNAME", "www.example.com.", {}) == {"cname": "www.example.com"}
    assert _record_params("DNAME", "other.example.net.", {}) == {"dname": "other.example.net"}
    assert _record_params("NS", "ns1.example.com.", {}) == {"nameServer": "ns1.example.com"}
    assert _record_params("PTR", "host.example.com.", {}) == {"ptrName": "host.example.com"}


def test_record_params_mx_uses_priority() -> None:
    rec = {"priority": 20}
    assert _record_params("MX", "mail.example.com.", rec) == {
        "exchange": "mail.example.com",
        "preference": 20,
    }


def test_record_params_mx_default_priority() -> None:
    assert _record_params("MX", "mail.example.com.", {})["preference"] == 10


def test_record_params_srv() -> None:
    rec = {"priority": 10, "weight": 20, "port": 5060}
    assert _record_params("SRV", "sip.example.com.", rec) == {
        "target": "sip.example.com",
        "priority": 10,
        "weight": 20,
        "port": 5060,
    }


def test_record_params_txt() -> None:
    assert _record_params("TXT", "v=spf1 -all", {}) == {"text": "v=spf1 -all"}


def test_record_params_caa() -> None:
    out = _record_params("CAA", '0 issue "letsencrypt.org"', {})
    assert out == {"flags": 0, "tag": "issue", "value": "letsencrypt.org"}


def test_record_params_tlsa() -> None:
    out = _record_params("TLSA", "3 1 1 abcd1234", {})
    assert out == {
        "tlsaCertificateUsage": "3",
        "tlsaSelector": "1",
        "tlsaMatchingType": "1",
        "tlsaCertificateAssociationData": "abcd1234",
    }


def test_record_params_sshfp() -> None:
    out = _record_params("SSHFP", "4 2 abcd1234", {})
    assert out == {
        "sshfpAlgorithm": "4",
        "sshfpFingerprintType": "2",
        "sshfpFingerprint": "abcd1234",
    }


def test_record_params_naptr() -> None:
    value = '100 10 U "E2U+sip" "!^.*$!sip:info@example.com!" .'
    out = _record_params("NAPTR", value, {})
    assert out["naptrOrder"] == "100"
    assert out["naptrPreference"] == "10"
    assert out["naptrFlags"] == "U"
    assert out["naptrServices"] == "E2U+sip"
    assert out["naptrReplacement"] == "."


def test_record_params_uri() -> None:
    out = _record_params("URI", "1 1 https://example.com", {})
    assert out == {"uriPriority": "1", "uriWeight": "1", "uri": "https://example.com"}


def test_svcb_params_single_value() -> None:
    priority, target, params = _svcb_params('1 . alpn="h2"')
    assert priority == 1
    assert target == "."
    assert params == "alpn|h2"


def test_svcb_params_no_params() -> None:
    priority, target, params = _svcb_params("1 svc.example.com.")
    assert priority == 1
    # Root dot stripped — the daemon stores the target un-dotted. See
    # test_svcb_target_root_dot_is_stripped.
    assert target == "svc.example.com"
    assert params == ""


def test_record_params_svcb() -> None:
    out = _record_params("SVCB", '1 . alpn="h2"', {})
    assert out == {"svcPriority": 1, "svcTargetName": ".", "svcParams": "alpn|h2"}


def test_record_params_https_no_params_omits_svcparams() -> None:
    out = _record_params("HTTPS", "1 .", {})
    assert out == {"svcPriority": 1, "svcTargetName": "."}


def test_admin_bootstrap_password_persists(tmp_path: Path) -> None:
    d = TechnitiumDriver(state_dir=tmp_path)
    pw1 = d.admin_bootstrap_password()
    pw2 = d.admin_bootstrap_password()
    assert pw1 == pw2
    path = tmp_path / "technitium-admin-password"
    assert path.exists()
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_render_skips_daemon_managed_apex_types(tmp_path: Path) -> None:
    d = TechnitiumDriver(state_dir=tmp_path)
    bundle = {
        "zones": [
            {
                "name": "example.com.",
                "type": "primary",
                "ttl": 3600,
                "records": [
                    {"name": "@", "type": "SOA", "value": "ignored"},
                    {"name": "@", "type": "NS", "value": "ns1.example.com."},
                    {"name": "sub", "type": "NS", "value": "ns2.example.com."},
                    {"name": "www", "type": "A", "value": "10.0.0.1", "ttl": 300},
                ],
            }
        ]
    }
    d.render(bundle)
    import json

    payload = json.loads((tmp_path / "rendered.new" / "zones.json").read_text())
    zone = payload[0]
    types_by_domain = {(r["domain"], r["type"]) for r in zone["records"]}
    # Apex SOA + apex NS are daemon-managed — excluded.
    assert ("example.com", "SOA") not in types_by_domain
    assert ("example.com", "NS") not in types_by_domain
    # Off-apex NS (delegation) and ordinary records pass through.
    assert ("sub.example.com", "NS") in types_by_domain
    assert ("www.example.com", "A") in types_by_domain


def test_render_skips_non_primary_zones(tmp_path: Path) -> None:
    d = TechnitiumDriver(state_dir=tmp_path)
    bundle = {
        "zones": [
            {"name": "secondary.example.com.", "type": "secondary", "records": []},
        ]
    }
    d.render(bundle)
    import json

    payload = json.loads((tmp_path / "rendered.new" / "zones.json").read_text())
    assert payload == []


# ── apply_record_op: rrset REPLACE vs append ────────────────────────────


def test_apply_record_op_update_replaces_rrset(tmp_path: Path) -> None:
    """A record edit must REPLACE the rrset, not append to it.

    Regression guard: with ``overwrite=false`` an edited A record leaves
    the daemon serving the OLD and NEW addresses side by side, and a
    TTL-only edit comes back "already exists" and is swallowed. Neither
    self-heals — record CRUD bumps the bundle ``etag`` but not
    ``structural_etag``, so the full-zone reconcile never fires on a
    record edit. Matches bind9's ``upd.replace`` / PowerDNS's rrset
    REPLACE.
    """
    d = TechnitiumDriver(state_dir=tmp_path)
    _seed_token(d)
    calls = _install_fake_request(d, lambda *_: {"status": "ok"})

    d.apply_record_op(
        {
            "zone_name": "example.com.",
            "op": "update",
            "record": {"name": "www", "type": "A", "value": "10.0.0.2", "ttl": 300},
        }
    )

    _, method, path, params = calls[0]
    assert method == "POST"
    assert path == "zones/records/add"
    assert params["overwrite"] == "true"
    assert params["domain"] == "www.example.com"
    assert params["ipAddress"] == "10.0.0.2"
    assert params["ttl"] == 300


def test_apply_record_op_create_replaces_rrset(tmp_path: Path) -> None:
    d = TechnitiumDriver(state_dir=tmp_path)
    _seed_token(d)
    calls = _install_fake_request(d, lambda *_: {"status": "ok"})

    d.apply_record_op(
        {
            "zone_name": "example.com.",
            "op": "create",
            "record": {"name": "www", "type": "A", "value": "10.0.0.1", "ttl": 300},
        }
    )
    assert calls[0][3]["overwrite"] == "true"


def test_apply_record_op_honours_rrset_action_add(tmp_path: Path) -> None:
    """DNS pools set ``rrset_action="add"`` because N A records share one
    name there — a REPLACE would clobber siblings on every member add.
    Same override bind9's driver honours."""
    d = TechnitiumDriver(state_dir=tmp_path)
    _seed_token(d)
    calls = _install_fake_request(d, lambda *_: {"status": "ok"})

    d.apply_record_op(
        {
            "zone_name": "example.com.",
            "op": "create",
            "record": {
                "name": "pool",
                "type": "A",
                "value": "10.0.0.7",
                "ttl": 60,
                "rrset_action": "add",
            },
        }
    )
    assert calls[0][3]["overwrite"] == "false"


def test_apply_record_op_delete_sends_no_overwrite(tmp_path: Path) -> None:
    d = TechnitiumDriver(state_dir=tmp_path)
    _seed_token(d)
    calls = _install_fake_request(d, lambda *_: {"status": "ok"})

    d.apply_record_op(
        {
            "zone_name": "example.com.",
            "op": "delete",
            "record": {"name": "www", "type": "A", "value": "10.0.0.1"},
        }
    )
    _, _, path, params = calls[0]
    assert path == "zones/records/delete"
    assert "overwrite" not in params
    assert "ttl" not in params


# ── _call: stale-token recovery ─────────────────────────────────────────


def test_call_reprovisions_on_invalid_token(tmp_path: Path) -> None:
    """The state dir (token) and /etc/dns (daemon admin account) are
    separate volumes and can desync. An ``invalid-token`` answer must
    re-provision and retry, not wedge the agent forever."""
    d = TechnitiumDriver(state_dir=tmp_path)
    _seed_token(d, "stale-token")

    def _responder(path: str, params: dict[str, Any], n: int) -> dict[str, Any]:
        return {"status": "invalid-token"} if n == 1 else {"status": "ok"}

    calls = _install_fake_request(d, _responder)
    d._create_api_token = lambda: (  # type: ignore[method-assign]
        d._write_secret(d._api_token_path(), "fresh-token") or "fresh-token"
    )

    resp = d._call("stale-token", "POST", "zones/create", {"zone": "example.com"})

    assert resp.json()["status"] == "ok"
    assert [c[0] for c in calls] == ["stale-token", "fresh-token"]
    assert d._api_token_path().read_text().strip() == "fresh-token"


def test_reprovision_reuses_token_another_call_already_refreshed(
    tmp_path: Path,
) -> None:
    """Several calls in one reconcile pass hold the same stale token in a
    local. Only the first should mint a replacement — ``createToken`` is
    not idempotent, so the rest would orphan tokens server-side."""
    d = TechnitiumDriver(state_dir=tmp_path)
    _seed_token(d, "already-fresh")
    minted: list[str] = []

    def _boom() -> str:
        minted.append("x")
        return "should-not-happen"

    d._create_api_token = _boom  # type: ignore[method-assign]

    assert d._reprovision_token("stale-token") == "already-fresh"
    assert minted == []


# ── _reconcile_zones: TTL convergence ───────────────────────────────────


def _reconcile_with(
    driver: TechnitiumDriver,
    existing: list[dict[str, Any]],
    desired: list[dict[str, Any]],
) -> list[tuple[str, str, str, dict[str, Any]]]:
    def _responder(path: str, params: dict[str, Any], n: int) -> dict[str, Any]:
        if path == "zones/records/get":
            return {"status": "ok", "response": {"records": existing}}
        return {"status": "ok"}

    calls = _install_fake_request(driver, _responder)
    driver._reconcile_zones(
        "tok-1", [{"zone": "example.com", "type": "Primary", "records": desired}]
    )
    return calls


def test_reconcile_converges_ttl_only_change(tmp_path: Path) -> None:
    """TTL participates in the record fingerprint — it is a real
    desired-state change and this is the only path that can converge it
    (the incremental op path never sees the old value)."""
    d = TechnitiumDriver(state_dir=tmp_path)
    calls = _reconcile_with(
        d,
        existing=[
            {"name": "www.example.com", "type": "A", "ttl": 3600,
             "rData": {"ipAddress": "10.0.0.1"}}
        ],
        desired=[
            {"domain": "www.example.com", "type": "A", "ttl": 300,
             "ipAddress": "10.0.0.1"}
        ],
    )
    paths = [c[2] for c in calls]
    assert "zones/records/delete" in paths
    added = [c[3] for c in calls if c[2] == "zones/records/add"]
    assert len(added) == 1
    assert added[0]["ttl"] == 300


def test_reconcile_is_a_noop_when_already_in_sync(tmp_path: Path) -> None:
    """No churn on an unchanged zone — the failure mode this guards is a
    delete+add of every record on every single pass."""
    d = TechnitiumDriver(state_dir=tmp_path)
    calls = _reconcile_with(
        d,
        existing=[
            {"name": "www.example.com", "type": "A", "ttl": 300,
             "rData": {"ipAddress": "10.0.0.1"}}
        ],
        desired=[
            {"domain": "www.example.com", "type": "A", "ttl": 300,
             "ipAddress": "10.0.0.1"}
        ],
    )
    assert [c[2] for c in calls if c[2] != "zones/create"] == ["zones/records/get"]


def test_reconcile_tolerates_numeric_vs_string_rdata(tmp_path: Path) -> None:
    """``_record_params`` renders SSHFP/TLSA/NAPTR/URI fields as strings
    while the daemon returns some of them as numbers. Without
    normalisation every such record reads as "changed" forever."""
    d = TechnitiumDriver(state_dir=tmp_path)
    calls = _reconcile_with(
        d,
        existing=[
            {"name": "example.com", "type": "MX", "ttl": 3600,
             "rData": {"exchange": "mail.example.com", "preference": 10}}
        ],
        desired=[
            {"domain": "example.com", "type": "MX", "ttl": 3600,
             "exchange": "mail.example.com", "preference": "10"}
        ],
    )
    assert [c[2] for c in calls if c[2] != "zones/create"] == ["zones/records/get"]


def test_reconcile_never_deletes_daemon_managed_apex(tmp_path: Path) -> None:
    """Technitium auto-creates SOA + one apex NS on zone create. Deleting
    them because the bundle doesn't list them would break the zone."""
    d = TechnitiumDriver(state_dir=tmp_path)
    calls = _reconcile_with(
        d,
        existing=[
            {"name": "example.com", "type": "SOA", "ttl": 3600, "rData": {}},
            {"name": "example.com", "type": "NS", "ttl": 3600,
             "rData": {"nameServer": "technitium.local"}},
        ],
        desired=[],
    )
    assert [c[2] for c in calls if c[2] == "zones/records/delete"] == []


# ── rData read-back normalisation ───────────────────────────────────────
#
# Technitium's record GET does not echo the params its record ADD takes:
# it renames keys for NAPTR/URI, translates TLSA/SSHFP numeric fields to
# enum NAMES, returns svcParams as a dict, and mutates a few values. Every
# expectation below was captured from a live technitium/dns-server:15.4.0.


def test_normalize_rdata_tlsa_enum_names() -> None:
    out = _normalize_rdata(
        "TLSA",
        {
            "certificateUsage": "DANE-EE",
            "selector": "SPKI",
            "matchingType": "SHA2-256",
            "certificateAssociationData": "ABCD",
        },
    )
    assert out == {
        "tlsaCertificateUsage": "3",
        "tlsaSelector": "1",
        "tlsaMatchingType": "1",
        "tlsaCertificateAssociationData": "abcd",
    }


def test_normalize_rdata_sshfp_enum_names() -> None:
    out = _normalize_rdata(
        "SSHFP",
        {"algorithm": "Ed25519", "fingerprintType": "SHA256", "fingerprint": "AB12"},
    )
    assert out == {
        "sshfpAlgorithm": "4",
        "sshfpFingerprintType": "2",
        "sshfpFingerprint": "ab12",
    }


def test_normalize_rdata_unknown_enum_passes_through() -> None:
    """Technitium echoes an algorithm it has no name for back as its own
    number. A future enum member must degrade to a comparison mismatch on
    one record, never a KeyError that kills the whole reconcile."""
    out = _normalize_rdata("SSHFP", {"algorithm": "5"})
    assert out == {"sshfpAlgorithm": "5"}


def test_normalize_rdata_naptr_and_uri_key_renames() -> None:
    assert _normalize_rdata(
        "NAPTR",
        {"order": 100, "preference": 10, "flags": "U", "services": "E2U+sip",
         "regexp": "!x!", "replacement": "."},
    ) == {
        "naptrOrder": 100, "naptrPreference": 10, "naptrFlags": "U",
        "naptrServices": "E2U+sip", "naptrRegexp": "!x!", "naptrReplacement": ".",
    }
    # Technitium appends "/" to a bare-authority URI when it stores it.
    assert _normalize_rdata(
        "URI", {"priority": 1, "weight": 1, "uri": "https://example.test/"}
    ) == {"uriPriority": 1, "uriWeight": 1, "uri": "https://example.test"}


def test_normalize_rdata_svcb_params_dict_and_apex_target() -> None:
    out = _normalize_rdata(
        "SVCB", {"svcPriority": 1, "svcTargetName": "", "svcParams": {"alpn": "h2,h3"}}
    )
    assert out == {"svcPriority": 1, "svcTargetName": ".", "svcParams": "alpn|h2,h3"}


def test_normalize_rdata_leaves_matching_types_alone() -> None:
    for rtype, rdata in [
        ("A", {"ipAddress": "10.0.0.1"}),
        ("MX", {"exchange": "mail.example.test", "preference": 10}),
        ("SRV", {"priority": 10, "weight": 20, "port": 5060, "target": "s.example.test"}),
        ("CAA", {"flags": 0, "tag": "issue", "value": "letsencrypt.org"}),
    ]:
        assert _normalize_rdata(rtype, rdata) == rdata


def test_reconcile_no_churn_on_enum_translated_types() -> None:
    """The bug this guards: desired TLSA params never equalled the
    daemon's enum-name rData, so every pass issued a delete built from
    key names the API rejects, then re-added the record. Forever."""
    desired = _record_params("TLSA", "3 1 1 " + "AB" * 32, {})
    from_daemon = _normalize_rdata(
        "TLSA",
        {
            "certificateUsage": "DANE-EE",
            "selector": "SPKI",
            "matchingType": "SHA2-256",
            "certificateAssociationData": ("AB" * 32).upper(),
        },
    )
    assert desired == from_daemon


# ── SVCB multi-value params (issue #745) ────────────────────────────────


def test_svcb_multivalue_param_is_preserved() -> None:
    """Technitium accepts a comma-joined value for a single param
    (`alpn|h2,h3`) — verified live. What it rejects is splitting into
    separate pairs. So nothing needs truncating."""
    _, _, params = _svcb_params('1 . alpn="h2,h3"')
    assert params == "alpn|h2,h3"


def test_svcb_target_root_dot_is_stripped() -> None:
    """The daemon stores the target un-dotted; leaving the root dot on
    made every SVCB/HTTPS record read as changed on every reconcile."""
    _, target, _ = _svcb_params('1 svc.example.test. alpn="h3"')
    assert target == "svc.example.test"
    # A bare apex target must survive as "." rather than becoming "".
    assert _svcb_params("1 .")[1] == "."


# ── Zone types, transfer options, TSIG, catalog (issue #743) ────────────


def _zone_bundle(**over):
    base = {
        "options": {"allow_transfer": ["none"]},
        "tsig_keys": [],
        "zones": [],
    }
    base.update(over)
    return base


def test_render_emits_every_supported_zone_type(tmp_path: Path) -> None:
    d = TechnitiumDriver(state_dir=tmp_path)
    d.render(
        _zone_bundle(
            zones=[
                {"name": "p.test.", "type": "primary", "records": []},
                {"name": "s.test.", "type": "secondary", "masters": ["192.0.2.1"]},
                {"name": "st.test.", "type": "stub", "masters": ["192.0.2.1"]},
                {"name": "f.test.", "type": "forward", "forwarders": ["8.8.8.8"]},
            ]
        )
    )
    import json as _json

    payload = _json.loads((tmp_path / "rendered.new" / "zones.json").read_text())
    assert {z["zone"]: z["type"] for z in payload} == {
        "p.test": "Primary",
        "s.test": "Secondary",
        "st.test": "Stub",
        "f.test": "Forwarder",
    }


def test_render_skips_zones_that_cannot_be_created(tmp_path: Path) -> None:
    """A secondary/stub with no primary to transfer from, or a forward zone
    with no upstream, cannot be created at all — Technitium rejects the
    call. Skip them rather than fail the whole render."""
    d = TechnitiumDriver(state_dir=tmp_path)
    d.render(
        _zone_bundle(
            zones=[
                {"name": "nomaster.test.", "type": "secondary", "masters": []},
                {"name": "nofwd.test.", "type": "forward", "forwarders": []},
                {"name": "bogus.test.", "type": "not-a-zone-type"},
                {"name": "ok.test.", "type": "primary", "records": []},
            ]
        )
    )
    import json as _json

    payload = _json.loads((tmp_path / "rendered.new" / "zones.json").read_text())
    assert [z["zone"] for z in payload] == ["ok.test"]


def test_render_omits_records_for_non_primary_zones(tmp_path: Path) -> None:
    """A secondary fills itself from the transfer. Rendering records for it
    would make the reconciler delete what the daemon just pulled down."""
    d = TechnitiumDriver(state_dir=tmp_path)
    d.render(
        _zone_bundle(
            zones=[
                {
                    "name": "s.test.",
                    "type": "secondary",
                    "masters": ["192.0.2.1"],
                    "records": [{"name": "www", "type": "A", "value": "10.0.0.1"}],
                }
            ]
        )
    )
    import json as _json

    payload = _json.loads((tmp_path / "rendered.new" / "zones.json").read_text())
    assert payload[0]["records"] == []


def test_zone_options_transfer_policy_mapping() -> None:
    assert _zone_options_payload("Primary", _zone_bundle())["zoneTransfer"] == "Deny"
    assert (
        _zone_options_payload("Primary", _zone_bundle(options={"allow_transfer": []}))[
            "zoneTransfer"
        ]
        == "Deny"
    )
    assert (
        _zone_options_payload(
            "Primary", _zone_bundle(options={"allow_transfer": ["any"]})
        )["zoneTransfer"]
        == "Allow"
    )
    acl = _zone_options_payload(
        "Primary", _zone_bundle(options={"allow_transfer": ["10.0.0.0/8"]})
    )
    assert acl["zoneTransfer"] == "UseSpecifiedNetworkACL"
    assert acl["zoneTransferNetworkACL"] == ["10.0.0.0/8"]


def test_zone_options_only_for_primary() -> None:
    """A secondary transfers IN. Whether it re-serves is a separate
    decision we don't make on the operator's behalf."""
    for ztype in ("Secondary", "Stub", "Forwarder"):
        assert _zone_options_payload(ztype, _zone_bundle()) == {}


def test_tsig_key_names_only_attached_when_transfer_permitted() -> None:
    """Pinning key names onto a Deny zone reads as if signed transfer were
    enabled when nothing can transfer at all."""
    keys = [{"name": "k1.", "secret": "s", "algorithm": "hmac-sha256"}]
    denied = _zone_options_payload("Primary", _zone_bundle(tsig_keys=keys))
    assert denied["zoneTransfer"] == "Deny"
    assert "zoneTransferTsigKeyNames" not in denied

    allowed = _zone_options_payload(
        "Primary", _zone_bundle(tsig_keys=keys, options={"allow_transfer": ["any"]})
    )
    assert allowed["zoneTransferTsigKeyNames"] == ["k1"]


def test_tsig_key_names_strip_root_dot() -> None:
    """Technitium stores names un-dotted, so a dotted name would compare
    as different forever."""
    assert _tsig_key_names(
        {"tsig_keys": [{"name": "a."}, {"name": "b"}, {"name": ""}]}
    ) == ["a", "b"]


def test_ensure_zone_exists_sends_type_specific_params(tmp_path: Path) -> None:
    d = TechnitiumDriver(state_dir=tmp_path)
    calls = _install_fake_request(d, lambda *_: {"status": "ok"})

    d._ensure_zone_exists("t", {"zone": "s.test", "type": "Secondary",
                                "masters": ["192.0.2.1", "192.0.2.2"]})
    assert calls[-1][3]["primaryNameServerAddresses"] == "192.0.2.1,192.0.2.2"

    d._ensure_zone_exists("t", {"zone": "f.test", "type": "Forwarder",
                                "forwarders": ["8.8.8.8", "9.9.9.9"]})
    # Technitium's Forwarder zone takes ONE upstream; extras are dropped
    # with a warning rather than silently.
    assert calls[-1][3]["forwarder"] == "8.8.8.8"
    assert "forwarders" not in calls[-1][3]


def test_apply_zone_options_refuses_unknown_transfer_value(tmp_path: Path) -> None:
    """zones/options/set answers ok for a value it doesn't recognise and
    keeps the OLD one — so an unvalidated typo silently leaves transfer at
    whatever it was, which for a zone meant to be locked down is a security
    regression no log line would report."""
    d = TechnitiumDriver(state_dir=tmp_path)
    calls = _install_fake_request(d, lambda *_: {"status": "ok"})
    d._apply_zone_options("t", "z.test", {"zoneTransfer": "Bogus"})
    assert calls == []


def test_apply_zone_options_joins_list_values(tmp_path: Path) -> None:
    d = TechnitiumDriver(state_dir=tmp_path)
    calls = _install_fake_request(d, lambda *_: {"status": "ok"})
    d._apply_zone_options(
        "t",
        "z.test",
        {
            "zoneTransfer": "UseSpecifiedNetworkACL",
            "zoneTransferNetworkACL": ["10.0.0.0/8", "192.168.0.0/16"],
            "zoneTransferTsigKeyNames": ["k1"],
        },
    )
    params = calls[0][3]
    assert params["zoneTransferNetworkACL"] == "10.0.0.0/8,192.168.0.0/16"
    assert params["zoneTransferTsigKeyNames"] == "k1"


def test_sync_tsig_keys_wire_format(tmp_path: Path) -> None:
    """FLAT pipe-delimited token list read in triples — name|secret|alg|…
    Not JSON, not one pipe-joined record per key, and not name|alg|secret
    (that one fails with "TSIG algorithm is not supported", because it
    reads the secret as the algorithm)."""
    d = TechnitiumDriver(state_dir=tmp_path)
    calls = _install_fake_request(d, lambda *_: {"status": "ok"})
    d._sync_tsig_keys(
        "t",
        [
            {"name": "k1.", "secret": "s1", "algorithm": "hmac-sha256"},
            {"name": "k2", "secret": "s2", "algorithm": "HMAC-SHA512"},
        ],
    )
    assert calls[0][2] == "settings/set"
    assert calls[0][3]["tsigKeys"] == "k1|s1|hmac-sha256|k2|s2|hmac-sha512"


def test_sync_tsig_keys_drops_unsupported_algorithm(tmp_path: Path) -> None:
    d = TechnitiumDriver(state_dir=tmp_path)
    calls = _install_fake_request(d, lambda *_: {"status": "ok"})
    d._sync_tsig_keys(
        "t",
        [
            {"name": "bad", "secret": "s", "algorithm": "hmac-sha3"},
            {"name": "good", "secret": "s", "algorithm": "hmac-sha256"},
        ],
    )
    assert calls[0][3]["tsigKeys"] == "good|s|hmac-sha256"


def test_get_zone_records_filters_daemon_managed_apex(tmp_path: Path) -> None:
    """Apex NS/SOA are stamped by the daemon at zone create. Left in, the
    apex NS lands in to_delete every pass, gets skipped, and is still
    counted — reporting a deletion that never happened."""
    d = TechnitiumDriver(state_dir=tmp_path)
    _install_fake_request(
        d,
        lambda *_: {
            "status": "ok",
            "response": {
                "records": [
                    {"name": "z.test", "type": "SOA", "ttl": 900, "rData": {}},
                    {"name": "z.test", "type": "NS", "ttl": 3600,
                     "rData": {"nameServer": "self."}},
                    {"name": "sub.z.test", "type": "NS", "ttl": 3600,
                     "rData": {"nameServer": "ns.other."}},
                    {"name": "www.z.test", "type": "A", "ttl": 300,
                     "rData": {"ipAddress": "10.0.0.1"}},
                ]
            },
        },
    )
    got = {(r["domain"], r["type"]) for r in d._get_zone_records("t", "z.test")}
    assert ("z.test", "SOA") not in got
    assert ("z.test", "NS") not in got
    # Off-apex NS is a real delegation and must survive.
    assert ("sub.z.test", "NS") in got
    assert ("www.z.test", "A") in got
