"""API token scopes (issue #74).

Revision ID: b6f1d4a82c97
Revises: a3c8e5d61b94
Create Date: 2026-05-03 19:30:00.000000

Adds a coarse-grained scope vocabulary to API tokens. The existing
``allowed_paths`` (path-prefix list) and ``permissions`` (RBAC
override dict) columns stay as-is — those are fine-grained
restriction tools that solve different problems.

``scopes`` is a JSONB list of well-known scope strings drawn from
a closed vocabulary defined in ``app.services.api_token_scopes``:
``read`` / ``ipam:write`` / ``dns:write`` / ``dhcp:write`` /
``agent``. Empty list = no scope restriction (token still falls
through to the user's RBAC permissions). Multiple scopes union
(any-match passes).

The auth layer enforces ``scopes`` BEFORE the RBAC permission
check, so a "read-only" token can never hit a write handler even
if its owner's RBAC would otherwise allow it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b6f1d4a82c97"
down_revision: Union[str, None] = "a3c8e5d61b94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_token",
        sa.Column(
            "scopes",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("api_token", "scopes")
