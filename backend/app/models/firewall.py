"""Fleet-firewall per-appliance apply state (#285 Phase 2).

One row per appliance, mirroring back what the host-side
``spatium-firewall-reload`` runner writes to its release-state sidecars
(``firewall-applied-hash`` / ``firewall-applied-status`` / the base-conf
marker) plus the control-plane's own view of what it last *rendered*.

Before Phase 2 the runner wrote those sidecars but nothing read them back
— they died on the box. This table closes that loop: the supervisor
echoes the sidecars on the heartbeat, the handler upserts them here, and
they drive the Fleet drift chip (2d) + the ``firewall.apply_stalled``
alarm (2d) + the test-apply/auto-revert bookkeeping (2c).

The full column set lands in one migration even though 2a/2c/2d are the
writers of some of them, so the table migrates exactly once. All columns
are nullable / defaulted — a fresh row carries only what's been observed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FirewallApplyState(Base):
    """Per-appliance firewall render/apply convergence state (#285 Phase 2)."""

    __tablename__ = "firewall_apply_state"

    appliance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appliance.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # What the control plane last rendered for this node (2a writes) vs what
    # the host runner reports it actually applied (echoed from the
    # firewall-applied-hash sidecar). Drift = rendered != applied.
    rendered_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    applied_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Runner outcome: "ok" | "error:dry-run-pre" | "error:dry-run-post" |
    # "error:apply" | "error:sentinel-dry-run" | "reverted" (2c) | ...
    applied_status: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # sha256 of the live base /etc/nftables.conf the runner applied against,
    # so the control plane can tell a node still on the pre-#285 LAN-wide
    # base apart from a hardened one (gates the master-enable flip in 2a).
    base_conf_marker: Mapped[str | None] = mapped_column(String(64), nullable=True)

    last_rendered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 2c — the last ruleset hash the control plane CONFIRMED healthy (drives
    # the stale-PASS compliance verdict + the auto-revert floor invariant).
    last_confirmed_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 2c — a test-apply is mid-countdown; commit_deadline is when the host
    # timer auto-reverts absent a confirm.
    pending_commit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    commit_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 2d — watermark for the apply-stalled alarm (set when an "ok"-status
    # node's applied_hash has lagged rendered_hash past the grace window).
    stalled_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["FirewallApplyState"]
