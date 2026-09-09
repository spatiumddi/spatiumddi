"""The upgrade path must never cross architectures (#1026).

``appliance_upgrade_image`` carried no architecture, and the catalogue,
``desired_slot_image_url`` and ``spatium-upgrade-slot`` all select by
*version*. So the day an arm64 slot image exists, an operator — or a
fleet-wide upgrade — can hand an amd64 appliance an arm64 root
filesystem: the download verifies (the SHA matches, it is a perfectly
good image), the slot writes, GRUB switches, and the node does not come
back.

These pin the control-plane half. The host-side half lives in
``appliance/tests/test_upgrade_slot_architecture.py`` — deliberately two
gates, because the control plane can only refuse what it knows and an
operator-pasted external URL tells it nothing.

The gate is asserted at ``stamp_desired_slot_image`` rather than per
call site: three surfaces write those columns, and a check remembered in
two of them is a check the third one skips.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.appliance import (
    APPLIANCE_STATE_APPROVED,
    Appliance,
    ApplianceUpgradeImage,
)
from app.models.auth import User
from app.services.appliance.architecture import architecture_conflict, normalize
from app.services.appliance.slot_image_target import (
    SlotImageArchitectureMismatch,
    SlotImageResolutionError,
    SlotImageTarget,
    stamp_desired_slot_image,
)


def _appliance(architecture: str | None) -> Appliance:
    return Appliance(
        id=uuid.uuid4(),
        hostname="ddi1",
        architecture=architecture,
        # supervisor_version drives the #419 URL-fragment gate; a modern
        # one keeps these tests off that branch.
        supervisor_version="2026.09.01-1",
    )


def _target(architecture: str | None) -> SlotImageTarget:
    return SlotImageTarget(
        url="https://ddi1.example/api/v1/appliance/upgrade-images/x/raw.xz?t=t",
        sha256="a" * 64,
        tls_insecure=True,
        architecture=architecture,
    )


# ── normalisation ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("x86_64", "amd64"),
        ("X86_64", "amd64"),
        ("amd64", "amd64"),
        ("aarch64", "arm64"),
        ("arm64", "arm64"),
        (" arm64 ", "arm64"),
    ],
)
def test_normalize_accepts_both_spellings(raw: str, expected: str) -> None:
    """``uname -m`` answers one way and the artifact names the other; a
    second spelling in the database would mean a mapping table between
    the artifacts and the rows describing them."""
    assert normalize(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "riscv64", "ppc64le", "nonsense"])
def test_normalize_returns_none_rather_than_guessing(raw: str | None) -> None:
    """None is UNKNOWN. An architecture invented from an unfamiliar
    string would be compared against a real one and produce a confident
    wrong answer — worse than admitting the field is not known."""
    assert normalize(raw) is None


# ── the conflict rule ──────────────────────────────────────────────


def test_known_mismatch_is_a_conflict() -> None:
    assert architecture_conflict("arm64", "amd64") is True
    assert architecture_conflict("amd64", "arm64") is True


def test_known_match_is_not_a_conflict() -> None:
    assert architecture_conflict("amd64", "amd64") is False
    assert architecture_conflict("arm64", "arm64") is False


@pytest.mark.parametrize(
    ("image", "node"),
    [(None, "amd64"), ("amd64", None), (None, None)],
)
def test_unknown_on_either_side_is_not_a_conflict(image: str | None, node: str | None) -> None:
    """Deliberate, and the reason is a migration rather than a
    principle: every image published before this change carries no
    architecture and every one of them is amd64. Refusing them would
    make the control plane unable to upgrade the fleet it already has,
    on the day it gained the ability to describe the problem."""
    assert architecture_conflict(image, node) is False


# ── the gate ───────────────────────────────────────────────────────


def test_stamp_refuses_a_cross_architecture_upgrade() -> None:
    row = _appliance("amd64")
    with pytest.raises(SlotImageArchitectureMismatch) as exc:
        stamp_desired_slot_image(row, _target("arm64"), desired_version="2026.09.04-1")
    # The message has to name both sides — "incompatible image" sends an
    # operator to re-download the one they already have.
    assert "arm64" in str(exc.value)
    assert "amd64" in str(exc.value)
    assert "ddi1" in str(exc.value)


def test_a_refused_stamp_writes_nothing() -> None:
    """Partial desired state is worse than none: the supervisor writes
    whatever four columns it finds into the host trigger file, so a
    version stamped without its URL is a node that fetches nothing and
    reports an upgrade in flight forever."""
    row = _appliance("amd64")
    with pytest.raises(SlotImageArchitectureMismatch):
        stamp_desired_slot_image(row, _target("arm64"), desired_version="2026.09.04-1")
    assert row.desired_appliance_version is None
    assert row.desired_slot_image_url is None
    assert row.desired_slot_image_sha256 is None


def test_the_mismatch_reaches_the_api_as_a_422() -> None:
    """Both scheduling endpoints already map ``SlotImageResolutionError``
    to a 422. Subclassing it is what makes the refusal reach the operator
    without either handler learning a new exception — so the subclass
    relationship is behaviour, not taxonomy."""
    assert issubclass(SlotImageArchitectureMismatch, SlotImageResolutionError)


def test_stamp_allows_a_matching_architecture() -> None:
    row = _appliance("arm64")
    stamp_desired_slot_image(row, _target("arm64"), desired_version="2026.09.04-1")
    assert row.desired_appliance_version == "2026.09.04-1"
    assert row.desired_slot_image_sha256 == "a" * 64


@pytest.mark.parametrize(
    ("image", "node"),
    [(None, "amd64"), ("amd64", None), (None, None)],
)
def test_stamp_allows_when_either_side_is_unknown(image: str | None, node: str | None) -> None:
    row = _appliance(node)
    stamp_desired_slot_image(row, _target(image), desired_version="2026.09.04-1")
    assert row.desired_appliance_version == "2026.09.04-1"


# ── the plan round-trip ────────────────────────────────────────────


def test_architecture_survives_the_run_plan() -> None:
    """A rolling run stores its target in ``UpgradeRun.plan`` and
    re-hydrates it on every node and every resume. Dropping the
    architecture there would disarm the gate for exactly the surface
    that touches the most nodes."""
    target = _target("arm64")
    assert SlotImageTarget.from_plan_fields(target.as_plan_fields()) == target


def test_a_pre_1026_plan_rehydrates_as_unknown() -> None:
    """Runs planned before the field existed resolve to the same
    (unknown) answer they had when they were planned, rather than
    raising on a missing key."""
    plan = {
        "slot_image_url": "https://example/x.raw.xz",
        "slot_image_sha256": None,
        "slot_image_tls_insecure": False,
        "slot_image_nonce": None,
    }
    assert SlotImageTarget.from_plan_fields(plan).architecture is None


# ── the refusal has to reach the operator as a 422 ─────────────────
#
# Asserting the subclass relationship (above) proves the exception COULD
# be mapped. It does not prove the call that raises it sits inside the
# handler that maps it — and in the first cut it did not: ``stamp`` was
# outside the ``try``, so the mismatch escaped as a 500 while the
# design, the docs and the Fleet picker's own comment all promised a
# 422 naming both architectures. Found by code review, not by the unit
# test, because a unit test of the exception class cannot see where the
# call site is.


async def _superadmin_headers(db: AsyncSession) -> dict[str, str]:
    user = User(
        username=f"arch-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Arch Admin",
        hashed_password=hash_password("test-pw-1026"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _seed(db: AsyncSession, *, node_arch: str, image_arch: str | None):
    appliance = Appliance(
        id=uuid.uuid4(),
        hostname=f"ddi-{uuid.uuid4().hex[:8]}",
        state=APPLIANCE_STATE_APPROVED,
        public_key_der=b"fake-key",
        public_key_fingerprint=uuid.uuid4().hex * 2,
        cert_serial="0001",
        deployment_kind="appliance",
        architecture=node_arch,
        supervisor_version="2026.09.01-1",
    )
    image = ApplianceUpgradeImage(
        id=uuid.uuid4(),
        filename="spatiumddi-appliance-slot-2026.09.04-1-arm64.raw.xz",
        size_bytes=1234,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        appliance_version="2026.09.04-1",
        architecture=image_arch,
    )
    db.add_all([appliance, image])
    await db.flush()
    return appliance, image


@pytest.mark.asyncio
async def test_scheduling_a_cross_architecture_upgrade_is_a_422(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    headers = await _superadmin_headers(db_session)
    appliance, image = await _seed(db_session, node_arch="amd64", image_arch="arm64")
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/appliance/appliances/{appliance.id}/upgrade",
        headers=headers,
        json={"desired_appliance_version": "2026.09.04-1", "slot_image_id": str(image.id)},
    )
    assert resp.status_code == 422, resp.text
    # The message has to name both sides — "incompatible image" sends an
    # operator to re-download the one they already have.
    detail = resp.text.lower()
    assert "arm64" in detail and "amd64" in detail


@pytest.mark.asyncio
async def test_a_refused_schedule_leaves_no_desired_state(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """A 422 that had already written half the desired-state columns
    would leave the supervisor fetching nothing and reporting an upgrade
    in flight forever."""
    headers = await _superadmin_headers(db_session)
    appliance, image = await _seed(db_session, node_arch="amd64", image_arch="arm64")
    await db_session.commit()

    await client.post(
        f"/api/v1/appliance/appliances/{appliance.id}/upgrade",
        headers=headers,
        json={"desired_appliance_version": "2026.09.04-1", "slot_image_id": str(image.id)},
    )
    await db_session.refresh(appliance)
    assert appliance.desired_appliance_version is None
    assert appliance.desired_slot_image_url is None


@pytest.mark.asyncio
async def test_a_matching_architecture_schedules_normally(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    headers = await _superadmin_headers(db_session)
    appliance, image = await _seed(db_session, node_arch="arm64", image_arch="arm64")
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/appliance/appliances/{appliance.id}/upgrade",
        headers=headers,
        json={"desired_appliance_version": "2026.09.04-1", "slot_image_id": str(image.id)},
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(appliance)
    assert appliance.desired_appliance_version == "2026.09.04-1"
