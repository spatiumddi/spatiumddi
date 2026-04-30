"""auto_profile_preset default → service_and_os

Phase 1 of device profiling shipped with ``service_version`` as the
default auto-profile preset, but that preset doesn't include ``-O``
so the IP detail modal's "OS guess" panel always rendered "—". The
new ``service_and_os`` preset adds ``-O`` on top of service detection,
giving operators the right device-profile output by default.

This migration:
1. Flips the column ``server_default`` so future subnet rows get the
   new preset.
2. Updates existing subnet rows still on ``'service_version'`` to the
   new default. Phase 1 only shipped hours ago so the back-compat
   risk is minimal — operators who explicitly picked a different
   preset already overrode the default and aren't touched. Operators
   who'd intentionally chosen ``service_version`` for the auto-profile
   path can flip it back via the subnet edit modal.

Revision ID: a92c81d4f5b0
Revises: e5a3f17b2d8c
Create Date: 2026-04-30 17:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a92c81d4f5b0"
down_revision: Union[str, None] = "e5a3f17b2d8c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "subnet",
        "auto_profile_preset",
        server_default=sa.text("'service_and_os'"),
    )
    op.execute(
        """
        UPDATE subnet
           SET auto_profile_preset = 'service_and_os'
         WHERE auto_profile_preset = 'service_version'
        """
    )


def downgrade() -> None:
    op.alter_column(
        "subnet",
        "auto_profile_preset",
        server_default=sa.text("'service_version'"),
    )
