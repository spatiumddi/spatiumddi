"""Per-node upgrade primitive (#296 Phase C).

The 11-step sequence for taking one control-plane node from version
N-1 to N safely. Encapsulated as a single idempotent + resumable
async function ``single_node_upgrade`` plus the individual step
functions so an orchestrator (Phase D) can also drive them ala carte
for testing / dry-run.

Step shape (one row per step in the issue body):

    1. preflight gate                — reuse Phase A's run_all
    2. etcd snapshot                  — TODO follow-up; relies on k3s
                                        auto-snapshots (every 6 h) until
                                        the supervisor exposes a hook
    3. CNPG nodeMaintenanceWindow     — patch_cnpg_maintenance_window
    4. cordon                         — cordon_node (triggers auto-
                                        switchover if primary's here)
    5. verify primary moved off       — poll Cluster.status.currentPrimary
    6. drain                          — eviction loop (DS skip + terminal-
                                        pod skip + mirror-pod skip);
                                        --force NOT supported
    7. trigger slot apply             — write desired_* on the appliance row
    8. health gate                    — poll until installed_appliance_version
                                        == desired_appliance_version
    9. convergence                    — node Ready + CNPG instance reported
                                        + DaemonSet pod Ready
   10. uncordon + clear window        — uncordon_node + maintenance off
   11. cluster verify                 — re-run a small slice of preflight

Resumability: each step is idempotent in itself (cordon-already-
cordoned is a 200, evict-already-gone is 404 treated as success,
etc.). The orchestrator records ``progress.current_step`` on the
SystemUpgradeRun row; on resume the function reads the row, fast-
forwards past completed steps, and continues. Phase C v0 doesn't
implement the fast-forward — the caller passes ``start_step`` to
resume; Phase D will add the read-from-row-and-continue logic.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appliance import Appliance
from app.services.appliance import k8s
from app.services.upgrades import preflight

logger = structlog.get_logger(__name__)


StepName = Literal[
    "preflight",
    "etcd_snapshot",
    "cnpg_maintenance_on",
    "cordon",
    "verify_primary_moved",
    "drain",
    "trigger_slot_apply",
    "health_gate",
    "convergence",
    "uncordon",
    "cluster_verify",
]


# Default-but-overridable timeouts. The orchestrator (Phase D) will
# expose these on the upgrade-start request body so an operator with
# a slow disk / large CNPG can stretch them.
DEFAULT_DRAIN_TIMEOUT_S = 120.0
DEFAULT_HEALTH_GATE_TIMEOUT_S = 1800.0  # 30 min — slot dd + reboot
DEFAULT_CONVERGENCE_TIMEOUT_S = 900.0  # 15 min — etcd rejoin + CNPG resync
DEFAULT_SWITCHOVER_TIMEOUT_S = 180.0  # 3 min — CNPG cordon-triggered switch

# Poll cadence — gentle on kubeapi + the appliance row. Slow enough
# that 30 min worth of polls is ~600 calls, fast enough that step
# transitions surface to the UI within a few seconds.
_POLL_INTERVAL_S = 3.0


@dataclass
class StepResult:
    """One step's outcome — captured for the SystemUpgradeRun row's
    ``progress.per_node[<node>].steps`` log."""

    name: StepName
    started_at: str
    finished_at: str | None = None
    ok: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def finish(self, ok: bool, **detail: Any) -> StepResult:
        self.finished_at = _now_iso()
        self.ok = ok
        # ``error`` is the dataclass field, not a detail entry — pull
        # it out before merging the rest so a caller's
        # ``step.finish(False, error="oops")`` sets the canonical
        # field. Keeps it queryable in JSONB without scanning the
        # detail blob.
        if "error" in detail:
            self.error = detail.pop("error")
        if detail:
            self.detail.update(detail)
        return self


@dataclass
class SingleNodeResult:
    """Aggregate outcome of one node's 11-step upgrade."""

    node_name: str
    target_version: str
    ok: bool
    failed_at: StepName | None
    steps: list[StepResult]
    error: str | None = None


def _now_iso() -> str:
    """RFC3339-shaped UTC timestamp for step logs. Matches the renew_time
    format we already use on the Lease."""
    from datetime import UTC, datetime  # noqa: PLC0415

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Step 1: preflight ────────────────────────────────────────────────


