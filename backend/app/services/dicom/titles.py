"""DICOM AE Title validation — issue #723.

The AE Value Representation is defined in PS3.5, and getting it wrong in
either direction has a real cost:

* Too permissive and the registry accepts a title a device will reject,
  so the plan fails at commissioning — which is the exact failure this
  whole registry exists to prevent.
* Too strict and the registry rejects titles real devices already use,
  which is worse: it makes the tool unusable at the sites that most need
  it, and the operator's fix is to stop using the tool.

The rules, verbatim from the specification rather than from folklore:

* maximum length **16 bytes** — bytes, not characters. Titles are
  overwhelmingly ASCII in practice, but the ceiling is a byte count, so
  it is measured as one.
* the default character repertoire, **excluding backslash** (``0x5C``,
  which is the DICOM multi-value delimiter) and **all control
  characters**.
* **spaces are legal.** They are non-significant padding — a title is
  padded with trailing spaces to an even length on the wire — so leading
  and trailing space is trimmed on the way in and internal spaces are
  kept.
* an **all-space value is forbidden**, which after trimming is the same
  statement as "must not be empty".

Note what is *not* a rule: there is no alphanumeric-only restriction and
no case requirement. Titles like ``CT_1``, ``PACS-ARCHIVE`` and
``MR SCANNER 2`` are all valid, and case is significant to peers — an AE
configured as ``CT_ONE`` does not answer to ``ct_one``. So titles are
stored as given and matched exactly; only the *default-title* advisory
check compares case-insensitively, because that is asking "did anyone
change this from the box" rather than "do these two match".
"""

from __future__ import annotations

from app.models.dicom import DICOM_AE_TITLE_MAX_BYTES, DICOM_DEFAULT_AE_TITLES

# 0x5C is the DICOM multi-value delimiter, so it can never appear inside
# a single value. Control characters are excluded by the VR definition.
_BACKSLASH = "\\"


class AETitleError(ValueError):
    """An AE Title violates the PS3.5 ``AE`` value representation.

    Carries a message naming the specific rule broken, because "invalid
    AE title" tells an operator nothing about whether their problem is
    length, a stray tab pasted out of a spreadsheet, or the backslash a
    vendor put in their conformance statement by mistake.
    """


def normalize_ae_title(raw: str) -> str:
    """Trim padding and return the canonical stored form.

    Spaces are non-significant padding per the VR, so trimming them is
    normalization, not mutation of operator intent. Nothing else is
    changed — in particular case is preserved, because peers match
    titles exactly.
    """
    return (raw or "").strip()


def validate_ae_title(raw: str) -> str:
    """Return the normalized title, or raise :class:`AETitleError`.

    Checks are ordered so the message names the most specific problem:
    empty first (which also covers the forbidden all-space value), then
    the character repertoire, then length. Length is checked last so a
    title that is both too long *and* contains a backslash reports the
    backslash — the operator can shorten a name themselves, but a
    forbidden character usually means the value was mis-transcribed.
    """
    title = normalize_ae_title(raw)
    if not title:
        # Also the all-space case, which the specification forbids
        # explicitly: after trimming padding there is nothing left.
        raise AETitleError("AE Title must not be empty or all spaces")

    if _BACKSLASH in title:
        raise AETitleError(
            "AE Title must not contain a backslash — it is the DICOM "
            "multi-value delimiter and cannot appear inside a single value"
        )

    control = [ch for ch in title if ord(ch) < 0x20 or ord(ch) == 0x7F]
    if control:
        raise AETitleError(
            "AE Title must not contain control characters "
            f"(found {', '.join(f'0x{ord(ch):02X}' for ch in sorted(set(control)))}) — "
            "this usually means the value was pasted out of a spreadsheet cell"
        )

    encoded = len(title.encode("utf-8"))
    if encoded > DICOM_AE_TITLE_MAX_BYTES:
        # Report bytes, not characters, and say so: a 16-character title
        # with a non-ASCII character in it is over the limit, and an
        # operator counting characters will not see why.
        raise AETitleError(
            f"AE Title must be at most {DICOM_AE_TITLE_MAX_BYTES} bytes " f"(this one is {encoded})"
        )
    return title


def is_vendor_default(title: str) -> bool:
    """Is this a known out-of-the-box vendor default?

    Compared case-insensitively on the trimmed value, because the
    question is "did anyone change this from the box", and a device
    shipped as ``ANY-SCP`` that someone re-typed as ``Any-SCP`` is
    equally un-changed.

    Advisory by design: a default title is not invalid, it is just the
    single most common cause of the estate-wide collisions this registry
    exists to prevent — two vendors' defaults collide long before anyone
    runs out of names.
    """
    return normalize_ae_title(title).lower() in DICOM_DEFAULT_AE_TITLES


__all__ = [
    "AETitleError",
    "is_vendor_default",
    "normalize_ae_title",
    "validate_ae_title",
]
