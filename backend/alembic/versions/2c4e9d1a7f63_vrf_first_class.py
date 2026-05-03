"""VRF as a first-class entity (issue #86, phase 1).

Revision ID: 2c4e9d1a7f63
Revises: f59a5371bdfb

Create Date: 2026-05-02 00:00:00

This migration:

1. Creates the new ``vrf`` table.
2. Adds nullable ``vrf_id`` foreign-key columns on ``ip_space`` and
   ``ip_block`` (both ON DELETE SET NULL).
3. Migrates the existing freeform ``vrf_name`` /
   ``route_distinguisher`` / ``route_targets`` values from
   ``ip_space`` rows into ``vrf`` rows, deduplicated on the
   ``(name, rd, rts)`` tuple, and stamps each space's ``vrf_id``
   onto the matching new row.
4. Leaves the freeform columns intact. They are deprecated and will
   be dropped in a follow-up migration after one release cycle so
   operators can verify the mapping landed correctly. ``IPBlock``
   never carried freeform VRF columns, so its ``vrf_id`` starts NULL
   (blocks inherit from the parent space).

ASN linkage: the ``vrf.asn_id`` column is added WITHOUT a foreign-
key constraint. Issue #85 lands the ``asn`` table; once that has
merged a follow-up migration will add the FK + cascade. We do not
declare ``depends_on`` on the asn migration since #85 is being
implemented in parallel and either ordering must be safe.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "2c4e9d1a7f63"
down_revision: Union[str, None] = "f59a5371bdfb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. ``vrf`` table ─────────────────────────────────────────────────
    op.create_table(
        "vrf",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        # FK to asn.id added in a follow-up after issue #85 merges.
        sa.Column("asn_id", UUID(as_uuid=True), nullable=True),
        sa.Column("route_distinguisher", sa.String(length=64), nullable=True),
        sa.Column(
            "import_targets",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "export_targets",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "tags",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "custom_fields",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("name", name="uq_vrf_name"),
    )
    op.create_index("ix_vrf_name", "vrf", ["name"])
    op.create_index("ix_vrf_asn_id", "vrf", ["asn_id"])

    # ── 2. ``ip_space.vrf_id`` + ``ip_block.vrf_id`` ─────────────────────
    op.add_column(
        "ip_space",
        sa.Column("vrf_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_ip_space_vrf",
        "ip_space",
        "vrf",
        ["vrf_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_ip_space_vrf_id", "ip_space", ["vrf_id"])

    op.add_column(
        "ip_block",
        sa.Column("vrf_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_ip_block_vrf",
        "ip_block",
        "vrf",
        ["vrf_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_ip_block_vrf_id", "ip_block", ["vrf_id"])

    # ── 3. Backfill ──────────────────────────────────────────────────────
    # For every IPSpace row that has any of the three freeform fields
    # populated, assemble a deduplication key from
    # ``(vrf_name, route_distinguisher, route_targets)`` and INSERT
    # one ``vrf`` row per distinct key, then UPDATE the IPSpace's
    # ``vrf_id`` to point at it.
    #
    # We build the auto-generated VRF name from the freeform
    # ``vrf_name`` when present, falling back to the RD when not.
    # Operators are expected to rename these post-migration through
    # the new VRF UI; the names just need to be unique now.
    #
    # ``import_targets`` and ``export_targets`` are seeded from the
    # legacy ``route_targets`` list (the freeform shape didn't
    # distinguish import vs export — operators were stuffing both
    # directions into the same list). The new schema requires the
    # split, so we duplicate the list into both columns. Operators
    # who relied on the inline ``import:A:B; export:C:D`` convention
    # can split them by editing the resulting VRF row.
    bind = op.get_bind()

    rows = bind.execute(
        sa.text(
            """
            SELECT id, name, vrf_name, route_distinguisher, route_targets
            FROM ip_space
            WHERE vrf_name IS NOT NULL
               OR route_distinguisher IS NOT NULL
               OR (route_targets IS NOT NULL AND jsonb_array_length(route_targets) > 0)
            """
        )
    ).fetchall()

    # Group by (vrf_name, rd, rts) so two spaces sharing the same VRF
    # land on the same vrf row.
    groups: dict[tuple[str | None, str | None, str], dict] = {}
    for row in rows:
        # ``route_targets`` comes back as a Python list (JSONB) or None.
        rts = row.route_targets or []
        if not isinstance(rts, list):
            rts = []
        # Stable canonical key: sorted RT list serialised, with the
        # vrf_name and RD verbatim. Sorting RT order makes
        # ``["A","B"]`` and ``["B","A"]`` collapse to one row.
        key = (
            row.vrf_name,
            row.route_distinguisher,
            ",".join(sorted(str(rt) for rt in rts)),
        )
        if key not in groups:
            # Pick a deterministic VRF name. Operators can rename
            # immediately afterwards through the new UI.
            display_name = row.vrf_name or row.route_distinguisher or row.name
            groups[key] = {
                "name": display_name,
                "rd": row.route_distinguisher,
                "rts": rts,
                "space_ids": [],
            }
        groups[key]["space_ids"].append(row.id)

    # If two distinct groups happen to have collided on display_name
    # (e.g. NULL vrf_name + NULL RD on two spaces with the same name)
    # disambiguate by appending the space-id suffix.
    seen_names: dict[str, int] = {}
    for grp in groups.values():
        base = grp["name"]
        if base in seen_names:
            seen_names[base] += 1
            grp["name"] = f"{base} ({seen_names[base]})"
        else:
            seen_names[base] = 0

    for grp in groups.values():
        result = bind.execute(
            sa.text(
                """
                INSERT INTO vrf (
                    name, description, route_distinguisher,
                    import_targets, export_targets, tags, custom_fields
                ) VALUES (
                    :name, :description, :rd,
                    CAST(:imp AS jsonb), CAST(:exp AS jsonb),
                    '{}'::jsonb, '{}'::jsonb
                )
                RETURNING id
                """
            ),
            {
                "name": grp["name"],
                "description": "Migrated from freeform IPSpace VRF fields (issue #86)",
                "rd": grp["rd"],
                "imp": _jsonb_array_literal(grp["rts"]),
                "exp": _jsonb_array_literal(grp["rts"]),
            },
        )
        new_vrf_id = result.scalar_one()
        for space_id in grp["space_ids"]:
            bind.execute(
                sa.text("UPDATE ip_space SET vrf_id = :vid WHERE id = :sid"),
                {"vid": new_vrf_id, "sid": space_id},
            )


def _jsonb_array_literal(values: list) -> str:
    """Return a JSON-array string literal for use as a JSONB cast input.

    Hand-rolled rather than using ``json.dumps`` so we control the
    string escaping; values are operator-supplied strings and we
    want a deterministic output the JSONB parser will accept. RT
    strings in practice are ASCII-only (``ASN:nn`` or
    ``IP:nn``) — but we still escape backslashes + quotes
    defensively.
    """
    escaped: list[str] = []
    for v in values:
        s = str(v).replace("\\", "\\\\").replace('"', '\\"')
        escaped.append(f'"{s}"')
    return "[" + ",".join(escaped) + "]"


def downgrade() -> None:
    op.drop_index("ix_ip_block_vrf_id", table_name="ip_block")
    op.drop_constraint("fk_ip_block_vrf", "ip_block", type_="foreignkey")
    op.drop_column("ip_block", "vrf_id")

    op.drop_index("ix_ip_space_vrf_id", table_name="ip_space")
    op.drop_constraint("fk_ip_space_vrf", "ip_space", type_="foreignkey")
    op.drop_column("ip_space", "vrf_id")

    op.drop_index("ix_vrf_asn_id", table_name="vrf")
    op.drop_index("ix_vrf_name", table_name="vrf")
    op.drop_table("vrf")
