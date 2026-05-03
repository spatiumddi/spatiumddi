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

from sqlalchemy import ForeignKey, String, Text
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
    * an optional :class:`app.models.asn.ASN`
      (``vrf.asn_id``). The FK constraint is added in the VRF
      Phase 2 migration once issue #85's ``asn`` table is in
      place. ``ON DELETE SET NULL`` — deleting an ASN nulls the
      VRF's ``asn_id`` rather than cascading the delete, since
      operators typically want to re-link to a replacement AS.
    """

    __tablename__ = "vrf"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # ASN binding — FK constraint added in the VRF Phase 2 migration
    # (see ``alembic/versions/<rev>_vrf_phase2.py``). ``ON DELETE
    # SET NULL`` — deleting an ASN nulls this column rather than
    # cascade-deleting the VRF.
    asn_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("asn.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

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
