"""Re-hash audit_log after fixing the defaults-vs-flush ordering bug

Original chain implementation (``d92f4a18c763_audit_chain_hash``)
computed each row's ``row_hash`` inside the SQLAlchemy
``before_flush`` event. Two of the columns it hashed over —
``id`` (Python ``default=uuid.uuid4``) and ``timestamp``
(``server_default=now()``) — are filled in DURING the flush,
AFTER the ``before_flush`` event fires. So the runtime hasher saw
``id=None`` + ``timestamp=None`` while Postgres later stored real
values; the nightly verifier reported ``row_hash_mismatch`` on
every row.

The runtime fix (in ``app/services/audit_chain.py``) materialises
both defaults BEFORE hashing so new rows are consistent. This
migration backfills the same fix across rows already in
``audit_log`` — re-hashes every row in ``seq`` order using the
current populated values, so the verifier reports ``ok=True``
after upgrade.

The repair is safe: we're not modifying any operator-visible
column, only the ``row_hash`` + ``prev_hash`` chain fields the
verifier owns. Audit content (action, user, timestamp,
old_value/new_value, …) is preserved verbatim.

Revision ID: d4f8c91a2e35
Revises: c87a3f29d108
Create Date: 2026-05-13
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision: str = "d4f8c91a2e35"
down_revision: str | None = "c87a3f29d108"
branch_labels: str | None = None
depends_on: str | None = None


def _canonical_payload(row: sa.engine.Row) -> str:
    """Match ``app.services.audit_chain.canonical_json`` byte-for-byte.

    Lives here as a separate copy so the migration runs without
    depending on the app package being importable from the
    alembic process — same pattern the original migration used.
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
    h.update(b"|")
    h.update(canonical.encode())
    return h.hexdigest()


def upgrade() -> None:
    bind = op.get_bind()
    rows = list(
        bind.execute(
            sa.text(
                "SELECT id, seq, timestamp, user_id, user_display_name, auth_source, "
                "source_ip, user_agent, action, resource_type, resource_id, "
                "resource_display, old_value, new_value, changed_fields, "
                "request_id, result, error_detail "
                "FROM audit_log ORDER BY seq ASC"
            )
        )
    )
    if not rows:
        # Fresh install — no rows to re-hash. The runtime fix in
        # ``compute_audit_hashes`` covers every future write from
        # the moment this migration completes.
        return

    update_stmt = sa.text(
        "UPDATE audit_log SET row_hash = :h, prev_hash = :p WHERE id = :id"
    )
    prev: str | None = None
    for row in rows:
        canonical = _canonical_payload(row)
        row_hash = _hash(prev, canonical)
        bind.execute(update_stmt, {"h": row_hash, "p": prev, "id": row.id})
        prev = row_hash


def downgrade() -> None:
    # Re-hash isn't reversible — the pre-repair hashes were broken by
    # definition (that's what the upgrade fixes). Downgrading the
    # schema would leave the rows hashed correctly with the new
    # logic, which is still consistent with verifier expectations,
    # so this is a no-op.
    pass
