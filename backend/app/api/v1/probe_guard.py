"""HTTP-layer enforcement of the do-not-probe flag — issue #722.

``app.services.ipam.probe_policy`` decides *whether* a target is
suppressed. This module is the single place that turns that decision
into an HTTP outcome, so ``tools.network`` and ``tools.nmap`` refuse in
exactly the same way, with the same status code, the same message, and
the same audit trail.

The override
------------

There is an escape hatch, and it is deliberately narrow. An operator
sometimes genuinely has to ping a device on a suppressed subnet — during
a documented maintenance window, or while proving to a vendor that the
device is unreachable. Refusing absolutely would just get the flag
turned off estate-wide, which is a worse outcome than a recorded
exception.

So ``override_do_not_probe=true``:

* requires superadmin. Not a permission grant — ``use_network_tools`` is
  a routine grant that delegated admins hold, and the point of the flag
  is that the person clearing it is accountable for the consequences.
* writes an ``audit_log`` row *before* the probe runs, with the scope,
  the level that set the flag, and the reason being overridden. The
  probe is what is auditable; whether it later succeeds is not the
  authorization decision.
* is per-request. Nothing is remembered, so the next call is refused
  again — an override is an exception, not a mode.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.services.ipam.probe_policy import ProbeVerdict, check_probe_target

# 422 rather than 403. The request is well-formed and the caller is
# authorized to use the tool — it is the *target* that is unacceptable,
# which is a semantic problem with the payload. It also keeps the
# refusal distinguishable from a genuine permission failure in logs.
_STATUS = status.HTTP_422_UNPROCESSABLE_ENTITY


def _is_superadmin(user: Any) -> bool:
    return bool(getattr(user, "is_superadmin", False))


async def enforce_probe_target(
    db: AsyncSession,
    *,
    target: str,
    tool: str,
    user: Any,
    override: bool = False,
    action_label: str = "Active probing",
) -> ProbeVerdict:
    """Refuse, or record an override, before a probe reaches the wire.

    Returns the resolved verdict so a caller can annotate its own result
    (the override path returns a blocked verdict — the target *is*
    suppressed; we are proceeding anyway). Callers that just want the
    guard can ignore the return value.

    **Commits the override audit row itself.** The obvious alternative —
    leave it in the caller's transaction — silently loses it on the
    stateless ``/tools/*`` handlers, which run their probe inline and
    never commit at all (``mtr`` is server-only, so its override would
    never have been auditable). Committing here also gets the ordering
    right everywhere else: the auditable event is the *decision to
    proceed*, so it must be durable before any packet leaves, whatever
    the probe then does or fails to do. This runs at the top of a
    handler, before any other mutation, so there is nothing half-written
    to sweep into the commit.
    """
    verdict = await check_probe_target(db, target)
    if not verdict.blocked:
        return verdict

    if not override:
        raise HTTPException(
            status_code=_STATUS,
            detail=(
                f"{verdict.message(action=action_label)}. A superadmin can proceed "
                "anyway by re-sending with override_do_not_probe=true, which is audited."
            ),
        )

    if not _is_superadmin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Overriding a do-not-probe scope requires superadmin. "
                f"{verdict.message(action=action_label)}."
            ),
        )

    db.add(
        AuditLog(
            user_id=getattr(user, "id", None),
            user_display_name=getattr(user, "display_name", "system"),
            auth_source=getattr(user, "auth_source", "local") or "local",
            action="probe.do_not_probe_override",
            resource_type="subnet",
            # The verdict names a level, not necessarily a subnet row, so
            # the source string is the identifier that actually explains
            # the override. resource_id stays free-form for the same
            # reason other cross-cutting audit rows do.
            resource_id=verdict.source,
            resource_display=verdict.scope_label or verdict.source,
            new_value={"tool": tool, "target": target, **verdict.as_dict()},
        )
    )
    await db.commit()
    return verdict


__all__ = ["enforce_probe_target"]
