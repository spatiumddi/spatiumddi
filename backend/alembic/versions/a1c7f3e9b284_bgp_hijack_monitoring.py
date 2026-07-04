"""bgp prefix-hijack monitoring — tracked prefixes + detections

Revision ID: a1c7f3e9b284
Revises: b7e4d1a92c30
Create Date: 2026-07-04 00:00:00.000000

Issue #527 — BGP prefix-hijack detection via RIS Live / RIPEstat for
tracked ASNs/prefixes. Two new tables plus two ``platform_settings``
columns:

* ``bgp_tracked_prefix`` — the set of prefixes SpatiumDDI watches on the
  public routing table (one row per ``(asn, prefix)``). Auto-populated
  by ``app.tasks.bgp_hijack_poll`` from the ASN's RPKI ROAs + RIPEstat
  announced-prefixes; ``source="manual"`` rows are operator-authored.
* ``bgp_hijack_detection`` — the latch/dedup state, one row per observed
  hijack. Open (``resolved_at`` NULL) on first observation, resolved
  after the delist window. The alert evaluator mirrors active rows into
  ``AlertEvent``.

Two ``platform_settings`` columns gate the periodic poll:
``bgp_monitoring_enabled`` (default OFF — the feature ships discoverable
but silent) and ``bgp_monitoring_interval_hours`` (per-prefix poll
cadence). The optional RIS Live WebSocket consumer is gated by the
``BGP_RIS_LIVE_ENABLED`` env flag, not a DB column.

Additive only. Downgrade drops the two tables + two columns (baselined
in ``migrations_lint_baseline.txt``).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c7f3e9b284"
down_revision: Union[str, None] = "b7e4d1a92c30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bgp_tracked_prefix",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("asn_id", sa.UUID(), nullable=False),
        sa.Column("prefix", postgresql.CIDR(), nullable=False),
        sa.Column("expected_origin_asn", sa.BigInteger(), nullable=False),
        sa.Column(
            "source",
            sa.String(length=16),
            server_default=sa.text("'roa'"),
            nullable=False,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "allowed_origins",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_origins",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["asn_id"], ["asn.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asn_id", "prefix", name="uq_bgp_tracked_prefix"),
    )
    op.create_index("ix_bgp_tracked_prefix_asn", "bgp_tracked_prefix", ["asn_id"])
    op.create_index("ix_bgp_tracked_prefix_enabled", "bgp_tracked_prefix", ["enabled"])

    op.create_table(
        "bgp_hijack_detection",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tracked_prefix_id", sa.UUID(), nullable=True),
        sa.Column("asn_id", sa.UUID(), nullable=False),
        sa.Column("tracked_prefix", postgresql.CIDR(), nullable=False),
        sa.Column("observed_prefix", postgresql.CIDR(), nullable=False),
        sa.Column("expected_origin_asn", sa.BigInteger(), nullable=False),
        sa.Column("observed_origin_asn", sa.BigInteger(), nullable=False),
        sa.Column("detection_kind", sa.String(length=24), nullable=False),
        sa.Column(
            "rpki_status",
            sa.String(length=12),
            server_default=sa.text("'unknown'"),
            nullable=False,
        ),
        sa.Column(
            "severity",
            sa.String(length=10),
            server_default=sa.text("'warning'"),
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.String(length=16),
            server_default=sa.text("'ripestat_poll'"),
            nullable=False,
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "acknowledged",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "notes",
            sa.Text(),
            server_default=sa.text("''"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # SET NULL (not CASCADE): pruning a tracked prefix (which the
        # poll does the instant it drops out of RIPEstat / ROA sources —
        # i.e. while a victim prefix is being hijacked) must NOT delete
        # its open detections. The detection survives with a NULL FK and
        # keeps latching/alerting off ``tracked_prefix`` + ``asn_id``.
        sa.ForeignKeyConstraint(
            ["tracked_prefix_id"], ["bgp_tracked_prefix.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["asn_id"], ["asn.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bgp_hijack_detection_asn", "bgp_hijack_detection", ["asn_id"])
    op.create_index(
        "ix_bgp_hijack_detection_tracked_prefix_id",
        "bgp_hijack_detection",
        ["tracked_prefix_id"],
    )
    op.create_index("ix_bgp_hijack_detection_resolved", "bgp_hijack_detection", ["resolved_at"])
    op.create_index(
        "ix_bgp_hijack_detection_open",
        "bgp_hijack_detection",
        ["asn_id", "observed_prefix", "observed_origin_asn", "detection_kind"],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )

    op.add_column(
        "platform_settings",
        sa.Column(
            "bgp_monitoring_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "bgp_monitoring_interval_hours",
            sa.Integer(),
            server_default=sa.text("6"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "bgp_monitoring_interval_hours")
    op.drop_column("platform_settings", "bgp_monitoring_enabled")

    op.drop_index("ix_bgp_hijack_detection_open", table_name="bgp_hijack_detection")
    op.drop_index("ix_bgp_hijack_detection_resolved", table_name="bgp_hijack_detection")
    op.drop_index("ix_bgp_hijack_detection_tracked_prefix_id", table_name="bgp_hijack_detection")
    op.drop_index("ix_bgp_hijack_detection_asn", table_name="bgp_hijack_detection")
    op.drop_table("bgp_hijack_detection")

    op.drop_index("ix_bgp_tracked_prefix_enabled", table_name="bgp_tracked_prefix")
    op.drop_index("ix_bgp_tracked_prefix_asn", table_name="bgp_tracked_prefix")
    op.drop_table("bgp_tracked_prefix")
