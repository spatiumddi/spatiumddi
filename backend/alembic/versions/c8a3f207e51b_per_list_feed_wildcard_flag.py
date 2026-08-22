"""Per-list control over whether feed entries block subdomains

Revision ID: c8a3f207e51b
Revises: b7e4a1c56d93
Create Date: 2026-08-22

#878 made every feed-sourced ``dns_blocklist_entry`` row a wildcard,
fixing the case where a list naming ``tracker.example`` left
``cdn.tracker.example`` resolving. That is right for all 19 catalog
sources — they are all "block this domain and everything under it"
lists — but it is a global constant, and it is wrong for a
host-specific feed (a threat-intel drop of individual C2 FQDNs), where
blocking the parent domain is over-blocking with no way to say so.

``feed_entries_are_wildcard`` makes it a per-list choice. It defaults
to true, so this migration is a no-op for behaviour: every existing
list keeps exactly what #878 gave it.

Only feed rows consult the flag. A manual entry's ``is_wildcard`` is
that row's own operator setting and is never rewritten by it.

Issue #894.
"""

import sqlalchemy as sa
from alembic import op

revision = "c8a3f207e51b"
down_revision = "b7e4a1c56d93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dns_blocklist",
        sa.Column(
            "feed_entries_are_wildcard",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dns_blocklist", "feed_entries_are_wildcard")
