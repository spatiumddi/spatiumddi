"""MAC-address canonicalisation — one implementation, no package weight.

``app.core`` has an empty ``__init__``, which is the whole reason this lives
here rather than beside its sibling ``norm_mac`` in
``app.services.dhcp.normalize``.

The history is worth keeping, because the second attempt was wrong too.
This function started in ``app/api/v1/dhcp/_mac.py``. #972's resolver needs
it and the DHCP config bundle imports that resolver, so reaching into
``app.api`` was a circular import. Moving it into
``app.services.dhcp.normalize`` replaced that with a subtler one: importing
any ``app.services.dhcp`` submodule executes the package ``__init__``, which
imports ``config_bundle``, which imports ``app.services.e911`` — so the cycle
came back, and only showed up when something imported the e911 package
*first*. ``app.core`` has no such init, so a pure string function placed here
cannot participate in a cycle at all.
"""

from __future__ import annotations

import re

__all__ = ["canonicalize_mac"]

_MAC_DELIMS = re.compile(r"[:\-.\s]")


def canonicalize_mac(raw: str) -> str:
    """Canonicalise an operator-entered MAC, REFUSING anything malformed.

    The strict sibling of ``app.services.dhcp.normalize.norm_mac``: that one
    folds for comparison and never rejects, because a reconciler diffing wire
    state against the DB must not fail on a value the wire produced. This one
    validates, because the column is ``MACADDR`` and a typo reaching Postgres
    comes back to the operator as a 500.
    """
    cleaned = _MAC_DELIMS.sub("", raw.strip()).lower()
    if len(cleaned) != 12 or not all(c in "0123456789abcdef" for c in cleaned):
        raise ValueError("mac_address must be 12 hex chars (any common separator allowed)")
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))
