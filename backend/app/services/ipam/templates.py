"""IPAM template apply / reapply / pre-fill service (issue #26).

Templates STAMP values onto blocks or subnets at apply time —
inheritance is a separate read-time mechanism. Re-apply is the
operator-driven way to refresh stamp values to match the latest
template definition.

Apply policy:
    - ``force=True``: every template-bearing column overwrites the
      target unconditionally.
    - ``force=False``: only empty/null target columns are filled
      from the template.

For ``applies_to='block'`` templates with a non-null ``child_layout``,
``carve_children`` walks the layout list and creates one Subnet per
child entry under the block. Idempotent — children already at a
target CIDR are left alone.
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ipam import IPAMTemplate, IPBlock, Subnet


class TemplateError(Exception):
    """Raised by the template service when validation fails."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


_TEMPLATE_FIELDS_COMMON: tuple[str, ...] = (
    "tags",
    "custom_fields",
    "dns_zone_id",
    "dns_additional_zone_ids",
    "ddns_enabled",
    "ddns_hostname_policy",
    "ddns_domain_override",
    "ddns_ttl",
)


def _is_empty(value: Any) -> bool:
    """Treat None / empty dict / empty list / empty string as fillable."""
    if value is None:
        return True
    if isinstance(value, (dict, list, str)) and len(value) == 0:
        return True
    return False


def _stamp(target: Any, name: str, template_value: Any, *, force: bool) -> bool:
    """Stamp ``template_value`` onto ``target.{name}`` per apply policy.

    Returns True if the column was written.
    """
    current = getattr(target, name, None)
    if force or _is_empty(current):
        setattr(target, name, template_value)
        return True
    return False


def _stamp_dns_group_ids(target: Any, dns_group_id: uuid.UUID | None, *, force: bool) -> bool:
    """The template carries a single ``dns_group_id`` column; the IPAM
    carrier columns store ``dns_group_ids`` as a JSONB list of strings
    (legacy multi-group shape). Coerce single → list-of-one on stamp.
    """
    current = getattr(target, "dns_group_ids", None)
    if dns_group_id is None:
        # Template explicitly says "no DNS group" — only overwrite on
        # force. Otherwise leave whatever the operator already set.
        if force:
            target.dns_group_ids = []
            return True
        return False
    new_value = [str(dns_group_id)]
    if force or _is_empty(current):
        target.dns_group_ids = new_value
        return True
    return False


def _stamp_dhcp_group(target: Any, dhcp_group_id: uuid.UUID | None, *, force: bool) -> bool:
    if dhcp_group_id is None and not force:
        return False
    current = getattr(target, "dhcp_server_group_id", None)
    if force or current is None:
        target.dhcp_server_group_id = dhcp_group_id
        return True
    return False


def _apply_ddns_lock(target: Any, template: IPAMTemplate, *, force: bool) -> None:
    """When the template stamps any DDNS column, also flip the target's
    ``ddns_inherit_settings=False`` so the stamped values actually
    take effect. Only relevant for IPBlock + Subnet — the IPSpace
    DDNS columns don't have an inherit flag.
    """
    if not hasattr(target, "ddns_inherit_settings"):
        return
    template_writes_ddns = (
        template.ddns_enabled
        or template.ddns_hostname_policy != "client_or_generated"
        or template.ddns_domain_override is not None
        or template.ddns_ttl is not None
    )
    if template_writes_ddns and (force or target.ddns_inherit_settings):
        target.ddns_inherit_settings = False


# ── Apply to existing carriers ────────────────────────────────────────


def apply_template_to_block(
    template: IPAMTemplate,
    block: IPBlock,
    *,
    force: bool = False,
) -> list[str]:
    """Stamp ``template`` values onto ``block``. Returns the list of
    column names that were actually written. Caller commits.
    """
    if template.applies_to != "block":
        raise TemplateError(
            f"Template {template.name!r} applies to {template.applies_to!r}, not 'block'."
        )
    written: list[str] = []
    for field in _TEMPLATE_FIELDS_COMMON:
        if _stamp(block, field, getattr(template, field), force=force):
            written.append(field)
    if _stamp_dns_group_ids(block, template.dns_group_id, force=force):
        written.append("dns_group_ids")
    if _stamp_dhcp_group(block, template.dhcp_group_id, force=force):
        written.append("dhcp_server_group_id")
    _apply_ddns_lock(block, template, force=force)
    block.applied_template_id = template.id
    return written


