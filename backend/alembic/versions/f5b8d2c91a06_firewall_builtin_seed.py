"""Issue #285 Phase 3a — firewall builtin policy seed + feature_module row.

Idempotent data seed (split from the schema migration so a seed bug can't
block schema). Seeds:

* the ``appliance.firewall`` feature_module row (enabled — discovery only;
  enforcement is the separate ``platform_settings.firewall_enabled`` switch).
* the builtin policies/rules that reproduce the Phase-2 hardcoded renderer
  BYTE-FOR-BYTE once the 3b merge subsumes ``compile_firewall_body``: a
  fleet baseline (empty), per-role DNS/DHCP service-port policies, and the
  ``control-plane`` policy (etcd/kubelet peer-scoped, 6443 = the kubeapi
  union, MetalLB memberlist guarded on multi-node + VIP). ``observer`` is a
  disabled empty placeholder; ``custom`` an empty operator fill-in. NO
  web/9100 rules — neither is in the drop-in today; seeding them would break
  byte-identity.

Idempotent: policies guard on NOT EXISTS (keyed on scope); rules use
ON CONFLICT (policy_id, seq) DO NOTHING — so a re-run never duplicates AND
never clobbers an operator-tuned builtin rule (3c makes them editable).

⚠️ downgrade() is LOSSY: it wipes builtin policies + their (possibly
operator-tuned) rules and a re-upgrade re-seeds pristine defaults. Operators
who tuned a builtin and want to keep it across a downgrade should clone the
policy (a non-builtin copy) first — clones are untouched by this downgrade.

Revision ID: f5b8d2c91a06
Revises: e4a7c1f08b9d
Create Date: 2026-06-02
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision: str = "f5b8d2c91a06"
down_revision: str | None = "e4a7c1f08b9d"
branch_labels: str | None = None
depends_on: str | None = None

# (scope_kind, scope_role, name, enabled, [rules])
# rule = (seq, action, protocol, ports, source_kind, family, comment, render_guard)
# Role-rule comments are NULL — the merge (3b) emits the per-node
# ``role:{profile}`` comment. Control-plane comments are the fixed base
# strings the renderer uses (``_emit_family_rule`` appends -v4/-v6).
_GUARD = {"min_cp_members": 2, "requires_vip": True}
_POLICIES: list = [
    ("fleet", None, "Fleet baseline", True, []),
    (
        "role",
        "dns-bind9",
        "DNS (BIND9)",
        True,
        [
            (10, "accept", "udp", [53], "any", "both", None, None),
            (20, "accept", "tcp", [53], "any", "both", None, None),
        ],
    ),
    (
        "role",
        "dns-powerdns",
        "DNS (PowerDNS)",
        True,
        [
            (10, "accept", "udp", [53], "any", "both", None, None),
            (20, "accept", "tcp", [53], "any", "both", None, None),
        ],
    ),
    (
        "role",
        "dhcp",
        "DHCP",
        True,
        [
            (10, "accept", "udp", [67, 68], "any", "both", None, None),
        ],
    ),
    (
        "role",
        "control-plane",
        "Control plane (k3s)",
        True,
        [
            (10, "accept", "tcp", [2379, 2380, 10250], "cluster_peers", "both", "k3s-peer", None),
            (20, "accept", "tcp", [6443], "kubeapi", "both", "kubeapi", None),
            (
                30,
                "accept",
                "tcp",
                [7946],
                "cluster_peers",
                "both",
                "metallb-memberlist-tcp",
                _GUARD,
            ),
            (
                40,
                "accept",
                "udp",
                [7946],
                "cluster_peers",
                "both",
                "metallb-memberlist-udp",
                _GUARD,
            ),
        ],
    ),
    ("role", "observer", "Observer", False, []),
    ("role", "custom", "Custom", True, []),
]


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO feature_module (id, enabled) VALUES ('appliance.firewall', true) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )

    for scope_kind, scope_role, name, enabled, rules in _POLICIES:
        if scope_role is None:
            exists_clause = "scope_kind = :sk AND scope_role IS NULL"
        else:
            exists_clause = "scope_kind = :sk AND scope_role = :sr"
        op.execute(
            sa.text(
                "INSERT INTO firewall_policy "
                "(id, name, description, scope_kind, scope_role, enabled, is_builtin, priority, "
                " created_at, updated_at) "
                "SELECT gen_random_uuid(), :name, NULL, :sk, :sr, :enabled, true, 100, now(), now() "
                f"WHERE NOT EXISTS (SELECT 1 FROM firewall_policy WHERE {exists_clause})"
            ).bindparams(name=name, sk=scope_kind, sr=scope_role, enabled=enabled)
        )
        for seq, action, proto, ports, skind, fam, comment, guard in rules:
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
                    sr=scope_role,
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
    # LOSSY (see module docstring): wipes builtin policies + rules + the
    # feature_module row. Clones (is_builtin=false) are preserved.
    op.execute(
        sa.text(
            "DELETE FROM firewall_rule WHERE policy_id IN "
            "(SELECT id FROM firewall_policy WHERE is_builtin)"
        )
    )
    op.execute(sa.text("DELETE FROM firewall_policy WHERE is_builtin"))
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'appliance.firewall'"))
