"""AV / Audio-Video-over-IP awareness — issue #540 (part of #543).

Specializes the multicast registry (#126) for media-over-IP shops:
Dante · AES67 · SMPTE ST 2110 · NDI · RAVENNA. It deliberately does
**not** add a parallel address plane — a Dante flow *is* a
``MulticastGroup``; this module adds the AV-specific descriptor that
turns a generic group into something an AoIP engineer recognizes.

Two pieces live here:

* :class:`AVFlowProfile` — a 1:1 sidecar on ``multicast_group``
  carrying the AV protocol, the human flow label, and the PTP clock
  domain. A sidecar rather than columns on ``MulticastGroup`` so the
  core multicast table stays vertical-agnostic: an operator who never
  turns on ``network.av`` gets no new columns on their hot table, and
  dropping the module is a clean table drop.

* :class:`AVReservedRange` — operator-declared address ranges per AV
  protocol (Dante defaults to ``239.69.0.0/16``, AES67 lives somewhere
  in ``239.0.0.0/8``, SMPTE 2110 studios hand-carve their own). This
  is what makes the two conformity checks real rather than advisory
  folklore: without a declared range there is nothing to check an
  allocation *against*.

Out of scope, permanently: RTP jitter / packet-loss analysis, SDP
essence decode, NMOS IS-05 connection control, PTP grandmaster
election. SpatiumDDI is a registry — media monitoring belongs to
media-monitoring tools.

FK semantics:

* ``AVFlowProfile.group_id`` — ``ON DELETE CASCADE`` + ``UNIQUE``.
  The profile is meaningless without its group, and a group has at
  most one AV identity.
* ``AVReservedRange.space_id`` — ``ON DELETE CASCADE``. A reserved
  range is scoped to the multicast IPSpace it carves up; losing the
  space makes the range meaningless.
* ``AVReservedRange.vlan_id`` — ``ON DELETE SET NULL``. SMPTE 2110
  plants separate video / audio / ancillary VLANs, so a range is
  often VLAN-scoped, but losing the VLAN tag shouldn't delete the
  address-plan documentation.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CIDR, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# AV transport protocols we understand well enough to reason about.
#
# ``dante`` and ``ravenna`` are vendor/implementation ecosystems that
# both ride AES67 on the wire; they stay distinct values because
# operators think and troubleshoot in those terms (a Dante shop says
# "Dante", not "AES67 profile"), and because their default address
# ranges differ.
#
# SMPTE 2110 splits into three essence types because 2110 plants
# routinely put video / audio / ancillary on separate VLANs and
# separate multicast ranges — collapsing them would make the
# reserved-range check useless for exactly the deployments that need
# it most.
#
# Frozenset rather than a Postgres ENUM, matching ``multicast.py`` —
# adding AVB/TSN or a new 2110 part later shouldn't need a migration.
AV_PROTOCOLS: frozenset[str] = frozenset(
    {
        "dante",
        "aes67",
        "smpte2110_video",
        "smpte2110_audio",
        "smpte2110_anc",
        "ndi",
        "ravenna",
        "other",
    }
)

# How an AV flow profile got here. Only ``manual`` is reachable today;
# ``mdns`` (Dante discovery) and ``nmos`` (IS-04 registry mirror) are
# the #540 Phase 2 / Phase 3 populators. The values are defined now so
# those phases don't need a data migration — and so the UI can already
# render provenance without a schema change.
AV_FLOW_SOURCES: frozenset[str] = frozenset({"manual", "mdns", "nmos"})

# PTP (IEEE 1588) domain numbers are a single octet: 0-255.
PTP_DOMAIN_MIN: int = 0
PTP_DOMAIN_MAX: int = 255

# Well-known default ranges, surfaced as allocation presets in the UI
# and as the seed suggestion when an operator declares their first
# reserved range. NOT auto-inserted: a range only becomes enforceable
# when an operator explicitly claims it, because "the Dante default"
# is a vendor default an operator may deliberately have moved off.
#
# Sources: Audinate Dante ships transmit flows in 239.69.0.0/16;
# AES67 (and RAVENNA) recommend a site-chosen block inside the
# administratively-scoped 239.0.0.0/8; SMPTE ST 2110 defines no
# range at all — plants carve their own, which is exactly why the
# operator-declared model is the right one here.
AV_DEFAULT_RANGES: dict[str, str] = {
    "dante": "239.69.0.0/16",
    "aes67": "239.0.0.0/8",
    "ravenna": "239.0.0.0/8",
}


class AVFlowProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The AV identity of a multicast group — 1:1 sidecar.

    A ``MulticastGroup`` with no profile row is just a group. With
    one, it is a Dante flow / a 2110 video essence / an NDI stream,
    and the AV conformity checks and Copilot tools can see it.
    """

    __tablename__ = "av_flow_profile"
    __table_args__ = (
        # One AV identity per group. Also the lookup index — every
        # read of this table is "the profile for this group".
        UniqueConstraint("group_id", name="uq_av_flow_profile_group"),
        Index("ix_av_flow_profile_av_protocol", "av_protocol"),
        Index("ix_av_flow_profile_ptp_domain", "ptp_domain"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("multicast_group.id", ondelete="CASCADE"),
        nullable=False,
    )

    # One of ``AV_PROTOCOLS``.
    av_protocol: Mapped[str] = mapped_column(String(24), nullable=False)

    # Human stream name as the AV engineer says it out loud —
    # "Cam7-Studio-B HD", "FOH-Mix L/R". Distinct from
    # ``MulticastGroup.name`` (an IPAM label) and ``.application``
    # (a generic app tag): this is the name that appears in Dante
    # Controller / the 2110 routing panel, so it is the string an
    # operator searches by when correlating with their AV tooling.
    av_flow_label: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )

    # IEEE 1588 PTP clock domain (0-255). Nullable because plenty of
    # flows are documented before anyone records the clock domain —
    # and surfacing that gap is the point of the
    # ``av_flow_no_ptp_domain`` check. PTP misconfiguration is the
    # single most common AoIP failure mode.
    ptp_domain: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # One of ``AV_FLOW_SOURCES``.
    seen_via: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual"
    )

    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )


