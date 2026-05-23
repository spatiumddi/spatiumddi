"""Cluster rolling-upgrade orchestrator (#296 Phase D).

Drives Phase C's per-node primitive across every node in the cluster.
DB-persisted state machine on ``SystemUpgradeRun`` rows. ``coordination.
k8s.io/v1/Lease``-based single-upgrader lock (renewed in a background
task so a 30-min upgrade doesn't expire mid-step). Quorum-aware "one
node at a time." Halt-on-failure: the first node that returns
``ok=False`` stops the loop + flips the run to ``state='failed'``.
Survives the orchestrator's own pod reschedule via the row state +
celery task re-enqueue.

Lifecycle (state transitions on the SystemUpgradeRun row):

    planned   ── operator calls /start ──> running
    running   ── all nodes succeeded   ──> succeeded
    running   ── per-node primitive ok=False  ──> failed
    running   ── operator /halt         ──> halted
    halted    ── operator /resume       ──> running
    running   ── operator /abort        ──> aborted
    halted    ── operator /abort        ──> aborted

Halt-on-failure semantics: ``failed`` is terminal. The orchestrator
does not auto-rollback — Phase 8c's slot health-gate already handles
the per-node revert path. To retry, the operator fixes the underlying
issue and starts a new run (plan + start).

Concurrency: the ``Lease`` is the cluster-wide mutex. At-most-one
orchestrator drives at any moment. The partial unique index on
``system_upgrade_run`` (Phase A's ``ix_system_upgrade_run_one_active``)
is the DB-level backstop.

Resumability: the celery task wrapper in ``app/tasks/upgrade_
orchestrator.py`` is idempotent. Re-enqueuing the same ``run_id`` (e.g.
after the celery worker dies mid-run) picks up from the row's
``progress.per_node`` map — completed nodes are skipped, the next
incomplete node is driven.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.system_upgrade import LIFECYCLE_STATES, SystemUpgradeRun
from app.services.appliance import k8s
from app.services.upgrades import chart_bump, mutex, node_order, per_node, preflight

logger = structlog.get_logger(__name__)


# Lease duration for orchestrator runs — much longer than the 60 s
# default that suits read-only mutex.get_state() calls. A single
# node's health-gate step can spend ~30 min waiting on a slot dd +
# reboot + firstboot; the renewal loop renews every
# ``LEASE_DURATION_S / 3`` so two missed renewals still leave time
# before expiration.
LEASE_DURATION_S = 600  # 10 min
_LEASE_RENEW_INTERVAL_S = LEASE_DURATION_S / 3.0


# When the per-node primitive succeeds the orchestrator gates the next
# node's start on a short cluster-wide health check + this minimum
# pause so the previous node's services have time to fully resync (DS
# bundle warm-up, CNPG replica streaming catches up).
_BETWEEN_NODES_PAUSE_S = 10.0


class OrchestratorError(RuntimeError):
    """Surfaces as 409 / 422 on the api side — bad operator request."""


@dataclass(frozen=True)
class PlanResult:
    """Output of ``plan_upgrade`` — what the operator sees on the
    preview screen before clicking Start."""

    run_id: uuid.UUID
    target_version: str
    node_order: list[str]
    preflight_overall: str  # ok | warn | fail
    preflight_results: list[dict[str, Any]]


# ── Lookup helpers ───────────────────────────────────────────────────


async def get_run(db: AsyncSession, run_id: uuid.UUID) -> SystemUpgradeRun:
    """Fetch + 404-raise if the row doesn't exist."""
    row = await db.get(SystemUpgradeRun, run_id)
    if row is None:
        raise OrchestratorError(f"upgrade run {run_id} not found")
    return row


