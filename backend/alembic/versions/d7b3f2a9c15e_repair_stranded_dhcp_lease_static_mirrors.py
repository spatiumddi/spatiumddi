"""repair stranded DHCP lease + static IPAM mirror rows (#623)

Before this release, deleting a DHCP scope left DHCP-derived IPAM rows dangling:

* the static reservation's ``ip_address`` mirror (``status="static_dhcp"``,
  back-linked via ``static_assignment_id``) was never released on the soft path,
* the dynamic lease's mirror (``status="dhcp"``, ``auto_from_lease=true``) and the
  ``dhcp_lease`` row itself were never touched (leases have only a nullable
  ``ON DELETE SET NULL`` backlink to the scope).

Both kept rendering as visible rows in the IPAM subnet table and inflating the
subnet's ``allocated_ips`` / utilization long after the scope was gone. The code
now deletes these on scope deletion; this one-shot migration repairs installs
already upgraded past #621 by clearing the rows that are already stranded, the
auto-generated DNS records those mirrors orphaned, then recomputes each subnet's
cached utilization.

Note: removing the mirror rows ``SET NULL``s the ``ip_address_id`` of the
auto-generated A/PTR records they published, so step 3b clears those too — a
zone with no primary DNS server can't push a wire delete, so they'd otherwise
be un-cleanable "ip-deleted" stale rows. The DB record is authoritative; if a
primary is later configured, a full Sync reconciles the wire.

Revision ID: d7b3f2a9c15e
Revises: c9a1f4e07b52
Create Date: 2026-07-12
"""

from __future__ import annotations

from alembic import op

revision = "d7b3f2a9c15e"
down_revision = "c9a1f4e07b52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Static reservation mirrors whose backing reservation is no longer live
    #    (soft-deleted, or gone entirely). These are the ``static_dhcp`` rows
    #    that kept showing as "Reservation" after their scope was deleted.
    op.execute("""
        DELETE FROM ip_address ia
        WHERE ia.status = 'static_dhcp'
          AND ia.static_assignment_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM dhcp_static_assignment sa
            WHERE sa.id::text = ia.static_assignment_id
              AND sa.deleted_at IS NULL
          )
        """)

    # 2. Dynamic-lease mirrors for leases whose scope is soft-deleted. Delete the
    #    ``auto_from_lease`` ip_address rows BEFORE the lease rows (step 3) since
    #    this join needs the lease. Correlate on the scope's subnet + the lease
    #    address (how pull_leases created the mirror).
    op.execute("""
        DELETE FROM ip_address ia
        USING dhcp_lease l, dhcp_scope s
        WHERE ia.auto_from_lease = true
          AND l.scope_id = s.id
          AND s.deleted_at IS NOT NULL
          AND ia.subnet_id = s.subnet_id
          AND ia.address = l.ip_address
        """)

    # 3. The stranded dynamic lease rows themselves (scope soft-deleted).
    op.execute("""
        DELETE FROM dhcp_lease l
        USING dhcp_scope s
        WHERE l.scope_id = s.id
          AND s.deleted_at IS NOT NULL
        """)

    # 3b. Delete the auto-generated forward/reverse DNS records orphaned by
    #     removing the static + lease mirror rows above. The FK is ON DELETE
    #     SET NULL, so those records' ``ip_address_id`` is now NULL (or dangling)
    #     — and a zone with no primary DNS server can't push a wire delete, so
    #     they'd otherwise linger forever as un-cleanable "ip-deleted" stale rows
    #     in the subnet's DNS view. GSLB pool records (``pool_member_id``) aren't
    #     IPAM-backed, so they're excluded.
    op.execute("""
        DELETE FROM dns_record r
        WHERE r.auto_generated = true
          AND r.pool_member_id IS NULL
          AND (
            r.ip_address_id IS NULL
            OR NOT EXISTS (SELECT 1 FROM ip_address ia WHERE ia.id = r.ip_address_id)
          )
        """)

    # 4. Recompute cached utilization for every subnet so the "N / total
    #    allocated" figures drop to match the rows just removed. ``available``
    #    rows don't count as allocated (they're the free state), mirroring
    #    ``_update_utilization``.
    op.execute("""
        UPDATE subnet s SET
          allocated_ips = COALESCE(cnt.n, 0),
          utilization_percent = CASE
            WHEN s.total_ips > 0 THEN round(COALESCE(cnt.n, 0)::numeric / s.total_ips * 100, 2)
            ELSE 0
          END
        FROM (
          SELECT sub.id AS subnet_id,
                 count(ia.id) FILTER (WHERE ia.status <> 'available') AS n
          FROM subnet sub
          LEFT JOIN ip_address ia ON ia.subnet_id = sub.id
          GROUP BY sub.id
        ) cnt
        WHERE s.id = cnt.subnet_id
        """)


def downgrade() -> None:
    # One-shot data repair — the deleted rows were dangling artifacts with no
    # valid state to restore, so there is nothing to undo.
    pass
