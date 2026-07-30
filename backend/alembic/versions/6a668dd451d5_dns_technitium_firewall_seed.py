"""Technitium DNS driver — builtin firewall policy seed.

Adds the ``dns-technitium`` role policy alongside the existing
``dns-bind9`` / ``dns-powerdns`` rows seeded by f5b8d2c91a06 — same shape
(udp/tcp 53), since Technitium binds the standard DNS port like the other
two drivers. Kept as its own migration (not an edit of f5b8d2c91a06, which
already shipped) per the append-only migration rule.

Idempotent: guards on NOT EXISTS / ON CONFLICT DO NOTHING, same as the
parent seed. ``backend/tests/test_firewall_merge.py::
test_builtin_seed_matches_migration`` keeps this in lock-step with
``_BUILTIN_SEED`` in ``app/services/appliance/firewall_merge.py``.

Revision ID: 6a668dd451d5
Revises: e5b2f09c71a4
Create Date: 2026-07-28
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision: str = "6a668dd451d5"
down_revision: str | None = "e5b2f09c71a4"
branch_labels: str | None = None
depends_on: str | None = None

_SCOPE_ROLE = "dns-technitium"
_NAME = "DNS (Technitium)"
_RULES: list = [
    (10, "accept", "udp", [53], "any", "both", None, None),
    (20, "accept", "tcp", [53], "any", "both", None, None),
]
# Same (scope_kind, scope_role, name, enabled, rules) shape as
# f5b8d2c91a06's ``_POLICIES`` — test_builtin_seed_matches_migration
# concatenates every seed migration's ``_POLICIES`` list uniformly.
_POLICIES: list = [("role", _SCOPE_ROLE, _NAME, True, _RULES)]


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO firewall_policy "
            "(id, name, description, scope_kind, scope_role, enabled, is_builtin, priority, "
            " created_at, modified_at) "
            "SELECT gen_random_uuid(), :name, NULL, 'role', :sr, true, true, 100, now(), now() "
            "WHERE NOT EXISTS "
            "(SELECT 1 FROM firewall_policy WHERE scope_kind = 'role' AND scope_role = :sr)"
        ).bindparams(name=_NAME, sr=_SCOPE_ROLE)
    )
    for seq, action, proto, ports, skind, fam, comment, guard in _RULES:
        op.execute(
            sa.text(
                "INSERT INTO firewall_rule "
                "(id, policy_id, seq, action, protocol, ports, source_kind, source_cidrs, "
                " source_alias, family, comment, render_guard, enabled) "
                "SELECT gen_random_uuid(), p.id, :seq, :action, :proto, CAST(:ports AS jsonb), "
                " :skind, '[]'::jsonb, NULL, :fam, :comment, CAST(:guard AS jsonb), true "
                "FROM firewall_policy p WHERE p.scope_kind = 'role' AND p.scope_role = :sr "
                "ON CONFLICT (policy_id, seq) DO NOTHING"
            ).bindparams(
                sr=_SCOPE_ROLE,
                seq=seq,
                action=action,
                proto=proto,
                ports=json.dumps(ports),
                skind=skind,
                fam=fam,
                comment=comment,
                guard=json.dumps(guard) if guard is not None else None,
            )
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM firewall_rule WHERE policy_id IN "
            "(SELECT id FROM firewall_policy WHERE scope_kind = 'role' AND scope_role = :sr "
            " AND is_builtin)"
        ).bindparams(sr=_SCOPE_ROLE)
    )
    op.execute(
        sa.text(
            "DELETE FROM firewall_policy WHERE scope_kind = 'role' AND scope_role = :sr "
            "AND is_builtin"
        ).bindparams(sr=_SCOPE_ROLE)
    )
