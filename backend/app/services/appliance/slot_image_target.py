"""One resolver for the desired slot-image state an appliance is told to fetch.

Two surfaces schedule OS slot upgrades — the per-box Fleet action
(``schedule_appliance_upgrade``) and the multi-node rolling orchestrator
(``/api/v1/upgrades/{run}/start`` → ``_step_trigger_slot_apply``). Both
must stamp the SAME four ``Appliance.desired_slot_image_*`` columns,
because the supervisor writes all four into the host trigger file and the
host runner needs every one of them to fetch successfully.

They diverged: the orchestrator stamped only version + URL, leaving
``desired_slot_image_sha256`` and ``desired_slot_image_tls_insecure``
unset. For an uploaded image that means the host fetches the appliance's
own self-signed HTTPS URL with cert verification ON and no hash to verify
against — exactly the #386 failure, re-introduced on the rolling path
(#787). Worse, a stale sha256 left on the row from an earlier per-box
schedule would be checked against the new image and fail as corruption.

So resolution and stamping live here, and both callers go through them.
Adding a fifth field means touching one function, not remembering two.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appliance import Appliance, ApplianceUpgradeImage

# The host runner only learned to strip a URL ``#fragment`` before
# fetching in #386 (2026-06-12). An older runner passes the fragment
# straight to the downloader and the apply wedges at "in-flight"
# forever (#419), so the nonce is gated on the supervisor's version.
URL_FRAGMENT_STRIP_MIN_VERSION = "2026.06.12"


class SlotImageResolutionError(Exception):
    """The requested upgrade image could not be resolved to a fetchable target."""


@dataclass(frozen=True)
class SlotImageTarget:
    """Everything the host needs to fetch + verify one slot image.

    ``sha256`` and ``tls_insecure`` are meaningful only for an image we
    serve ourselves: we know its hash, and we know the URL points at our
    own self-signed web cert. An operator-pasted external URL is fetched
    over public-CA TLS with no hash (the ``.xz`` container's own integrity
    check is what catches a truncated or corrupted download there).
    """

    url: str
    sha256: str | None = None
    tls_insecure: bool = False
    # #386 Part B re-fire nonce, appended to the URL as ``#a=<nonce>`` so a
    # fresh apply of the same image is a distinct desired-state and the
    # supervisor's fire-once marker doesn't suppress it. It lives on the
    # TARGET, not minted per stamp, because the rolling orchestrator
    # re-drives an incomplete node from step 1 on resume: a nonce minted
    # per call would hand that node a URL it has never fired and trigger a
    # second full slot apply of an image it already staged. Carried in the
    # run plan so every node in a run, and every resume of it, agrees.
    nonce: str | None = None

    def as_plan_fields(self) -> dict[str, object]:
        """Serialise into an ``UpgradeRun.plan`` JSON blob."""
        return {
            "slot_image_url": self.url,
            "slot_image_sha256": self.sha256,
            "slot_image_tls_insecure": self.tls_insecure,
            "slot_image_nonce": self.nonce,
        }

    @classmethod
    def from_plan_fields(cls, plan: dict) -> SlotImageTarget:
        """Rebuild from an ``UpgradeRun.plan`` blob.

        Tolerates a plan written before the two integrity fields existed —
        those runs resolve to the same (url-only) target they had then.
        """
        return cls(
            url=plan.get("slot_image_url") or "",
            sha256=plan.get("slot_image_sha256"),
            tls_insecure=bool(plan.get("slot_image_tls_insecure", False)),
            nonce=plan.get("slot_image_nonce"),
        )


def supervisor_strips_url_fragment(row: Appliance) -> bool:
    """True if this appliance's runner strips a URL ``#fragment`` (≥ #386).

    CalVer (``YYYY.MM.DD-N``) sorts lexicographically, so a string compare
    is correct. A dev / unknown / pre-CalVer version stays on the safe
    clean-URL path — losing only auto-re-fire of the *same* image (a new
    version already changes the URL), never the ability to upgrade (#419).
    """
    ver = row.supervisor_version or row.installed_appliance_version or ""
    return (
        re.match(r"\d{4}\.\d{2}\.\d{2}", ver) is not None and ver >= URL_FRAGMENT_STRIP_MIN_VERSION
    )


async def resolve_slot_image_target(
    db: AsyncSession,
    *,
    base_url: str,
    slot_image_id: uuid.UUID | None = None,
    slot_image_url: str | None = None,
) -> SlotImageTarget:
    """Resolve an upload-or-URL choice into a fetchable target.

    Exactly one of ``slot_image_id`` / ``slot_image_url`` must be given;
    callers validate that at the request-model layer and this asserts it.

    For an uploaded image the URL is composed against ``base_url`` — the
    operator-facing scheme + host the frontend reached us on, which is
    also the address the supervisor already heartbeats to. The ``?t=``
    HMAC authorises the host runner's unauthenticated GET; it is bound to
    the image id, so a leaked URL cannot be replayed for another image.

    Raises ``SlotImageResolutionError`` when the image row is missing.
    """
    if (slot_image_id is None) == (slot_image_url is None):
        raise SlotImageResolutionError("Pass exactly one of slot_image_id or slot_image_url.")

    if slot_image_url is not None:
        return SlotImageTarget(url=slot_image_url)

    assert slot_image_id is not None  # narrowed by the check above
    image = await db.get(ApplianceUpgradeImage, slot_image_id)
    if image is None:
        raise SlotImageResolutionError(f"Upgrade image {slot_image_id} not found.")

    # Imported locally: the token mint lives on the API router that also
    # serves the bytes, and importing it at module scope would cycle back
    # through the router package.
    from app.api.v1.appliance.upgrade_images import (  # noqa: PLC0415
        slot_image_download_token,
    )

    token = slot_image_download_token(image.id)
    return SlotImageTarget(
        url=(
            f"{base_url.rstrip('/')}"
            f"/api/v1/appliance/upgrade-images/{image.id}/raw.xz?t={token}"
        ),
        sha256=image.sha256,
        tls_insecure=True,
    )


def new_refire_nonce() -> str:
    """Mint one ``#a=`` re-fire nonce. Call once per scheduling decision."""
    return uuid.uuid4().hex[:12]


def stamp_desired_slot_image(
    row: Appliance,
    target: SlotImageTarget,
    *,
    desired_version: str,
) -> None:
    """Write all four desired-state columns onto ``row``.

    The re-fire nonce comes from ``target``, so re-stamping the same target
    (an orchestrator resume re-driving a node) reproduces the identical URL
    and the supervisor's fire-once marker still suppresses it. Appended
    only when the appliance's runner is known to strip the fragment (#419).
    """
    url = target.url
    if target.nonce and supervisor_strips_url_fragment(row):
        url = f"{url}#a={target.nonce}"

    row.desired_appliance_version = desired_version
    row.desired_slot_image_url = url
    row.desired_slot_image_sha256 = target.sha256
    row.desired_slot_image_tls_insecure = target.tls_insecure


__all__ = [
    "URL_FRAGMENT_STRIP_MIN_VERSION",
    "SlotImageResolutionError",
    "SlotImageTarget",
    "new_refire_nonce",
    "resolve_slot_image_target",
    "stamp_desired_slot_image",
    "supervisor_strips_url_fragment",
]
