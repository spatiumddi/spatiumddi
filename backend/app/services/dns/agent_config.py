"""Build the AgentConfigBundle delivered to DNS agents via long-poll.

Seam with the driver-abstraction agent:
  The canonical ConfigBundle type lives at
  ``app.services.dns.config_bundle.ConfigBundle`` (authored by the parallel
  driver-abstraction agent). If that module is not present at import time we
  fall back to a local TypedDict-based adapter with the same shape so this
  code still builds. When the real module appears, imports resolve to it
  transparently.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dns import (
    DNSAcl,
    DNSRecord,
    DNSRecordOp,
    DNSServer,
    DNSServerGroup,
    DNSServerOptions,
    DNSView,
    DNSZone,
)
from app.services.dns_blocklist import (
    build_effective_for_group,
    build_effective_for_view,
)

try:  # pragma: no cover - seam with parallel driver-abstraction agent
    from app.services.dns.config_bundle import ConfigBundle  # type: ignore[assignment]
except ImportError:  # fallback local adapter — same shape as canonical type

    class ConfigBundle(TypedDict, total=False):  # type: ignore[no-redef]
        etag: str
        server_id: str
        driver: str
        options: dict[str, Any]
        views: list[dict[str, Any]]
        acls: list[dict[str, Any]]
        zones: list[dict[str, Any]]
        tsig_keys: list[dict[str, Any]]
        forwarders: list[str]
        blocklists: list[dict[str, Any]]
        pending_record_ops: list[dict[str, Any]]


if TYPE_CHECKING:
    pass


def _compute_etag(payload: dict[str, Any]) -> str:
    """SHA-256 of the canonicalized payload (sorted keys)."""
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


async def build_config_bundle(db: AsyncSession, server: DNSServer) -> ConfigBundle:
    """Build the config bundle for a given server from DB state.

    The driver-abstraction agent will swap this implementation to delegate to
    ``DNSDriverBase.render_bundle(server)``. For now we inline a minimal build
    so the agent long-poll endpoint can be exercised end-to-end.
    """
    # Options (per group)
    opts_res = await db.execute(
        select(DNSServerOptions).where(DNSServerOptions.group_id == server.group_id)
    )
    opts = opts_res.scalar_one_or_none()

    # Views
    views_res = await db.execute(select(DNSView).where(DNSView.group_id == server.group_id))
    views = views_res.scalars().all()

    # ACLs
    acls_res = await db.execute(
        select(DNSAcl)
        .where(DNSAcl.group_id == server.group_id)
        .options(selectinload(DNSAcl.entries))  # type: ignore[attr-defined]
    )
    acls = acls_res.scalars().all()

    # Zones (+ records for primary only)
    zones_res = await db.execute(select(DNSZone).where(DNSZone.group_id == server.group_id))
    zones = zones_res.scalars().all()

    zone_payload: list[dict[str, Any]] = []
    for z in zones:
        zp: dict[str, Any] = {
            "id": str(z.id),
            "name": getattr(z, "name", None) or getattr(z, "fqdn", None),
            "type": getattr(z, "zone_type", "primary"),
            "ttl": getattr(z, "default_ttl", 3600),
            # Forward-zone-only fields (ignored by the agent for other types).
            "forwarders": list(getattr(z, "forwarders", []) or []),
            "forward_only": bool(getattr(z, "forward_only", True)),
        }
        # Ship records to every server in the group. The is_primary flag
        # historically gated this, but agents need records to render zone
        # files for serving — primary/secondary distinction matters for
        # accepting RFC 2136 updates, not for which server gets the data.
        rec_res = await db.execute(select(DNSRecord).where(DNSRecord.zone_id == z.id))
        zp["records"] = [
            {
                "name": r.name,
                "type": r.record_type,
                "ttl": r.ttl,
                "value": r.value,
                "priority": r.priority,
                "weight": r.weight,
                "port": r.port,
            }
            for r in rec_res.scalars().all()
        ]
        zone_payload.append(zp)

    # Pending record ops — only for primary. Mark in_flight on dispatch so
    # the same op doesn't get re-shipped on every long-poll cycle until the
    # agent's next heartbeat acks it. Failure ack resets to pending (with
    # attempt++); after 5 failures it becomes "failed" and stays out.
    pending_ops: list[dict[str, Any]] = []
    if server.is_primary:
        op_res = await db.execute(
            select(DNSRecordOp)
            .where(
                DNSRecordOp.server_id == server.id,
                DNSRecordOp.state == "pending",
            )
            .order_by(DNSRecordOp.created_at)
        )
        ops_to_dispatch = list(op_res.scalars().all())
        for op in ops_to_dispatch:
            pending_ops.append(
                {
                    "op_id": str(op.id),
                    "zone_name": op.zone_name,
                    "op": op.op,
                    "record": op.record,
                    "target_serial": op.target_serial,
                }
            )
            op.state = "in_flight"
        if ops_to_dispatch:
            await db.flush()

    # Group-level TSIG key for RFC 2136 dynamic updates
    grp = await db.get(DNSServerGroup, server.group_id)
    tsig_keys: list[dict[str, Any]] = []
    if grp and grp.tsig_key_name and grp.tsig_key_secret:
        tsig_keys.append(
            {
                "name": grp.tsig_key_name,
                "secret": grp.tsig_key_secret,
                "algorithm": grp.tsig_key_algorithm,
            }
        )

    options_block = {
        "forwarders": getattr(opts, "forwarders", []) if opts else [],
        "forward_policy": getattr(opts, "forward_policy", "first") if opts else "first",
        "recursion_enabled": getattr(opts, "recursion_enabled", True) if opts else True,
        "dnssec_validation": getattr(opts, "dnssec_validation", "auto") if opts else "auto",
        "allow_query": getattr(opts, "allow_query", ["any"]) if opts else ["any"],
        "allow_transfer": getattr(opts, "allow_transfer", ["none"]) if opts else ["none"],
    }
    views_block = [
        {"id": str(v.id), "name": v.name, "match_clients": getattr(v, "match_clients", [])}
        for v in views
    ]
    acls_block = [{"id": str(a.id), "name": a.name} for a in acls]

    # Blocklists: one RPZ zone per view (if any) + one group-level zone.
    # Each assembled list has its entries resolved against view/group scope
    # with exceptions already applied by `build_effective_for_{view,group}`.
    def _entries_payload(eff_entries: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                "domain": e.domain,
                "action": e.action,
                "block_mode": e.block_mode,
                "target": e.target,
                "is_wildcard": e.is_wildcard,
            }
            for e in eff_entries
        ]

    blocklists_payload: list[dict[str, Any]] = []
    if views:
        for v in views:
            eff_v = await build_effective_for_view(db, v.id)
            if eff_v.entries:
                blocklists_payload.append(
                    {
                        "rpz_zone_name": f"spatium-blocklist-{v.name}.rpz.",
                        "entries": _entries_payload(eff_v.entries),
                        "exceptions": sorted(eff_v.exceptions),
                    }
                )
    eff_g = await build_effective_for_group(db, server.group_id)
    if eff_g.entries:
        blocklists_payload.append(
            {
                "rpz_zone_name": "spatium-blocklist.rpz.",
                "entries": _entries_payload(eff_g.entries),
                "exceptions": sorted(eff_g.exceptions),
            }
        )

    bundle_body: dict[str, Any] = {
        "server_id": str(server.id),
        "driver": server.driver,
        "options": options_block,
        "views": views_block,
        "acls": acls_block,
        "zones": zone_payload,
        "tsig_keys": tsig_keys,
        "forwarders": options_block["forwarders"],
        "blocklists": blocklists_payload,
        "pending_record_ops": pending_ops,
    }

    # Structural fingerprint excludes records and pending ops so record-only
    # changes don't trigger a full daemon reload — agent applies them via
    # RFC 2136 over loopback instead. Agent compares this to its cached value
    # and only re-renders config when it changes.
    structural = {
        "options": options_block,
        "views": views_block,
        "acls": acls_block,
        "tsig_keys": tsig_keys,
        "zones_structural": [{k: v for k, v in z.items() if k != "records"} for z in zone_payload],
        # Blocklists affect named.conf (response-policy block) + RPZ zone
        # files, so a change MUST trigger a daemon reload.
        "blocklists": blocklists_payload,
    }
    structural_etag = _compute_etag(structural)
    bundle_body["structural_etag"] = structural_etag

    etag = _compute_etag(bundle_body)
    bundle: ConfigBundle = {"etag": etag, **bundle_body}  # type: ignore[misc]
    return bundle
