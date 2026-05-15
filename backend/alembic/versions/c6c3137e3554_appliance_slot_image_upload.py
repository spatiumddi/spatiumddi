"""Issue #170 follow-up — slot-image upload for air-gapped upgrades.

Lands the schema half of the air-gap upgrade pipeline. Operators on
isolated networks can't reach github.com/spatiumddi/spatiumddi/
releases to give the supervisor a ``desired_slot_image_url`` — but
they can upload the ``.raw.xz`` (downloaded out-of-band) directly
to the control plane through the new Fleet UI. The control plane
stores the bytes on a local volume, computes the SHA-256 and
verifies against the operator-provided value, then serves them back
under an authenticated internal URL. The supervisor's existing
heartbeat → trigger-file → host runner pipeline picks it up
unchanged (#170 Wave C1's ``desired_slot_image_url`` column points
at the internal URL instead of github releases).

New table ``appliance_slot_image``:

* ``id`` — UUID PK.
* ``filename`` — operator-supplied display name (e.g.
  ``spatiumddi-appliance-slot-amd64.raw.xz``). Not load-bearing —
  only the UI shows it; the supervisor downloads via the UUID.
* ``size_bytes`` — file size for sanity checking + display.
* ``sha256`` — hex digest of the uploaded bytes. Computed on the
  server side and verified against the operator-supplied value
  before the row commits (mismatch → 422 + the partial file is
  deleted from disk).
* ``appliance_version`` — operator-supplied version label (e.g.
  ``2026.05.14-1``). Shown alongside the filename so operators
  can pick the right slot image without inspecting the URL. Used
  by the supervisor's "installed == desired" auto-clear logic on
  the appliance row, so this must match the version the slot image
  actually ships.
* ``uploaded_by_user_id`` — audit linkage; ``ON DELETE SET NULL``
  so removing a user doesn't drop the upload history.
* ``uploaded_at`` — server timestamp at commit.

Storage path lives on the api container's existing
``spatium_backups`` volume? No — slot images can be 1-2 GiB each
and we shouldn't conflate them with backup archives. New volume
``spatium_slot_images`` mounted at
``/var/lib/spatiumddi/slot-images/`` on the api container. The
file path on disk is ``{id}.raw.xz`` so we never trust operator
input for filesystem paths.

No retention policy yet — operators delete uploads manually from
the Fleet UI. The Wave-D-or-later polish can add a "prune images
older than N days that are no longer referenced by any appliance's
desired_slot_image_url" beat task if /var pressure shows up.

Revision ID: c6c3137e3554
Revises: 78bfb374d56d
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "c6c3137e3554"
down_revision: str | None = "78bfb374d56d"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "appliance_slot_image",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("appliance_version", sa.String(length=64), nullable=False),
        sa.Column(
            "uploaded_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
            comment=(
                "Operator-supplied free-form note shown next to the row "
                "in the Fleet UI. Useful for 'pre-release X verified by Y' "
                "kind of bookkeeping."
            ),
        ),
    )
    op.create_index(
        "ix_appliance_slot_image_sha256",
        "appliance_slot_image",
        ["sha256"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_appliance_slot_image_sha256", table_name="appliance_slot_image"
    )
    op.drop_table("appliance_slot_image")
