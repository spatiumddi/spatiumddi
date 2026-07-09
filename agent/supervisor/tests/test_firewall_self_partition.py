"""The firewall must never partition a live etcd member (#593).

The per-role nftables drop-in was rendered purely from the control plane's row
(``cluster_role``). Observed live on a 3-node appliance: ddi2's row had gone
``cluster_role = NULL`` after a failed re-join, while ddi2 was still a voting
etcd member. The supervisor rendered an agent firewall with no ``k3s-peer``
rule, and ddi2's own nftables dropped its peers' inbound raft traffic — the
peers logged ``dial tcp 192.168.0.133:2380: i/o timeout`` every 5 s against a
member that was up the whole time.

The row bug is fixed (#591). The COUPLING is what these tests pin: any
row/reality divergence — stuck heartbeat, control plane restored from an older
backup, half-landed promote, operator clearing state with the #591 escape
hatch — must not be able to close etcd's peer port on a node that k3s still
calls an etcd member.

Two defences, both covered here:
  1. recover a peer set from live membership when the row supplies none
  2. refuse to apply any body that would close 2380 on a live etcd member

And the counter-property, which matters just as much: a plain agent node (or a
node whose apiserver is unreadable) must still get its firewall updated. A
guard that fails closed would brick every non-etcd appliance.

    python3 -m pytest agent/supervisor/tests/test_firewall_self_partition.py -v
"""

from __future__ import annotations

import pytest

from spatium_supervisor import firewall_peer_audit as fpa

# A real agent-profile body: mgmt + role ports, NO k3s-peer rule. This is what
# got applied to ddi2 and partitioned it.
AGENT_BODY = """\
# ── Base management ────────────────────────────────────────
tcp dport 22 accept comment "mgmt-ssh"
tcp dport { 80, 443 } accept comment "web-ui"

# ── Per-role service ports ─────────────────────────────
udp dport 53 accept comment "role:dns-only"
tcp dport 53 accept comment "role:dns-only"
"""

# A control-plane body: carries the peer-scoped etcd/kubelet accept.
CP_BODY = """\
# ── Base management ────────────────────────────────────────
tcp dport 22 accept comment "mgmt-ssh"

# ── Control-plane derived (peer-scoped, #272/#285) ─────
ip saddr { 192.168.0.199, 192.168.0.125 } tcp dport { 2379, 2380, 10250 } \
accept comment "k3s-peer"
ip saddr { 10.42.0.0/16 } tcp dport 6443 accept comment "k3s-api"
"""


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """The membership answer is cached process-globally; leaking it across
    cases would make each test depend on the order of the previous one."""
    fpa._reset_etcd_member_cache()
    yield
    fpa._reset_etcd_member_cache()


# ── body_opens_etcd_peers ───────────────────────────────────────────────────


def test_cp_body_is_recognised_as_opening_the_peer_port() -> None:
    assert fpa.body_opens_etcd_peers(CP_BODY) is True


def test_agent_body_does_not_open_the_peer_port() -> None:
    assert fpa.body_opens_etcd_peers(AGENT_BODY) is False


def test_the_port_named_only_in_a_comment_does_not_count() -> None:
    """The renderer's header block mentions ``2379`` / ``2380`` in prose. A
    naive substring check would read that as "peers are open" and let the
    guard pass on a body that partitions the node."""
    commented = (
        "# etcd 2379 / 2380 + kubelet 10250 scoped to the peer set\n"
        'tcp dport 22 accept comment "mgmt-ssh"\n'
    )
    assert fpa.body_opens_etcd_peers(commented) is False


def test_a_trailing_comment_does_not_fake_an_accept() -> None:
    """Inline comment after a non-accept rule must not smuggle the port in."""
    body = 'tcp dport 22 accept comment "ssh; not 2380"\n'
    assert fpa.body_opens_etcd_peers(body) is False


# ── would_self_partition ────────────────────────────────────────────────────


def test_refuses_an_agent_body_on_a_live_etcd_member(monkeypatch) -> None:
    """THE bug. Row says agent; k3s says etcd member; body closes 2380."""
    monkeypatch.setattr(fpa, "_etcd_membership_uncached", lambda: True)
    assert fpa.would_self_partition(AGENT_BODY) is True


