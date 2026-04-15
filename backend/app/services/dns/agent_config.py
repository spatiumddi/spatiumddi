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
    DNSServerOptions,
    DNSView,
    DNSZone,
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
    views_res = await db.execute(
        select(DNSView).where(DNSView.group_id == server.group_id)
    )
    views = views_res.scalars().all()

    # ACLs
    acls_res = await db.execute(
        select(DNSAcl).where(DNSAcl.group_id == server.group_id)
        .options(selectinload(DNSAcl.entries))  # type: ignore[attr-defined]
    )
    acls = acls_res.scalars().all()

    # Zones (+ records for primary only)
    zones_res = await db.execute(
        select(DNSZone).where(DNSZone.group_id == server.group_id)
    )
    zones = zones_res.scalars().all()

    zone_payload: list[dict[str, Any]] = []
    for z in zones:
        zp: dict[str, Any] = {
            "id": str(z.id),
            "name": getattr(z, "name", None) or getattr(z, "fqdn", None),
            "type": getattr(z, "zone_type", "primary"),
            "ttl": getattr(z, "default_ttl", 3600),
        }
        if server.is_primary:
            rec_res = await db.execute(select(DNSRecord).where(DNSRecord.zone_id == z.id))
            zp["records"] = [
                {
                    "name": r.name,
                    "type": r.record_type,
                    "ttl": r.ttl,
                    "value": r.value,
                }
                for r in rec_res.scalars().all()
            ]
        zone_payload.append(zp)

    # Pending record ops — only for primary
    pending_ops: list[dict[str, Any]] = []
    if server.is_primary:
        op_res = await db.execute(
            select(DNSRecordOp)
            .where(DNSRecordOp.server_id == server.id, DNSRecordOp.state == "pending")
            .order_by(DNSRecordOp.created_at)
        )
        for op in op_res.scalars().all():
            pending_ops.append(
                {
                    "op_id": str(op.id),
                    "zone_name": op.zone_name,
                    "op": op.op,
                    "record": op.record,
                    "target_serial": op.target_serial,
                }
            )

    bundle_body: dict[str, Any] = {
        "server_id": str(server.id),
        "driver": server.driver,
        "options": {
            "forwarders": getattr(opts, "forwarders", []) if opts else [],
            "forward_policy": getattr(opts, "forward_policy", "first") if opts else "first",
            "recursion_enabled": getattr(opts, "recursion_enabled", True) if opts else True,
            "dnssec_validation": getattr(opts, "dnssec_validation", "auto") if opts else "auto",
            "allow_query": getattr(opts, "allow_query", ["any"]) if opts else ["any"],
            "allow_transfer": getattr(opts, "allow_transfer", ["none"]) if opts else ["none"],
        },
        "views": [
            {"id": str(v.id), "name": v.name, "match_clients": getattr(v, "match_clients", [])}
            for v in views
        ],
        "acls": [{"id": str(a.id), "name": a.name} for a in acls],
        "zones": zone_payload,
        "tsig_keys": [],  # populated by driver-abstraction agent
        "forwarders": getattr(opts, "forwarders", []) if opts else [],
        "blocklists": [],
        "pending_record_ops": pending_ops,
    }
    etag = _compute_etag(bundle_body)
    bundle: ConfigBundle = {"etag": etag, **bundle_body}  # type: ignore[misc]
    return bundle
