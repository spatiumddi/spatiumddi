"""Assemble a neutral peer-config bundle for a Looking Glass collector.

The GoBGP collector long-polls ``GET /looking-glass/agents/config`` with
its last-seen ETag; the api handler rebuilds this bundle from the
collector's enabled ``BGPLGPeer`` rows, hashes it to an ETag, and returns
the new bundle the instant the ETag shifts (cross-cutting pattern #2).

Every field that affects the rendered GoBGP neighbor config MUST flow
into ``compute_etag`` — miss one and a peer edit never re-renders on the
collector (the ETag never moves, the long-poll 304s forever). Mirrors
``app.drivers.dhcp.base.ConfigBundle.compute_etag``.

The decrypted TCP-MD5 password rides **only** inside the bundle body,
which is delivered over TLS to the JWT-authed agent. It is never returned
on any operator-facing surface (the CRUD router exposes
``md5_password_set: bool`` instead).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_str
from app.models.bgp_looking_glass import BGPLGPeer, LookingGlassCollector

logger = structlog.get_logger(__name__)


@dataclass
class LGPeerDef:
    """One receive-only BGP neighbor the collector renders + peers with."""

    peer_id: str
    name: str
    peer_address: str
    peer_asn: int
    local_asn: int
    address_families: tuple[str, ...]
    max_prefixes: int
    import_filter: dict
    # Decrypted TCP-MD5 password — present ONLY in the bundle sent over
    # TLS to the authed agent. Never surfaced on an operator API.
    md5_password: str | None = None
    md5_password_set: bool = False


@dataclass
class LGConfigBundle:
    collector_id: str
    collector_name: str
    peers: tuple[LGPeerDef, ...] = ()
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    etag: str = ""

    def compute_etag(self) -> str:
        """Stable SHA-256 over every config-affecting field.

        Excludes ``generated_at`` / ``etag`` (non-deterministic /
        derived). Includes the decrypted ``md5_password`` so rotating a
        peer's MD5 secret shifts the ETag and re-renders the neighbor;
        the resulting hash is irreversible and safe to hand to the agent
        as the ``ETag`` header.
        """
        payload = {
            "collector_id": self.collector_id,
            "collector_name": self.collector_name,
            "peers": [asdict(p) for p in self.peers],
        }
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return "sha256:" + hashlib.sha256(blob).hexdigest()


async def build_lg_config_bundle(
    db: AsyncSession, collector: LookingGlassCollector
) -> LGConfigBundle:
    """Build the peer-config bundle for ``collector`` from its enabled peers.

    A collector toggled ``enabled=False`` renders an EMPTY peer set — the
    ETag shifts, the agent re-renders to zero neighbors, and every session is
    torn down. So the operator's disable actually takes effect on the box.
    """
    peers: list[LGPeerDef] = []

    if collector.enabled:
        rows = list(
            (
                await db.execute(
                    select(BGPLGPeer)
                    .where(
                        BGPLGPeer.collector_id == collector.id,
                        BGPLGPeer.enabled.is_(True),
                    )
                    .order_by(BGPLGPeer.id)
                )
            )
            .scalars()
            .all()
        )

        for p in rows:
            md5_plain: str | None = None
            if p.md5_password_encrypted:
                try:
                    md5_plain = decrypt_str(p.md5_password_encrypted)
                except Exception:  # noqa: BLE001 — corrupt/unrotatable secret
                    # Never ship a SILENTLY unauthenticated neighbor: a peer
                    # configured WITH an MD5 password whose ciphertext won't
                    # decrypt (SECRET_KEY / CREDENTIAL_ENCRYPTION_KEY changed,
                    # corrupt column) is EXCLUDED from the bundle rather than
                    # rendered without auth. The session drops — the safe
                    # failure mode for an auth-config problem — and the loud
                    # log tells the operator to re-enter the key.
                    logger.error(
                        "lg_peer_md5_decrypt_failed",
                        collector_id=str(collector.id),
                        peer_id=str(p.id),
                        peer_address=str(p.peer_address),
                    )
                    continue
            peers.append(
                LGPeerDef(
                    peer_id=str(p.id),
                    name=p.name,
                    peer_address=str(p.peer_address),
                    peer_asn=p.peer_asn,
                    local_asn=p.local_asn,
                    address_families=tuple(p.address_families or ()),
                    max_prefixes=p.max_prefixes,
                    import_filter=dict(p.import_filter or {}),
                    md5_password=md5_plain,
                    md5_password_set=bool(p.md5_password_encrypted),
                )
            )

    bundle = LGConfigBundle(
        collector_id=str(collector.id),
        collector_name=collector.name,
        peers=tuple(peers),
        generated_at=datetime.now(UTC),
    )
    bundle.etag = bundle.compute_etag()
    return bundle


__all__ = ["LGConfigBundle", "LGPeerDef", "build_lg_config_bundle"]
