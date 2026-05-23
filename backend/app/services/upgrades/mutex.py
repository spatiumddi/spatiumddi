"""Cluster-wide single-upgrader mutex (#296 Phase A).

Built on ``coordination.k8s.io/v1/Lease`` — the same primitive Kubernetes
uses internally for controller-manager leader election. We pick this
shape over a DB row because:

* The lease lives in etcd, not Postgres. If CNPG fails over mid-upgrade
  we don't briefly lose the lock.
* Lease expiration is server-side. An api pod that holds the lease then
  crashes loses it after ``leaseDurationSeconds`` without anyone having
  to clean up; whichever pod next renews wins automatically.
* The lease's holder identity is operator-visible via ``kubectl get
  leases`` — a debugging surface that doesn't require app changes.

The lease is **not** the source of truth for the upgrade row in
Postgres; the ``SystemUpgradeRun`` row records what's planned + which
holder started it for audit. The lease just guarantees that at most
one api pod is *driving* the orchestrator at any moment.

Phase A ships the helper; Phase D's orchestrator beat task is what
calls ``acquire()`` / ``renew()`` / ``release()`` while it runs.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from app.services.appliance import k8s

logger = structlog.get_logger(__name__)

# The Lease name + namespace are fixed per cluster — one lease object,
# always in the same place. Lives in the namespace the api pod runs in
# (``kube-system`` for the appliance shape; whatever namespace the
# Helm release was installed into for docker/k8s deployments).
LEASE_NAME = "spatium-upgrade-lock"
# 60 s default matches the upstream k8s leader-election library. The
# orchestrator (Phase D) renews every ``LEASE_DURATION_S / 3`` so two
# missed renewals still leave time before expiration.
LEASE_DURATION_S = 60


@dataclass(frozen=True)
class LeaseState:
    """Operator-visible lease state.

    ``held`` is true when the lease exists + has a non-expired
    ``renewTime``. ``holder`` is whoever wrote the lease last.
    ``transitions`` is k8s's leadership-change counter — a value
    higher than what the orchestrator recorded last means another
    api pod took over since.
    """

    held: bool
    holder: str | None
    renew_time: str | None
    transitions: int
    expired: bool


def _identity() -> str:
    """Stable identity for the api pod making the request.

    k8s convention: ``<pod-name>_<random-uuid>`` for the
    controller-manager. We use the pod hostname (set by k8s to the
    pod name) — sufficient for "which replica holds it" surfacing
    without needing a per-process UUID.
    """
    return os.environ.get("HOSTNAME") or socket.gethostname()


def _parse_lease(body: dict[str, Any] | None) -> LeaseState:
    if not body:
        return LeaseState(
            held=False,
            holder=None,
            renew_time=None,
            transitions=0,
            expired=False,
        )
    spec = body.get("spec") or {}
    holder = spec.get("holderIdentity")
    renew_time = spec.get("renewTime")
    duration = int(spec.get("leaseDurationSeconds") or LEASE_DURATION_S)
    transitions = int(spec.get("leaseTransitions") or 0)
    expired = False
    if renew_time:
        try:
            # ``renewTime`` is RFC3339 UTC, e.g. "2026-05-22T10:00:00Z".
            # ``fromisoformat`` accepts the trailing-Z form on 3.11+.
            renewed = datetime.fromisoformat(renew_time.replace("Z", "+00:00"))
            age = (datetime.now(UTC) - renewed).total_seconds()
            expired = age > duration
        except ValueError:
            # Unparseable timestamp — treat as expired so callers can
            # take over rather than refusing to start forever.
            expired = True
    return LeaseState(
        held=bool(holder) and not expired,
        holder=holder,
        renew_time=renew_time,
        transitions=transitions,
        expired=expired,
    )


def get_state(*, namespace: str | None = None) -> LeaseState:
    """Read the lease's current state without trying to claim it.

    Used by the preflight endpoint to surface "another upgrade is in
    flight" cleanly. Returns the all-false state if the lease doesn't
    exist yet (no upgrade has ever run on this cluster).
    """
    try:
        status, body = k8s.get_lease(LEASE_NAME, namespace=namespace)
    except k8s.KubeapiUnavailableError:
        # On docker-compose deployments the SA isn't mounted; treat as
        # "no lease, no concurrent upgrade" — single-node deployments
        # don't need a cluster-wide lock anyway.
        return LeaseState(
            held=False,
            holder=None,
            renew_time=None,
            transitions=0,
            expired=False,
        )
    if status == 404:
        return LeaseState(
            held=False,
            holder=None,
            renew_time=None,
            transitions=0,
            expired=False,
        )
    if status != 200 or body is None:
        # Treat ambiguity as "held" so we don't race into a second
        # upgrade on a transient kubeapi blip.
        logger.warning("upgrade_lease_read_failed", status=status)
        return LeaseState(
            held=True,
            holder="<unreachable>",
            renew_time=None,
            transitions=0,
            expired=False,
        )
    return _parse_lease(body)


def acquire(*, namespace: str | None = None) -> tuple[bool, str | None]:
    """Acquire the upgrade lease for this api pod.

    Three outcomes:

    1. Lease doesn't exist → ``create_lease`` claims it; return (True, None).
    2. Lease exists + expired → ``update_lease`` bumps transitions
       (takeover); return (True, None) on success.
    3. Lease exists + held by someone else (not expired) → return
       (False, "held by <holder>"). Caller refuses to start.

    Holder identity is this api pod's hostname (see ``_identity``).
    Not idempotent across holders — if this pod already holds it,
    use ``renew()`` instead (cheaper, doesn't increment transitions).
    """
    me = _identity()
    state = get_state(namespace=namespace)
    if not state.held and state.holder is None:
        ok, err = k8s.create_lease(
            LEASE_NAME,
            me,
            namespace=namespace,
            lease_duration_seconds=LEASE_DURATION_S,
        )
        if ok:
            return True, None
        # Race: someone else created it between our read + write.
        # Re-read to surface their identity.
        state = get_state(namespace=namespace)
        if state.held and state.holder != me:
            return False, f"held by {state.holder}"
        # Some other failure (RBAC, kubeapi down) — propagate.
        return False, err
    if state.held and state.holder == me:
        # Already ours; renew rather than re-acquire.
        return renew(namespace=namespace)
    if state.held and state.holder != me:
        return False, f"held by {state.holder}"
    # Expired — take over with a transitions bump.
    ok, err = k8s.update_lease(
        LEASE_NAME,
        me,
        namespace=namespace,
        lease_duration_seconds=LEASE_DURATION_S,
        bump_transitions=True,
        expected_transitions=state.transitions,
    )
    if ok:
        return True, None
    return False, err


def renew(*, namespace: str | None = None) -> tuple[bool, str | None]:
    """Renew a lease we already hold.

    Does NOT bump ``leaseTransitions``. Used by Phase D's orchestrator
    on a heartbeat (every ``LEASE_DURATION_S / 3`` seconds). If renew
    fails because someone else has taken over, the caller should halt
    the in-flight upgrade — they no longer hold the cluster lock.
    """
    me = _identity()
    return k8s.update_lease(
        LEASE_NAME,
        me,
        namespace=namespace,
        lease_duration_seconds=LEASE_DURATION_S,
    )


def release(*, namespace: str | None = None) -> tuple[bool, str | None]:
    """Release the lease we hold by setting an empty holder.

    We don't ``DELETE`` the Lease object because keeping it around
    surfaces the historical "last upgrade ran at <timestamp> by
    <holder>" via ``kubectl get leases`` — operationally useful.
    Empty ``holderIdentity`` + a ``renewTime`` in the past lets the
    next ``acquire()`` claim it cleanly via the expired-takeover
    path.

    On docker-compose / non-k8s deployments (no SA mounted) returns
    (True, None) without making a call — single-instance deploys
    don't have a cluster lock to release.
    """
    if k8s.get_config() is None:
        return True, None
    return k8s.clear_lease_holder(LEASE_NAME, namespace=namespace)
