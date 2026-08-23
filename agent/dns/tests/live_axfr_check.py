#!/usr/bin/env python3
"""Live TSIG-transfer check against a real ``named`` (#734).

Runs INSIDE the built bind9 agent image, where ``named``, ``python3``,
dnspython and the agent package all already exist. Renders a config with
the real :class:`Bind9Driver`, starts ``named`` on it, and asserts that a
zone transfer is permitted **only** when correctly TSIG-signed.

Why this exists as a standalone script rather than a pytest: the image
ships no test framework, and this has to run against the actual artifact
we publish — the agent renderer, the BIND version we pin, and the two
meeting on a real socket. A unit test can only prove we emit the string we
meant to emit; it cannot prove ``named`` reads that string the way we
believe it does.

Why it exists at all: before #734 the repo had **no automated AXFR
coverage anywhere**. ``grep -rl axfr .github/workflows`` returned nothing,
and the one test aimed at it —
``agent/dns/tests/test_acceptance.py::test_helm_chart_primary_secondary_axfr``
— was a permanent ``pytest.skip`` whose docstring claimed coverage lived
in ``agent-e2e.yml``. That workflow only ever ran ``dig version.bind CH
TXT``, a liveness smoke. So the skip stub was asserting a coverage that
did not exist, which is a large part of why #61's drift report could ship
broken against every agent-managed BIND9 and stay broken across two
releases.

Exits non-zero on the first failed expectation, so a CI step can simply
run it. Never skips: a missing prerequisite is a failure, because "the
check quietly didn't run" is the exact failure mode this replaces.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Any valid base64; the peer only has to agree with us about the bytes.
_SECRET = "c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0MDE="
_KEY = "spatium-live-check"
_OTHER_KEY = "operator-second-key"
# Deliberately a dotted, FQDN-shaped name — the form operators actually use
# for a DNSTSIGKey, and the one #920 was reported against.
_OPERATOR_KEY = "tsig-update.operator.example"
_ZONE = "live.example."
_PORT = 15353

_FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{f'  — {detail}' if detail else ''}")
    if not ok:
        _FAILURES.append(label)


def _bundle(tsig_keys: list[dict] | None = None) -> dict:
    return {
        "options": {
            "forwarders": [],
            "recursion_enabled": False,
            "allow_query": ["any"],
            "dnssec_validation": "no",
            # The stock default. Before #734 this denied everything AND the
            # key grant only existed on dynamic zones, so a static zone was
            # unreadable by anyone — which is the bug.
            "allow_transfer": ["none"],
        },
        "tsig_keys": tsig_keys
        if tsig_keys is not None
        else [
            {"name": _KEY, "secret": _SECRET, "algorithm": "hmac-sha256"},
            {"name": _OTHER_KEY, "secret": _SECRET, "algorithm": "hmac-sha256"},
        ],
        "zones": [
            {
                "name": _ZONE,
                "type": "primary",
                "ttl": 3600,
                "serial": 1,
                # Deliberately NOT a dynamic zone: the pre-#734 grant only
                # covered dynamic ones, so a static zone is the regression.
                "dynamic_update_enabled": False,
                "update_acl": [],
                "allow_transfer": None,
                "records": [
                    {"name": "www", "type": "A", "value": "192.0.2.1", "ttl": 300},
                    {"name": "mail", "type": "A", "value": "192.0.2.2", "ttl": 300},
                ],
            }
        ],
    }


def _wait_for_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _wait_for_port_free(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                time.sleep(0.2)
        except OSError:
            return


def _xfr(keyname: str | None, secret: str = _SECRET, algorithm: str = "hmac-sha256"):
    """Attempt an AXFR. Returns (ok, detail)."""
    import dns.name
    import dns.query
    import dns.tsig
    import dns.zone

    kwargs = {}
    if keyname is not None:
        kn = dns.name.from_text(keyname)
        algo = dns.name.from_text(algorithm)
        kwargs = {
            "keyring": {kn: dns.tsig.Key(kn, secret, algorithm=algo)},
            "keyname": kn,
            "keyalgorithm": algo,
        }
    try:
        z = dns.zone.from_xfr(
            dns.query.xfr(
                "127.0.0.1", dns.name.from_text(_ZONE), port=_PORT, timeout=10, **kwargs
            )
        )
        return True, f"{len(list(z.nodes))} nodes"
    except Exception as exc:  # noqa: BLE001 — every failure mode is a result here
        return False, type(exc).__name__


def _render(bundle: dict) -> tuple[Path, Path]:
    """Render ``bundle`` into a fresh state dir. Returns (state, named.conf)."""
    from spatium_dns_agent.drivers.bind9 import Bind9Driver  # noqa: PLC0415

    state = Path(tempfile.mkdtemp(prefix="axfr-check-"))
    Bind9Driver(state_dir=state).render(bundle)
    # ``render`` stages into ``rendered.new`` while the zone-file paths it
    # writes into named.conf point at the promoted ``rendered`` directory —
    # the agent renames one to the other before starting the daemon. Do the
    # same, or named loads a config whose every zone file is missing and
    # then REFUSES transfers for not being authoritative, which looks
    # exactly like an ACL failure and is not one.
    rendered = state / "rendered"
    (state / "rendered.new").rename(rendered)
    conf = rendered / "named.conf"
    text = conf.read_text()

    # #920: the key include is derived from state_dir, so it already points at
    # the file render() just wrote. Assert that rather than rewriting it — a
    # hardcoded path here would send named to a DIFFERENT install's key file,
    # which passes named-checkconf and then fails every transfer BADKEY.
    expected_include = f'include "{state / "tsig" / "ddns.key"}";'
    check(
        "key include points at this render's own state dir",
        expected_include in text,
        expected_include,
    )

    # named needs a writable working directory it owns.
    conf.write_text(text.replace('directory "/var/cache/bind";', f'directory "{state}";'))
    return state, conf


def _run_case(label: str, bundle: dict, expectations) -> None:
    """Start named on ``bundle`` and run ``expectations`` against it."""
    print(f"\n=== {label} ===")
    state, conf = _render(bundle)
    text = conf.read_text()
    for line in text.splitlines():
        if "allow-transfer" in line:
            print(f"    {line.strip()[:120]}")

    proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
        [shutil.which("named") or "named", "-c", str(conf), "-f", "-g", "-p", str(_PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        if not _wait_for_port(_PORT):
            proc.terminate()
            out = proc.communicate(timeout=10)[0]
            check(f"{label}: named listened on {_PORT}", False, out[-2000:])
            return
        expectations()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        # named holds the port briefly after exit; the next case rebinds it.
        _wait_for_port_free(_PORT)
        shutil.rmtree(state, ignore_errors=True)


def main() -> int:
    if not shutil.which("named"):
        print("FAIL: `named` not on PATH — this must run inside the bind9 agent image")
        return 1
    try:
        import spatium_dns_agent.drivers.bind9  # noqa: F401,PLC0415
    except ImportError as exc:
        print(f"FAIL: agent package not importable ({exc})")
        return 1

    def group_key_expectations() -> None:
        ok, detail = _xfr(_KEY)
        check("signed with the group key returns the zone", ok, detail)

        ok, detail = _xfr(_OTHER_KEY)
        check("signed with a second granted key also works", ok, detail)

        # The regression itself. Pre-#734 this was the ONLY outcome, for
        # every zone, because the grant existed nowhere a static zone saw.
        ok, detail = _xfr(None)
        check("unsigned is REFUSED", not ok, detail)

        ok, detail = _xfr(_KEY, secret="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        check("wrong secret is rejected", not ok, detail)

        ok, detail = _xfr(_KEY, algorithm="hmac-sha512")
        check("wrong algorithm is rejected", not ok, detail)

        ok, detail = _xfr("never-granted-key")
        check("a key the server never granted is rejected", not ok, detail)

    def operator_only_expectations() -> None:
        ok, detail = _xfr(_OPERATOR_KEY)
        check("operator-only: signed with the operator key returns the zone", ok, detail)

        ok, detail = _xfr(None)
        check("operator-only: unsigned is REFUSED", not ok, detail)

        # The distinction #920 turns on. A key named in named.conf answers
        # BADSIG for a wrong secret; a key named NOWHERE answers BADKEY. So
        # "wrong secret is rejected" passing here is what proves the operator
        # key was actually rendered, rather than the transfer failing for the
        # unrelated reason that named has never heard of it.
        ok, detail = _xfr(_OPERATOR_KEY, secret="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        check("operator-only: wrong secret is rejected", not ok, detail)

    _run_case("group legacy key + operator key", _bundle(), group_key_expectations)

    # #920 — a group whose ONLY TSIG material is an operator DNSTSIGKey row.
    # This shape arises when a server is adopted into an existing group rather
    # than direct-registered, since only registration auto-mints the legacy
    # group key. ``tsig_keys[0]`` is then an operator key, which is the head
    # the control plane's ``resolve_group_transfer_key`` also picks.
    _run_case(
        "operator key only (no legacy group key)",
        _bundle([{"name": _OPERATOR_KEY, "secret": _SECRET, "algorithm": "hmac-sha256"}]),
        operator_only_expectations,
    )

    if _FAILURES:
        print(f"\n{len(_FAILURES)} expectation(s) failed: {', '.join(_FAILURES)}")
        return 1
    print("\nAll transfer expectations held.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
