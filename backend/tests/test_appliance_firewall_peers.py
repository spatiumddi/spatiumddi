"""Heartbeat derives the firewall peer + data-plane sets (#285 Phase 1).

The control plane computes, per heartbeating node: the family-split
control-plane peer CIDRs (etcd/kubelet/6443 scope), the all-cluster
data-plane peer set (flannel/wireguard floor), and the pod/service CIDRs
(6443 widening). Asymmetric-on-leave keeps a demoting member in the peer
set until it reports ``left``.
"""

from __future__ import annotations

import hashlib
import os
import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appliance import (
    APPLIANCE_STATE_APPROVED,
    CLUSTER_JOIN_STATE_LEAVING,
    CLUSTER_JOIN_STATE_LEFT,
    CLUSTER_ROLE_MEMBER,
    CLUSTER_ROLE_PRIMARY,
    DESIRED_CLUSTER_ROLE_NONE,
    Appliance,
)
from app.models.settings import PlatformSettings
from app.services.appliance.ca import generate_session_token


async def _enable(db: AsyncSession) -> None:
    s = await db.get(PlatformSettings, 1)
    if s is None:
        s = PlatformSettings(id=1)
        db.add(s)
    s.supervisor_registration_enabled = True
    await db.flush()


def _cp(hostname: str, **kw: object) -> Appliance:
    der = os.urandom(32)
    return Appliance(
        id=uuid.uuid4(),
        hostname=hostname,
        public_key_der=der,
        public_key_fingerprint=hashlib.sha256(der).hexdigest(),
        state=APPLIANCE_STATE_APPROVED,
        deployment_kind="appliance",
        **kw,
    )


async def _heartbeat(client: AsyncClient, row: Appliance, token: str) -> dict:
    r = await client.post(
        "/api/v1/appliance/supervisor/heartbeat",
        json={"appliance_id": str(row.id), "session_token": token},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_peer_and_apiserver_sets_family_split(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _enable(db_session)
    token, token_hash = generate_session_token()
    # The heartbeating node (primary) + two members; mixed families.
    primary = _cp(
        "cp-1",
        cluster_role=CLUSTER_ROLE_PRIMARY,
        node_ip="192.168.1.11",
        node_ips=["192.168.1.11"],
        pod_cidr="10.42.0.0/16,2001:cafe:42::/56",
        service_cidr="10.43.0.0/16",
        session_token_hash=token_hash,
    )
    m2 = _cp(
        "cp-2",
        cluster_role=CLUSTER_ROLE_MEMBER,
        node_ip="192.168.1.12",
        node_ips=["192.168.1.12", "2001:db8::12"],
    )
    m3 = _cp("cp-3", cluster_role=CLUSTER_ROLE_MEMBER, node_ip="192.168.1.13", node_ips=[])
    db_session.add_all([primary, m2, m3])
    await db_session.commit()

    body = await _heartbeat(client, primary, token)

    # Peer set = the OTHER two members, family-split (/32 v4, /128 v6),
    # sorted; m3 falls back to node_ip since node_ips is empty.
    assert body["cluster_peer_cidrs"] == [
        "192.168.1.12/32",
        "192.168.1.13/32",
        "2001:db8::12/128",
    ]
    # pod/service CIDR split for the apiserver node.
    assert body["firewall_pod_cidrs"] == ["10.42.0.0/16", "2001:cafe:42::/56"]
    assert body["firewall_service_cidrs"] == ["10.43.0.0/16"]


async def test_asymmetric_on_leave(client: AsyncClient, db_session: AsyncSession) -> None:
    await _enable(db_session)
    token, token_hash = generate_session_token()
    primary = _cp(
        "cp-1",
        cluster_role=CLUSTER_ROLE_PRIMARY,
        node_ip="192.168.1.11",
        node_ips=["192.168.1.11"],
        session_token_hash=token_hash,
    )
    # A member mid-LEAVE: desired=none, but not yet 'left'.
    leaver = _cp(
        "cp-2",
        cluster_role=CLUSTER_ROLE_MEMBER,
        desired_cluster_role=DESIRED_CLUSTER_ROLE_NONE,
        cluster_join_state=CLUSTER_JOIN_STATE_LEAVING,
        node_ip="192.168.1.12",
        node_ips=["192.168.1.12"],
    )
    db_session.add_all([primary, leaver])
    await db_session.commit()

    # Still in the peer set while leaving (etcd member-remove needs it).
    body = await _heartbeat(client, primary, token)
    assert "192.168.1.12/32" in body["cluster_peer_cidrs"]

    # Once it reports 'left', the next render drops it.
    leaver.cluster_join_state = CLUSTER_JOIN_STATE_LEFT
    await db_session.commit()
    body2 = await _heartbeat(client, primary, token)
    assert "192.168.1.12/32" not in body2["cluster_peer_cidrs"]


async def test_non_cp_node_gets_no_peers_no_apiserver(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _enable(db_session)
    token, token_hash = generate_session_token()
    # A plain application appliance (DNS worker) — not a CP node.
    worker = _cp(
        "dns-1",
        appliance_variant="appliance",
        node_ip="192.168.1.20",
        node_ips=["192.168.1.20"],
        pod_cidr="10.42.0.0/16",
        session_token_hash=token_hash,
    )
    db_session.add(worker)
    await db_session.commit()

    body = await _heartbeat(client, worker, token)
    assert body["cluster_peer_cidrs"] == []  # not a CP node
    assert body["firewall_pod_cidrs"] == []  # doesn't run the apiserver
