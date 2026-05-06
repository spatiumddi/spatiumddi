"""active session viewer + force-logout (issue #72)

Extends ``user_session`` with two columns the admin viewer relies on:

* ``auth_source`` — which provider this session was minted against
  (``local`` / LDAP name / OIDC provider name / SAML name). Lets the
  viewer show "Logged in via Okta" without an extra join, and lets
  superadmins triage suspicious sessions per-IdP.
* ``last_seen_at`` — bumped on each authenticated request (with a
  60 s throttle in the auth dep so we don't hammer the table) so
  the viewer can render "Last active 5 m ago" and operators can
  spot dormant sessions worth revoking.

The session id (UUID PK that already exists) is now embedded as the
``jti`` claim on the access token. ``get_current_user`` looks the
session up by jti and rejects when ``revoked`` is True — that's the
force-logout effect: revoke flips the bit + every in-flight access
token using that jti starts 401-ing on the next request.

Revision ID: c8e4f7a91d36
Revises: a7b3c8d92e14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c8e4f7a91d36"
down_revision = "a7b3c8d92e14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_session",
        sa.Column(
            "auth_source",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'local'"),
        ),
    )
    op.add_column(
        "user_session",
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Index the not-revoked + not-expired lookup the auth dep does on
    # every request — without it that's a seq scan once the table is
    # 100k rows deep on a long-running deployment.
    op.create_index(
        "ix_user_session_revoked_expires",
        "user_session",
        ["revoked", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_session_revoked_expires", table_name="user_session")
    op.drop_column("user_session", "last_seen_at")
    op.drop_column("user_session", "auth_source")
