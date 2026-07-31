"""The Web UI must be reachable before the supervisor exists (#769).

#285 Phase 6 made the supervisor-rendered ``spatium-role.nft``
authoritative for 80/443 and removed the accept from the base
``/etc/nftables.conf``. But the supervisor can only render that drop-in
after its first successful heartbeat, and on a control-plane appliance
that heartbeat goes to the LOCAL api — so the port could not open until
the api was already serving.

Measured on a clean install of ``dev-f3ca07f2-d796``: boot 20:57:02, api
``Ready=True`` 20:59:39, drop-in applied 21:00:20. The Web UI port opened
41 s AFTER the control plane was up, so an operator browsing a booting
box got a ~3-minute connection timeout and the "SpatiumDDI is
initialising" page (#767 / #299) was unreachable for the whole window it
exists for.

``00-spatium-webui.nft`` is baked into the image to close that gap, and
is retired by the host runner once the operator configures a source
scope — its unconditional accept sorts earlier in the include glob than
the scoped rule, and nftables accepts on first match.

HOW TO RUN (from the repo root):
    python3 -m pytest appliance/tests/test_firewall_webui_sentinel.py -v

No appliance, no nftables, no root — this reads the shipped files.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NFT_DIR = REPO / "appliance/mkosi.extra/etc/nftables.d"
SENTINEL = NFT_DIR / "00-spatium-webui.nft"
K3S_SENTINEL = NFT_DIR / "00-spatium-k3s-bootstrap.nft"
RELOAD = REPO / "appliance/mkosi.extra/usr/local/bin/spatium-firewall-reload"
REVERT = REPO / "appliance/mkosi.extra/usr/local/bin/spatiumddi-firewall-revert"
BASE_CONF = REPO / "appliance/mkosi.extra/etc/nftables.conf"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_sentinel_is_baked_and_opens_web_ports() -> None:
    assert SENTINEL.is_file(), "the Web UI would be unreachable during a cold boot"
    rules = [
        ln.strip()
        for ln in _text(SENTINEL).splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert rules == ['tcp dport { 80, 443 } accept comment "web-ui-bootstrap"'], rules


def test_sentinel_sorts_before_the_supervisor_drop_in() -> None:
    """Glob order is load-bearing: first match wins in nftables.

    The sentinel must be reached BEFORE ``spatium-role.nft`` for the
    cold-boot open to take effect at all.
    """
    names = sorted(p.name for p in NFT_DIR.glob("*.nft"))
    assert names[0].startswith("00-"), names
    assert SENTINEL.name < "spatium-role.nft"


def test_sentinel_is_a_bare_rule_like_its_sibling() -> None:
    """The include glob sits INSIDE ``chain input`` — no table/chain wrapper."""
    for path in (SENTINEL, K3S_SENTINEL):
        rules = [
            ln.strip()
            for ln in _text(path).splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        assert rules, f"{path.name} has no rule lines"
        for rule in rules:
            assert not rule.startswith(("table ", "chain ")), rule
            assert "{" not in rule.split("dport")[0], rule


def test_base_conf_still_does_not_open_web_ports() -> None:
    """The sentinel is the mechanism, not a revert of #285 Phase 6.

    If the base conf opened 80/443 again, ``web_ui_allowed_cidrs`` would
    be unenforceable no matter what the drop-in or the sentinel say.
    """
    for line in _text(BASE_CONF).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not re.search(r"dport\s*\{?[^}]*\b(80|443)\b", stripped), stripped


def test_reload_runner_manages_both_sentinels() -> None:
    body = _text(RELOAD)
    assert "00-spatium-webui.nft" in body
    # Both sentinels go through the one directive helper — a second
    # hand-rolled copy is how the two drift apart.
    assert body.count("apply_sentinel_directive()") == 1
    calls = re.findall(r"^apply_sentinel_directive .+$", body, re.M)
    assert len(calls) == 2, calls
    assert any("spatium-bootstrap" in c for c in calls)
    assert any("spatium-webui" in c for c in calls)


def test_reload_runner_snapshots_webui_state_for_revert() -> None:
    """A test-apply that retires the sentinel must be revertible.

    Otherwise the auto-revert — whose entire job is undoing a lockout —
    would restore the drop-in but leave the Web UI narrower than before.
    """
    assert "LAST_GOOD_WEBUI_SENTINEL" in _text(RELOAD)


def test_revert_runner_restores_webui_sentinel() -> None:
    body = _text(REVERT)
    assert "LAST_GOOD_WEBUI_SENTINEL" in body
    calls = re.findall(r"^restore_sentinel_state .+$", body, re.M)
    assert len(calls) == 2, calls
    assert any("WEBUI" in c for c in calls)
    # The snapshot must be cleared alongside the k3s one, or a stale file
    # would drive the next revert.
    assert 'rm -f "$LAST_GOOD_SENTINEL" "$LAST_GOOD_WEBUI_SENTINEL"' in body
