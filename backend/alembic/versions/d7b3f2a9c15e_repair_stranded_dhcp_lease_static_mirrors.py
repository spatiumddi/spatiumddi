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

Note: removing a mirror row ``SET NULL``s the ``ip_address_id`` of the
auto-generated A/PTR records it published, so those records are captured (step 0)
by their still-intact FK *before* the mirror is deleted, then step 3b removes only
that captured set — a zone with no primary DNS server can't push a wire delete, so
they'd otherwise be un-cleanable "ip-deleted" stale rows. Crucially, records that
merely have a null ``ip_address_id`` (the normal state for ACME / Kubernetes /
Tailscale / NetBird auto-generated records) are NOT captured and stay untouched.
The DB record is authoritative; if a primary is later configured, a full Sync
reconciles the wire.

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


# The upgrade SQL, in order, as a single source of truth so a test can drive the
# exact statements (the test schema is built from the models via ``create_all``,
# not by running Alembic, so there is no other faithful way to exercise this).
_UPGRADE_STATEMENTS: tuple[str, ...] = (
    # 0. Capture the ids of the auto-generated forward/reverse DNS records that
    #    step 3b must clear — BEFORE steps 1–2 delete the ip_address mirrors they
    #    point at. The mirror FK is ``ON DELETE SET NULL``, so once the mirror is
    #    gone the record's ``ip_address_id`` becomes NULL and the correlation is
    #    lost. We snapshot the exact records this migration is about to orphan —
    #    records that JOIN to one of the two mirror sets deleted below — and
    #    delete only those in 3b.
    #
    #    A null ``ip_address_id`` on an ``auto_generated`` record is the NORMAL
    #    state for ACME DNS-01, Kubernetes-ingress, and Tailscale/NetBird mesh
    #    records, so a blanket "``ip_address_id IS NULL``" delete would wipe those
    #    unrelated zones. We only ever touch records tied to the specific mirror
    #    rows removed here. GSLB pool records (``pool_member_id``) aren't
    #    IPAM-backed, so they're excluded.
    "CREATE TEMP TABLE _dhcp_orphan_dns (id uuid PRIMARY KEY) ON COMMIT DROP",
    # 0a. records tied to the step-1 static-reservation mirrors.
    """
    INSERT INTO _dhcp_orphan_dns (id)
    SELECT r.id
    FROM dns_record r
    JOIN ip_address ia ON ia.id = r.ip_address_id
    WHERE r.auto_generated = true
      AND r.pool_member_id IS NULL
      AND ia.status = 'static_dhcp'
      AND ia.static_assignment_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM dhcp_static_assignment sa
        WHERE sa.id::text = ia.static_assignment_id
          AND sa.deleted_at IS NULL
      )
    ON CONFLICT (id) DO NOTHING
    """,
    # 0b. records tied to the step-2 dynamic-lease mirrors.
    """
    INSERT INTO _dhcp_orphan_dns (id)
    SELECT r.id
    FROM dns_record r
    JOIN ip_address ia ON ia.id = r.ip_address_id
    JOIN dhcp_lease l ON l.ip_address = ia.address
    JOIN dhcp_scope s ON l.scope_id = s.id AND ia.subnet_id = s.subnet_id
    WHERE r.auto_generated = true
      AND r.pool_member_id IS NULL
      AND ia.auto_from_lease = true
      AND s.deleted_at IS NOT NULL
    ON CONFLICT (id) DO NOTHING
    """,
    # 1. Static reservation mirrors whose backing reservation is no longer live
    #    (soft-deleted, or gone entirely). These are the ``static_dhcp`` rows
    #    that kept showing as "Reservation" after their scope was deleted.
    """
    DELETE FROM ip_address ia
    WHERE ia.status = 'static_dhcp'
      AND ia.static_assignment_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM dhcp_static_assignment sa
        WHERE sa.id::text = ia.static_assignment_id
          AND sa.deleted_at IS NULL
      )
    """,
    # 2. Dynamic-lease mirrors for leases whose scope is soft-deleted. Delete the
    #    ``auto_from_lease`` ip_address rows BEFORE the lease rows (step 3) since
    #    this join needs the lease. Correlate on the scope's subnet + the lease
    #    address (how pull_leases created the mirror).
    """
    DELETE FROM ip_address ia
    USING dhcp_lease l, dhcp_scope s
    WHERE ia.auto_from_lease = true
      AND l.scope_id = s.id
      AND s.deleted_at IS NOT NULL
      AND ia.subnet_id = s.subnet_id
      AND ia.address = l.ip_address
    """,
    # 3. The stranded dynamic lease rows themselves (scope soft-deleted).
    """
    DELETE FROM dhcp_lease l
    USING dhcp_scope s
    WHERE l.scope_id = s.id
      AND s.deleted_at IS NOT NULL
    """,
    # 3b. Delete only the auto-generated DNS records captured in step 0 — the
    #     ones this migration orphaned by removing their IPAM mirror above. A zone
    #     with no primary DNS server can't push a wire delete, so they'd otherwise
    #     linger forever as un-cleanable "ip-deleted" stale rows in the subnet's
    #     DNS view. Unrelated auto_generated records (ACME / integrations) are not
    #     in the temp table, so they are untouched.
    "DELETE FROM dns_record r USING _dhcp_orphan_dns o WHERE r.id = o.id",
    # 4. Recompute cached utilization for every subnet so the "N / total
    #    allocated" figures drop to match the rows just removed. ``available``
    #    rows don't count as allocated (they're the free state), mirroring
    #    ``_update_utilization``.
    """
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
    """,
)


def upgrade() -> None:
    for statement in _UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # One-shot data repair — the deleted rows were dangling artifacts with no
    # valid state to restore, so there is nothing to undo.
    pass
