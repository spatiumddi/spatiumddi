"""dns_server.api_key_encrypted: Text → LargeBinary (Fernet, issue #210)

Pre-#210 ``dns_server.api_key_encrypted`` was a ``TEXT`` column whose
writers stored plaintext with a ``# TODO: encrypt`` marker; no
consumer read it. This migration scrubs any pre-existing plaintext
to NULL (no operator-visible loss — column was effectively
decorative) and changes the column type to ``BYTEA`` so subsequent
writes through ``encrypt_str`` land cleanly.

Operators who had set a value will need to re-paste it through the
UI on the DNS server's edit form; the new write path Fernet-
encrypts before storing.

Revision ID: 97190c1b0325
Revises: d4c1f7e3b821
Create Date: 2026-05-17

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "97190c1b0325"
down_revision = "d4c1f7e3b821"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL out any pre-existing plaintext — column was decorative
    # (no readers), so this is safe. Casting Text → bytea on a non-
    # empty value would fail; clearing first makes the alter
    # idempotent regardless of operator state.
    op.execute("UPDATE dns_server SET api_key_encrypted = NULL")
    op.alter_column(
        "dns_server",
        "api_key_encrypted",
        type_=sa.LargeBinary(),
        existing_type=sa.Text(),
        existing_nullable=True,
        postgresql_using="NULL::bytea",
    )


def downgrade() -> None:
    # Downgrade path mirrors upgrade — NULL out anything Fernet-
    # encrypted (it's unreadable on the legacy Text column) and
    # change the type back.
    op.execute("UPDATE dns_server SET api_key_encrypted = NULL")
    op.alter_column(
        "dns_server",
        "api_key_encrypted",
        type_=sa.Text(),
        existing_type=sa.LargeBinary(),
        existing_nullable=True,
        postgresql_using="NULL::text",
    )
