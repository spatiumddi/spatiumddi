"""Feed-sourced blocklist entries block subdomains too

Revision ID: b7e4a1c56d93
Revises: d3f8b6c02a41
Create Date: 2026-08-21

Feed-sourced ``dns_blocklist_entry`` rows were created without setting
``is_wildcard``, so they took the column default of ``False`` and the
rendered RPZ rule matched the apex only. A list naming
``tracker.example`` therefore left ``cdn.tracker.example`` resolving —
not what any of these feeds mean, and not what the manual add-entry
form does (it defaults the flag on, with a comment citing Pi-hole /
uBlock behaviour as the reason).

The refresh task now sets the flag on new rows. This backfills the ones
already stored, so an existing install converges immediately instead of
drifting entry-by-entry as feeds churn.

Manual entries are deliberately untouched: there the flag is an operator
choice, and someone who unchecked "include subdomains" meant it.

Issue #878.
"""

from alembic import op

revision = "b7e4a1c56d93"
down_revision = "d3f8b6c02a41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE dns_blocklist_entry
           SET is_wildcard = true
         WHERE source = 'feed'
           AND is_wildcard = false
        """)


def downgrade() -> None:
    # Restores the pre-#878 shape for feed rows. Not perfectly reversible:
    # a feed row that was legitimately wildcard-flagged before this ran
    # is indistinguishable from one this migration set, so both go back
    # to false. That matches the old code path, which never set the flag
    # on a feed row at all.
    op.execute("""
        UPDATE dns_blocklist_entry
           SET is_wildcard = false
         WHERE source = 'feed'
        """)
