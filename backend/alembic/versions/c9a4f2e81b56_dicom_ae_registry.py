"""DICOM AE Title registry + peer-association map (#723, part of #543)

Adds the ``network.dicom`` feature module and its two tables.

* ``dicom_ae`` — one Application Entity. ``uq_dicom_ae_title`` is the
  point of the whole module: a DICOM AE Title is institution-wide-unique
  by specification (PS3.15 Annex H defines a registry object whose only
  job is allocating them), duplicates break C-MOVE / Storage Commitment /
  Modality Worklist / MPPS, and in practice the namespace lives in a
  spreadsheet. Enforcing it in the database is what turns a convention
  into a guarantee.
* ``dicom_peer`` — a directed AE→AE configured-association edge, so
  "what breaks if I renumber this host" and an ePHI data-flow map become
  answerable.

``dicom_ae.ip_address_id`` is **nullable** with ``ON DELETE SET NULL``,
deliberately unlike ``bacnet_device``'s ``CASCADE``: an AE Title outlives
the host it runs on because it is burned into peer configuration across
the estate. Decommissioning the IP must demote the title to a
reservation, not hand the name to the next commissioning engineer.

CHECK constraints cover the values with a specification-defined range —
the 16-**byte** AE length ceiling (PS3.5; bytes, not characters, and
spaces are legal padding) and the port range. The role / device-class
vocabularies stay unconstrained frozensets validated at the API layer,
matching the rationale in ``models/multicast.py``: adding a value later
should not need a migration.

Default-ENABLED per non-negotiable #14 — registry only, no probes, no
device I/O, no off-prem calls, and an operator cannot turn off what they
never knew shipped.

Revision ID: c9a4f2e81b56
Revises: b1e7c04a93df
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c9a4f2e81b56"
down_revision: str | None = "b1e7c04a93df"
branch_labels: str | None = None
depends_on: str | None = None

_AE_TITLE_MAX_BYTES = 16
_DEFAULT_PORT = 11112


def upgrade() -> None:
    op.create_table(
        "dicom_ae",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.Column("ip_address_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("subnet_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ae_title", sa.String(length=_AE_TITLE_MAX_BYTES), nullable=False),
        sa.Column(
            "port", sa.Integer(), nullable=False, server_default=sa.text(str(_DEFAULT_PORT))
        ),
        sa.Column(
            "tls_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("role", sa.String(length=8), nullable=False, server_default=sa.text("'both'")),
        sa.Column(
            "device_class",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'other'"),
        ),
        sa.Column("vendor", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "model_name", sa.String(length=255), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "department", sa.String(length=255), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "location", sa.String(length=255), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "seen_via", sa.String(length=16), nullable=False, server_default=sa.text("'manual'")
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "tags", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "custom_fields",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(["ip_address_id"], ["ip_address.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subnet_id"], ["subnet.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ae_title", name="uq_dicom_ae_title"),
        sa.CheckConstraint("port >= 1 AND port <= 65535", name="ck_dicom_ae_port"),
        sa.CheckConstraint(
            f"octet_length(ae_title) BETWEEN 1 AND {_AE_TITLE_MAX_BYTES}",
            name="ck_dicom_ae_title_length",
        ),
    )
    op.create_index("ix_dicom_ae_ip_address_id", "dicom_ae", ["ip_address_id"])
    op.create_index("ix_dicom_ae_device_class", "dicom_ae", ["device_class"])
    op.create_index(
        "ix_dicom_ae_unbound",
        "dicom_ae",
        ["ae_title"],
        postgresql_where=sa.text("ip_address_id IS NULL"),
    )

    op.create_table(
        "dicom_peer",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.Column("source_ae_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_ae_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "services", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.ForeignKeyConstraint(["source_ae_id"], ["dicom_ae.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_ae_id"], ["dicom_ae.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_ae_id", "target_ae_id", name="uq_dicom_peer_pair"),
        sa.CheckConstraint("source_ae_id <> target_ae_id", name="ck_dicom_peer_not_self"),
    )
    op.create_index("ix_dicom_peer_source", "dicom_peer", ["source_ae_id"])
    op.create_index("ix_dicom_peer_target", "dicom_peer", ["target_ae_id"])

    # ── feature_module seed (non-negotiable #14) ───────────────────────
    op.execute(
        sa.text(
            """
            INSERT INTO feature_module (id, enabled)
            VALUES ('network.dicom', TRUE)
            ON CONFLICT (id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'network.dicom'"))

    op.drop_index("ix_dicom_peer_target", table_name="dicom_peer")
    op.drop_index("ix_dicom_peer_source", table_name="dicom_peer")
    op.drop_table("dicom_peer")

    op.drop_index("ix_dicom_ae_unbound", table_name="dicom_ae")
    op.drop_index("ix_dicom_ae_device_class", table_name="dicom_ae")
    op.drop_index("ix_dicom_ae_ip_address_id", table_name="dicom_ae")
    op.drop_table("dicom_ae")