def apply_template_to_subnet(
    template: IPAMTemplate,
    subnet: Subnet,
    *,
    force: bool = False,
) -> list[str]:
    if template.applies_to != "subnet":
        raise TemplateError(
            f"Template {template.name!r} applies to {template.applies_to!r}, not 'subnet'."
        )
    written: list[str] = []
    for field in _TEMPLATE_FIELDS_COMMON:
        if _stamp(subnet, field, getattr(template, field), force=force):
            written.append(field)
    if _stamp_dns_group_ids(subnet, template.dns_group_id, force=force):
        written.append("dns_group_ids")
    if _stamp_dhcp_group(subnet, template.dhcp_group_id, force=force):
        written.append("dhcp_server_group_id")
    _apply_ddns_lock(subnet, template, force=force)
    subnet.applied_template_id = template.id
    return written


# ── Pre-fill on create ────────────────────────────────────────────────


def _prefill(body: Any, name: str, template_value: Any) -> None:
    """Fill ``body.{name}`` from ``template_value`` when the operator
    didn't explicitly set the field. Pydantic's ``model_fields_set``
    distinguishes "operator typed False" from "operator omitted the
    field" — booleans default to False on the create schema, so
    introspection is the only way to know which.

    When ``model_fields_set`` is unavailable (non-Pydantic body), we
    fall back to the empty-check used by the post-create apply path.
    """
    fields_set = getattr(body, "model_fields_set", None)
    if fields_set is not None:
        if name in fields_set:
            return
        setattr(body, name, template_value)
        return
    current = getattr(body, name, None)
    if _is_empty(current):
        setattr(body, name, template_value)


def _prefill_unset(body: Any, name: str, value: Any) -> None:
    """Pre-fill ``body.{name}`` only when Pydantic confirms the
    operator didn't supply it. Used for bookkeeping fields that the
    template spec maps onto a different body field name (e.g. the
    template's ``dns_group_id`` → body ``dns_group_ids`` list).
    """
    fields_set = getattr(body, "model_fields_set", None)
    if fields_set is not None and name in fields_set:
        return
    setattr(body, name, value)


def apply_template_on_create_block(template: IPAMTemplate, body: Any) -> None:
    """Pre-fill an IPBlockCreate body in-place. ``template`` is the
    fully-loaded ORM row.
    """
    if template.applies_to != "block":
        raise TemplateError(
            f"Template {template.name!r} applies to {template.applies_to!r}, not 'block'."
        )
    for field in _TEMPLATE_FIELDS_COMMON:
        _prefill(body, field, getattr(template, field))
    if template.dns_group_id is not None:
        _prefill_unset(body, "dns_group_ids", [str(template.dns_group_id)])
    if template.dhcp_group_id is not None:
        _prefill_unset(body, "dhcp_server_group_id", template.dhcp_group_id)
    if hasattr(body, "ddns_inherit_settings"):
        if (
            template.ddns_enabled
            or template.ddns_hostname_policy != "client_or_generated"
            or template.ddns_domain_override is not None
            or template.ddns_ttl is not None
        ):
            _prefill_unset(body, "ddns_inherit_settings", False)


def apply_template_on_create_subnet(template: IPAMTemplate, body: Any) -> None:
    if template.applies_to != "subnet":
        raise TemplateError(
            f"Template {template.name!r} applies to {template.applies_to!r}, not 'subnet'."
        )
    for field in _TEMPLATE_FIELDS_COMMON:
        _prefill(body, field, getattr(template, field))
    if template.dns_group_id is not None:
        _prefill_unset(body, "dns_group_ids", [str(template.dns_group_id)])
    if template.dhcp_group_id is not None:
        _prefill_unset(body, "dhcp_server_group_id", template.dhcp_group_id)
    if hasattr(body, "ddns_inherit_settings"):
        if (
            template.ddns_enabled
            or template.ddns_hostname_policy != "client_or_generated"
            or template.ddns_domain_override is not None
            or template.ddns_ttl is not None
        ):
            _prefill_unset(body, "ddns_inherit_settings", False)


# ── Child layout carving (block templates only) ───────────────────────


@dataclass
class CarvedChild:
    cidr: str
    name: str
    skipped: bool  # True if a Subnet at this CIDR already existed


def _render_child_name(
    template: str,
    *,
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
    index: int,
) -> str:
    """Render ``name_template`` with the same token vocabulary as
    bulk-allocate: ``{n}`` / ``{n:03d}`` / ``{oct1}``–``{oct4}``.
    """
    tokens: dict[str, Any] = {"n": index}
    if isinstance(network, ipaddress.IPv4Network):
        octets = str(network.network_address).split(".")
        for i, oct_value in enumerate(octets, start=1):
            tokens[f"oct{i}"] = oct_value
    try:
        return template.format(**tokens)
    except (KeyError, ValueError, IndexError):
        # Bad template → fall back to the network string so we never
        # crash the carve. UI-side validation catches these earlier.
        return str(network)


