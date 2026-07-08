"""Magic-packet dispatch for Scheduled Wake-on-LAN — Phase 1 (issue #586).

Given a resolved list of :class:`~app.services.wol_scheduler.resolver.WakeTarget`
plus the schedule's send knobs (``vantage`` / ``repeat_count`` /
``repeat_interval_ms`` / ``stagger_ms`` / ``port``), fire one magic packet per
target by **reusing the shipped #533 send path verbatim**
(:func:`app.services.wol.wake_from_server` /
:func:`app.services.wol.wake_via_appliance`).  The packet build + UDP
broadcast + SSRF guard live in ``app.services.wol`` — this module never
re-implements any of that; it only orchestrates the repeat / stagger loop the
wire payload deliberately can't carry.

Repeat vs stagger:

* ``repeat_count`` / ``repeat_interval_ms`` — N packets to *one* host, back to
  back (UDP is fire-and-forget; a couple of repeats papers over a dropped
  frame).
* ``stagger_ms`` — gap *between* hosts, so waking a large fleet doesn't create
  a power-inrush + DHCP/PXE thundering-herd event in the same second.

Vantage: ``server`` sends from the control-plane container;
``appliance`` dispatches to a Fleet appliance on the target's segment (the
recon's single-replica caveat applies — the worker enqueuing must be the one
awaiting the supervisor reply).  Any other kind is rejected per target.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.services import wol
from app.services.wol_scheduler.resolver import WakeTarget, group_by_segment

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class DispatchOutcome:
    """Per-host result of a wake attempt — the shape the runner persists into
    a ``wol_run_target`` row."""

    target: WakeTarget
    sent: bool
    vantage: dict[str, Any]
    ran_from: str | None = None
    error: str | None = None


def _normalise_vantage(vantage: dict[str, Any] | None) -> tuple[str, uuid.UUID | None]:
    """Coerce the stored ``{kind, id}`` JSONB into ``(kind, appliance_id?)``.

    Defaults to the control-plane server vantage; a string id is parsed to a
    UUID (appliance vantage).  A malformed id yields ``None`` so the dispatch
    of that target fails cleanly with a message rather than raising.
    """
    if not vantage:
        return "server", None
    kind = str(vantage.get("kind") or "server")
    raw_id = vantage.get("id")
    if raw_id is None:
        return kind, None
    if isinstance(raw_id, uuid.UUID):
        return kind, raw_id
    try:
        return kind, uuid.UUID(str(raw_id))
    except (ValueError, AttributeError, TypeError):
        return kind, None


async def _dispatch_one(
    db: AsyncSession,
    target: WakeTarget,
    *,
    kind: str,
    appliance_id: uuid.UUID | None,
    repeat_count: int,
    repeat_interval_ms: int,
    port: int,
) -> DispatchOutcome:
    """Fire ``repeat_count`` packets to a single host via ``kind`` vantage.

    Builds the validated :class:`app.services.wol.WolWireRequest` once (its
    validators are the SSRF/arg guard), then calls the reused emitter in a
    tight repeat loop.  A dispatch failure stops the repeat for that host and
    is reported on the outcome — one bad host never aborts the run.
    """
    vantage_repr: dict[str, Any] = {
        "kind": kind,
        "id": str(appliance_id) if appliance_id is not None else None,
    }

    try:
        wire = wol.WolWireRequest(mac=target.mac, broadcast=target.broadcast, port=port)
    except ValueError as exc:
        return DispatchOutcome(target=target, sent=False, vantage=vantage_repr, error=str(exc))

    if kind == "appliance" and appliance_id is None:
        return DispatchOutcome(
            target=target,
            sent=False,
            vantage=vantage_repr,
            error="appliance vantage requires an appliance id",
        )
    if kind not in ("server", "appliance"):
        return DispatchOutcome(
            target=target,
            sent=False,
            vantage=vantage_repr,
            error=f"cannot wake from a {kind!r} vantage",
        )

    sent_any = False
    ran_from: str | None = None
    error: str | None = None
    reps = max(1, repeat_count)
    for i in range(reps):
        try:
            if kind == "appliance":
                assert appliance_id is not None  # narrowed above
                result = await wol.wake_via_appliance(db, appliance_id, wire)
            else:
                result = await wol.wake_from_server(wire)
        except wol.WolDispatchError as exc:
            # Expected dispatch failure (SSRF guard, unreachable appliance, …).
            error = str(exc)
            break
        except Exception as exc:  # noqa: BLE001
            # Any other failure from the send — e.g. a non-None-but-malformed
            # supervisor reply that trips pydantic.ValidationError inside
            # wake_via_appliance — must fail THIS host only, never abort the
            # multi-host run (the module's one-bad-host guarantee).
            error = str(exc)
            break
        sent_any = sent_any or result.sent
        ran_from = result.ran_from
        if i < reps - 1 and repeat_interval_ms > 0:
            await asyncio.sleep(repeat_interval_ms / 1000.0)

    return DispatchOutcome(
        target=target,
        sent=sent_any,
        vantage=vantage_repr,
        ran_from=ran_from,
        error=error,
    )


async def dispatch_wol_targets(
    db: AsyncSession,
    targets: list[WakeTarget],
    *,
    vantage: dict[str, Any] | None,
    repeat_count: int = 2,
    repeat_interval_ms: int = 100,
    stagger_ms: int = 0,
    port: int = 9,
) -> list[DispatchOutcome]:
    """Send a magic packet to every target, honouring repeat + stagger.

    Iterates the targets grouped by L2 segment (so appliance-vantage NIC picks
    and staggering stay segment-aware) and applies ``stagger_ms`` *between*
    hosts.  Returns one :class:`DispatchOutcome` per input target, in send
    order, for the runner to persist as ``wol_run_target`` rows.
    """
    kind, appliance_id = _normalise_vantage(vantage)
    outcomes: list[DispatchOutcome] = []
    first = True
    for _segment, group in group_by_segment(targets).items():
        for target in group:
            if not first and stagger_ms > 0:
                await asyncio.sleep(stagger_ms / 1000.0)
            first = False
            outcomes.append(
                await _dispatch_one(
                    db,
                    target,
                    kind=kind,
                    appliance_id=appliance_id,
                    repeat_count=repeat_count,
                    repeat_interval_ms=repeat_interval_ms,
                    port=port,
                )
            )
    return outcomes
