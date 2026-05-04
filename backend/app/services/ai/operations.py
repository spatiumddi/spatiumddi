"""Copilot write operations — preview / apply pattern.

A write tool the LLM fires never executes directly. Instead, the
``propose_*`` tool calls the operation's :func:`preview` (read-only)
and persists an ``ai_operation_proposal`` row. The chat surface
renders the proposal as an Apply / Discard card; the actual mutation
runs only after an explicit POST to ``/api/v1/ai/proposals/{id}/apply``.

This module owns the registry of operations + their preview / apply
implementations. The ``propose_*`` tools live in
``services/ai/tools/`` and import :func:`get_operation` to do the
preview + persist dance; the API router lives in
``api/v1/ai/proposals.py`` and imports the same registry to do the
apply / discard dance.

CLAUDE.md non-negotiables that apply here:
* #4 (audit everything) — apply functions MUST go through the
  service layer paths that already audit, OR write their own audit
  row before commit. The proposal row itself is *not* a substitute
  for an audit-log row.
* #2 (async throughout) — preview + apply are both async; they
  receive the calling user's DB session and User row.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.ipam import IPAddress, Subnet

# Per-proposal TTL. 30 minutes is a generous window for a thoughtful
# review without keeping yesterday's proposals lying around — the
# cleanup task drops expired+unapplied rows on the next sweep.
PROPOSAL_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class Operation:
    """One write operation. ``preview`` produces the human-readable
    description (no side effects); ``apply`` performs the mutation
    and returns a JSON-serialisable result.
    """

    name: str
    description: str
    args_model: type[BaseModel]
    preview: Callable[[AsyncSession, User, BaseModel], Awaitable[PreviewResult]]
    apply: Callable[[AsyncSession, User, BaseModel], Awaitable[dict[str, Any]]]
    # Free-form category for grouping in the admin UI — same vocabulary
    # as the read-only tool registry ("ipam", "dns", "dhcp").
    category: str = "ops"


@dataclass(frozen=True)
class PreviewResult:
    """Outcome of an operation's :func:`preview` step.

    ``ok=False`` means the preview itself rejected the args (e.g.
    subnet doesn't exist, address out of range) — surface ``detail``
    to the operator and don't even create a proposal row. ``ok=True``
    proceeds to persist the proposal with ``preview_text``.
    """

    ok: bool
    detail: str
    preview_text: str = ""


_OPERATIONS: dict[str, Operation] = {}


def register(op: Operation) -> None:
    if op.name in _OPERATIONS:
        raise ValueError(f"Operation {op.name!r} already registered")
    _OPERATIONS[op.name] = op


def get_operation(name: str) -> Operation | None:
    return _OPERATIONS.get(name)


def all_operations() -> list[Operation]:
    return sorted(_OPERATIONS.values(), key=lambda o: o.name)


def expires_at_default() -> datetime:
    """Stamp every new proposal with ``now + PROPOSAL_TTL``."""
    return datetime.now(UTC) + PROPOSAL_TTL


# ── create_ip_address operation (issue #90 Phase 2 first write tool) ─────────


class CreateIPAddressArgs(BaseModel):
    """Args for the ``create_ip_address`` operation."""

    subnet_id: str = Field(description="UUID of the subnet to create the address in")
    address: str = Field(description="The IP address as a string (e.g. 10.0.5.10)")
    status: str = Field(
        default="allocated",
        description=(
            "IP status: 'allocated' (default), 'reserved', or "
            "'static_dhcp'. Static_dhcp requires mac_address."
        ),
    )
    hostname: str | None = Field(default=None, description="Hostname (e.g. web01)")
    fqdn: str | None = Field(
        default=None, description="Fully-qualified domain name (e.g. web01.prod.example.com)"
    )
    mac_address: str | None = Field(default=None, description="MAC address in any standard format")
    description: str = Field(default="", description="Free-form description")


async def _preview_create_ip_address(
    db: AsyncSession, user: User, args: CreateIPAddressArgs
) -> PreviewResult:
    # Resolve the subnet so the preview can name it.
    subnet = await db.get(Subnet, args.subnet_id)
    if subnet is None:
        return PreviewResult(ok=False, detail=f"Subnet {args.subnet_id} not found")

    try:
        addr_obj = ipaddress.ip_address(args.address)
    except ValueError:
        return PreviewResult(ok=False, detail=f"Invalid IP address: {args.address!r}")

    try:
        net = ipaddress.ip_network(str(subnet.network), strict=False)
    except ValueError:
        return PreviewResult(ok=False, detail=f"Subnet network {subnet.network!r} is unparseable")
    if addr_obj not in net:
        return PreviewResult(
            ok=False,
            detail=(
                f"Address {args.address} is not within subnet {subnet.network} "
                f"({subnet.name or 'unnamed'})"
            ),
        )

    # Check for existing allocation — a non-blocking cue that apply
    # will likely 409. The preview deliberately doesn't reject; the
    # operator might be replacing a stale row.
    existing = (
        await db.execute(
            select(IPAddress).where(
                IPAddress.subnet_id == subnet.id,
                IPAddress.address == args.address,
            )
        )
    ).scalar_one_or_none()
    suffix = ""
    if existing is not None:
        suffix = (
            f" — note: address is already recorded with status "
            f"{existing.status!r}; apply will fail unless you delete it first"
        )

    parts = [
        f"Create IP {args.address}",
        f"in subnet {subnet.network}{f' ({subnet.name})' if subnet.name else ''}",
        f"status={args.status}",
    ]
    if args.hostname:
        parts.append(f"hostname={args.hostname}")
    if args.fqdn:
        parts.append(f"fqdn={args.fqdn}")
    if args.mac_address:
        parts.append(f"mac={args.mac_address}")
    if args.description:
        # Truncate to keep the preview readable.
        d = args.description if len(args.description) < 80 else args.description[:77] + "..."
        parts.append(f"desc={d!r}")
    return PreviewResult(ok=True, detail="ready", preview_text=", ".join(parts) + suffix)


async def _apply_create_ip_address(
    db: AsyncSession, user: User, args: CreateIPAddressArgs
) -> dict[str, Any]:
    """Re-validate at apply time + insert the row.

    Mirrors the conflict checks from the IPAM router's create_address
    handler. We don't import that handler directly (it's bound to a
    FastAPI request shape) — duplicating the few-line conflict check
    is the simpler alternative until the apply set grows.
    """
    from app.api.v1.dhcp._audit import write_audit  # local import to avoid cycle

    subnet = await db.get(Subnet, args.subnet_id)
    if subnet is None:
        raise ValueError(f"Subnet {args.subnet_id} not found")

    try:
        addr_obj = ipaddress.ip_address(args.address)
    except ValueError as exc:
        raise ValueError(f"Invalid IP address: {args.address!r}") from exc

    net = ipaddress.ip_network(str(subnet.network), strict=False)
    if addr_obj not in net:
        raise ValueError(f"Address {args.address} is not within subnet {subnet.network}")

    # Re-check for existing allocation under the apply transaction.
    existing = (
        await db.execute(
            select(IPAddress).where(
                IPAddress.subnet_id == subnet.id,
                IPAddress.address == args.address,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(
            f"Address {args.address} is already allocated in subnet {subnet.network} "
            f"(status={existing.status})"
        )

    if args.status == "static_dhcp" and not args.mac_address:
        raise ValueError("mac_address is required when status is 'static_dhcp'")

    row = IPAddress(
        subnet_id=subnet.id,
        address=args.address,
        status=args.status,
        hostname=args.hostname,
        fqdn=args.fqdn,
        mac_address=args.mac_address,
        description=args.description or "",
    )
    db.add(row)
    await db.flush()

    # Audit the apply path so the audit log captures the outcome (the
    # propose step doesn't audit — proposals can be discarded). The
    # event ties the AI to the mutation via the user_display_name +
    # action="ai_apply".
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="ipam.ip_address",
        resource_id=str(row.id),
        resource_display=str(args.address),
        new_value={
            "subnet_id": str(subnet.id),
            "subnet": str(subnet.network),
            "address": args.address,
            "status": args.status,
            "hostname": args.hostname,
            "via": "ai_proposal",
        },
    )
    await db.commit()
    await db.refresh(row)
    return {
        "id": str(row.id),
        "address": args.address,
        "subnet_id": str(subnet.id),
        "status": args.status,
        "hostname": args.hostname,
    }


register(
    Operation(
        name="create_ip_address",
        description=(
            "Allocate a new IP address inside a subnet. Use this when "
            "the operator asks you to create / allocate / assign an "
            "IP. Pass subnet_id (UUID), address, status, and optional "
            "hostname / fqdn / mac_address / description. Always go "
            "through propose_create_ip_address — never call this "
            "directly without an explicit operator approval step."
        ),
        args_model=CreateIPAddressArgs,
        preview=_preview_create_ip_address,
        apply=_apply_create_ip_address,
        category="ipam",
    )
)
