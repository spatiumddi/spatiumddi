"""Operator Copilot read tool for the appliance pairing-code surface
(issue #169).

Surfaces the ``pairing_code`` table — pending + recently-terminal
rows — so a superadmin can ask the Copilot "any active pairing
codes?" / "did dns-west-2 successfully pair?" / "list claimed codes
from the last hour" through chat instead of clicking into the
Appliance UI.

Restricted to superadmin because pairing codes are agent bootstrap
auth: even the redacted shape leaks "there are N pending codes
right now for the DNS group at <id>", which is signal an attacker
could use to time a brute-force window. The runtime gate inside the
executor (``user.is_superadmin``) is the only thing standing
between a regular operator's LLM session and that data — see
``admin._superadmin_gate`` for the canonical pattern; the LLM
itself has no input on this.

No ``propose_create_pairing_code`` write tool by design. Issuing a
pairing code returns the cleartext code in the response, and we don't
want the operator's copilot transcript carrying a live bootstrap-auth
secret. The dedicated Settings → Appliance flow is the friendly path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appliance import PairingCode
from app.models.auth import User
from app.services.ai.tools.base import register_tool


def _superadmin_gate(user: User) -> dict[str, Any] | None:
    if not user.is_superadmin:
        return {
            "error": (
                "Pairing codes contain agent bootstrap secrets, so this "
                "tool is restricted to superadmin users. Ask your "
                "platform admin to run the query."
            )
        }
    return None


def _state_for(
    row: PairingCode, now: datetime
) -> Literal["pending", "claimed", "expired", "revoked"]:
    if row.used_at is not None:
        return "claimed"
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at <= now:
        return "expired"
    return "pending"


class FindPairingCodesArgs(BaseModel):
    deployment_kind: Literal["dns", "dhcp", "both"] | None = Field(
        default=None,
        description=(
            "Filter by agent kind: 'dns', 'dhcp', or 'both' (combined "
            "BIND9 + Kea). Omit to return every kind."
        ),
    )
    state: Literal["pending", "claimed", "expired", "revoked"] | None = Field(
        default=None,
        description=(
            "Filter by code state. 'pending' is the only state that's "
            "still actively usable. Omit for all states."
        ),
    )
    limit: int = Field(default=25, ge=1, le=200)


@register_tool(
    name="find_pairing_codes",
    description=(
        "List appliance pairing codes (superadmin only). Each row "
        "carries the last two digits of the code, deployment_kind "
        "(dns / dhcp / both), state (pending / claimed / expired / "
        "revoked), pre-assigned server_group_id (nullable; always "
        "null for kind='both'), expires_at, and — for claimed rows "
        "— the claiming agent's IP + hostname. Use to answer 'any "
        "active pairing codes?', 'who claimed code ending in 47?', "
        "or 'how many codes have expired without being claimed "
        "today?'. Defaults to the 25 most recent rows across every "
        "state; filter with deployment_kind + state."
    ),
    args_model=FindPairingCodesArgs,
    category="admin",
    # Default enabled (NN #13) — read-only, no secrets in response
    # (cleartext code never surfaced; code_hash never surfaced), no
    # off-prem calls. Admins discover it without opt-in. The
    # superadmin gate is the real auth.
    default_enabled=True,
    module="appliance.pairing",
)
async def find_pairing_codes(
    db: AsyncSession, user: User, args: FindPairingCodesArgs
) -> dict[str, Any]:
    if (err := _superadmin_gate(user)) is not None:
        return err

    stmt = select(PairingCode).order_by(PairingCode.created_at.desc())
    if args.deployment_kind is not None:
        stmt = stmt.where(PairingCode.deployment_kind == args.deployment_kind)
    rows = (await db.execute(stmt)).scalars().all()

    now = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for row in rows:
        state = _state_for(row, now)
        if args.state is not None and state != args.state:
            continue
        out.append(
            {
                "id": str(row.id),
                "code_last_two": row.code_last_two,
                "deployment_kind": row.deployment_kind,
                "state": state,
                "server_group_id": (str(row.server_group_id) if row.server_group_id else None),
                "note": row.note,
                "expires_at": row.expires_at.isoformat(),
                "used_at": row.used_at.isoformat() if row.used_at else None,
                "used_by_ip": row.used_by_ip,
                "used_by_hostname": row.used_by_hostname,
                "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
                "created_at": row.created_at.isoformat(),
            }
        )
        if len(out) >= args.limit:
            break

    return {
        "codes": out,
        "count": len(out),
        # Quick "is there a code an agent could be redeeming right now?"
        # rollup for the LLM's summarisation.
        "pending_count": sum(1 for c in out if c["state"] == "pending"),
    }
