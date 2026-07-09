"""Post-wake liveness verify + stagger auto-tune for Scheduled Wake-on-LAN
— Phase 3 (issue #586).

After a run dispatches magic packets (:mod:`app.services.wol_scheduler.dispatch`)
an optional chained Celery task
(:func:`app.tasks.wol_scheduler.verify_wol_run`) probes each SENT host for
liveness and re-wakes the non-responders up to a bound. This module holds the
two pure-ish building blocks that task orchestrates:

* :func:`probe_liveness` — is a host up? A single server-vantage ping
  (reuses :func:`app.services.nettools.runner.run_ping` verbatim), returning
  ``(up, method)``. Never raises: a probe error / missing binary / timeout is a
  ``down`` verdict, never an aborted verify pass.
* :func:`verify_run_targets` — probe the still-unverified SENT targets of a run,
  stamp their ``wol_run_target`` verify columns, stamp the Seen infra
  (``IPAddress.last_seen_at`` / ``last_seen_method='ping'``) on responders, and
  return the down set (the re-wake candidates). Does **not** commit — the caller
  owns the transaction. Idempotent: only touches not-yet-verified rows, so a
  double-fire re-probes the same rows to the same verdict without side effects.
* :func:`auto_stagger_ms` — the stagger auto-tune: turns a resolved target count
  into a suggested inter-host gap so a large fleet doesn't inrush /
  PXE-thundering-herd. An explicit operator ``stagger_ms`` always wins.

**Vantage decision (v1 is server-vantage ping ONLY).** ``probe_liveness`` takes
a ``vantage`` for signature/forward-compat but always probes from the
control-plane server, *regardless of the wake vantage*. Appliance-vantage verify
is deferred: :mod:`app.services.appliance.agent_cmd` is an in-memory, per-replica
dispatch — the supervisor long-polls the **api** process while the verify task
runs in the **worker**, so there is no worker→supervisor result-return path
today. For an appliance-vantage *wake*, verify still probes from the server;
that is correct when the api/worker can reach the target segment (routed ICMP /
directed broadcast) and, when it can't, yields a false-negative (unverified) —
never a false wake. ``verify_method`` records ``"ping"`` so the surface is
honest about how it checked. A worker→supervisor result channel is the named
Phase-3 follow-up.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from app.models.ipam import IPAddress
from app.models.wol_schedule import WolRunTarget
from app.services.nettools.runner import run_ping

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.wol_schedule import WolRun

logger = structlog.get_logger(__name__)

# v1 probe method. Kept as a named constant so a future TCP/agent method slots
# in without a migration (``wol_schedule.verify_method`` / the per-target
# ``wol_run_target.verify_method`` already carry the value).
VERIFY_METHOD_PING = "ping"

# Packets per probe. Two keeps each probe fast (``run_ping`` caps it with a 15 s
# ``-w`` deadline) while papering over a single dropped reply — a freshly-woken
# host that answers the 2nd packet still reads as up.
_PING_COUNT = 2

# Bound on concurrent in-flight probes. The probe pass is network-bound ICMP, so
# fanning out the pings (then serialising the DB writes after) collapses the
# wall-clock of a large fleet verify from N × seconds to ~ceil(N/concurrency) ×
# seconds while staying well within the worker's resources. Idempotency is
# order-independent (each row's verdict is self-contained), so concurrency is
# safe here.
_PROBE_CONCURRENCY = 32


def auto_stagger_ms(target_count: int, override: int = 0) -> int:
    """Suggested inter-host stagger (ms) for a resolved wake of ``target_count``.

    ``override`` is the schedule's stored ``stagger_ms``:

    * ``override > 0`` — an explicit operator value; returned verbatim (the
      operator's ramp always wins, we never override an override).
    * ``override == 0`` — "auto": ramp large fleets to cap the power-inrush +
      DHCP/PXE thundering-herd a same-second all-at-once fire would create. A
      small set still fires immediately (returns 0).

    Bands (host count → ms gap), tuned against the resolver's 512 fan-out cap:

    ======================  =========  ==================
    resolved target count   stagger    approx wakes/sec
    ======================  =========  ==================
    ≤ 20                    0          all-at-once
    21 – 100                50         ~20/s
    101 – 256               100        ~10/s
    > 256 (up to 512)       150        ~6–7/s
    ======================  =========  ==================

    Pure + side-effect-free — the same helper feeds the beat runner's dispatch,
    the verify re-wake pass, and the ``preview-targets`` surface's
    ``suggested_stagger_ms`` (call with ``override=0`` for the raw suggestion).
    """
    if override > 0:
        return override
    if target_count <= 20:
        return 0
    if target_count <= 100:
        return 50
    if target_count <= 256:
        return 100
    return 150


async def probe_liveness(
    address: str | None,
    vantage: dict[str, Any] | None = None,
    *,
    method: str = VERIFY_METHOD_PING,
) -> tuple[bool, str]:
    """Probe whether ``address`` is up, returning ``(up, method_used)``.

    v1: a single server-vantage ping via :func:`run_ping` — ``up`` is
    ``exit_code == 0`` (ping exits 0 iff ≥1 reply). A missing ``ping`` binary
    (``available=False``), a timeout, or *any* exception is a ``down`` verdict
    (``False``) — this function NEVER raises, so one un-pingable host can't abort
    a verify pass over a fleet.

    ``vantage`` is accepted for signature/forward-compat but ignored in v1
    (server-vantage only — see the module docstring's vantage decision). The
    returned method is always ``"ping"`` so the persisted
    ``wol_run_target.verify_method`` is an honest record of *how* the check ran,
    independent of the wake vantage.
    """
    if not address:
        return False, VERIFY_METHOD_PING
    try:
        result = await run_ping(address, count=_PING_COUNT)
    except Exception as exc:  # noqa: BLE001 — a probe error is a "down" verdict.
        logger.debug("wol_verify_probe_error", address=address, error=str(exc))
        return False, VERIFY_METHOD_PING
    up = result.exit_code == 0 and not result.timed_out
    return up, VERIFY_METHOD_PING


async def verify_run_targets(
    db: AsyncSession,
    run: WolRun,
    attempt: int,
) -> list[WolRunTarget]:
    """Probe the still-unverified SENT targets of ``run`` and stamp the result.

    One verify pass:

    1. Select ``wol_run_target`` rows for this run that were ``sent`` and are
       not yet UP (``verified IS NULL`` — never probed — OR ``verified IS
       FALSE`` — probed down on a previous pass, a re-wake candidate). Rows that
       were skipped/failed at dispatch (``sent = false``) are never probed and
       keep ``verified = NULL``.
    2. Probe each row's ``address`` concurrently (bounded by
       :data:`_PROBE_CONCURRENCY`) via :func:`probe_liveness`. Rows with no
       ``address`` snapshot can't be probed — left ``verified = NULL`` (neither
       up nor a re-wake candidate).
    3. Stamp each probed row: ``verified`` (True/False), ``verified_at = now``,
       ``verify_method``. On an UP verdict, stamp the Seen infra on the linked
       ``IPAddress`` (``last_seen_at = now``, ``last_seen_method = 'ping'``) —
       identical to :func:`app.services.ipam.discovery.reconcile_subnet`. A DOWN
       verdict proves nothing about liveness, so it never touches Seen.

    Returns the down set (``verified = False`` this pass) — the caller's re-wake
    candidates. Does **not** commit or bump ``wake_attempts`` (the orchestrating
    task owns the transaction, the re-wake, and the attempt bookkeeping). Pure-ish
    + idempotent: only not-yet-UP rows are touched, so a re-run re-probes to the
    same verdict without double-counting.
    """
    now = datetime.now(UTC)

    rows = list(
        (
            await db.execute(
                select(WolRunTarget)
                .where(WolRunTarget.run_id == run.id)
                .where(WolRunTarget.sent.is_(True))
                .where((WolRunTarget.verified.is_(None)) | (WolRunTarget.verified.is_(False)))
            )
        )
        .scalars()
        .all()
    )
    # Only probe rows we can actually address; an address-less sent row (edge /
    # legacy) stays verified=NULL — not up, not a re-wake candidate.
    probeable = [r for r in rows if r.address]
    if not probeable:
        logger.info(
            "wol_verify_pass_noop",
            run_id=str(run.id),
            attempt=attempt,
            candidate_rows=len(rows),
        )
        return []

    # Fan out the probes (network-bound), then serialise the DB writes below —
    # asyncpg sessions aren't concurrency-safe, so no DB access happens inside
    # the gathered coroutines.
    sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def _probe(row: WolRunTarget) -> tuple[WolRunTarget, bool, str]:
        async with sem:
            up, method = await probe_liveness(row.address, run_vantage_of(row))
        return row, up, method

    results = await asyncio.gather(*(_probe(r) for r in probeable))

    non_responders: list[WolRunTarget] = []
    up_ip_ids: list[Any] = []
    for row, up, method in results:
        row.verified = up
        row.verified_at = now
        row.verify_method = method
        if up:
            if row.ip_address_id is not None:
                up_ip_ids.append(row.ip_address_id)
        else:
            non_responders.append(row)

    # Batch-stamp the Seen infra on every responder's IPAddress in one query
    # (never per-row db.get). Mirrors discovery.reconcile_subnet exactly.
    if up_ip_ids:
        ip_rows = (
            (await db.execute(select(IPAddress).where(IPAddress.id.in_(up_ip_ids)))).scalars().all()
        )
        for ip_row in ip_rows:
            ip_row.last_seen_at = now
            ip_row.last_seen_method = VERIFY_METHOD_PING

    logger.info(
        "wol_verify_pass",
        run_id=str(run.id),
        attempt=attempt,
        probed=len(probeable),
        up=len(probeable) - len(non_responders),
        down=len(non_responders),
    )
    return non_responders


def run_vantage_of(row: WolRunTarget) -> dict[str, Any] | None:
    """The ``{kind, id}`` a target was woken from (per-row snapshot).

    Passed to :func:`probe_liveness` for forward-compat only — v1 probes
    server-vantage regardless. Kept as a tiny helper so the appliance-vantage
    verify follow-up has a single, obvious seam to thread a real per-segment
    probe vantage through.
    """
    return row.vantage


__all__ = [
    "VERIFY_METHOD_PING",
    "auto_stagger_ms",
    "probe_liveness",
    "verify_run_targets",
    "run_vantage_of",
]