def test_allows_a_cp_body_on_a_live_etcd_member(monkeypatch) -> None:
    monkeypatch.setattr(fpa, "_etcd_membership_uncached", lambda: True)
    assert fpa.would_self_partition(CP_BODY) is False


def test_allows_an_agent_body_on_a_real_agent_node(monkeypatch) -> None:
    """The counter-property: a genuine DNS/DHCP agent appliance is NOT an etcd
    member, and must still receive its (peer-less) firewall."""
    monkeypatch.setattr(fpa, "_etcd_membership_uncached", lambda: False)
    assert fpa.would_self_partition(AGENT_BODY) is False


def test_unknown_membership_never_blocks_an_update(monkeypatch) -> None:
    """``None`` means the kube API was unreadable. Blocking on "don't know"
    would wedge the firewall on every node whose apiserver blips, and on every
    non-k3s deployment kind. Fail open here; the row-driven render is still
    the normal path."""
    monkeypatch.setattr(fpa, "_etcd_membership_uncached", lambda: None)
    assert fpa.would_self_partition(AGENT_BODY) is False


# ── membership probe + cache ────────────────────────────────────────────────


def test_membership_reads_the_local_nodes_own_labels(monkeypatch) -> None:
    monkeypatch.setenv("NODE_NAME", "ddi2")
    seen: dict[str, str] = {}

    def fake_request(method: str, path: str):
        seen["method"], seen["path"] = method, path
        body = '{"metadata":{"labels":{"node-role.kubernetes.io/etcd":"true"}}}'
        return 200, body

    monkeypatch.setattr(fpa.k8s_api, "_request", fake_request)
    assert fpa.local_node_is_etcd_member() is True
    assert seen == {"method": "GET", "path": "/api/v1/nodes/ddi2"}


def test_membership_is_false_when_the_etcd_label_is_absent(monkeypatch) -> None:
    monkeypatch.setenv("NODE_NAME", "agent1")
    monkeypatch.setattr(
        fpa.k8s_api,
        "_request",
        lambda m, p: (200, '{"metadata":{"labels":{"kubernetes.io/os":"linux"}}}'),
    )
    assert fpa.local_node_is_etcd_member() is False


def test_membership_is_unknown_when_the_api_is_unreachable(monkeypatch) -> None:
    monkeypatch.setenv("NODE_NAME", "ddi2")

    def boom(method: str, path: str):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(fpa.k8s_api, "_request", boom)
    assert fpa.local_node_is_etcd_member() is None


def test_membership_is_unknown_without_a_node_name(monkeypatch) -> None:
    monkeypatch.delenv("NODE_NAME", raising=False)
    monkeypatch.delenv("APPLIANCE_HOSTNAME", raising=False)
    assert fpa.local_node_is_etcd_member() is None


def test_a_known_answer_is_cached_but_unknown_is_re_probed(monkeypatch) -> None:
    """A stale ``True`` only delays narrowing after a demote (harmless). A
    cached ``None`` would remember "don't know" through an apiserver blip, so
    it must be re-probed."""
    calls = {"n": 0}

    def probe_true() -> bool:
        calls["n"] += 1
        return True

    monkeypatch.setattr(fpa, "_etcd_membership_uncached", probe_true)
    assert fpa.local_node_is_etcd_member() is True
    assert fpa.local_node_is_etcd_member() is True
    assert calls["n"] == 1, "a known answer must be served from cache"

    fpa._reset_etcd_member_cache()
    calls["n"] = 0

    def probe_none() -> None:
        calls["n"] += 1
        return None

    monkeypatch.setattr(fpa, "_etcd_membership_uncached", probe_none)
    assert fpa.local_node_is_etcd_member() is None
    assert fpa.local_node_is_etcd_member() is None
    assert calls["n"] == 2, "None must never be cached"


# ── observed_peer_cidrs ─────────────────────────────────────────────────────


def test_peer_recovery_excludes_self_and_emits_host_routes(monkeypatch) -> None:
    monkeypatch.setattr(
        fpa,
        "list_control_plane_node_ips",
        lambda: ["192.168.0.199", "192.168.0.133", "192.168.0.125"],
    )
    got = fpa.observed_peer_cidrs(["192.168.0.133"])
    assert got == ["192.168.0.125/32", "192.168.0.199/32"]