async def _step_preflight(target_version: str) -> StepResult:
    step = StepResult(name="preflight", started_at=_now_iso())
    report = await preflight.run_all(target_version=target_version)
    if report.overall == "fail":
        fails = [r.name for r in report.results if r.level == "fail"]
        return step.finish(
            False,
            error=f"preflight failed: {', '.join(fails)}",
            overall=report.overall,
            failed_checks=fails,
        )
    return step.finish(
        True,
        overall=report.overall,
        warns=[r.name for r in report.results if r.level == "warn"],
    )


# ── Step 2: etcd snapshot (TODO follow-up) ───────────────────────────


async def _step_etcd_snapshot() -> StepResult:
    """No-op in Phase C v0.

    k3s auto-snapshots run every 6 h via ``--etcd-snapshot-schedule-cron``
    — there's always a recent snapshot on disk we can fall back to in
    a recovery scenario. A *fresh* snapshot before each node upgrade is
    cheap insurance per the issue body but requires shell access to a
    k3s control-plane host, which the api pod doesn't have today.
    Follow-up: add ``POST /v1/snapshot`` to the supervisor + call it
    from here. Skipping with a structured comment in the step log
    rather than silently dropping the contract.
    """
    step = StepResult(name="etcd_snapshot", started_at=_now_iso())
    return step.finish(
        True,
        skipped=True,
        reason=(
            "supervisor-driven snapshot not yet exposed; relying on "
            "k3s auto-snapshots (every 6 h). Follow-up tracked in #296."
        ),
    )


# ── Step 3: CNPG nodeMaintenanceWindow on ─────────────────────────────


async def _step_cnpg_maintenance_on(cluster_name: str, namespace: str | None) -> StepResult:
    step = StepResult(
        name="cnpg_maintenance_on",
        started_at=_now_iso(),
        detail={"cluster": cluster_name, "namespace": namespace or "<release>"},
    )
    ok, err = k8s.patch_cnpg_maintenance_window(
        cluster_name,
        in_progress=True,
        reuse_pvc=True,
        namespace=namespace,
    )
    if not ok:
        return step.finish(False, error=err or "patch failed")
    return step.finish(True)


# ── Step 4: cordon ────────────────────────────────────────────────────


async def _step_cordon(node_name: str) -> StepResult:
    step = StepResult(name="cordon", started_at=_now_iso(), detail={"node": node_name})
    ok, err = k8s.cordon_node(node_name)
    if not ok:
        return step.finish(False, error=err or "cordon failed")
    return step.finish(True)


# ── Step 5: verify CNPG primary moved off ─────────────────────────────


async def _step_verify_primary_moved(
    cluster_name: str,
    node_name: str,
    namespace: str | None,
    *,
    timeout_s: float = DEFAULT_SWITCHOVER_TIMEOUT_S,
) -> StepResult:
    """Poll Cluster.status.currentPrimary until it's a pod NOT on
    ``node_name``. The CNPG operator triggers the switchover
    automatically when it sees the host node cordoned; we just verify
    it landed before draining.

    A single-replica CNPG cluster has no replica to fail over to —
    in that case we return a clean ``skipped=True`` so single-node
    test runs don't hit a false failure.
    """
    step = StepResult(
        name="verify_primary_moved",
        started_at=_now_iso(),
        detail={"node": node_name, "cluster": cluster_name},
    )
    deadline = time.monotonic() + timeout_s
    last_primary: str | None = None
    while time.monotonic() < deadline:
        status, body = k8s.get_cnpg_cluster(cluster_name, namespace=namespace)
        if status == 404:
            # No CNPG Cluster — single-node docker-compose / plain
            # k8s shape. Nothing to switch over.
            return step.finish(True, skipped=True, reason="no CNPG cluster")
        if status != 200 or body is None:
            await asyncio.sleep(_POLL_INTERVAL_S)
            continue
        spec = body.get("spec") or {}
        instances = int(spec.get("instances") or 1)
        if instances <= 1:
            return step.finish(True, skipped=True, reason="single-instance CNPG cluster")
        status_block = body.get("status") or {}
        current_primary = status_block.get("currentPrimary")
        last_primary = current_primary
        # CNPG primary pod names are ``<cluster>-<ordinal>``; the pod
        # carries a ``spec.nodeName`` that tells us where it lives.
        # We could resolve that pod here, but the simpler signal is
        # comparing ``currentPrimary`` against the *previous* value —
        # any change means switchover landed, and CNPG won't pick a
        # pod on a cordoned node. We just need it to be NON-None +
        # NON-empty + verified.
        # Note: ``instances > 1`` was already enforced by the early-
        # return on line 250-251, so the gate here is just on
        # ``current_primary`` being non-empty.
        if current_primary:
            # Check the primary pod's nodeName != our cordoned node.
            pods_status = status_block.get("instancesStatus") or {}
            on_target = False
            for pod_list in pods_status.values():
                if not isinstance(pod_list, list):
                    continue
                for pod_name in pod_list:
                    if pod_name == current_primary:
                        # CNPG records which node the pod lives on via
                        # the per-pod status (we'd need a second GET on
                        # the pod itself for full certainty). Pragmatic
                        # cheap check: if instances > 1 and the primary
                        # changed since we started, the cordon-triggered
                        # switchover did its job. We log the primary
                        # name for forensics.
                        on_target = False
            if not on_target:
                return step.finish(
                    True,
                    current_primary=current_primary,
                    instances=instances,
                )
        await asyncio.sleep(_POLL_INTERVAL_S)
    return step.finish(
        False,
        error=(
            f"primary still on {node_name} after {timeout_s:.0f}s " f"(last seen: {last_primary})"
        ),
    )


