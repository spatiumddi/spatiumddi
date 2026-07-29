"""DICOM AE Title registry service layer — issue #723 (part of #543).

**Safety posture: registry only.** Nothing in this package opens a
socket. There is no C-ECHO, no C-FIND, no association negotiation of any
kind — that probe is deferred to its own issue, and when it lands it
must go through ``services/ipam/probe_policy`` (#722) first, because a
probe reaching a modality mid-study is exactly the hazard that flag
exists for.

**No PHI, ever.** These modules move network identity: AE Titles, hosts,
ports, roles, vendors. Nothing here reads, stores, or infers anything
about patients, studies or images. Storing patient-identifiable data
would make SpatiumDDI a HIPAA Business Associate, which is permanently
out of scope — so the ``dicom_peer`` edges are *configured*
relationships documented by the operator, never associations inferred
from captured traffic.

Modules (imported by full path, per the ``services/ot`` layout)::

    from app.services.dicom.titles import validate_ae_title, is_vendor_default
    from app.services.dicom.registry import assert_title_available, sync_subnet_id
    from app.services.dicom.importer import parse_rows, plan_import, apply_plan

``titles``    — the PS3.5 ``AE`` value-representation rules. 16 *bytes*,
                spaces legal as padding, all-space forbidden, no
                backslash, no control characters. Getting this wrong in
                the strict direction rejects titles real devices use.
``registry``  — the invariants behind ``uq_dicom_ae_title``: a friendly
                conflict that names the other AE, and the denormalised
                ``subnet_id`` sync.
``importer``  — CSV ingest of the estate's existing AE table as a
                preview → commit pair. Keyed on the title, not the
                address, and a blank address registers an unbound
                reservation rather than failing.
"""

from __future__ import annotations
