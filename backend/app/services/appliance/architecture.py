"""One vocabulary for appliance CPU architecture (#1026).

Three places need to agree on what architecture a thing is: the node
(reported by the supervisor from ``uname -m``), the upgrade image (from
our own release metadata, or declared at upload), and the gate that
refuses to point one at the other.

**The names are ``amd64`` / ``arm64``**, not ``x86_64`` / ``aarch64``.
Both spellings mean the same silicon, and the choice is not arbitrary:
``appliance/scripts/fetch-k3s.sh`` already normalises to this pair for
the k3s airgap tarball, ``bake-images.sh`` takes ``BAKE_SAVE_PLATFORM=
linux/amd64``, and the release assets are named
``spatiumddi-appliance-slot-<tag>-amd64.raw.xz``. A second spelling in
the database would mean a mapping table between the artifact names and
the rows that describe them, which is exactly the kind of seam a
mismatch hides in.

``normalize()`` accepts either spelling because the input comes from
``uname -m`` on one side and from an artifact name on the other, and
returns **None** for anything it does not recognise. None is UNKNOWN,
never a guess: an architecture invented from an unfamiliar string would
be compared against a real one and produce a confident wrong answer,
which is worse than admitting the field is not known — see
``architecture_conflict``.
"""

from __future__ import annotations

from typing import Literal

#: The architectures an appliance image can be built for. Extending this
#: means an ISO, a slot image and a CI matrix leg — it is not a free
#: string, and the DB columns are validated against it on the way in.
ApplianceArchitecture = Literal["amd64", "arm64"]

ARCHITECTURES: tuple[str, ...] = ("amd64", "arm64")

#: Every spelling we accept on input, mapped to the canonical name.
#: ``uname -m`` answers the left column; artifact names and Docker
#: platforms answer the right.
_ALIASES: dict[str, str] = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "x86-64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "arm64v8": "arm64",
}


def normalize(value: str | None) -> str | None:
    """Canonicalise an architecture name, or None if unrecognised.

    None means UNKNOWN and must never be treated as a match — the whole
    point of this field is to refuse an upgrade that would brick a node,
    and a wrong answer defeats it more thoroughly than no answer does.
    """
    if not value:
        return None
    return _ALIASES.get(value.strip().lower())


def architecture_conflict(image_arch: str | None, node_arch: str | None) -> bool:
    """True when these two are known to disagree.

    **UNKNOWN on either side is not a conflict**, and that is a
    deliberate choice rather than an oversight:

    * Every upgrade image published before this change carries no
      architecture, and every one of them is amd64 — there was no other
      appliance build. Refusing them would make the control plane unable
      to upgrade the fleet it already has, on the day it gained the
      ability to describe the problem.
    * A supervisor too old to report ``architecture`` leaves the node
      side unknown for the same reason.

    So this gate stops the mistake it can *see*, and the host runner
    (``spatium-upgrade-slot``) is the backstop that inspects the actual
    bytes it is about to write. The control plane is deliberately not
    the only gate on an operation that bricks a node.
    """
    if image_arch is None or node_arch is None:
        return False
    return image_arch != node_arch


__all__ = [
    "ARCHITECTURES",
    "ApplianceArchitecture",
    "architecture_conflict",
    "normalize",
]