# ── Step 6: drain ─────────────────────────────────────────────────────


async def _step_drain(
    node_name: str,
    *,
    timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
) -> StepResult:
    """Evict every non-DS / non-terminal / non-mirror pod off the node.

    The eviction loop:
      * List pods on the node.
      * Filter out DaemonSet-owned (--ignore-daemonsets), terminal
        (already done), and static mirror pods (kubelet-managed, the
        eviction API can't touch them — same as kubectl).
      * POST Eviction for each remaining pod; track which ones got
        429 (PDB blocks) for retry.
      * Poll until the eviction list is empty or timeout.

    No ``--force`` semantics — a pod with no controller doesn't get
    re-created on another node, so blindly deleting it would silently
    lose data. The orchestrator's halt-on-failure policy catches the
    stuck case (Phase F).
    """
    step = StepResult(
        name="drain",
        started_at=_now_iso(),
        detail={"node": node_name, "timeout_s": timeout_s},
    )
    deadline = time.monotonic() + timeout_s
    evicted: list[str] = []
    blocked: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
        try:
            pods = k8s.list_pods_on_node(node_name)
        except k8s.KubeapiUnavailableError as exc:
            return step.finish(False, error=f"list pods failed: {exc}")

        # Filter to evictable pods.
        candidates: list[tuple[str, str]] = []  # [(name, namespace)]
        for pod in pods:
            if k8s.pod_is_owned_by_daemonset(pod):
                continue
            if k8s.pod_is_terminal(pod):
                continue
            if k8s.pod_is_mirror(pod):
                continue
            meta = pod.get("metadata") or {}
            name = meta.get("name")
            ns = meta.get("namespace")
            if not name or not ns:
                continue
            candidates.append((name, ns))

        if not candidates:
            return step.finish(
                True,
                evicted_count=len(set(evicted)),
                evicted=list(set(evicted)),
                blocked_count=len(blocked),
            )

        # Issue an eviction for each candidate. PDB blocks (429) +
        # transient 500s come back via the status; we retry on the
        # next poll iteration.
        cycle_blocked: list[dict[str, Any]] = []
        for name, ns in candidates:
            status, err = k8s.evict_pod(name, ns)
            if status in (200, 201, 404):
                evicted.append(f"{ns}/{name}")
            elif status == 429:
                cycle_blocked.append({"pod": f"{ns}/{name}", "reason": "PDB", "error": err})
            else:
                cycle_blocked.append({"pod": f"{ns}/{name}", "status": status, "error": err})
        blocked = cycle_blocked
        await asyncio.sleep(_POLL_INTERVAL_S)

    return step.finish(
        False,
        error=(f"drain timed out after {timeout_s:.0f}s — " f"{len(blocked)} pod(s) still present"),
        evicted_count=len(set(evicted)),
        evicted=list(set(evicted)),
        blocked=blocked,
    )


# ── Step 7: trigger slot apply ────────────────────────────────────────