def test_peer_recovery_handles_ipv6_prefix_length(monkeypatch) -> None:
    monkeypatch.setattr(fpa, "list_control_plane_node_ips", lambda: ["fd00::2"])
    assert fpa.observed_peer_cidrs([]) == ["fd00::2/128"]


def test_peer_recovery_is_empty_when_membership_is_unreadable(monkeypatch) -> None:
    """Empty, not a guess — the refuse-to-apply guard is what protects us then."""
    monkeypatch.setattr(fpa, "list_control_plane_node_ips", lambda: None)
    assert fpa.observed_peer_cidrs(["192.168.0.133"]) == []


# ── the real _maybe_apply_firewall path ─────────────────────────────────────
#
# The unit tests above pin the predicates. These drive the actual function that
# writes the trigger file the host runner consumes, because that write is what
# partitioned ddi2.


def _wire(monkeypatch, tmp_path, *, is_member, live_members):
    """Point the heartbeat's trigger paths at tmp_path and stub the world."""
    from spatium_supervisor import appliance_state, heartbeat

    trigger = tmp_path / "firewall-pending"
    monkeypatch.setattr(heartbeat, "_NFT_TRIGGER_PATH", trigger)
    monkeypatch.setattr(heartbeat, "_NFT_APPLIED_HASH_PATH", tmp_path / "applied-hash")
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "appliance")
    monkeypatch.setattr(appliance_state, "read_node_ips", lambda: ["192.168.0.133"])
    monkeypatch.setattr(fpa, "_etcd_membership_uncached", lambda: is_member)
    monkeypatch.setattr(fpa, "list_control_plane_node_ips", lambda: live_members)
    monkeypatch.setattr(fpa, "warn_on_peer_drift", lambda *a, **k: None)
    return heartbeat, trigger


def test_a_live_etcd_member_never_writes_a_partitioning_trigger(
    monkeypatch, tmp_path
) -> None:
    """THE #593 scenario end to end. Row says agent (no peers), kube API is
    unreadable so recovery finds nothing, and the rendered body has no peer
    rule. The trigger must NOT be written — the previous ruleset stands."""
    import structlog

    heartbeat, trigger = _wire(monkeypatch, tmp_path, is_member=True, live_members=None)
    heartbeat._maybe_apply_firewall(
        {"roles": ["dns_bind9"]}, structlog.get_logger("t"), []
    )
    assert not trigger.exists(), "wrote a drop-in that would partition this etcd member"


def test_peer_recovery_rescues_the_render_when_membership_is_readable(
    monkeypatch, tmp_path
) -> None:
    """Row still says agent, but the cluster can be read. We recover the peer
    set from live membership, so the rendered body DOES open 2380 and the
    trigger is written normally — the node keeps its firewall current AND
    stays in raft."""
    import structlog

    heartbeat, trigger = _wire(
        monkeypatch,
        tmp_path,
        is_member=True,
        live_members=["192.168.0.199", "192.168.0.133", "192.168.0.125"],
    )
    heartbeat._maybe_apply_firewall(
        {"roles": ["dns_bind9"]}, structlog.get_logger("t"), []
    )
    assert trigger.exists(), "a recoverable peer set should still produce a drop-in"
    body = trigger.read_text()
    assert fpa.body_opens_etcd_peers(body)
    assert "192.168.0.199" in body and "192.168.0.125" in body
    assert "192.168.0.133" not in body.split("k3s-peer")[0], "self must not be a peer"


def test_a_real_agent_node_still_gets_its_firewall(monkeypatch, tmp_path) -> None:
    """The counter-property. A DNS agent appliance is not an etcd member, so the
    peer-less body is correct and must be applied. A guard that failed closed
    here would freeze the firewall on every non-etcd appliance."""
    import structlog

    heartbeat, trigger = _wire(
        monkeypatch, tmp_path, is_member=False, live_members=None
    )
    heartbeat._maybe_apply_firewall(
        {"roles": ["dns_bind9"]}, structlog.get_logger("t"), []
    )
    assert trigger.exists()
    assert not fpa.body_opens_etcd_peers(trigger.read_text())
