"""Per-rcode DNS accounting for the device-fleet orchestrator (#1057).

``_dns_query`` counted ``dns_ok`` for NOERROR/NXDOMAIN and ``dns_timeout`` on
an exception. Any other rcode was counted as nothing: a run whose 606k queries
BIND answered REFUSED read "ok 0, timeouts 46" beside a 606k-sample latency
histogram. ``DnsTally`` counts every answer under its rcode; ``dns_ok`` keeps
its meaning (NOERROR + NXDOMAIN), ``dns_answered`` is every response received,
and timeouts are separated from other exceptions.

Pure and stdlib-only so it is unit-tested without dnspython or an appliance.
"""

from __future__ import annotations

from typing import Any


class DnsTally:
    """Every DNS answer under its rcode; timeouts apart from other exceptions."""

    def __init__(self) -> None:
        self.rcodes: dict[str, int] = {}
        self.errors: dict[str, int] = {}

    def answered(self, rcode_name: str) -> None:
        self.rcodes[rcode_name] = self.rcodes.get(rcode_name, 0) + 1

    def error(self, exc_type_name: str) -> bool:
        """Count an exception by type; True the first time a type is seen (so
        the caller can log it once instead of once per query)."""
        first = exc_type_name not in self.errors
        self.errors[exc_type_name] = self.errors.get(exc_type_name, 0) + 1
        return first

    def counter_fields(self) -> dict[str, int]:
        """``dns_rcode_<NAME>`` fields for the counter snapshot (sorted, so the
        summary line is stable)."""
        return {f"dns_rcode_{k}": v for k, v in sorted(self.rcodes.items())}


def rcode_name(rc: Any, to_text: Any = None) -> str:
    """A stable rcode label: dnspython's mnemonic when available (``to_text``),
    else ``RCODE<n>``; never raises (an unknown code is still counted)."""
    if to_text is not None:
        try:
            name = str(to_text(rc))
            if name:
                return name
        except Exception:  # noqa: BLE001 — an unknown value must still be counted
            pass
    try:
        return f"RCODE{int(rc)}"
    except (TypeError, ValueError):
        return "RCODE?"