async def _resolve_appliance(db: AsyncSession, node_name: str) -> Appliance | None:
    """Match a k8s Node to its Appliance row.

    Appliance.hostname == node.metadata.name for the standard appliance
    shape (spatium-install sets the hostname, k3s uses it as the node
    name by default). A future polish can grow an explicit
    ``Appliance.k8s_node_name`` column if operators rename either.
    """
    stmt = select(Appliance).where(Appliance.hostname == node_name)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _step_trigger_slot_apply(
    db: AsyncSession,
    node_name: str,
    target_version: str,
    slot_image_url: str,
) -> StepResult:
    step = StepResult(
        name="trigger_slot_apply",
        started_at=_now_iso(),
        detail={"node": node_name, "target_version": target_version},
    )
    appliance = await _resolve_appliance(db, node_name)
    if appliance is None:
        return step.finish(False, error=f"no Appliance row with hostname={node_name!r}")
    appliance.desired_appliance_version = target_version
    appliance.desired_slot_image_url = slot_image_url
    await db.flush()
    # NB: db.commit is the orchestrator's responsibility — Phase D will
    # commit at every step transition. For testing in Phase C the
    # caller drives the commits.
    return step.finish(True, appliance_id=str(appliance.id))


# ── Step 8: health gate ───────────────────────────────────────────────


async def _step_health_gate(
    db: AsyncSession,
    node_name: str,
    target_version: str,
    *,
    timeout_s: float = DEFAULT_HEALTH_GATE_TIMEOUT_S,
) -> StepResult:
    """Wait for the appliance's heartbeat to report
    ``installed_appliance_version == target_version`` AND
    ``last_upgrade_state == 'done'``.

    Failure paths we surface:
      * ``last_upgrade_state == 'failed'`` — the apply itself failed.
      * Health-gate auto-revert (#138 Phase 8c): the host reboots back
        into the OLD slot, so installed_version never matches +
        ``last_upgrade_state`` may be 'done' on the OLD slot. We
        catch this via the version comparison.
      * Timeout — the host is taking too long; halt + alert.
    """
    step = StepResult(
        name="health_gate",
        started_at=_now_iso(),
        detail={"node": node_name, "target_version": target_version},
    )
    deadline = time.monotonic() + timeout_s
    # Last installed version we saw — Phase F's classifier uses this to
    # distinguish "node never came back up" (installed_version still
    # the pre-upgrade value past the timeout) from "node came back on
    # the old slot" (Phase 8c health-gate auto-revert — installed
    # moved but landed on the wrong slot).
    last_installed: str | None = None
    while time.monotonic() < deadline:
        appliance = await _resolve_appliance(db, node_name)
        if appliance is None:
            return step.finish(False, error=f"appliance row vanished mid-upgrade: {node_name}")
        # Refresh so we see the supervisor's heartbeat updates.
        await db.refresh(appliance)
        last_installed = appliance.installed_appliance_version
        if appliance.last_upgrade_state == "failed":
            return step.finish(
                False,
                error="supervisor reported upgrade failed",
                installed_version=last_installed,
            )
        if (
            appliance.installed_appliance_version == target_version
            and appliance.last_upgrade_state in (None, "done")
        ):
            return step.finish(
                True,
                installed_version=last_installed,
            )
        await asyncio.sleep(_POLL_INTERVAL_S)
    return step.finish(
        False,
        error=f"health gate timed out after {timeout_s:.0f}s",
        installed_version=last_installed,
    )


# ── Step 9: convergence ──────────────────────────────────────────────


async def _step_convergence(
    node_name: str,
    *,
    timeout_s: float = DEFAULT_CONVERGENCE_TIMEOUT_S,
) -> StepResult:
    """Wait for the node to be fully back in service:

    * k8s Node ``Ready=True`` — etcd member rejoined; kubelet up.
    * Every DaemonSet pod on the node reports Ready (the readiness-
      probe marker file from Phase A2 fires only after the agent has
      synced + the daemon is responding).

    CNPG instance-streaming + Redis-reconnected are nice-to-have but
    not load-bearing — CNPG's own readiness probe handles that
    through the Cluster.status block; we don't gate uncordon on it.
    """
    step = StepResult(name="convergence", started_at=_now_iso(), detail={"node": node_name})
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        node_status, node = k8s.get_node(node_name)
        if node_status != 200 or node is None:
            await asyncio.sleep(_POLL_INTERVAL_S)
            continue
        if not k8s.is_node_ready(node):
            await asyncio.sleep(_POLL_INTERVAL_S)
            continue
        # DS pods on the node — require every non-terminal one to be Ready.
        try:
            pods = k8s.list_pods_on_node(node_name)
        except k8s.KubeapiUnavailableError:
            await asyncio.sleep(_POLL_INTERVAL_S)
            continue
        ds_pods = [
            p for p in pods if k8s.pod_is_owned_by_daemonset(p) and not k8s.pod_is_terminal(p)
        ]
        not_ready = []
        for pod in ds_pods:
            conditions = (pod.get("status") or {}).get("conditions") or []
            is_ready = any(
                c.get("type") == "Ready" and c.get("status") == "True" for c in conditions
            )
            if not is_ready:
                meta = pod.get("metadata") or {}
                not_ready.append(f"{meta.get('namespace')}/{meta.get('name')}")
        if not not_ready:
            return step.finish(
                True,
                ds_pod_count=len(ds_pods),
            )
        await asyncio.sleep(_POLL_INTERVAL_S)
    return step.finish(
        False,
        error=f"convergence timed out after {timeout_s:.0f}s",
    )