async def list_recent_runs(db: AsyncSession, *, limit: int = 25) -> list[SystemUpgradeRun]:
    """Most-recent-first list for the Fleet UI's history pane."""
    stmt = select(SystemUpgradeRun).order_by(SystemUpgradeRun.started_at.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


def _now() -> datetime:
    return datetime.now(UTC)


# ── plan_upgrade ─────────────────────────────────────────────────────


async def plan_upgrade(
    db: AsyncSession,
    *,
    target_version: str,
    slot_image_url: str,
    cnpg_cluster_name: str = "",
    cnpg_namespace: str | None = None,
    started_by_user_id: uuid.UUID | None = None,
    audit_actor_display: str | None = None,
    audit_actor_source: str | None = None,
) -> PlanResult:
    """Plan a rolling upgrade — read-only preflight + node enumeration +
    DB-persisted ``planned`` row. Does NOT acquire the lease yet; that
    happens on ``start_upgrade``.

    Refuses if:
      * The preflight aggregate returns ``overall='fail'``.
      * Another non-terminal SystemUpgradeRun row already exists (the
        partial unique index would 500 us anyway; we surface the
        409 cleanly first).
    """
    # Preflight gate.
    report = await preflight.run_all(target_version=target_version)
    if report.overall == "fail":
        fails = [r.name for r in report.results if r.level == "fail"]
        raise OrchestratorError(f"preflight failed; refusing to plan: {', '.join(fails)}")

    # Refuse if a non-terminal run already exists. The unique partial
    # index would catch this too but the operator-facing error is
    # nicer.
    existing = (
        await db.execute(
            select(SystemUpgradeRun).where(
                SystemUpgradeRun.state.in_(["planned", "running", "halted"])
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise OrchestratorError(
            f"another upgrade is in flight (id={existing.id}, "
            f"state={existing.state}); finish or abort it first"
        )

    # Enumerate appliance nodes for the upgrade order.
    try:
        status, items = k8s.list_nodes(label_selector="spatium.io/role=appliance")
    except k8s.KubeapiUnavailableError as exc:
        raise OrchestratorError(f"kubeapi unreachable: {exc}") from exc
    if status != 200:
        raise OrchestratorError(f"node list returned {status}")
    order = node_order.pick_node_order(items)
    if not order:
        raise OrchestratorError(
            "no appliance nodes found (label selector "
            "spatium.io/role=appliance) — refusing to plan an upgrade with "
            "zero targets"
        )

    # Capture source versions per node so the audit row shows the
    # before/after picture even if the appliance row's
    # installed_appliance_version is updated mid-run.
    from app.models.appliance import Appliance  # noqa: PLC0415

    source_versions: dict[str, str | None] = {}
    appliance_rows = (
        await db.execute(
            select(Appliance.hostname, Appliance.installed_appliance_version).where(
                Appliance.hostname.in_(order)
            )
        )
    ).all()
    for hostname, installed in appliance_rows:
        source_versions[hostname] = installed

    run = SystemUpgradeRun(
        kind="cluster_rolling",
        state="planned",
        target_version=target_version,
        source_versions=source_versions,
        plan={
            "node_order": order,
            "slot_image_url": slot_image_url,
            "cnpg_cluster_name": cnpg_cluster_name,
            "cnpg_namespace": cnpg_namespace,
            "preflight_at_plan": [
                {"name": r.name, "level": r.level, "message": r.message} for r in report.results
            ],
        },
        progress={"per_node": {}, "events": []},
        started_by_user_id=started_by_user_id,
    )
    db.add(run)
    if audit_actor_display or started_by_user_id:
        db.add(
            AuditLog(
                user_id=started_by_user_id,
                user_display_name=audit_actor_display,
                auth_source=audit_actor_source,
                action="upgrade.planned",
                resource_type="system_upgrade_run",
                resource_id=str(run.id),
                resource_display=target_version,
                result="success",
                new_value={
                    "target_version": target_version,
                    "node_order": order,
                    "preflight_overall": report.overall,
                },
            )
        )
    await db.commit()
    await db.refresh(run)
    logger.info(
        "upgrade_planned",
        run_id=str(run.id),
        target_version=target_version,
        node_count=len(order),
    )
    return PlanResult(
        run_id=run.id,
        target_version=target_version,
        node_order=order,
        preflight_overall=report.overall,
        preflight_results=[
            {"name": r.name, "level": r.level, "message": r.message, "detail": r.detail}
            for r in report.results
        ],
    )


# ── State-transition operator endpoints ──────────────────────────────


async def _record_event(
    db: AsyncSession,
    run: SystemUpgradeRun,
    event: str,
    **detail: Any,
) -> None:
    """Append an entry to ``run.progress.events`` for audit + UI surface.

    SQLAlchemy doesn't track in-place JSONB mutation; replacing the
    dict + adding to the session is how we persist the change. The
    same pattern repeats in every progress-write below.
    """
    events = list(run.progress.get("events") or [])
    events.append({"event": event, "at": _now().isoformat(), **detail})
    run.progress = {**run.progress, "events": events}


async def _transition(
    db: AsyncSession,
    run: SystemUpgradeRun,
    new_state: str,
    *,
    allowed_from: tuple[str, ...],
    event: str,
    **event_detail: Any,
) -> None:
    if new_state not in LIFECYCLE_STATES:
        raise OrchestratorError(f"unknown lifecycle state {new_state!r}")
    if run.state not in allowed_from:
        raise OrchestratorError(
            f"can't transition {run.state} → {new_state}; " f"allowed from {allowed_from}"
        )
    old = run.state
    run.state = new_state
    if new_state in ("succeeded", "failed", "aborted"):
        run.finished_at = _now()
    await _record_event(db, run, event, from_state=old, **event_detail)


async def halt_upgrade(
    db: AsyncSession,
    run_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_display: str | None = None,
    actor_source: str | None = None,
) -> SystemUpgradeRun:
    """Operator-initiated pause. Survives across orchestrator restarts —
    the next drive loop iteration sees state=halted + exits cleanly.
    """
    run = await get_run(db, run_id)
    await _transition(
        db,
        run,
        "halted",
        allowed_from=("running",),
        event="halted",
        actor=actor_display,
    )
    db.add(
        AuditLog(
            user_id=actor_user_id,
            user_display_name=actor_display,
            auth_source=actor_source,
            action="upgrade.halted",
            resource_type="system_upgrade_run",
            resource_id=str(run.id),
            resource_display=run.target_version,
            result="success",
        )
    )
    await db.commit()
    await db.refresh(run)
    return run


async def resume_upgrade(
    db: AsyncSession,
    run_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_display: str | None = None,
    actor_source: str | None = None,
) -> SystemUpgradeRun:
    """Operator-initiated resume from halt. Flips the row state but
    DOESN'T re-enqueue the celery task — the api endpoint handler does
    that after this call returns ok."""
    run = await get_run(db, run_id)
    await _transition(
        db,
        run,
        "running",
        allowed_from=("halted",),
        event="resumed",
        actor=actor_display,
    )
    db.add(
        AuditLog(
            user_id=actor_user_id,
            user_display_name=actor_display,
            auth_source=actor_source,
            action="upgrade.resumed",
            resource_type="system_upgrade_run",
            resource_id=str(run.id),
            resource_display=run.target_version,
            result="success",
        )
    )
    await db.commit()
    await db.refresh(run)
    return run


async def abort_upgrade(
    db: AsyncSession,
    run_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_display: str | None = None,
    actor_source: str | None = None,
) -> SystemUpgradeRun:
    """Operator-initiated abort. Terminal — no resume. Leaves the
    cluster in whatever partial state the in-flight nodes ended in
    (some may be on the new slot, others on the old). Operator owns
    cleanup from here.
    """
    run = await get_run(db, run_id)
    await _transition(
        db,
        run,
        "aborted",
        allowed_from=("planned", "running", "halted"),
        event="aborted",
        actor=actor_display,
    )
    db.add(
        AuditLog(
            user_id=actor_user_id,
            user_display_name=actor_display,
            auth_source=actor_source,
            action="upgrade.aborted",
            resource_type="system_upgrade_run",
            resource_id=str(run.id),
            resource_display=run.target_version,
            result="success",
        )
    )
    await db.commit()
    # Release the lease so a new run can plan immediately.
    ok, err = mutex.release()
    if not ok:
        logger.warning("upgrade_lease_release_failed", error=err)
    await db.refresh(run)
    return run


# ── drive_upgrade — the actual orchestration loop ────────────────────


async def _lease_renewal_loop(stop_event: asyncio.Event) -> None:
    """Background task — renew the upgrade Lease every
    ``LEASE_DURATION_S / 3`` seconds until ``stop_event`` is set.

    On renewal failure (someone else claimed the lease, or kubeapi
    transient) we set ``stop_event`` so the main drive loop exits
    cleanly. The drive loop checks ``stop_event`` between per-node
    iterations; we DON'T abort an in-flight per-node primitive
    because canceling a mid-drain / mid-cordon would leave the
    cluster in an ambiguous state. The next orchestrator that picks
    up the row resumes from the row's progress.
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_LEASE_RENEW_INTERVAL_S)
            return  # stop_event set during the wait
        except TimeoutError:
            pass
        ok, err = mutex.renew(lease_duration_seconds=LEASE_DURATION_S)
        if not ok:
            logger.warning("upgrade_lease_renew_failed", error=err)
            stop_event.set()
            return


async def drive_upgrade(
    db: AsyncSession,
    run_id: uuid.UUID,
) -> SystemUpgradeRun:
    """Drive a planned / halted-and-resumed / partially-completed run
    through to terminal state.

    Idempotent + resumable: reading the row's ``progress.per_node`` map
    tells us which nodes are done; the rest get driven in plan order.

    Acquires the upgrade lease for the duration; spawns the renewal
    task; releases on terminal transition.
    """
    run = await get_run(db, run_id)
    if run.state == "planned":
        # First call — acquire the lease.
        ok, err = mutex.acquire(lease_duration_seconds=LEASE_DURATION_S)
        if not ok:
            raise OrchestratorError(f"could not acquire upgrade lease: {err}")
        run.lease_holder = mutex._identity()  # noqa: SLF001 — same module family
        run.lease_acquired_at = _now()
        await _transition(db, run, "running", allowed_from=("planned",), event="started")
        await db.commit()
        await db.refresh(run)
    elif run.state == "running":
        # Resume / re-enqueue path — confirm we still hold the lease,
        # taking over if it expired (the previous celery worker died).
        ok, err = mutex.acquire(lease_duration_seconds=LEASE_DURATION_S)
        if not ok:
            raise OrchestratorError(f"can't take over upgrade lease for resume: {err}")
        if run.lease_holder != mutex._identity():  # noqa: SLF001
            run.lease_holder = mutex._identity()  # noqa: SLF001
            run.lease_acquired_at = _now()
            await _record_event(db, run, "lease_takeover")
            await db.commit()
    else:
        # Terminal or halted — nothing to drive.
        return run

    stop_event = asyncio.Event()
    renewal_task = asyncio.create_task(_lease_renewal_loop(stop_event))
    try:
        await _drive_loop(db, run, stop_event)
    finally:
        stop_event.set()
        await renewal_task

    await db.refresh(run)
    return run


async def _drive_loop(
    db: AsyncSession,
    run: SystemUpgradeRun,
    stop_event: asyncio.Event,
) -> None:
    """The per-node iteration. Each cycle:

    * Refreshes the row (so operator halts land mid-loop).
    * Picks the next un-completed node from the plan.
    * Drives Phase C's ``single_node_upgrade`` for it.
    * On success: records progress + brief pause + cluster verify.
    * On failure: flips state to ``failed`` + halts.
    * On halt/abort signal: exits cleanly without further work.
    """
    plan_order: list[str] = run.plan.get("node_order") or []
    slot_image_url: str = run.plan.get("slot_image_url") or ""
    cnpg_name: str = run.plan.get("cnpg_cluster_name") or ""
    cnpg_namespace: str | None = run.plan.get("cnpg_namespace")

    while True:
        if stop_event.is_set():
            # Lease lost — leave the row in ``running``; the next
            # take-over will pick up. Don't flip to failed; that'd
            # mis-attribute a transient kubeapi blip as a real
            # upgrade failure.
            logger.warning("upgrade_drive_exit_lease_lost", run_id=str(run.id))
            return

        await db.refresh(run)
        if run.state in ("halted", "aborted", "succeeded", "failed"):
            logger.info(
                "upgrade_drive_exit_state",
                run_id=str(run.id),
                state=run.state,
            )
            return

        per_node_progress = dict(run.progress.get("per_node") or {})
        completed_nodes = [
            name for name, entry in per_node_progress.items() if entry.get("ok") is True
        ]
        next_node = node_order.next_node_to_upgrade(plan_order, completed_nodes)
        if next_node is None:
            # Every node committed the new slot. Phase E — bump the
            # chart's image.tag so the api / worker / frontend
            # Deployments roll onto the new application code +
            # migrate Job runs against the new schema. Until this
            # fires, every pod is still on N-1 code despite N-baked
            # images sitting on every node's new slot. The bump is
            # idempotent so a resumed orchestrator (worker died
            # between the all-nodes-done state + the chart bump
            # completion) re-runs cleanly.
            await _record_event(
                db,
                run,
                "all_nodes_complete",
                chart_bump_starting=True,
            )
            await db.commit()
            chart_name: str = run.plan.get("chart_name") or chart_bump.DEFAULT_CHART_NAME
            chart_ns: str = run.plan.get("chart_namespace") or chart_bump.DEFAULT_CHART_NS
            release_ns: str = run.plan.get("release_namespace") or chart_bump.DEFAULT_RELEASE_NS
            bump = await chart_bump.bump_chart_image_tag(
                run.target_version,
                chart_name=chart_name,
                chart_namespace=chart_ns,
                release_namespace=release_ns,
            )
            run.progress = {
                **run.progress,
                "chart_bump": chart_bump.result_to_dict(bump),
            }
            if not bump.ok:
                run.last_error = f"chart bump to {run.target_version} failed: {bump.error}"
                await _transition(
                    db,
                    run,
                    "failed",
                    allowed_from=("running",),
                    event="chart_bump_failed",
                    error=bump.error,
                )
                await db.commit()
                ok, err = mutex.release()
                if not ok:
                    logger.warning("upgrade_lease_release_failed", error=err)
                logger.warning(
                    "upgrade_chart_bump_failed",
                    run_id=str(run.id),
                    error=bump.error,
                )
                return

            await _transition(
                db,
                run,
                "succeeded",
                allowed_from=("running",),
                event="chart_bump_complete",
                rolled_deployments=bump.rolled_deployments,
                migrate_job_state=bump.migrate_job_state,
                skipped=bump.skipped,
            )
            await db.commit()
            ok, err = mutex.release()
            if not ok:
                logger.warning("upgrade_lease_release_failed", error=err)
            logger.info(
                "upgrade_succeeded",
                run_id=str(run.id),
                rolled=bump.rolled_deployments,
            )
            return

        # Drive Phase C for this node.
        await _record_event(db, run, "node_started", node=next_node)
        await db.commit()
        result = await per_node.single_node_upgrade(
            db,
            node_name=next_node,
            target_version=run.target_version,
            slot_image_url=slot_image_url,
            cnpg_cluster_name=cnpg_name,
            cnpg_namespace=cnpg_namespace,
        )

        per_node_progress[next_node] = {
            "ok": result.ok,
            "failed_at": result.failed_at,
            "error": result.error,
            "steps": [
                {
                    "name": s.name,
                    "ok": s.ok,
                    "started_at": s.started_at,
                    "finished_at": s.finished_at,
                    "detail": s.detail,
                    "error": s.error,
                }
                for s in result.steps
            ],
        }
        run.progress = {**run.progress, "per_node": per_node_progress}

        if not result.ok:
            run.last_error = (
                f"node {next_node} failed at step {result.failed_at}: " f"{result.error}"
            )
            await _transition(
                db,
                run,
                "failed",
                allowed_from=("running",),
                event="node_failed",
                node=next_node,
                failed_at=result.failed_at,
            )
            await db.commit()
            ok, err = mutex.release()
            if not ok:
                logger.warning("upgrade_lease_release_failed", error=err)
            logger.warning(
                "upgrade_failed",
                run_id=str(run.id),
                node=next_node,
                failed_at=result.failed_at,
            )
            return

        # Success — log + brief settle pause before next node.
        await _record_event(db, run, "node_succeeded", node=next_node)
        await db.commit()
        logger.info("upgrade_node_complete", run_id=str(run.id), node=next_node)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_BETWEEN_NODES_PAUSE_S)
            # stop_event fired during the pause — loop will exit.
        except TimeoutError:
            pass