def _validate_child_layout(layout: dict[str, Any], parent_prefix: int) -> list[dict[str, Any]]:
    if not isinstance(layout, dict):
        raise TemplateError("child_layout must be an object with a 'children' array.")
    children = layout.get("children")
    if not isinstance(children, list) or not children:
        raise TemplateError("child_layout.children must be a non-empty array.")
    cleaned: list[dict[str, Any]] = []
    for idx, raw in enumerate(children):
        if not isinstance(raw, dict):
            raise TemplateError(f"child_layout.children[{idx}] must be an object.")
        prefix = raw.get("prefix")
        if not isinstance(prefix, int) or prefix <= parent_prefix:
            raise TemplateError(
                f"child_layout.children[{idx}].prefix must be an int strictly "
                f"greater than the carrier's /{parent_prefix} (got {prefix!r})."
            )
        name_template = raw.get("name_template", "")
        if not isinstance(name_template, str):
            raise TemplateError(f"child_layout.children[{idx}].name_template must be a string.")
        cleaned.append(
            {
                "prefix": prefix,
                "name_template": name_template,
                "description": raw.get("description", "") or "",
                "tags": raw.get("tags") or {},
                "custom_fields": raw.get("custom_fields") or {},
            }
        )
    return cleaned


async def _existing_subnets_under_block(db: AsyncSession, block: IPBlock) -> set[str]:
    rows = (
        await db.execute(
            select(Subnet.network).where(
                Subnet.block_id == block.id,
                Subnet.deleted_at.is_(None),
            )
        )
    ).all()
    return {str(r[0]) for r in rows}


async def carve_children(
    db: AsyncSession,
    template: IPAMTemplate,
    block: IPBlock,
) -> list[CarvedChild]:
    """Carve sub-subnets per ``template.child_layout``. Idempotent —
    skips any CIDR that already has a Subnet under this block.

    Children are carved sequentially: the layout consumes blocks of
    each child's prefix size starting at the block's network address.
    Caller is responsible for committing.
    """
    if template.child_layout is None:
        return []
    if template.applies_to != "block":
        raise TemplateError("child_layout is only valid on block templates.")

    parent_net = ipaddress.ip_network(str(block.network), strict=False)
    spec = _validate_child_layout(template.child_layout, parent_net.prefixlen)
    existing = await _existing_subnets_under_block(db, block)
    results: list[CarvedChild] = []

    cursor = int(parent_net.network_address)
    end = int(parent_net.broadcast_address)
    for idx, entry in enumerate(spec, start=1):
        prefix = entry["prefix"]
        size = 1 << ((parent_net.max_prefixlen) - prefix)
        if cursor + size - 1 > end:
            raise TemplateError(
                f"child_layout overflows the carrier {block.network}: "
                f"child[{idx-1}] /{prefix} would extend past the block range."
            )
        child_net = ipaddress.ip_network(
            (
                ipaddress.IPv4Address(cursor)
                if isinstance(parent_net, ipaddress.IPv4Network)
                else ipaddress.IPv6Address(cursor)
            ),
        )
        # Snap to the prefix boundary
        child_net = ipaddress.ip_network(f"{child_net.network_address}/{prefix}", strict=False)
        cidr = str(child_net)
        rendered_name = (
            _render_child_name(entry["name_template"] or "", network=child_net, index=idx)
            if entry["name_template"]
            else ""
        )
        if cidr in existing:
            results.append(CarvedChild(cidr=cidr, name=rendered_name, skipped=True))
            cursor += size
            continue
        # Carved subnets skip the network/broadcast auto-address rows
        # because we're not going through ``create_subnet``. Operator
        # can flesh them out via the IPAM UI afterward; the row exists
        # in the tree.
        sub = Subnet(
            space_id=block.space_id,
            block_id=block.id,
            network=cidr,
            name=rendered_name,
            description=entry["description"],
            tags=entry["tags"],
            custom_fields=entry["custom_fields"],
            applied_template_id=template.id,
        )
        db.add(sub)
        results.append(CarvedChild(cidr=cidr, name=rendered_name, skipped=False))
        cursor += size

    return results


# ── Reapply across instances ──────────────────────────────────────────


async def find_block_instances(db: AsyncSession, template_id: uuid.UUID) -> list[IPBlock]:
    rows = (
        (
            await db.execute(
                select(IPBlock).where(
                    IPBlock.applied_template_id == template_id,
                    IPBlock.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def find_subnet_instances(db: AsyncSession, template_id: uuid.UUID) -> list[Subnet]:
    rows = (
        (
            await db.execute(
                select(Subnet).where(
                    Subnet.applied_template_id == template_id,
                    Subnet.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)