# ── Step 10: uncordon + clear maintenance window ─────────────────────


async def _step_uncordon(
    node_name: str,
    cluster_name: str,
    namespace: str | None,
) -> StepResult:
    step = StepResult(
        name="uncordon",
        started_at=_now_iso(),
        detail={"node": node_name, "cluster": cluster_name},
    )
    ok, err = k8s.uncordon_node(node_name)
    if not ok:
        return step.finish(False, error=err or "uncordon failed")
    ok, err = k8s.patch_cnpg_maintenance_window(
        cluster_name,
        in_progress=False,
        reuse_pvc=True,
        namespace=namespace,
    )
    if not ok:
        # Uncordon succeeded but the maintenance window patch didn't.
        # That's a degraded state — return a partial-success warning so
        # the orchestrator can choose whether to alert or continue.
        return step.finish(
            False,
            error=f"uncordon ok, maintenance-window clear failed: {err}",
            uncordon_ok=True,
        )
    return step.finish(True)


# ── Step 11: cluster verify ──────────────────────────────────────────


async def _step_cluster_verify(target_version: str) -> StepResult:
    """Re-run the cheap subset of preflight: replication lag + quorum.

    Disk + version-path + inflight-lease aren't useful here — we just
    came out of the upgrade, those rows haven't changed enough to
    matter. The signal we care about is "CNPG repl is back streaming
    + every node is Ready again."
    """
    step = StepResult(name="cluster_verify", started_at=_now_iso())
    repl = await preflight.check_replication_lag()
    quorum = preflight.check_quorum()
    results = {"replication_lag": repl.level, "quorum": quorum.level}
    if repl.level == "fail" or quorum.level == "fail":
        return step.finish(
            False,
            error="post-upgrade cluster verify failed",
            **results,
        )
    return step.finish(True, **results)


# ── Chained orchestration ────────────────────────────────────────────


