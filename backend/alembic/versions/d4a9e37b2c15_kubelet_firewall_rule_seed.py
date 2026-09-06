"""Kubelet 10250 from inside the cluster — builtin firewall rule seed (#993).

Adds one rule to the existing ``control-plane`` builtin policy seeded by
f5b8d2c91a06: ``tcp/10250`` accepted from the pod ∪ service CIDRs.

#990 added a direct kubelet transport for the cluster-health screen and it
never connected on an appliance: the supervisor-rendered ``input`` chain is
``policy drop`` and opened 10250 to CLUSTER PEERS only, a set that is EMPTY
on a single node — so the rule was not even emitted. Traffic from a
non-hostNetwork api pod to its own node's IP enters via ``cni0`` with a
pod-CIDR source and traverses INPUT like any LAN packet, so it was dropped,
and every cluster-health request paid a full connect timeout per node before
falling back to the apiserver ``nodes/proxy`` transport.

``source_kind='kubelet'`` resolves to pod ∪ service and deliberately NOT to
the operator's ``kubeapi_expose_cidrs`` allowlist, which widens 6443. That
allowlist means "let me reach the apiserver from the LAN", and the apiserver
guards every request with RBAC; the kubelet API (``/exec``, ``/run``,
``/attach``) is a different proposition and must not inherit it.

seq 25 places it between ``kubeapi`` (20) and the MetalLB memberlist rules
(30/40), matching where both hardcoded renderers emit it — the byte-identity
contract across the three renderers is on the ORDER, not just the set.

Idempotent: ``ON CONFLICT (policy_id, seq) DO NOTHING``, same as the parent
seed. ``backend/tests/test_firewall_merge.py::
test_builtin_seed_matches_migration`` keeps this in lock-step with
``_BUILTIN_SEED`` in ``app/services/appliance/firewall_merge.py``.

Revision ID: d4a9e37b2c15
Revises: b6f2c04a71d8
Create Date: 2026-09-05
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision: str = "d4a9e37b2c15"
down_revision: str | None = "b6f2c04a71d8"
branch_labels: str | None = None
depends_on: str | None = None

_SCOPE_ROLE = "control-plane"
_RULES: list = [
    (25, "accept", "tcp", [10250], "kubelet", "both", "kubelet", None),
]
# Same (scope_kind, scope_role, name, enabled, rules) shape as
# f5b8d2c91a06's ``_POLICIES``. The name is None because this migration adds
# a RULE to a policy that already exists rather than creating one —
# test_builtin_seed_matches_migration folds every seed migration's policies
# together by (scope_kind, scope_role) before comparing, so a rule-only
# contribution lands on the policy it belongs to.
_POLICIES: list = [("role", _SCOPE_ROLE, None, True, _RULES)]


def upgrade() -> None:
    for seq, action, proto, ports, skind, fam, comment, guard in _RULES:
        op.execute(
            sa.text(
                "INSERT INTO firewall_rule "
                "(id, policy_id, seq, action, protocol, ports, source_kind, source_cidrs, "
                " source_alias, family, comment, render_guard, enabled) "
                "SELECT gen_random_uuid(), p.id, :seq, :action, :proto, CAST(:ports AS jsonb), "
                " :skind, '[]'::jsonb, NULL, :fam, :comment, CAST(:guard AS jsonb), true "
                "FROM firewall_policy p WHERE p.scope_kind = 'role' AND p.scope_role = :sr "
                " AND p.is_builtin "
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
    # Only this rule — the control-plane policy and its other rules belong to
    # f5b8d2c91a06 and must survive.
    for seq, *_rest in _RULES:
        op.execute(
            sa.text(
                "DELETE FROM firewall_rule WHERE seq = :seq AND policy_id IN "
                "(SELECT id FROM firewall_policy WHERE scope_kind = 'role' "
                " AND scope_role = :sr AND is_builtin)"
            ).bindparams(sr=_SCOPE_ROLE, seq=seq)
        )
