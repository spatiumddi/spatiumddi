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
    _qualified_name,
    _record_params,
    _svcb_params,
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
    assert target == "svc.example.com."
    assert params == ""


def test_svcb_params_multivalue_truncates_first() -> None:
    priority, target, params = _svcb_params('1 . alpn="h2,h3"')
    assert params == "alpn|h2"


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
