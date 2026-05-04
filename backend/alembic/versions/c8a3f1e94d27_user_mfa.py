"""User MFA — TOTP secret + recovery codes (issue #69).

Revision ID: c8a3f1e94d27
Revises: b6f1d4a82c97
Create Date: 2026-05-03 21:00:00.000000

The ``user`` table has carried unused ``totp_secret`` (varchar 64) and
``totp_enabled`` (bool) columns since alpha — early scaffolding for
the MFA work that this migration finally closes out. The plaintext-
string secret was the wrong shape; replace it with a ``BYTEA`` Fernet
ciphertext column (renamed to ``totp_secret_encrypted`` so the model
column type can change without confusing future readers). Existing
rows have ``totp_secret IS NULL`` everywhere — verified at write time
— so the drop is lossless.

``recovery_codes_encrypted`` is new — a Fernet-encrypted JSON list of
sha256 hashes. Recovery codes themselves never sit in the DB in
recoverable form; the operator's one-time copy at enrolment is the
only chance to record the raw values.

``totp_enabled`` (bool) stays as-is — already lives at the right
shape.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8a3f1e94d27"
down_revision: Union[str, None] = "b6f1d4a82c97"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the unused string column. No row has data — verified by:
    #   SELECT count(*) FROM "user" WHERE totp_secret IS NOT NULL;  -- 0
    op.drop_column("user", "totp_secret")
    op.add_column(
        "user",
        sa.Column("totp_secret_encrypted", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "user",
        sa.Column("recovery_codes_encrypted", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user", "recovery_codes_encrypted")
    op.drop_column("user", "totp_secret_encrypted")
    op.add_column(
        "user",
        sa.Column("totp_secret", sa.String(64), nullable=True),
    )
