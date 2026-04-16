"""extended schema

Revision ID: 1ad1e7de9fc4
Revises: 1a48e694db0b
Create Date: 2026-04-13 14:00:00.000000

"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = '1ad1e7de9fc4'
down_revision: Union[str, None] = '1a48e694db0b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Already applied directly to the database; this file is a placeholder.
    pass


def downgrade() -> None:
    pass