class AVReservedRange(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An operator-declared multicast range belonging to one AV protocol.

    Declaring ``239.69.0.0/16`` as the Dante range is what lets
    SpatiumDDI answer two questions no generic IPAM can:

    1. "Did I just allocate a group on top of the Dante auto-assign
       pool?" (the widened collision guard)
    2. "Is this flow living outside the range its protocol is
       supposed to use?" (``av_flow_outside_reserved_range``)

    ``exclusive`` distinguishes "this range is *for* Dante and nothing
    else may live here" from "Dante lives here, among other things".
    Most plants want exclusive; shared exists because small shops run
    one flat 239.x range for everything and would otherwise get a
    permanent wall of warnings.
    """

    __tablename__ = "av_reserved_range"
    __table_args__ = (
        # A protocol may claim several disjoint ranges (2110 plants
        # carve video/audio/anc separately), so the natural key is
        # the range itself within its space.
        UniqueConstraint("space_id", "cidr", name="uq_av_reserved_range_space_cidr"),
        Index("ix_av_reserved_range_space_id", "space_id"),
        Index("ix_av_reserved_range_av_protocol", "av_protocol"),
        Index("ix_av_reserved_range_vlan_id", "vlan_id"),
    )

    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_space.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Stored as CIDR (not INET) — this is a range, and Postgres CIDR
    # rejects a value with host bits set, which catches a fat-fingered
    # "239.69.0.5/16" at the database boundary.
    cidr: Mapped[str] = mapped_column(CIDR, nullable=False)

    # One of ``AV_PROTOCOLS``.
    av_protocol: Mapped[str] = mapped_column(String(24), nullable=False)

    name: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    # When true, a non-matching-protocol group inside this range is a
    # conformity failure rather than an informational note.
    exclusive: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")

    vlan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vlan.id", ondelete="SET NULL"),
        nullable=True,
    )

    tags: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )


__all__ = [
    "AV_DEFAULT_RANGES",
    "AV_FLOW_SOURCES",
    "AV_PROTOCOLS",
    "AVFlowProfile",
    "AVReservedRange",
    "PTP_DOMAIN_MAX",
    "PTP_DOMAIN_MIN",
]
