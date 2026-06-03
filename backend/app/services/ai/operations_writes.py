"""Copilot write operations — batch #2 (issues #280 / #304).

The original write-operations registry lives in
:mod:`app.services.ai.operations`. This module adds the catch-up set
of ``propose_*`` operations called for by #280 / #304 — conformity,
webhooks, DNSSEC sign/unsign, multicast-domain CRUD, SNMP/NTP host
config, and DNS/DHCP config-import commits. Splitting them into a
second file keeps each registry readable; both register into the same
shared ``_OPERATIONS`` table via :func:`operations.register`.

Every operation follows the established preview/apply contract:

* ``preview(db, user, args) -> PreviewResult`` — read-only validation +
  a human description of the planned change. Never mutates.
* ``apply(db, user, args) -> dict`` — re-validates, performs the write
  through the same service paths the REST handlers use, writes an audit
  row (``via="ai_proposal"``), commits, returns a JSON summary. Raises
  ``ValueError`` on failure (the apply endpoint stamps it on the
  proposal).

The matching ``propose_*`` tool shells live in
:mod:`app.services.ai.tools.proposals`; importing the args models from
here triggers this module's registration as a side effect.

Backup write tools (#304) are intentionally **not** here: restore needs
an uploaded archive + passphrase + a typed confirmation phrase that an
MCP ``apply()`` can't (and shouldn't) supply, and create-and-download
returns a file stream rather than an apply-shaped mutation. The backup
*read* tools stay the supported surface.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dhcp._audit import write_audit
from app.core.permissions import is_effective_superadmin
from app.models.auth import User
from app.services.ai.operations import Operation, PreviewResult, register

# Re-declared here (tiny) rather than imported from the routers, so this
# module doesn't drag the whole router import graph in at load time.
_CONFORMITY_SEVERITIES = frozenset({"info", "warning", "critical"})
_CONFORMITY_TARGET_KINDS = frozenset({"platform", "subnet", "ip_address", "dns_zone", "dhcp_scope"})
_CONFORMITY_BUILTIN_MUTABLE = frozenset(
    {"enabled", "eval_interval_hours", "severity", "fail_alert_rule_id", "description"}
)


def _clip(text: str, n: int = 80) -> str:
    return text if len(text) <= n else text[: n - 3] + "..."


# ══════════════════════════════════════════════════════════════════════
# Conformity (#105 / #106)
# ══════════════════════════════════════════════════════════════════════


class CreateConformityPolicyArgs(BaseModel):
    name: str = Field(description="Policy name (unique-ish label).")
    target_kind: str = Field(
        description="One of platform / subnet / ip_address / dns_zone / dhcp_scope."
    )
    check_kind: str = Field(
        description="Check kind from the catalog (has_field / in_separate_vrf / "
        "no_open_ports / alert_rule_covers / last_seen_within / audit_log_immutable)."
    )
    framework: str = Field(default="custom", description="Framework label, e.g. PCI-DSS.")
    severity: str = Field(default="warning", description="info / warning / critical.")
    description: str = ""
    reference: str | None = None
    target_filter: dict[str, Any] = Field(default_factory=dict)
    check_args: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    eval_interval_hours: int = Field(default=24, ge=0, le=8760)


def _validate_conformity_common(args: CreateConformityPolicyArgs) -> str | None:
    from app.services.conformity.checks import CHECK_REGISTRY  # noqa: PLC0415

    if args.severity not in _CONFORMITY_SEVERITIES:
        return f"severity must be one of {sorted(_CONFORMITY_SEVERITIES)}"
    if args.target_kind not in _CONFORMITY_TARGET_KINDS:
        return f"target_kind must be one of {sorted(_CONFORMITY_TARGET_KINDS)}"
    if args.check_kind not in CHECK_REGISTRY:
        return f"check_kind must be one of {sorted(CHECK_REGISTRY)}"
    return None


async def _preview_create_conformity_policy(
    db: AsyncSession, user: User, args: CreateConformityPolicyArgs
) -> PreviewResult:
    err = _validate_conformity_common(args)
    if err:
        return PreviewResult(ok=False, detail=err)
    return PreviewResult(
        ok=True,
        detail="ready",
        preview_text=(
            f"Create conformity policy {args.name!r} "
            f"[{args.framework}/{args.severity}] — check {args.check_kind} "
            f"against {args.target_kind}; "
            f"{'enabled' if args.enabled else 'disabled'}, "
            f"every {args.eval_interval_hours}h"
        ),
    )


async def _apply_create_conformity_policy(
    db: AsyncSession, user: User, args: CreateConformityPolicyArgs
) -> dict[str, Any]:
    from app.models.conformity import ConformityPolicy  # noqa: PLC0415

    err = _validate_conformity_common(args)
    if err:
        raise ValueError(err)
    row = ConformityPolicy(
        name=args.name,
        description=args.description,
        framework=args.framework,
        reference=args.reference,
        severity=args.severity,
        target_kind=args.target_kind,
        target_filter=args.target_filter,
        check_kind=args.check_kind,
        check_args=args.check_args,
        enabled=args.enabled,
        eval_interval_hours=args.eval_interval_hours,
        is_builtin=False,
    )
    db.add(row)
    await db.flush()
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="conformity_policy",
        resource_id=str(row.id),
        resource_display=args.name,
        new_value={
            "via": "ai_proposal",
            "framework": args.framework,
            "check_kind": args.check_kind,
            "target_kind": args.target_kind,
        },
    )
    await db.commit()
    await db.refresh(row)
    return {"id": str(row.id), "name": row.name, "framework": row.framework}


class UpdateConformityPolicyArgs(BaseModel):
    policy_id: uuid.UUID
    name: str | None = None
    description: str | None = None
    severity: str | None = None
    enabled: bool | None = None
    eval_interval_hours: int | None = Field(default=None, ge=0, le=8760)
    target_filter: dict[str, Any] | None = None
    check_args: dict[str, Any] | None = None


def _conformity_update_changes(args: UpdateConformityPolicyArgs) -> dict[str, Any]:
    fields = (
        "name",
        "description",
        "severity",
        "enabled",
        "eval_interval_hours",
        "target_filter",
        "check_args",
    )
    return {f: getattr(args, f) for f in fields if getattr(args, f) is not None}


async def _preview_update_conformity_policy(
    db: AsyncSession, user: User, args: UpdateConformityPolicyArgs
) -> PreviewResult:
    from app.models.conformity import ConformityPolicy  # noqa: PLC0415

    p = await db.get(ConformityPolicy, args.policy_id)
    if p is None:
        return PreviewResult(ok=False, detail=f"Conformity policy {args.policy_id} not found")
    changes = _conformity_update_changes(args)
    if not changes:
        return PreviewResult(ok=False, detail="No fields to update")
    if args.severity is not None and args.severity not in _CONFORMITY_SEVERITIES:
        return PreviewResult(
            ok=False, detail=f"severity must be one of {sorted(_CONFORMITY_SEVERITIES)}"
        )
    if p.is_builtin:
        offending = sorted(set(changes) - _CONFORMITY_BUILTIN_MUTABLE)
        if offending:
            return PreviewResult(
                ok=False,
                detail=(
                    f"Built-in policy {p.name!r} only accepts "
                    f"{sorted(_CONFORMITY_BUILTIN_MUTABLE)}; rejected: {offending}"
                ),
            )
    summary = ", ".join(f"{k}={_clip(str(v), 40)}" for k, v in changes.items())
    return PreviewResult(
        ok=True, detail="ready", preview_text=f"Update policy {p.name!r}: {summary}"
    )


async def _apply_update_conformity_policy(
    db: AsyncSession, user: User, args: UpdateConformityPolicyArgs
) -> dict[str, Any]:
    from app.models.conformity import ConformityPolicy  # noqa: PLC0415

    p = await db.get(ConformityPolicy, args.policy_id)
    if p is None:
        raise ValueError(f"Conformity policy {args.policy_id} not found")
    changes = _conformity_update_changes(args)
    if not changes:
        raise ValueError("No fields to update")
    if args.severity is not None and args.severity not in _CONFORMITY_SEVERITIES:
        raise ValueError(f"severity must be one of {sorted(_CONFORMITY_SEVERITIES)}")
    if p.is_builtin:
        offending = sorted(set(changes) - _CONFORMITY_BUILTIN_MUTABLE)
        if offending:
            raise ValueError(
                f"Built-in policy only accepts {sorted(_CONFORMITY_BUILTIN_MUTABLE)}; "
                f"rejected: {offending}"
            )
    applied: dict[str, Any] = {}
    for key, value in changes.items():
        if getattr(p, key) != value:
            setattr(p, key, value)
            applied[key] = value
    write_audit(
        db,
        user=user,
        action="update",
        resource_type="conformity_policy",
        resource_id=str(p.id),
        resource_display=p.name,
        changed_fields=list(applied),
        new_value={"via": "ai_proposal", **{k: str(v)[:200] for k, v in applied.items()}},
    )
    await db.commit()
    await db.refresh(p)
    return {"id": str(p.id), "name": p.name, "updated_fields": list(applied)}


class EvaluateConformityPolicyArgs(BaseModel):
    policy_id: uuid.UUID


async def _preview_evaluate_conformity_policy(
    db: AsyncSession, user: User, args: EvaluateConformityPolicyArgs
) -> PreviewResult:
    from app.models.conformity import ConformityPolicy  # noqa: PLC0415

    p = await db.get(ConformityPolicy, args.policy_id)
    if p is None:
        return PreviewResult(ok=False, detail=f"Conformity policy {args.policy_id} not found")
    return PreviewResult(
        ok=True,
        detail="ready",
        preview_text=f"Run conformity policy {p.name!r} ({p.check_kind}) now",
    )


async def _apply_evaluate_conformity_policy(
    db: AsyncSession, user: User, args: EvaluateConformityPolicyArgs
) -> dict[str, Any]:
    from app.models.conformity import ConformityPolicy  # noqa: PLC0415
    from app.services.conformity import evaluate_policy  # noqa: PLC0415

    p = await db.get(ConformityPolicy, args.policy_id)
    if p is None:
        raise ValueError(f"Conformity policy {args.policy_id} not found")
    summary = await evaluate_policy(db, p)
    write_audit(
        db,
        user=user,
        action="evaluate",
        resource_type="conformity_policy",
        resource_id=str(p.id),
        resource_display=p.name,
        new_value={"via": "ai_proposal", **{k: int(v) for k, v in summary.items()}},
    )
    await db.commit()
    return {"id": str(p.id), "name": p.name, **{k: int(v) for k, v in summary.items()}}


register(
    Operation(
        name="create_conformity_policy",
        description="Create a custom conformity policy.",
        args_model=CreateConformityPolicyArgs,
        preview=_preview_create_conformity_policy,
        apply=_apply_create_conformity_policy,
        category="compliance",
    )
)
register(
    Operation(
        name="update_conformity_policy",
        description="Update a conformity policy (built-ins accept only a safe field subset).",
        args_model=UpdateConformityPolicyArgs,
        preview=_preview_update_conformity_policy,
        apply=_apply_update_conformity_policy,
        category="compliance",
    )
)
register(
    Operation(
        name="evaluate_conformity_policy",
        description="Run a conformity policy on demand and return the pass/fail rollup.",
        args_model=EvaluateConformityPolicyArgs,
        preview=_preview_evaluate_conformity_policy,
        apply=_apply_evaluate_conformity_policy,
        category="compliance",
    )
)


# ══════════════════════════════════════════════════════════════════════
# Webhooks (typed-event subscriptions) — superadmin-gated
# ══════════════════════════════════════════════════════════════════════


def _require_superadmin(user: User) -> None:
    """Apply-time guard — raises so a non-superadmin apply is rejected."""
    if not is_effective_superadmin(user):
        raise ValueError("This operation is restricted to superadmin users")


def _superadmin_preview_block(user: User) -> PreviewResult | None:
    """Preview-time guard — returns a clean rejection (never raises, so
    ``_propose_via`` surfaces it as proposal_rejected rather than an
    error)."""
    if not is_effective_superadmin(user):
        return PreviewResult(ok=False, detail="This operation is restricted to superadmin users")
    return None


class CreateWebhookArgs(BaseModel):
    name: str = Field(description="Subscription name.")
    url: str = Field(description="Delivery URL (http:// or https://).")
    description: str = ""
    enabled: bool = True
    event_types: list[str] | None = Field(
        default=None, description="Event-type filter; null = all events."
    )
    timeout_seconds: int = Field(default=10, ge=1, le=30)
    max_attempts: int = Field(default=8, ge=1, le=20)


def _webhook_write_body(args: CreateWebhookArgs, *, secret: str | None = None) -> Any:
    from app.api.v1.webhooks.router import WebhookSubscriptionWrite  # noqa: PLC0415

    return WebhookSubscriptionWrite(
        name=args.name,
        url=args.url,
        description=args.description,
        enabled=args.enabled,
        secret=secret,
        event_types=args.event_types,
        timeout_seconds=args.timeout_seconds,
        max_attempts=args.max_attempts,
    )


async def _preview_create_webhook(
    db: AsyncSession, user: User, args: CreateWebhookArgs
) -> PreviewResult:
    if (block := _superadmin_preview_block(user)) is not None:
        return block
    try:
        _webhook_write_body(args)
    except Exception as exc:  # noqa: BLE001 — surface pydantic validation
        return PreviewResult(ok=False, detail=str(exc))
    ev = "all events" if not args.event_types else f"{len(args.event_types)} event type(s)"
    return PreviewResult(
        ok=True,
        detail="ready",
        preview_text=(
            f"Create webhook {args.name!r} → {args.url} ({ev}; a signing secret "
            "is auto-generated and revealed once on apply)"
        ),
    )


async def _apply_create_webhook(
    db: AsyncSession, user: User, args: CreateWebhookArgs
) -> dict[str, Any]:
    _require_superadmin(user)
    from app.api.v1.webhooks.router import _apply_body  # noqa: PLC0415
    from app.core.demo_mode import forbid_in_demo_mode  # noqa: PLC0415
    from app.models.event_subscription import EventSubscription  # noqa: PLC0415

    forbid_in_demo_mode("Webhook subscription creation is disabled")
    sub = EventSubscription()
    plaintext = _apply_body(sub, _webhook_write_body(args), creating=True)
    db.add(sub)
    await db.flush()
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="event_subscription",
        resource_id=str(sub.id),
        resource_display=args.name,
        new_value={"via": "ai_proposal", "url": args.url, "event_types": args.event_types},
    )
    await db.commit()
    await db.refresh(sub)
    return {
        "id": str(sub.id),
        "name": sub.name,
        "url": sub.url,
        "secret_plaintext": plaintext,
    }


class UpdateWebhookArgs(BaseModel):
    subscription_id: uuid.UUID
    name: str | None = None
    url: str | None = None
    description: str | None = None
    enabled: bool | None = None
    event_types: list[str] | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=30)
    max_attempts: int | None = Field(default=None, ge=1, le=20)


async def _preview_update_webhook(
    db: AsyncSession, user: User, args: UpdateWebhookArgs
) -> PreviewResult:
    if (block := _superadmin_preview_block(user)) is not None:
        return block
    from app.models.event_subscription import EventSubscription  # noqa: PLC0415

    sub = await db.get(EventSubscription, args.subscription_id)
    if sub is None:
        return PreviewResult(ok=False, detail=f"Webhook {args.subscription_id} not found")
    bits = [f for f in ("name", "url", "enabled", "event_types") if getattr(args, f) is not None]
    return PreviewResult(
        ok=True,
        detail="ready",
        preview_text=f"Update webhook {sub.name!r}"
        + (f" — change {', '.join(bits)}" if bits else " (no identity changes)"),
    )


async def _apply_update_webhook(
    db: AsyncSession, user: User, args: UpdateWebhookArgs
) -> dict[str, Any]:
    _require_superadmin(user)
    from app.api.v1.webhooks.router import (  # noqa: PLC0415
        WebhookSubscriptionWrite,
        _apply_body,
    )
    from app.core.demo_mode import forbid_in_demo_mode  # noqa: PLC0415
    from app.models.event_subscription import EventSubscription  # noqa: PLC0415

    forbid_in_demo_mode("Webhook subscription updates are disabled")
    sub = await db.get(EventSubscription, args.subscription_id)
    if sub is None:
        raise ValueError(f"Webhook {args.subscription_id} not found")
    # _apply_body does a full replace, so merge existing values for any
    # field the operator didn't override. secret=None keeps the existing.
    # event_types is special: ``None`` is a meaningful value ("subscribe
    # to all events"), so distinguish "explicitly set (even to None)"
    # from "omitted" via model_fields_set — otherwise an update could
    # never clear a filter back to all-events.
    if "event_types" in args.model_fields_set:
        event_types = args.event_types
    else:
        event_types = sub.event_types
    body = WebhookSubscriptionWrite(
        name=args.name if args.name is not None else sub.name,
        url=args.url if args.url is not None else sub.url,
        description=args.description if args.description is not None else sub.description,
        enabled=args.enabled if args.enabled is not None else sub.enabled,
        secret=None,
        event_types=event_types,
        timeout_seconds=(
            args.timeout_seconds if args.timeout_seconds is not None else sub.timeout_seconds
        ),
        max_attempts=args.max_attempts if args.max_attempts is not None else sub.max_attempts,
    )
    _apply_body(sub, body, creating=False)
    write_audit(
        db,
        user=user,
        action="update",
        resource_type="event_subscription",
        resource_id=str(sub.id),
        resource_display=sub.name,
        new_value={"via": "ai_proposal", "url": sub.url, "enabled": sub.enabled},
    )
    await db.commit()
    await db.refresh(sub)
    return {"id": str(sub.id), "name": sub.name, "url": sub.url, "enabled": sub.enabled}


class TestWebhookArgs(BaseModel):
    subscription_id: uuid.UUID


async def _preview_test_webhook(
    db: AsyncSession, user: User, args: TestWebhookArgs
) -> PreviewResult:
    if (block := _superadmin_preview_block(user)) is not None:
        return block
    from app.models.event_subscription import EventSubscription  # noqa: PLC0415

    sub = await db.get(EventSubscription, args.subscription_id)
    if sub is None:
        return PreviewResult(ok=False, detail=f"Webhook {args.subscription_id} not found")
    return PreviewResult(
        ok=True,
        detail="ready",
        preview_text=f"Send a synthetic test.ping event to webhook {sub.name!r} ({sub.url})",
    )


async def _apply_test_webhook(
    db: AsyncSession, user: User, args: TestWebhookArgs
) -> dict[str, Any]:
    _require_superadmin(user)
    from datetime import UTC, datetime  # noqa: PLC0415

    import httpx as _httpx  # noqa: PLC0415

    from app.core.crypto import decrypt_str  # noqa: PLC0415
    from app.models.event_subscription import EventOutbox, EventSubscription  # noqa: PLC0415
    from app.services import event_delivery  # noqa: PLC0415

    sub = await db.get(EventSubscription, args.subscription_id)
    if sub is None:
        raise ValueError(f"Webhook {args.subscription_id} not found")
    secret = ""
    if sub.secret_encrypted:
        try:
            secret = decrypt_str(sub.secret_encrypted)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"secret decrypt failed: {exc}") from exc
    fake_id = uuid.uuid4()
    payload: dict[str, Any] = {
        "event_id": str(fake_id),
        "event_type": "test.ping",
        "occurred_at": datetime.now(UTC).isoformat(),
        "actor": {
            "user_id": str(user.id),
            "display_name": user.display_name,
            "auth_source": getattr(user, "auth_source", "local") or "local",
        },
        "resource": {"type": "event_subscription", "id": str(sub.id), "display": sub.name},
        "action": "test_ping",
        "result": "success",
        "old_value": None,
        "new_value": None,
        "changed_fields": [],
    }
    fake_row = EventOutbox(
        id=fake_id,
        subscription_id=sub.id,
        event_type="test.ping",
        payload=payload,
        state="in_flight",
        attempts=0,
        next_attempt_at=datetime.now(UTC),
    )
    async with _httpx.AsyncClient() as client:
        status_code, error = await event_delivery._deliver_one(  # noqa: SLF001
            client, sub, fake_row, secret, "dev"
        )
    return {
        "subscription_id": str(sub.id),
        "status": "ok" if error is None else "error",
        "status_code": status_code,
        "error": error,
    }


register(
    Operation(
        name="create_webhook",
        description="Create a typed-event webhook subscription (superadmin).",
        args_model=CreateWebhookArgs,
        preview=_preview_create_webhook,
        apply=_apply_create_webhook,
        category="webhooks",
    )
)
register(
    Operation(
        name="update_webhook",
        description="Update a webhook subscription (superadmin).",
        args_model=UpdateWebhookArgs,
        preview=_preview_update_webhook,
        apply=_apply_update_webhook,
        category="webhooks",
    )
)
register(
    Operation(
        name="test_webhook",
        description="Send a synthetic test event through a webhook's delivery path (superadmin).",
        args_model=TestWebhookArgs,
        preview=_preview_test_webhook,
        apply=_apply_test_webhook,
        category="webhooks",
    )
)


# ══════════════════════════════════════════════════════════════════════
# Multicast domain CRUD (#126)
# ══════════════════════════════════════════════════════════════════════

_PIM_MODES = frozenset({"sparse", "dense", "ssm", "bidir", "none"})
# Modes that need a rendezvous point — mirrors the multicast router's
# ``_validate_rp_for_mode`` so an update can't leave an invalid state.
_PIM_MODES_REQUIRING_RP = frozenset({"sparse", "bidir"})


def _validate_multicast_rp(mode: str, device_id: Any, rp_address: str | None) -> str | None:
    if mode in _PIM_MODES_REQUIRING_RP and device_id is None and not rp_address:
        return (
            f"pim_mode={mode!r} requires a rendezvous point "
            "(rendezvous_point_device_id or rendezvous_point_address)"
        )
    return None


class CreateMulticastDomainArgs(BaseModel):
    name: str
    pim_mode: str = Field(default="sparse", description="sparse / dense / ssm / bidir / none.")
    description: str = ""
    vrf_id: uuid.UUID | None = None
    rendezvous_point_device_id: uuid.UUID | None = None
    rendezvous_point_address: str | None = None
    ssm_range: str | None = None
    notes: str = ""


def _multicast_domain_body(args: CreateMulticastDomainArgs) -> Any:
    from app.api.v1.multicast.router import MulticastDomainCreate  # noqa: PLC0415

    return MulticastDomainCreate(
        name=args.name,
        description=args.description,
        pim_mode=args.pim_mode,
        vrf_id=args.vrf_id,
        rendezvous_point_device_id=args.rendezvous_point_device_id,
        rendezvous_point_address=args.rendezvous_point_address,
        ssm_range=args.ssm_range,
        notes=args.notes,
        tags={},
    )


async def _preview_create_multicast_domain(
    db: AsyncSession, user: User, args: CreateMulticastDomainArgs
) -> PreviewResult:
    if args.pim_mode not in _PIM_MODES:
        return PreviewResult(ok=False, detail=f"pim_mode must be one of {sorted(_PIM_MODES)}")
    try:
        self_body = _multicast_domain_body(args)
    except Exception as exc:  # noqa: BLE001
        return PreviewResult(ok=False, detail=str(exc))
    del self_body
    return PreviewResult(
        ok=True,
        detail="ready",
        preview_text=f"Create multicast (PIM) domain {args.name!r} mode={args.pim_mode}",
    )


async def _apply_create_multicast_domain(
    db: AsyncSession, user: User, args: CreateMulticastDomainArgs
) -> dict[str, Any]:
    from app.api.v1.multicast.router import (  # noqa: PLC0415
        _check_device,
        _check_vrf,
        _validate_rp_for_mode,
    )
    from app.models.multicast import MulticastDomain  # noqa: PLC0415

    if args.pim_mode not in _PIM_MODES:
        raise ValueError(f"pim_mode must be one of {sorted(_PIM_MODES)}")
    try:
        await _check_vrf(db, args.vrf_id)
        await _check_device(db, args.rendezvous_point_device_id)
        _validate_rp_for_mode(
            args.pim_mode, args.rendezvous_point_device_id, args.rendezvous_point_address
        )
    except Exception as exc:  # noqa: BLE001 — HTTPException / ValueError → operator text
        raise ValueError(getattr(exc, "detail", str(exc))) from exc
    row = MulticastDomain(
        name=args.name,
        description=args.description,
        pim_mode=args.pim_mode,
        vrf_id=args.vrf_id,
        rendezvous_point_device_id=args.rendezvous_point_device_id,
        rendezvous_point_address=args.rendezvous_point_address,
        ssm_range=args.ssm_range,
        notes=args.notes,
        tags={},
    )
    db.add(row)
    await db.flush()
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="multicast_domain",
        resource_id=str(row.id),
        resource_display=row.name,
        new_value={"via": "ai_proposal", "pim_mode": args.pim_mode},
    )
    await db.commit()
    await db.refresh(row)
    return {"id": str(row.id), "name": row.name, "pim_mode": row.pim_mode}


class UpdateMulticastDomainArgs(BaseModel):
    domain_id: uuid.UUID
    name: str | None = None
    description: str | None = None
    pim_mode: str | None = None
    rendezvous_point_address: str | None = None
    ssm_range: str | None = None
    notes: str | None = None


def _multicast_domain_changes(args: UpdateMulticastDomainArgs) -> dict[str, Any]:
    fields = ("name", "description", "pim_mode", "rendezvous_point_address", "ssm_range", "notes")
    return {f: getattr(args, f) for f in fields if getattr(args, f) is not None}


async def _preview_update_multicast_domain(
    db: AsyncSession, user: User, args: UpdateMulticastDomainArgs
) -> PreviewResult:
    from app.models.multicast import MulticastDomain  # noqa: PLC0415

    row = await db.get(MulticastDomain, args.domain_id)
    if row is None:
        return PreviewResult(ok=False, detail=f"Multicast domain {args.domain_id} not found")
    changes = _multicast_domain_changes(args)
    if not changes:
        return PreviewResult(ok=False, detail="No fields to update")
    if args.pim_mode is not None and args.pim_mode not in _PIM_MODES:
        return PreviewResult(ok=False, detail=f"pim_mode must be one of {sorted(_PIM_MODES)}")
    new_mode = args.pim_mode or row.pim_mode
    new_rp = (
        args.rendezvous_point_address
        if args.rendezvous_point_address is not None
        else row.rendezvous_point_address
    )
    rp_err = _validate_multicast_rp(new_mode, row.rendezvous_point_device_id, new_rp)
    if rp_err:
        return PreviewResult(ok=False, detail=rp_err)
    summary = ", ".join(f"{k}={_clip(str(v), 40)}" for k, v in changes.items())
    return PreviewResult(
        ok=True, detail="ready", preview_text=f"Update multicast domain {row.name!r}: {summary}"
    )


async def _apply_update_multicast_domain(
    db: AsyncSession, user: User, args: UpdateMulticastDomainArgs
) -> dict[str, Any]:
    from app.models.multicast import MulticastDomain  # noqa: PLC0415

    row = await db.get(MulticastDomain, args.domain_id)
    if row is None:
        raise ValueError(f"Multicast domain {args.domain_id} not found")
    changes = _multicast_domain_changes(args)
    if not changes:
        raise ValueError("No fields to update")
    if args.pim_mode is not None and args.pim_mode not in _PIM_MODES:
        raise ValueError(f"pim_mode must be one of {sorted(_PIM_MODES)}")
    new_mode = args.pim_mode or row.pim_mode
    new_rp = (
        args.rendezvous_point_address
        if args.rendezvous_point_address is not None
        else row.rendezvous_point_address
    )
    rp_err = _validate_multicast_rp(new_mode, row.rendezvous_point_device_id, new_rp)
    if rp_err:
        raise ValueError(rp_err)
    applied: dict[str, Any] = {}
    for key, value in changes.items():
        if getattr(row, key) != value:
            setattr(row, key, value)
            applied[key] = value
    write_audit(
        db,
        user=user,
        action="update",
        resource_type="multicast_domain",
        resource_id=str(row.id),
        resource_display=row.name,
        changed_fields=list(applied),
        new_value={"via": "ai_proposal", **{k: str(v)[:200] for k, v in applied.items()}},
    )
    await db.commit()
    await db.refresh(row)
    return {"id": str(row.id), "name": row.name, "updated_fields": list(applied)}


class DeleteMulticastDomainArgs(BaseModel):
    domain_id: uuid.UUID


async def _preview_delete_multicast_domain(
    db: AsyncSession, user: User, args: DeleteMulticastDomainArgs
) -> PreviewResult:
    from sqlalchemy import func  # noqa: PLC0415

    from app.models.multicast import MulticastDomain, MulticastGroup  # noqa: PLC0415

    row = await db.get(MulticastDomain, args.domain_id)
    if row is None:
        return PreviewResult(ok=False, detail=f"Multicast domain {args.domain_id} not found")
    n = (
        await db.execute(
            select(func.count(MulticastGroup.id)).where(MulticastGroup.domain_id == row.id)
        )
    ).scalar_one()
    note = (
        f" — {n} group(s) reference it and will have their domain link cleared (SET NULL)"
        if n
        else ""
    )
    return PreviewResult(
        ok=True, detail="ready", preview_text=f"Delete multicast domain {row.name!r}{note}"
    )


async def _apply_delete_multicast_domain(
    db: AsyncSession, user: User, args: DeleteMulticastDomainArgs
) -> dict[str, Any]:
    from app.models.multicast import MulticastDomain  # noqa: PLC0415

    row = await db.get(MulticastDomain, args.domain_id)
    if row is None:
        raise ValueError(f"Multicast domain {args.domain_id} not found")
    name = row.name
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="multicast_domain",
        resource_id=str(row.id),
        resource_display=name,
        new_value={"via": "ai_proposal"},
    )
    await db.delete(row)
    await db.commit()
    return {"id": str(args.domain_id), "name": name, "deleted": True}


register(
    Operation(
        name="create_multicast_domain",
        description="Create a multicast PIM domain.",
        args_model=CreateMulticastDomainArgs,
        preview=_preview_create_multicast_domain,
        apply=_apply_create_multicast_domain,
        category="multicast",
    )
)
register(
    Operation(
        name="update_multicast_domain",
        description="Update a multicast PIM domain.",
        args_model=UpdateMulticastDomainArgs,
        preview=_preview_update_multicast_domain,
        apply=_apply_update_multicast_domain,
        category="multicast",
    )
)
register(
    Operation(
        name="delete_multicast_domain",
        description="Delete a multicast PIM domain (group links are cleared, not deleted).",
        args_model=DeleteMulticastDomainArgs,
        preview=_preview_delete_multicast_domain,
        apply=_apply_delete_multicast_domain,
        category="multicast",
    )
)


# ══════════════════════════════════════════════════════════════════════
# DNSSEC sign / unsign (#49 / #127)
# ══════════════════════════════════════════════════════════════════════


class SignZoneDNSSECArgs(BaseModel):
    group_id: uuid.UUID
    zone_id: uuid.UUID
    policy_id: uuid.UUID | None = Field(
        default=None, description="DNSSEC policy to apply; omit to keep the current/default."
    )


class UnsignZoneDNSSECArgs(BaseModel):
    group_id: uuid.UUID
    zone_id: uuid.UUID


async def _dnssec_lookup(
    db: AsyncSession, user: User, group_id: uuid.UUID, zone_id: uuid.UUID, op: str
) -> Any:
    """Fetch+gate the zone the way the DNS router does, translating its
    HTTPException into an operator-facing ValueError. The DNS router's
    sign/unsign endpoints are superadmin-only, so re-check that here —
    a proposal approval must not bypass the REST authorization."""
    _require_superadmin(user)
    from fastapi import HTTPException  # noqa: PLC0415

    from app.api.v1.dns import router as dns_router  # noqa: PLC0415

    try:
        zone = await dns_router._require_zone(group_id, zone_id, db)  # noqa: SLF001
        dns_router._reject_if_synthesised_zone(zone, op)  # noqa: SLF001
        await dns_router._check_driver_gated_operation(op, group_id, db)  # noqa: SLF001
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc
    return zone


async def _preview_sign_zone_dnssec(
    db: AsyncSession, user: User, args: SignZoneDNSSECArgs
) -> PreviewResult:
    try:
        zone = await _dnssec_lookup(db, user, args.group_id, args.zone_id, "dnssec_sign")
    except ValueError as exc:
        return PreviewResult(ok=False, detail=str(exc))
    pol = ""
    if args.policy_id is not None:
        pol = f" with policy {args.policy_id}"
    return PreviewResult(
        ok=True,
        detail="ready",
        preview_text=f"Enable DNSSEC signing on zone {zone.name}{pol} (propagates to live servers)",
    )


async def _apply_sign_zone_dnssec(
    db: AsyncSession, user: User, args: SignZoneDNSSECArgs
) -> dict[str, Any]:
    from app.api.v1.dns import router as dns_router  # noqa: PLC0415
    from app.models.dns import DNSSECPolicy  # noqa: PLC0415

    zone = await _dnssec_lookup(db, user, args.group_id, args.zone_id, "dnssec_sign")
    zone.dnssec_enabled = True
    if args.policy_id is not None:
        pol = await db.get(DNSSECPolicy, args.policy_id)
        if pol is None:
            raise ValueError("DNSSEC policy not found")
        zone.dnssec_policy_id = args.policy_id
    await dns_router.enqueue_record_op(db, zone, "dnssec_sign", {"name": "@", "type": "DNSSEC_OP"})
    write_audit(
        db,
        user=user,
        action="dnssec_sign",
        resource_type="dns_zone",
        resource_id=str(zone.id),
        resource_display=zone.name,
        new_value={
            "via": "ai_proposal",
            "policy_id": str(args.policy_id) if args.policy_id else None,
        },
    )
    await db.commit()
    await db.refresh(zone)
    return {"id": str(zone.id), "name": zone.name, "dnssec_enabled": True}


async def _preview_unsign_zone_dnssec(
    db: AsyncSession, user: User, args: UnsignZoneDNSSECArgs
) -> PreviewResult:
    try:
        zone = await _dnssec_lookup(db, user, args.group_id, args.zone_id, "dnssec_unsign")
    except ValueError as exc:
        return PreviewResult(ok=False, detail=str(exc))
    return PreviewResult(
        ok=True,
        detail="ready",
        preview_text=(
            f"Disable DNSSEC on zone {zone.name} — clears keys + DS; validating "
            "resolvers will SERVFAIL until the parent DS is removed"
        ),
    )


async def _apply_unsign_zone_dnssec(
    db: AsyncSession, user: User, args: UnsignZoneDNSSECArgs
) -> dict[str, Any]:
    from sqlalchemy import delete as sa_delete  # noqa: PLC0415

    from app.api.v1.dns import router as dns_router  # noqa: PLC0415
    from app.models.dns import DNSKey  # noqa: PLC0415

    zone = await _dnssec_lookup(db, user, args.group_id, args.zone_id, "dnssec_unsign")
    zone.dnssec_enabled = False
    zone.dnssec_ds_records = None
    await db.execute(sa_delete(DNSKey).where(DNSKey.zone_id == zone.id))
    await dns_router.enqueue_record_op(
        db, zone, "dnssec_unsign", {"name": "@", "type": "DNSSEC_OP"}
    )
    write_audit(
        db,
        user=user,
        action="dnssec_unsign",
        resource_type="dns_zone",
        resource_id=str(zone.id),
        resource_display=zone.name,
        new_value={"via": "ai_proposal"},
    )
    await db.commit()
    await db.refresh(zone)
    return {"id": str(zone.id), "name": zone.name, "dnssec_enabled": False}


register(
    Operation(
        name="sign_zone_dnssec",
        description="Enable DNSSEC signing on a DNS zone (BIND9 / PowerDNS).",
        args_model=SignZoneDNSSECArgs,
        preview=_preview_sign_zone_dnssec,
        apply=_apply_sign_zone_dnssec,
        category="dns",
    )
)
register(
    Operation(
        name="unsign_zone_dnssec",
        description="Disable DNSSEC signing on a DNS zone (clears keys + DS).",
        args_model=UnsignZoneDNSSECArgs,
        preview=_preview_unsign_zone_dnssec,
        apply=_apply_unsign_zone_dnssec,
        category="dns",
    )
)


# ══════════════════════════════════════════════════════════════════════
# SNMP / NTP appliance host config (#153 / #154)
# ══════════════════════════════════════════════════════════════════════

_SNMP_VERSIONS = frozenset({"v2c", "v3"})
_NTP_SOURCE_MODES = frozenset({"pool", "servers", "mixed"})


class UpdateSNMPSettingsArgs(BaseModel):
    enabled: bool
    version: str = Field(default="v2c", description="v2c or v3.")
    community: str | None = Field(
        default=None, description="v2c community string (stored Fernet-encrypted)."
    )
    allowed_sources: list[str] | None = Field(
        default=None, description="Source CIDR allow-list for snmpd."
    )
    sys_contact: str | None = None
    sys_location: str | None = None


async def _preview_update_snmp_settings(
    db: AsyncSession, user: User, args: UpdateSNMPSettingsArgs
) -> PreviewResult:
    if (block := _superadmin_preview_block(user)) is not None:
        return block
    if args.version not in _SNMP_VERSIONS:
        return PreviewResult(ok=False, detail=f"version must be one of {sorted(_SNMP_VERSIONS)}")
    bits = [f"enabled={args.enabled}", f"version={args.version}"]
    if args.community is not None:
        bits.append("community=<set>")
    if args.allowed_sources is not None:
        bits.append(f"allowed_sources={args.allowed_sources}")
    return PreviewResult(
        ok=True,
        detail="ready",
        preview_text="Update SNMP host config: " + ", ".join(bits),
    )


async def _apply_update_snmp_settings(
    db: AsyncSession, user: User, args: UpdateSNMPSettingsArgs
) -> dict[str, Any]:
    _require_superadmin(user)
    if args.version not in _SNMP_VERSIONS:
        raise ValueError(f"version must be one of {sorted(_SNMP_VERSIONS)}")
    from app.api.v1.settings.router import _get_or_create  # noqa: PLC0415
    from app.core.crypto import encrypt_str  # noqa: PLC0415
    from app.core.demo_mode import forbid_in_demo_mode  # noqa: PLC0415

    forbid_in_demo_mode("Platform settings updates are disabled")
    settings = await _get_or_create(db)
    settings.snmp_enabled = args.enabled
    settings.snmp_version = args.version
    if args.community is not None:
        settings.snmp_community_encrypted = encrypt_str(args.community) if args.community else None
    if args.allowed_sources is not None:
        settings.snmp_allowed_sources = args.allowed_sources
    if args.sys_contact is not None:
        settings.snmp_sys_contact = args.sys_contact
    if args.sys_location is not None:
        settings.snmp_sys_location = args.sys_location
    write_audit(
        db,
        user=user,
        action="update",
        resource_type="platform_settings",
        resource_id="snmp",
        resource_display="SNMP host config",
        new_value={"via": "ai_proposal", "enabled": args.enabled, "version": args.version},
    )
    await db.commit()
    return {"snmp_enabled": args.enabled, "snmp_version": args.version}


class UpdateNTPSettingsArgs(BaseModel):
    source_mode: str = Field(description="pool / servers / mixed.")
    pool_servers: list[str] | None = None
    allow_clients: bool | None = None
    allow_client_networks: list[str] | None = None


async def _preview_update_ntp_settings(
    db: AsyncSession, user: User, args: UpdateNTPSettingsArgs
) -> PreviewResult:
    if (block := _superadmin_preview_block(user)) is not None:
        return block
    if args.source_mode not in _NTP_SOURCE_MODES:
        return PreviewResult(
            ok=False, detail=f"source_mode must be one of {sorted(_NTP_SOURCE_MODES)}"
        )
    bits = [f"source_mode={args.source_mode}"]
    if args.pool_servers is not None:
        bits.append(f"pool_servers={args.pool_servers}")
    if args.allow_clients is not None:
        bits.append(f"allow_clients={args.allow_clients}")
    return PreviewResult(
        ok=True, detail="ready", preview_text="Update NTP host config: " + ", ".join(bits)
    )


async def _apply_update_ntp_settings(
    db: AsyncSession, user: User, args: UpdateNTPSettingsArgs
) -> dict[str, Any]:
    _require_superadmin(user)
    if args.source_mode not in _NTP_SOURCE_MODES:
        raise ValueError(f"source_mode must be one of {sorted(_NTP_SOURCE_MODES)}")
    from app.api.v1.settings.router import _get_or_create  # noqa: PLC0415
    from app.core.demo_mode import forbid_in_demo_mode  # noqa: PLC0415

    forbid_in_demo_mode("Platform settings updates are disabled")
    settings = await _get_or_create(db)
    settings.ntp_source_mode = args.source_mode
    if args.pool_servers is not None:
        settings.ntp_pool_servers = args.pool_servers
    if args.allow_clients is not None:
        settings.ntp_allow_clients = args.allow_clients
    if args.allow_client_networks is not None:
        settings.ntp_allow_client_networks = args.allow_client_networks
    write_audit(
        db,
        user=user,
        action="update",
        resource_type="platform_settings",
        resource_id="ntp",
        resource_display="NTP host config",
        new_value={"via": "ai_proposal", "source_mode": args.source_mode},
    )
    await db.commit()
    return {"ntp_source_mode": args.source_mode}


register(
    Operation(
        name="update_snmp_settings",
        description="Update appliance SNMP host config (superadmin).",
        args_model=UpdateSNMPSettingsArgs,
        preview=_preview_update_snmp_settings,
        apply=_apply_update_snmp_settings,
        category="admin",
    )
)
register(
    Operation(
        name="update_ntp_settings",
        description="Update appliance NTP / chrony host config (superadmin).",
        args_model=UpdateNTPSettingsArgs,
        preview=_preview_update_ntp_settings,
        apply=_apply_update_ntp_settings,
        category="admin",
    )
)


# ══════════════════════════════════════════════════════════════════════
# DNS / DHCP config import (commit) — live-pull sources only (#128 / #129)
# ══════════════════════════════════════════════════════════════════════
#
# File-upload sources (BIND9 archive, Kea/ISC config) can't be driven
# from chat — you can't paste a tarball — so these tools restrict to the
# live-pull sources (Windows DNS / PowerDNS for DNS; Windows DHCP for
# DHCP). The Operation does the pull + commit in one apply(); the
# preview shows the would-import counts.


class CommitDNSImportArgs(BaseModel):
    source: Literal["windows_dns", "powerdns"] = Field(
        description="Live-pull source. File uploads (bind9) must use the UI."
    )
    target_group_id: uuid.UUID
    server_id: uuid.UUID | None = Field(
        default=None, description="DNS server row (required for windows_dns)."
    )
    api_url: str | None = Field(default=None, description="PowerDNS API URL (powerdns).")
    api_key: str | None = Field(default=None, description="PowerDNS API key (powerdns).")
    server_name: str = "localhost"
    target_view_id: uuid.UUID | None = None


async def _dns_import_pull(db: AsyncSession, args: CommitDNSImportArgs) -> Any:
    """Live-pull the source into an ImportPreview + attach conflicts.
    Raises ValueError with operator-facing text on any failure."""
    from app.services.dns_import import (  # noqa: PLC0415
        PowerDNSImportError,
        WindowsDNSImportError,
        detect_conflicts,
        parse_powerdns_server,
        parse_windows_dns_server,
    )

    if args.source == "windows_dns":
        if args.server_id is None:
            raise ValueError("server_id is required for the windows_dns source")
        from app.models.dns import DNSServer  # noqa: PLC0415

        server = (
            await db.execute(select(DNSServer).where(DNSServer.id == args.server_id))
        ).scalar_one_or_none()
        if server is None:
            raise ValueError(f"DNS server {args.server_id} not found")
        if server.driver != "windows_dns":
            raise ValueError(f"Server {server.name!r} is driver {server.driver!r}, not windows_dns")
        if not server.credentials_encrypted:
            raise ValueError(f"Server {server.name!r} has no WinRM credentials configured")
        try:
            preview = await parse_windows_dns_server(server)
        except WindowsDNSImportError as exc:
            raise ValueError(str(exc)) from exc
    else:  # powerdns
        if not args.api_url or not args.api_key:
            raise ValueError("api_url and api_key are required for the powerdns source")
        try:
            preview = await parse_powerdns_server(
                api_url=args.api_url, api_key=args.api_key, server_name=args.server_name
            )
        except PowerDNSImportError as exc:
            raise ValueError(str(exc)) from exc

    zone_names = [(z.name if z.name.endswith(".") else z.name + ".").lower() for z in preview.zones]
    preview.conflicts = await detect_conflicts(
        db,
        zone_names=zone_names,
        target_group_id=args.target_group_id,
        target_view_id=args.target_view_id,
    )
    return preview


def _dns_preview_text(preview: Any) -> str:
    conflicts = len(preview.conflicts)
    return (
        f"Import {len(preview.zones)} zone(s) / {preview.total_records} record(s) from "
        f"{preview.source}" + (f"; {conflicts} conflict(s) will be SKIPPED" if conflicts else "")
    )


async def _preview_commit_dns_import(
    db: AsyncSession, user: User, args: CommitDNSImportArgs
) -> PreviewResult:
    if (block := _superadmin_preview_block(user)) is not None:
        return block
    try:
        preview = await _dns_import_pull(db, args)
    except ValueError as exc:
        return PreviewResult(ok=False, detail=str(exc))
    if not preview.zones:
        return PreviewResult(ok=False, detail="No zones found to import from the source")
    return PreviewResult(ok=True, detail="ready", preview_text=_dns_preview_text(preview))


async def _apply_commit_dns_import(
    db: AsyncSession, user: User, args: CommitDNSImportArgs
) -> dict[str, Any]:
    _require_superadmin(user)
    from app.services.dns_import import commit_import  # noqa: PLC0415

    preview = await _dns_import_pull(db, args)
    if not preview.zones:
        raise ValueError("No zones found to import from the source")
    result = await commit_import(
        db,
        preview=preview,
        target_group_id=args.target_group_id,
        target_view_id=args.target_view_id,
        conflict_actions={},
        current_user=user,
    )
    return {
        "zones_created": result.total_zones_created,
        "zones_skipped": result.total_zones_skipped,
        "zones_failed": result.total_zones_failed,
        "records_created": result.total_records_created,
    }


class CommitDHCPImportArgs(BaseModel):
    source: Literal["windows_dhcp"] = Field(
        description="Live-pull source. File uploads (kea / isc) must use the UI."
    )
    server_id: uuid.UUID = Field(description="DHCP server row to pull from.")
    target_group_id: uuid.UUID
    ipam_space_id: uuid.UUID | None = Field(
        default=None, description="IP space for subnet auto-create (with ipam_block_id)."
    )
    ipam_block_id: uuid.UUID | None = None


async def _dhcp_import_pull(db: AsyncSession, args: CommitDHCPImportArgs) -> Any:
    from app.models.dhcp import DHCPServer  # noqa: PLC0415
    from app.services.dhcp_import import (  # noqa: PLC0415
        WindowsDHCPImportError,
        detect_conflicts,
        parse_windows_dhcp_server,
    )

    server = (
        await db.execute(select(DHCPServer).where(DHCPServer.id == args.server_id))
    ).scalar_one_or_none()
    if server is None:
        raise ValueError(f"DHCP server {args.server_id} not found")
    if server.driver != "windows_dhcp":
        raise ValueError(f"Server {server.name!r} is driver {server.driver!r}, not windows_dhcp")
    if not server.credentials_encrypted:
        raise ValueError(f"Server {server.name!r} has no Windows credentials configured")
    try:
        preview = await parse_windows_dhcp_server(server)
    except WindowsDHCPImportError as exc:
        raise ValueError(str(exc)) from exc
    preview.conflicts = await detect_conflicts(
        db,
        scope_cidrs=[s.subnet_cidr for s in preview.scopes],
        target_group_id=args.target_group_id,
        ipam_space_id=args.ipam_space_id,
    )
    return preview


def _dhcp_preview_text(preview: Any) -> str:
    return (
        f"Import {len(preview.scopes)} scope(s) / {preview.total_pools} pool(s) / "
        f"{preview.total_reservations} reservation(s) from {preview.source}"
    )


async def _preview_commit_dhcp_import(
    db: AsyncSession, user: User, args: CommitDHCPImportArgs
) -> PreviewResult:
    if (block := _superadmin_preview_block(user)) is not None:
        return block
    try:
        preview = await _dhcp_import_pull(db, args)
    except ValueError as exc:
        return PreviewResult(ok=False, detail=str(exc))
    if not preview.scopes:
        return PreviewResult(ok=False, detail="No scopes found to import from the source")
    return PreviewResult(ok=True, detail="ready", preview_text=_dhcp_preview_text(preview))


async def _apply_commit_dhcp_import(
    db: AsyncSession, user: User, args: CommitDHCPImportArgs
) -> dict[str, Any]:
    _require_superadmin(user)
    from app.services.dhcp_import import commit_import  # noqa: PLC0415

    preview = await _dhcp_import_pull(db, args)
    if not preview.scopes:
        raise ValueError("No scopes found to import from the source")
    result = await commit_import(
        db,
        preview=preview,
        target_group_id=args.target_group_id,
        ipam_space_id=args.ipam_space_id,
        ipam_block_id=args.ipam_block_id,
        conflict_actions={},
        current_user=user,
    )
    return {
        "scopes_created": result.total_scopes_created,
        "scopes_skipped": result.total_scopes_skipped,
        "scopes_failed": result.total_scopes_failed,
        "subnets_created": result.total_subnets_created,
        "pools_created": result.total_pools_created,
        "reservations_created": result.total_reservations_created,
    }


register(
    Operation(
        name="commit_dns_import",
        description="Live-pull + import DNS zones from a Windows DNS / PowerDNS server.",
        args_model=CommitDNSImportArgs,
        preview=_preview_commit_dns_import,
        apply=_apply_commit_dns_import,
        category="dns",
    )
)
register(
    Operation(
        name="commit_dhcp_import",
        description="Live-pull + import DHCP scopes from a Windows DHCP server.",
        args_model=CommitDHCPImportArgs,
        preview=_preview_commit_dhcp_import,
        apply=_apply_commit_dhcp_import,
        category="dhcp",
    )
)
