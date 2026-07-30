"""DICOM AE registry invariants — issue #723 Phase 1.

The same shape as ``services/bacnet/registry.py``, and for the same
reason: these helpers are about the *registry*, not about a request, and
they must behave identically whichever surface (REST, CSV importer,
Copilot proposal) drives them.

* :func:`sync_subnet_id` keeps the denormalised ``subnet_id`` in step
  with the AE's IPAM anchor.
* :func:`assert_title_available` turns ``uq_dicom_ae_title`` into a
  domain error naming the *conflicting* AE. "CT_1 is already used by the
  Radiology CT" is the answer a PACS administrator needs; a raw
  ``IntegrityError`` surfacing as a 500 is not.

Zero network I/O: nothing in this package speaks DICOM. The C-ECHO
verification probe that would confirm an AE answers at its recorded
host:port is deferred to its own issue, and when it lands it must go
through ``services/ipam/probe_policy`` (#722) first — a probe that
reaches a modality mid-study is precisely the hazard that flag exists
for.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dicom import DICOMApplicationEntity
from app.models.ipam import IPAddress


class AETitleConflict(Exception):
    """An AE Title is already registered to another row.

    Carries the conflicting AE's identity because "who has it" is the
    only fact that helps resolve a collision — the caller turns this
    into a 409 that names the other device rather than restating that
    the name is taken.
    """

    def __init__(self, *, ae_title: str, existing_id: uuid.UUID) -> None:
        self.ae_title = ae_title
        self.existing_id = existing_id
        super().__init__(
            f"AE Title {ae_title!r} is already registered (application entity {existing_id})"
        )


async def sync_subnet_id(db: AsyncSession, ae: DICOMApplicationEntity) -> uuid.UUID | None:
    """Point ``ae.subnet_id`` at its IPAM address's current subnet.

    Call on create and on any update that changes ``ip_address_id``.
    Mutates in place (no flush, no commit — the caller owns the
    transaction) and returns the resolved subnet id.

    ``None`` is a normal state here, not an error, and it means one of
    two things: the AE is an unbound *reservation* (a title still in peer
    configuration whose host was decommissioned — the case
    ``ON DELETE SET NULL`` exists for), or the anchor row vanished
    concurrently. Either way the AE drops out of per-subnet grouping
    rather than being counted against the wrong subnet.
    """
    if ae.ip_address_id is None:
        ae.subnet_id = None
        return None
    subnet_id = (
        await db.execute(select(IPAddress.subnet_id).where(IPAddress.id == ae.ip_address_id))
    ).scalar_one_or_none()
    ae.subnet_id = subnet_id
    return subnet_id


async def assert_title_available(
    db: AsyncSession,
    ae_title: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Raise :class:`AETitleConflict` when the title is taken.

    ``exclude_id`` is the row being edited, so re-submitting an AE's own
    title in a PATCH is not reported as a conflict with itself.

    This is a check-then-act against a uniquely-indexed column, so it is
    racy by construction under concurrent creates — intentionally.
    ``uq_dicom_ae_title`` is what guarantees uniqueness, and the caller
    re-maps the resulting ``IntegrityError`` to the same 409. This
    function exists to make the common case legible, not to enforce.
    """
    stmt = select(DICOMApplicationEntity.id).where(DICOMApplicationEntity.ae_title == ae_title)
    if exclude_id is not None:
        stmt = stmt.where(DICOMApplicationEntity.id != exclude_id)
    existing = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    if existing is None:
        return
    raise AETitleConflict(ae_title=ae_title, existing_id=existing)


__all__ = ["AETitleConflict", "assert_title_available", "sync_subnet_id"]
