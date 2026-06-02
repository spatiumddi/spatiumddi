"""Fleet-firewall models (#285).

Two halves:

* ``FirewallApplyState`` (Phase 2) — one row per appliance, mirroring back
  what the host-side ``spatium-firewall-reload`` runner writes to its
  release-state sidecars (applied-hash / applied-status / base-conf marker)
  plus the control-plane's view of what it last *rendered*. Drives the
  Fleet drift chip + the ``firewall.apply_stalled`` alarm + the test-apply
  bookkeeping.
* ``FirewallPolicy`` / ``FirewallRule`` / ``FirewallAlias`` (Phase 3) — the
  declarative policy model: a fleet baseline + per-role overlays + per-
  appliance overrides, compiled server-side into the nftables drop-in by
  ``services/appliance/firewall_merge.py``. Seeded builtin role policies
  reproduce the Phase-2 hardcoded renderer byte-for-byte (the merge
  subsumes ``compile_firewall_body``); operators tune from there. All of
  this is DARK until ``platform_settings.firewall_enabled`` is flipped on —
  the policy model is the source of truth for the render, but the render is
  only authoritative when enforcement is enabled.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


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


# ── Phase 3 — declarative policy model ───────────────────────────────

# Allowed scope_role values. NOTE: ``control-plane`` is NOT a node role
# (the node-label taxonomy is _VALID_ROLES = dns-bind9 / dns-powerdns /
# dhcp / observer / custom). It is a MERGE-INTERNAL scope key the compiler
# matches onto a node via the ``is_cp`` predicate (peer/pod/service
# presence), mirroring the Phase-2 renderer — never via "control-plane in
# node.roles". Do not add a CHECK tying scope_role to the node-role set.
_POLICY_ROLES = ("dns-bind9", "dns-powerdns", "dhcp", "observer", "custom", "control-plane")


class FirewallPolicy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A layer of firewall rules at one scope (#285 Phase 3).

    Three scope kinds compose into a node's effective ruleset: ``fleet``
    (one singleton baseline), ``role`` (one per role token, incl. the
    merge-internal ``control-plane``), ``appliance`` (one override per
    appliance). ``is_builtin`` marks the seeded role/fleet policies whose
    identity is locked (operators tune their rules / enable flag, but can't
    rename or re-scope them — clone first).
    """

    __tablename__ = "firewall_policy"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # fleet | role | appliance
    scope_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope_appliance_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appliance.id", ondelete="CASCADE"),
        nullable=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default=text("100")
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    rules: Mapped[list[FirewallRule]] = relationship(
        back_populates="policy",
        cascade="all, delete-orphan",
        order_by="FirewallRule.seq",
    )

    @validates("scope_role")
    def _validate_scope_role(self, _key: str, value: str | None) -> str | None:
        # Belt-and-braces over the API-layer validator: a non-null scope_role
        # must be a known policy-role token. NOTE this is the POLICY-role set
        # (``_POLICY_ROLES``, which INCLUDES the merge-internal ``control-plane``
        # key) — deliberately NOT the node-label taxonomy, and intentionally a
        # Python-side ORM validator (no DB CHECK / migration) so the scope_role
        # column stays free to gain new policy roles without a schema change.
        # ``None`` is allowed (fleet / appliance scopes); the scope-shape CHECK
        # enforces the null-vs-set rule per scope_kind.
        if value is not None and value not in _POLICY_ROLES:
            raise ValueError(f"scope_role must be one of {_POLICY_ROLES} or None")
        return value

    __table_args__ = (
        # One policy per role token (incl. control-plane).
        UniqueConstraint("scope_kind", "scope_role", name="uq_fw_policy_role"),
        # One override per appliance; one fleet singleton (partial uniques).
        Index(
            "uq_fw_policy_appliance",
            "scope_appliance_id",
            unique=True,
            postgresql_where=text("scope_kind = 'appliance'"),
        ),
        Index(
            "uq_fw_policy_fleet_singleton",
            "scope_kind",
            unique=True,
            postgresql_where=text("scope_kind = 'fleet'"),
        ),
        CheckConstraint(
            "(scope_kind='fleet' AND scope_role IS NULL AND scope_appliance_id IS NULL) OR "
            "(scope_kind='role' AND scope_role IS NOT NULL AND scope_appliance_id IS NULL) OR "
            "(scope_kind='appliance' AND scope_appliance_id IS NOT NULL AND scope_role IS NULL)",
            name="ck_fw_policy_scope_shape",
        ),
        Index("ix_fw_policy_scope", "scope_kind", "enabled"),
    )


class FirewallRule(UUIDPrimaryKeyMixin, Base):
    """One rule within a policy (#285 Phase 3).

    Compiles to a family-split nft fragment. ``source_kind`` is the heart of
    fine-grained scoping: literal ``cidr``/``alias``, ``any`` (no saddr
    clause), or a DERIVED scope the merge resolves per-node at render time
    (``cluster_peers`` / ``pod_cidr`` / ``service_cidr`` / ``kubeapi`` =
    the 6443 union / ``mgmt`` / ``vip``). ``render_guard`` carries emission
    conditions for builtins (e.g. memberlist only when multi-node + VIP).
    """

    __tablename__ = "firewall_rule"

    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("firewall_policy.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(
        String(8), nullable=False, default="accept", server_default=text("'accept'")
    )  # accept | drop
    protocol: Mapped[str] = mapped_column(String(8), nullable=False)  # tcp | udp | icmp | icmpv6
    ports: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)  # [53] / [67,68] / []
    source_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="any", server_default=text("'any'")
    )  # any|cidr|alias|cluster_peers|pod_cidr|service_cidr|kubeapi|mgmt|vip
    source_cidrs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source_alias: Mapped[str | None] = mapped_column(String(64), nullable=True)
    family: Mapped[str] = mapped_column(
        String(6), nullable=False, default="both", server_default=text("'both'")
    )  # v4 | v6 | both
    comment: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Emission guard for builtins, e.g. {"min_cp_members": 2, "requires_vip": true}.
    render_guard: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    policy: Mapped[FirewallPolicy] = relationship(back_populates="rules")

    __table_args__ = (
        # Deterministic conflict target for the idempotent seed + downgrade.
        UniqueConstraint("policy_id", "seq", name="uq_fw_rule_policy_seq"),
        Index("ix_fw_rule_policy_seq", "policy_id", "seq"),
        # Floor protection: no rule may DROP ssh (port 22). The mgmt floor
        # is also emitted in code, first + un-removable — this is the
        # belt-and-braces DB guard so the policy surface can't author a
        # self-lockout.
        CheckConstraint(
            "NOT (action = 'drop' AND ports @> '22'::jsonb)",
            name="ck_fw_rule_no_drop_ssh",
        ),
    )


class FirewallAlias(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named, reusable port-set or CIDR-set (#285 Phase 3).

    CIDR members are family-split AT REST (``v4_members`` / ``v6_members``)
    so a v6 entry can never leak into a v4 nft set (the v6-lockout bug the
    design flags). Referenced by ``FirewallRule.source_alias`` (kind=cidr)
    or expanded into ``ports`` (kind=port).
    """

    __tablename__ = "firewall_alias"

    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # port | cidr
    port_members: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    v4_members: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    v6_members: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


__all__ = ["FirewallApplyState", "FirewallPolicy", "FirewallRule", "FirewallAlias"]
