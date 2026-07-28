"""BACnet vendor-id → display-name convenience labels.

**This is a convenience label map, not an authoritative registry.**
ASHRAE administers the BACnet Vendor ID space and has assigned well
over a thousand ids, with new ones issued continuously; the
authoritative list lives at <https://bacnet.org/vendor-ids/> and is
deliberately not vendored here. What this module carries is a small
hand-checked set covering manufacturers an operator is overwhelmingly
likely to meet on a building network, so a device list can render
"Delta Controls" instead of a bare ``8``.

Why it is small on purpose: a *wrong* label is worse than no label.
An integrator chasing a duplicate device instance who sees the wrong
manufacturer against an HVAC controller is being actively misled into
looking at the wrong panel, on the wrong floor, from the wrong
vendor's tool. So an id is only listed when its assignment is
verifiable, and everything else falls through to the operator-supplied
``BACnetDevice.vendor_name`` — which is free text somebody at the site
typed, and is therefore the better guess for an id we cannot resolve.

If a deployment needs full coverage, the shape that fits is an
importer that loads the published registry into a table with
provenance and a fetched-at timestamp, the way OUI data is handled.
The fall-through here means nothing breaks until that exists.
"""

from __future__ import annotations

# Keyed by the ``BACnetVendorID`` reported in an ``I-Am`` APDU
# (Unsigned16). Ordered by id.
#
# Deliberately minimal. An entry earns its place only when the
# assignment was corroborated independently more than once; several
# large BAS manufacturers whose ids could not be pinned down that
# firmly are absent on purpose rather than guessed, because an unknown
# id costs a label while a wrong one sends an integrator to the wrong
# panel. Extending it is a one-line change and unknown ids already
# degrade gracefully to the operator-supplied ``vendor_name``, so the
# conservative default is cheap to walk back and expensive to get wrong.
#
# Authoritative registry: <https://bacnet.org/vendor-ids/>.
VENDOR_NAMES: dict[int, str] = {
    0: "ASHRAE",
    2: "The Trane Company",
    5: "Johnson Controls",
    7: "Siemens Schweiz AG",
    8: "Delta Controls",
    10: "Schneider Electric",
    389: "Tridium",
}


def vendor_name_for(vendor_id: int | None) -> str | None:
    """Label for a known BACnet vendor id, or ``None``.

    ``None`` is the honest answer for both "no id reported" and "id
    not in this map" — callers decide what to fall back to, which for
    the API layer is :func:`resolve_vendor_label`.
    """
    if vendor_id is None:
        return None
    return VENDOR_NAMES.get(vendor_id)


def resolve_vendor_label(vendor_id: int | None, vendor_name: str | None) -> str | None:
    """Best available human label for a device's vendor.

    Registry-first: a *known* id wins over the stored string, because
    the id is machine-reported (it comes off the wire in the ``I-Am``,
    or out of an import) while ``vendor_name`` is free text that drifts
    — "JCI", "Johnson", "johnson controls inc" all mean the same
    manufacturer and none of them group or sort together.

    Unknown or absent ids fall through to whatever the operator
    recorded. An empty ``vendor_name`` collapses to ``None`` so the UI
    renders its own placeholder instead of an empty cell that looks
    like a rendering bug.
    """
    known = vendor_name_for(vendor_id)
    if known is not None:
        return known
    if vendor_name:
        stripped = vendor_name.strip()
        if stripped:
            return stripped
    return None


__all__ = ["VENDOR_NAMES", "resolve_vendor_label", "vendor_name_for"]
