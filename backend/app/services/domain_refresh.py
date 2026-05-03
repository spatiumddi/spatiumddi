"""Shared "refresh one domain row" logic.

Both the synchronous ``POST /domains/{id}/refresh-whois`` endpoint and
the beat-fired ``app.tasks.domain_whois_refresh.refresh_due_domains``
task converge here so the apply-side semantics stay identical:

* call :func:`app.services.rdap.lookup_domain` (RDAP only — no
  legacy WHOIS fallback yet)
* normalise + write the result back to the row
* recompute ``nameserver_drift`` + ``whois_state``
* stamp ``whois_last_checked_at`` + ``next_check_at``

Returning a structured :class:`DomainRefreshResult` dataclass lets the
caller decide what to do next (audit-log it, write a task summary,
etc.) without coupling this module to either path.

Side-effect-free w.r.t. the DB session: the caller is responsible for
``commit()``. This keeps the task path's bulk-flush pattern fast
(stamp every due row, commit once) and keeps the endpoint's
audit-log-then-commit pattern simple.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.models.domain import Domain
from app.services.rdap import (
    compute_nameserver_drift,
    derive_whois_state,
    lookup_domain,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DomainRefreshResult:
    """Outcome of a single :func:`refresh_one_domain` call.

    Most callers just need the booleans. The "before" values are
    surfaced so the audit-log path can write meaningful diffs without
    having to snapshot the row itself before the call.
    """

    rdap_reachable: bool
    state_before: str
    state_after: str
    state_changed: bool
    registrar_before: str | None
    registrar_after: str | None
    registrar_changed: bool
    nameserver_drift_before: bool
    nameserver_drift_after: bool
    nameserver_drift_changed: bool
    dnssec_signed_before: bool
    dnssec_signed_after: bool
    dnssec_signed_changed: bool

    @property
    def any_meaningful_change(self) -> bool:
        """True iff this refresh moved any of the four observable
        facts the alert evaluator cares about. The scheduled task uses
        this to avoid logging "still ok" ticks — the audit log isn't
        meant to capture the noise of "we polled and nothing happened",
        only the actual transitions."""
        return (
            self.state_changed
            or self.registrar_changed
            or self.nameserver_drift_changed
            or self.dnssec_signed_changed
        )


async def refresh_one_domain(
    domain: Domain,
    *,
    interval_hours: int,
    now: datetime | None = None,
) -> DomainRefreshResult:
    """Hit RDAP, write the result back to ``domain``, return a diff.

    Does NOT commit — the caller owns the session.

    ``interval_hours`` controls ``next_check_at`` (now + interval). The
    endpoint passes the same value the task does so a manual refresh
    pushes the next scheduled poll out by the configured cadence
    (avoids duplicate work).
    """
    when = now or datetime.now(UTC)

    # Snapshot before — used by both the result diff and the
    # transition-once alert evaluators that read the prior value off
    # the most recent open event.
    state_before = domain.whois_state
    registrar_before = domain.registrar
    drift_before = bool(domain.nameserver_drift)
    dnssec_before = bool(domain.dnssec_signed)

    parsed = await lookup_domain(domain.name)
    domain.whois_last_checked_at = when
    domain.next_check_at = when + timedelta(hours=max(1, interval_hours))

    if parsed is None:
        # RDAP unreachable — preserve prior facts (registrar / NS / DS)
        # so a transient TLD outage doesn't fire a spurious "registrar
        # changed" alert. Just stamp the state and the check time.
        domain.whois_state = "unreachable"
    else:
        domain.registrar = parsed.get("registrar")
        domain.registrant_org = parsed.get("registrant_org")
        domain.registered_at = parsed.get("registered_at")
        domain.expires_at = parsed.get("expires_at")
        domain.last_renewed_at = parsed.get("last_renewed_at")
        domain.actual_nameservers = list(parsed.get("nameservers") or [])
        domain.dnssec_signed = bool(parsed.get("dnssec_signed"))
        domain.whois_data = parsed.get("raw")
        domain.nameserver_drift = compute_nameserver_drift(
            domain.expected_nameservers, domain.actual_nameservers
        )
        domain.whois_state = derive_whois_state(
            rdap_returned_data=True,
            expires_at=domain.expires_at,
            expected_nameservers=domain.expected_nameservers or [],
            actual_nameservers=domain.actual_nameservers or [],
            now=when,
        )

    state_after = domain.whois_state
    registrar_after = domain.registrar
    drift_after = bool(domain.nameserver_drift)
    dnssec_after = bool(domain.dnssec_signed)

    return DomainRefreshResult(
        rdap_reachable=parsed is not None,
        state_before=state_before,
        state_after=state_after,
        state_changed=(state_before != state_after),
        registrar_before=registrar_before,
        registrar_after=registrar_after,
        # Only treat registrar as "changed" when RDAP succeeded — an
        # unreachable poll preserves the prior value so the field
        # itself didn't move.
        registrar_changed=(parsed is not None and registrar_before != registrar_after),
        nameserver_drift_before=drift_before,
        nameserver_drift_after=drift_after,
        nameserver_drift_changed=(drift_before != drift_after),
        dnssec_signed_before=dnssec_before,
        dnssec_signed_after=dnssec_after,
        dnssec_signed_changed=(parsed is not None and dnssec_before != dnssec_after),
    )


def build_refresh_audit_payload(
    domain: Domain, result: DomainRefreshResult
) -> dict[str, Any]:
    """Compact dict suitable for the ``new_value`` of an audit row.

    Shared so endpoint + task audit rows have the same shape — makes
    it easier to filter the log surface for "every refresh of foo.com"
    regardless of who triggered it.
    """
    return {
        "whois_state": domain.whois_state,
        "whois_state_before": result.state_before,
        "registrar": domain.registrar,
        "registrar_before": result.registrar_before,
        "expires_at": domain.expires_at.isoformat() if domain.expires_at else None,
        "nameserver_drift": domain.nameserver_drift,
        "nameserver_drift_before": result.nameserver_drift_before,
        "dnssec_signed": domain.dnssec_signed,
        "dnssec_signed_before": result.dnssec_signed_before,
        "rdap_reachable": result.rdap_reachable,
    }


__all__ = [
    "DomainRefreshResult",
    "refresh_one_domain",
    "build_refresh_audit_payload",
]
