"""audit-log tamper detection (issue #73)

Adds a hash chain over ``audit_log`` so a nightly verifier can flag any
row that's been edited or removed between commits.

Three new columns:

* ``seq`` — bigserial, gives deterministic ordering even when two rows
  share a timestamp. Indexed.
* ``row_hash`` — sha256 of ``prev_hash || canonical_json(row)``. Set at
  insert time by ``app.services.audit_chain.compute_audit_hashes``
  hooked into the SQLAlchemy ``before_flush`` event. NOT NULL once
  set; the migration backfills the entire existing table at once so
  the chain is contiguous from row 1.
* ``prev_hash`` — sha256 of the previous row in seq order. ``NULL``
  for the very first row.

A ``BEFORE DELETE`` trigger on ``audit_log`` raises an exception so
even a superuser can't silently snip the chain — matches the existing
non-negotiable that audit rows are never deleted.

Revision ID: d92f4a18c763
Revises: c8e4f7a91d36
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "d92f4a18c763"
down_revision = "c8e4f7a91d36"
branch_labels = None
depends_on = None


def _canonical_payload(row: sa.engine.Row) -> str:
    """Build the same canonical JSON string the runtime hasher
    produces. The two MUST stay in sync — any new column included in
    the runtime canonicaliser must be reflected here, or the verifier
    will report a false positive on every old row.

    See ``app.services.audit_chain.canonical_json`` for the live
    spec. ``seq`` and ``row_hash`` are NEVER part of the payload —
    they describe the row's position in the chain, not its content.
    """
    payload = {
        "id": str(row.id),
        "timestamp": row.timestamp.isoformat() if row.timestamp else None,
        "user_id": str(row.user_id) if row.user_id else None,
        "user_display_name": row.user_display_name,
        "auth_source": row.auth_source,
        "source_ip": row.source_ip,
        "user_agent": row.user_agent,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "resource_display": row.resource_display,
        "old_value": row.old_value,
        "new_value": row.new_value,
        "changed_fields": row.changed_fields,
        "request_id": row.request_id,
        "result": row.result,
        "error_detail": row.error_detail,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash(prev: str | None, canonical: str) -> str:
    h = hashlib.sha256()
    if prev is not None:
        h.update(prev.encode())
    h.update(b"|")  # separator so prefix-collisions can't happen
    h.update(canonical.encode())
    return h.hexdigest()


def upgrade() -> None:
    op.add_column(
        "audit_log",
        sa.Column("seq", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "audit_log",
        sa.Column("row_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "audit_log",
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
    )

    # Backfill ``seq`` in (timestamp, id) order, then compute the chain
    # over the same ordering. We do this in a single Python pass instead
    # of a CTE because the canonical JSON shape lives in Python and we
    # need it to match the runtime hasher byte-for-byte.
    bind = op.get_bind()
    rows = list(
        bind.execute(
            sa.text(
                "SELECT id, timestamp, user_id, user_display_name, auth_source, "
                "source_ip, user_agent, action, resource_type, resource_id, "
                "resource_display, old_value, new_value, changed_fields, "
                "request_id, result, error_detail "
                "FROM audit_log ORDER BY timestamp ASC, id ASC"
            )
        )
    )
    prev: str | None = None
    update_stmt = sa.text(
        "UPDATE audit_log SET seq = :seq, row_hash = :h, prev_hash = :p WHERE id = :id"
    )
    for n, row in enumerate(rows, start=1):
        canonical = _canonical_payload(row)
        row_hash = _hash(prev, canonical)
        bind.execute(
            update_stmt,
            {"seq": n, "h": row_hash, "p": prev, "id": row.id},
        )
        prev = row_hash

    # Lock seq + row_hash to NOT NULL once backfill is complete. Use a
    # serial-style sequence so future inserts auto-pick the next seq.
    op.alter_column("audit_log", "seq", nullable=False)
    op.alter_column("audit_log", "row_hash", nullable=False)

    op.execute(
        "CREATE SEQUENCE audit_log_seq_seq OWNED BY audit_log.seq"
    )
    op.execute(
        "SELECT setval('audit_log_seq_seq', "
        "COALESCE((SELECT MAX(seq) FROM audit_log), 0) + 1, false)"
    )
    op.execute(
        "ALTER TABLE audit_log ALTER COLUMN seq SET DEFAULT nextval('audit_log_seq_seq')"
    )
    op.create_index("ix_audit_log_seq", "audit_log", ["seq"], unique=True)

    # Block DELETEs at the DB level so a verifier break can't be
    # "fixed" by removing the offending row. Matches the existing
    # non-negotiable: audit rows are append-only forever.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_block_delete()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only (issue #73)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_block_delete()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_block_delete()")
    op.drop_index("ix_audit_log_seq", table_name="audit_log")
    op.execute("ALTER TABLE audit_log ALTER COLUMN seq DROP DEFAULT")
    op.execute("DROP SEQUENCE IF EXISTS audit_log_seq_seq")
    op.drop_column("audit_log", "prev_hash")
    op.drop_column("audit_log", "row_hash")
    op.drop_column("audit_log", "seq")
