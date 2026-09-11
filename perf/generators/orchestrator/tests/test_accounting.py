"""DnsTally: every DNS answer under its rcode, exceptions by type (#1057)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from accounting import DnsTally, rcode_name  # noqa: E402


def test_dns_tally_counts_every_rcode_and_names_errors_once() -> None:
    t = DnsTally()
    for name in ("REFUSED", "REFUSED", "NOERROR", "SERVFAIL"):
        t.answered(name)
    assert t.counter_fields() == {
        "dns_rcode_NOERROR": 1, "dns_rcode_REFUSED": 2, "dns_rcode_SERVFAIL": 1}
    assert t.error("OSError") is True
    assert t.error("OSError") is False
    assert t.error("BadResponse") is True
    assert t.errors == {"OSError": 2, "BadResponse": 1}


def test_rcode_name_uses_the_mnemonic_and_never_raises() -> None:
    assert rcode_name(5, lambda rc: {5: "REFUSED"}[rc]) == "REFUSED"
    assert rcode_name(17, lambda rc: {5: "REFUSED"}[rc]) == "RCODE17"   # to_text raised
    assert rcode_name(3) == "RCODE3"
    assert rcode_name("x") == "RCODE?"
