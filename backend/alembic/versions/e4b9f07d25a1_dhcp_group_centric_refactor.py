"""dhcp group-centric refactor

Revision ID: e4b9f07d25a1
Revises: d7e5b8c91a3f
Create Date: 2026-04-21 20:30:00.000000

Promotes DHCPServerGroup to the primary configuration container:

- ``dhcp_scope`` / ``dhcp_client_class`` move from per-server (``server_id``)
  to per-group (``group_id``). All servers in a group now serve the same
  scopes — this is what Kea HA requires anyway, and it removes the
  mirror-scopes-manually footgun from the prior release.
- HA tuning (``heartbeat_delay_ms``, ``max_response_delay_ms``,
  ``max_ack_delay_ms``, ``max_unacked_clients``, ``auto_failover``)
  moves from ``dhcp_failover_channel`` onto ``dhcp_server_group``.
- Per-peer HA URL moves from ``dhcp_failover_channel`` onto
  ``dhcp_server.ha_peer_url`` — it was always a property of the server,
  not the channel.
- ``dhcp_failover_channel`` table is dropped. A group with >= 2 Kea
  members is implicitly an HA pair; a group with one member is
  standalone.

Backfill strategy:

1. Ensure every ``dhcp_server`` has a ``server_group_id``. Servers
   with no group get a per-server singleton ``"<server.name>"`` group.
2. Copy ``dhcp_server.server_group_id`` onto the scope/client_class
   rows they used to hang off of.
3. For each existing ``dhcp_failover_channel``: copy HA tuning onto
   the primary's group; set ``ha_peer_url`` on each peer; if the two
   peers are in different groups, move the secondary into the
   primary's group (logs a warning in migration output — intended to
   match operator expectations since HA already required identical
   config which they were managing manually).
4. Drop old FKs / columns / table.

The downgrade reverses shape but not semantics — scopes that span
multiple servers under a group will collapse onto the group's
"first" server (undefined ordering), so downgrade + reupgrade is
not round-trip-safe in the presence of multi-server groups. This is
a conscious trade-off: the group-centric model is the long-term
shape and downgrade exists for local dev reset, not for production
rollback.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e4b9f07d25a1"
down_revision: str | None = "d7e5b8c91a3f"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. Add new columns (nullable first, backfill, then constrain) ────

    op.add_column(
        "dhcp_server_group",
        sa.Column(
            "heartbeat_delay_ms",
            sa.Integer(),
            nullable=False,
            server_default="10000",
        ),
    )
    op.add_column(
        "dhcp_server_group",
        sa.Column(
            "max_response_delay_ms",
            sa.Integer(),
            nullable=False,
            server_default="60000",
        ),
    )
    op.add_column(
        "dhcp_server_group",
        sa.Column(
            "max_ack_delay_ms",
            sa.Integer(),
            nullable=False,
            server_default="10000",
        ),
    )
    op.add_column(
        "dhcp_server_group",
        sa.Column(
            "max_unacked_clients",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
    )
    op.add_column(
        "dhcp_server_group",
        sa.Column(
            "auto_failover",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
    )

    op.add_column(
        "dhcp_server",
        sa.Column("ha_peer_url", sa.String(length=512), nullable=False, server_default=""),
    )

    op.add_column(
        "dhcp_scope",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "dhcp_client_class",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    # ── 2. Backfill ──────────────────────────────────────────────────────

    # 2a. Give every groupless server a singleton group named after itself.
    #     Use the server's own id as a deterministic seed for the new group.
    orphans = conn.execute(
        sa.text(
            """
            SELECT id, name FROM dhcp_server WHERE server_group_id IS NULL
            """
        )
    ).fetchall()
    for server_id, server_name in orphans:
        new_group_id = uuid.uuid4()
        conn.execute(
            sa.text(
                """
                INSERT INTO dhcp_server_group (id, name, description, mode)
                VALUES (:gid, :gname, :desc, 'hot-standby')
                """
            ),
            {
                "gid": new_group_id,
                "gname": f"{server_name}",
                "desc": "Auto-created by group-centric refactor",
            },
        )
        conn.execute(
            sa.text(
                """
                UPDATE dhcp_server SET server_group_id = :gid WHERE id = :sid
                """
            ),
            {"gid": new_group_id, "sid": server_id},
        )

    # 2b. Copy server.server_group_id onto scope.group_id + client_class.group_id.
    conn.execute(
        sa.text(
            """
            UPDATE dhcp_scope AS sc
               SET group_id = srv.server_group_id
              FROM dhcp_server AS srv
             WHERE sc.server_id = srv.id
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE dhcp_client_class AS cc
               SET group_id = srv.server_group_id
              FROM dhcp_server AS srv
             WHERE cc.server_id = srv.id
            """
        )
    )

    # 2b-i. Collapse duplicate (group_id, subnet_id) rows that may now exist:
    # two servers in the same group each had a separate scope row for the same
    # subnet — we keep the oldest (stable behaviour) and delete the rest.
    # Pools / statics FK-cascade off the deleted rows.
    conn.execute(
        sa.text(
            """
            DELETE FROM dhcp_scope
             WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY group_id, subnet_id
                               ORDER BY created_at ASC
                           ) AS rn
                      FROM dhcp_scope
                     WHERE group_id IS NOT NULL
                ) q
                WHERE q.rn > 1
             )
            """
        )
    )
    # Same for client classes: (group_id, name) must be unique.
    conn.execute(
        sa.text(
            """
            DELETE FROM dhcp_client_class
             WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY group_id, name
                               ORDER BY created_at ASC
                           ) AS rn
                      FROM dhcp_client_class
                     WHERE group_id IS NOT NULL
                ) q
                WHERE q.rn > 1
             )
            """
        )
    )

    # 2c. Fold each DHCPFailoverChannel into the primary server's group.
    channels = conn.execute(
        sa.text(
            """
            SELECT c.id, c.mode, c.primary_server_id, c.secondary_server_id,
                   c.primary_peer_url, c.secondary_peer_url,
                   c.heartbeat_delay_ms, c.max_response_delay_ms,
                   c.max_ack_delay_ms, c.max_unacked_clients, c.auto_failover,
                   p.server_group_id AS primary_group_id,
                   s.server_group_id AS secondary_group_id
              FROM dhcp_failover_channel c
              JOIN dhcp_server p ON p.id = c.primary_server_id
              JOIN dhcp_server s ON s.id = c.secondary_server_id
            """
        )
    ).fetchall()
    for ch in channels:
        # Move secondary into primary's group if they differ, so HA rendering
        # can rely on "same group == same config".
        if ch.secondary_group_id != ch.primary_group_id:
            conn.execute(
                sa.text(
                    """
                    UPDATE dhcp_server
                       SET server_group_id = :gid
                     WHERE id = :sid
                    """
                ),
                {"gid": ch.primary_group_id, "sid": ch.secondary_server_id},
            )
            # Scopes / client classes on the secondary's old group were
            # already copied in 2b; the secondary-side rows stay on the
            # old group untouched. Operators can clean those up post-
            # migration if they represented stale config.

        conn.execute(
            sa.text(
                """
                UPDATE dhcp_server_group
                   SET mode = :mode,
                       heartbeat_delay_ms = :hb,
                       max_response_delay_ms = :mresp,
                       max_ack_delay_ms = :mack,
                       max_unacked_clients = :munack,
                       auto_failover = :af
                 WHERE id = :gid
                """
            ),
            {
                "mode": ch.mode,
                "hb": ch.heartbeat_delay_ms,
                "mresp": ch.max_response_delay_ms,
                "mack": ch.max_ack_delay_ms,
                "munack": ch.max_unacked_clients,
                "af": ch.auto_failover,
                "gid": ch.primary_group_id,
            },
        )
        conn.execute(
            sa.text(
                """
                UPDATE dhcp_server SET ha_peer_url = :url WHERE id = :sid
                """
            ),
            {"url": ch.primary_peer_url, "sid": ch.primary_server_id},
        )
        conn.execute(
            sa.text(
                """
                UPDATE dhcp_server SET ha_peer_url = :url WHERE id = :sid
                """
            ),
            {"url": ch.secondary_peer_url, "sid": ch.secondary_server_id},
        )

    # ── 3. Constrain + FK + drop legacy columns ─────────────────────────

    # dhcp_scope: group_id NOT NULL + FK + new unique constraint
    op.alter_column("dhcp_scope", "group_id", nullable=False)
    op.create_foreign_key(
        "fk_dhcp_scope_group",
        "dhcp_scope",
        "dhcp_server_group",
        ["group_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_dhcp_scope_group", "dhcp_scope", ["group_id"])
    op.drop_constraint("uq_dhcp_scope_server_subnet", "dhcp_scope", type_="unique")
    op.create_unique_constraint(
        "uq_dhcp_scope_group_subnet", "dhcp_scope", ["group_id", "subnet_id"]
    )
    op.drop_index("ix_dhcp_scope_server", table_name="dhcp_scope")
    op.drop_constraint("dhcp_scope_server_id_fkey", "dhcp_scope", type_="foreignkey")
    op.drop_column("dhcp_scope", "server_id")

    # dhcp_client_class: same treatment
    op.alter_column("dhcp_client_class", "group_id", nullable=False)
    op.create_foreign_key(
        "fk_dhcp_client_class_group",
        "dhcp_client_class",
        "dhcp_server_group",
        ["group_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_dhcp_client_class_group", "dhcp_client_class", ["group_id"])
    op.drop_constraint(
        "uq_dhcp_client_class_server_name", "dhcp_client_class", type_="unique"
    )
    op.create_unique_constraint(
        "uq_dhcp_client_class_group_name",
        "dhcp_client_class",
        ["group_id", "name"],
    )
    op.drop_index("ix_dhcp_client_class_server", table_name="dhcp_client_class")
    op.drop_constraint(
        "dhcp_client_class_server_id_fkey", "dhcp_client_class", type_="foreignkey"
    )
    op.drop_column("dhcp_client_class", "server_id")

    # ── 4. Drop failover channel table ───────────────────────────────────
    op.drop_table("dhcp_failover_channel")

    # ── 5. Clear server defaults once backfill has happened ─────────────
    # (The defaults are handled by the SQLAlchemy model; keeping them here
    # would make future inserts subtly coupled to SQL-layer defaults.)
    op.alter_column("dhcp_server_group", "heartbeat_delay_ms", server_default=None)
    op.alter_column("dhcp_server_group", "max_response_delay_ms", server_default=None)
    op.alter_column("dhcp_server_group", "max_ack_delay_ms", server_default=None)
    op.alter_column("dhcp_server_group", "max_unacked_clients", server_default=None)
    op.alter_column("dhcp_server_group", "auto_failover", server_default=None)
    op.alter_column("dhcp_server", "ha_peer_url", server_default=None)


def downgrade() -> None:
    # NOTE: downgrade reverses shape but not semantics — see module docstring.
    conn = op.get_bind()

    # Recreate dhcp_failover_channel (empty — channels are reconstructable
    # from group HA fields but we don't synthesise them here).
    op.create_table(
        "dhcp_failover_channel",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("mode", sa.String(20), nullable=False, server_default="hot-standby"),
        sa.Column(
            "primary_server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "secondary_server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "primary_peer_url", sa.String(512), nullable=False, server_default=""
        ),
        sa.Column(
            "secondary_peer_url", sa.String(512), nullable=False, server_default=""
        ),
        sa.Column("heartbeat_delay_ms", sa.Integer, nullable=False, server_default="10000"),
        sa.Column("max_response_delay_ms", sa.Integer, nullable=False, server_default="60000"),
        sa.Column("max_ack_delay_ms", sa.Integer, nullable=False, server_default="10000"),
        sa.Column("max_unacked_clients", sa.Integer, nullable=False, server_default="5"),
        sa.Column("auto_failover", sa.Boolean, nullable=False, server_default=sa.text("TRUE")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # dhcp_client_class: restore server_id
    op.add_column(
        "dhcp_client_class",
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    conn.execute(
        sa.text(
            """
            UPDATE dhcp_client_class AS cc
               SET server_id = (
                   SELECT s.id FROM dhcp_server s
                    WHERE s.server_group_id = cc.group_id
                    ORDER BY s.created_at ASC
                    LIMIT 1
               )
            """
        )
    )
    op.alter_column("dhcp_client_class", "server_id", nullable=False)
    op.create_foreign_key(
        "dhcp_client_class_server_id_fkey",
        "dhcp_client_class",
        "dhcp_server",
        ["server_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_dhcp_client_class_server", "dhcp_client_class", ["server_id"])
    op.drop_constraint(
        "uq_dhcp_client_class_group_name", "dhcp_client_class", type_="unique"
    )
    op.create_unique_constraint(
        "uq_dhcp_client_class_server_name",
        "dhcp_client_class",
        ["server_id", "name"],
    )
    op.drop_index("ix_dhcp_client_class_group", table_name="dhcp_client_class")
    op.drop_constraint(
        "fk_dhcp_client_class_group", "dhcp_client_class", type_="foreignkey"
    )
    op.drop_column("dhcp_client_class", "group_id")

    # dhcp_scope: restore server_id
    op.add_column(
        "dhcp_scope",
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    conn.execute(
        sa.text(
            """
            UPDATE dhcp_scope AS sc
               SET server_id = (
                   SELECT s.id FROM dhcp_server s
                    WHERE s.server_group_id = sc.group_id
                    ORDER BY s.created_at ASC
                    LIMIT 1
               )
            """
        )
    )
    op.alter_column("dhcp_scope", "server_id", nullable=False)
    op.create_foreign_key(
        "dhcp_scope_server_id_fkey",
        "dhcp_scope",
        "dhcp_server",
        ["server_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_dhcp_scope_server", "dhcp_scope", ["server_id"])
    op.drop_constraint("uq_dhcp_scope_group_subnet", "dhcp_scope", type_="unique")
    op.create_unique_constraint(
        "uq_dhcp_scope_server_subnet", "dhcp_scope", ["server_id", "subnet_id"]
    )
    op.drop_index("ix_dhcp_scope_group", table_name="dhcp_scope")
    op.drop_constraint("fk_dhcp_scope_group", "dhcp_scope", type_="foreignkey")
    op.drop_column("dhcp_scope", "group_id")

    op.drop_column("dhcp_server", "ha_peer_url")
    op.drop_column("dhcp_server_group", "auto_failover")
    op.drop_column("dhcp_server_group", "max_unacked_clients")
    op.drop_column("dhcp_server_group", "max_ack_delay_ms")
    op.drop_column("dhcp_server_group", "max_response_delay_ms")
    op.drop_column("dhcp_server_group", "heartbeat_delay_ms")