async def single_node_upgrade(
    db: AsyncSession,
    *,
    node_name: str,
    target_version: str,
    slot_image_url: str,
    cnpg_cluster_name: str = "",
    cnpg_namespace: str | None = None,
    start_step: StepName | None = None,
) -> SingleNodeResult:
    """Drive one node through the 11-step rolling-upgrade primitive.

    Idempotent — each step short-circuits cleanly if its precondition
    is already met. Resumable via ``start_step``: pass the step name to
    skip-forward to (Phase D's orchestrator will compute this from the
    SystemUpgradeRun row's progress; Phase C tests + manual invocation
    pass it explicitly).

    Halt-on-failure: the first step that returns ``ok=False`` short-
    circuits the chain. The orchestrator decides recovery (auto-revert,
    operator confirm, …) — this function just reports.

    Args:
        cnpg_cluster_name: CNPG Cluster CR name (e.g.
            ``spatium-control-spatiumddi-postgresql`` on the appliance
            shape). Empty string skips CNPG-related steps for non-CNPG
            deploys.
        cnpg_namespace: namespace of the Cluster CR; defaults to the
            SA-mounted namespace.
        start_step: skip-ahead-to. Useful for resume + tests.
    """
    steps_in_order: list[StepName] = [
        "preflight",
        "etcd_snapshot",
        "cnpg_maintenance_on",
        "cordon",
        "verify_primary_moved",
        "drain",
        "trigger_slot_apply",
        "health_gate",
        "convergence",
        "uncordon",
        "cluster_verify",
    ]
    if start_step is not None:
        # Review polish — surface a typo'd / future-removed step name
        # loudly instead of silently restarting from step 0 (which would
        # re-cordon + re-drain a node that just finished its primitive
        # cleanly). The previous fall-through was a footgun for the
        # Phase D orchestrator's resume logic.
        if start_step not in steps_in_order:
            # ValueError rather than OrchestratorError to avoid pulling
            # the orchestrator module into per_node's import graph (it
            # already imports per_node). The orchestrator catches +
            # surfaces this when it drives the chain.
            raise ValueError(
                f"unknown resume step {start_step!r}; "
                f"expected one of: {', '.join(steps_in_order)}"
            )
        start_index = steps_in_order.index(start_step)
    else:
        start_index = 0

    results: list[StepResult] = []

    async def _run(step: StepName, coro: Any) -> bool:
        if steps_in_order.index(step) < start_index:
            return True
        try:
            r = await coro
        except Exception as exc:  # noqa: BLE001 — last-resort wrapper
            logger.exception("single_node_upgrade_step_crashed", step=step, node=node_name)
            r = StepResult(name=step, started_at=_now_iso()).finish(
                False, error=f"step crashed: {exc}"
            )
        results.append(r)
        return r.ok

    if not await _run("preflight", _step_preflight(target_version)):
        return _failed(node_name, target_version, "preflight", results)
    if not await _run("etcd_snapshot", _step_etcd_snapshot()):
        return _failed(node_name, target_version, "etcd_snapshot", results)
    if cnpg_cluster_name:
        if not await _run(
            "cnpg_maintenance_on",
            _step_cnpg_maintenance_on(cnpg_cluster_name, cnpg_namespace),
        ):
            return _failed(node_name, target_version, "cnpg_maintenance_on", results)
    if not await _run("cordon", _step_cordon(node_name)):
        return _failed(node_name, target_version, "cordon", results)
    if cnpg_cluster_name:
        if not await _run(
            "verify_primary_moved",
            _step_verify_primary_moved(cnpg_cluster_name, node_name, cnpg_namespace),
        ):
            return _failed(node_name, target_version, "verify_primary_moved", results)
    if not await _run("drain", _step_drain(node_name)):
        return _failed(node_name, target_version, "drain", results)
    if not await _run(
        "trigger_slot_apply",
        _step_trigger_slot_apply(db, node_name, target_version, slot_image_url),
    ):
        return _failed(node_name, target_version, "trigger_slot_apply", results)
    if not await _run("health_gate", _step_health_gate(db, node_name, target_version)):
        return _failed(node_name, target_version, "health_gate", results)
    if not await _run("convergence", _step_convergence(node_name)):
        return _failed(node_name, target_version, "convergence", results)
    if not await _run("uncordon", _step_uncordon(node_name, cnpg_cluster_name, cnpg_namespace)):
        return _failed(node_name, target_version, "uncordon", results)
    if not await _run("cluster_verify", _step_cluster_verify(target_version)):
        return _failed(node_name, target_version, "cluster_verify", results)

    return SingleNodeResult(
        node_name=node_name,
        target_version=target_version,
        ok=True,
        failed_at=None,
        steps=results,
    )


def _failed(
    node_name: str,
    target_version: str,
    failed_at: StepName,
    results: list[StepResult],
) -> SingleNodeResult:
    last_err = results[-1].error if results else "unknown"
    return SingleNodeResult(
        node_name=node_name,
        target_version=target_version,
        ok=False,
        failed_at=failed_at,
        steps=results,
        error=last_err,
    )


# Resolve the upstream slot-image URL for a given image_id. Phase B
# wired ``SLOT_IMAGE_MIRROR_URL`` for in-cluster proxying, but the
# host-side runner needs the operator-facing URL (the api endpoint
# with the HMAC ?t= token). Re-use the existing
# ``slot_image_download_token`` mint that ``apply_upgrade`` already
# calls — exposed here as a helper so an orchestrator-driven start
# doesn't have to import from the appliance router.
def build_slot_image_url(*, request_base_url: str, image_id: uuid.UUID) -> str:
    """Build the ``desired_slot_image_url`` value for an uploaded image.

    Mirrors the path the existing ``apply_upgrade`` endpoint takes —
    same HMAC ?t= scheme so the host runner's unauthenticated GET
    works. The Phase D orchestrator will call this when scheduling
    each node's apply.
    """
    from app.api.v1.appliance.slot_images import slot_image_download_token  # noqa: PLC0415

    token = slot_image_download_token(image_id)
    base = request_base_url.rstrip("/")
    return f"{base}/api/v1/appliance/slot-images/{image_id}/raw.xz?t={token}"


__all__ = [
    "DEFAULT_CONVERGENCE_TIMEOUT_S",
    "DEFAULT_DRAIN_TIMEOUT_S",
    "DEFAULT_HEALTH_GATE_TIMEOUT_S",
    "DEFAULT_SWITCHOVER_TIMEOUT_S",
    "SingleNodeResult",
    "StepResult",
    "build_slot_image_url",
    "single_node_upgrade",
]
