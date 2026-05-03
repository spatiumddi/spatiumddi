"""First-class VRF (Virtual Routing and Forwarding) entity.

Replaces the freeform ``vrf_name`` / ``route_distinguisher`` /
``route_targets`` columns on :class:`app.models.ipam.IPSpace` with a
proper relational entity that carries:

* a stable identity (UUID) so other rows can FK to it;
* a route-distinguisher with a known shape (validated at the API
  layer against the regex ``^(\\d+|(\\d+\\.){3}\\d+):\\d+$``);
* split import / export route-target lists (vendor convention);
* an optional ASN binding (FK added in a follow-up after issue #85
  merges — see the migration for context).

The freeform columns on :class:`IPSpace` are intentionally kept
through this release for one cycle so operators can confirm the
data migration mapped values correctly. They will be dropped in a
follow-up migration; until then both surfaces co-exist and the
``vrf_id`` FK is the source of truth.
"""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class VRF(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A VRF — a routing/forwarding domain, optionally MPLS-flavoured.

    Bound to:

    * zero or one :class:`app.models.ipam.IPSpace` rows
      (``IPSpace.vrf_id``).
    * zero or more :class:`app.models.ipam.IPBlock` rows
      (``IPBlock.vrf_id``) — typically inherits the VRF of the
      parent space, but a block may pin its own (e.g. hub-and-spoke
      where one space hosts blocks belonging to multiple VRFs).
    * an optional :class:`app.models.asn.ASN` once issue #85 lands
      (``vrf.asn_id`` is added in this migration without an FK
      constraint; the constraint is added in a follow-up after the
      ASN model lands).
    """

    __tablename__ = "vrf"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # ASN binding. Added in this migration WITHOUT a FK constraint —
    # the ``asn`` table is delivered by issue #85 and may not exist
    # in this worktree yet. A follow-up migration will add the
    # constraint once #85 has merged.
    asn_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)

    # Route distinguisher. Validated at the API layer against the
    # regex ``^(\\d+|(\\d+\\.){3}\\d+):\\d+$``. Stored as text since
    # the canonical form is operator-driven; we don't try to
    # canonicalise the ASN/IP portion.
    route_distinguisher: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Split import / export RT lists. Default is ``[]`` rather than
    # ``NULL`` so the API surface always returns a list — saves a
    # ``v if isinstance(v, list) else []`` coercion at every read
    # site.
    import_targets: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    export_targets: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    custom_fields: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )


__all__ = ["VRF"]
