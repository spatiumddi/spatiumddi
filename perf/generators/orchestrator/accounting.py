"""Exchange-correlated DHCP accounting + per-rcode DNS accounting for the
device-fleet orchestrator (#1057).

Two holes made the orchestrator's counters — the evidence a load run's
handshake and DNS verdicts are read from — disagree with the appliance's own
counters on the same run:

1. ``_on_ack`` decided what an ACK meant from the DEVICE'S STATE at the moment
   it arrived. After ``MAX_DORA_RETRIES`` the device is OFFLINE, so an ACK that
   answers its last REQUEST a moment later matched no branch: the DORA had
   already been counted as a timeout and the lease kea just allocated was
   discarded. The per-packet ``dora_timeout`` timers (one per DISCOVER, one per
   REQUEST) also fired on whichever exchange happened to be current, so a
   DISCOVER's deadline could retransmit over a live REQUEST.

   The ledger below keys every exchange on its xid instead. An ACK is
   attributed to the exchange it answers, with that exchange's own send time
   (so retried DORAs measure the leg the ACK actually closes) and a verdict:
   ``duplicate`` (the exchange was already closed), ``over_budget`` (its own
   timer had fired — the device retried past it but had not given up) or
   ``late`` (the device had given up on the whole DORA / renewal and counted a
   timeout or a lapse). Late ACKs are counted under their own name and the
   lease they carry is taken (the appliance holds it; a model that ignores it
   drifts from the server it is measuring).

2. ``_dns_query`` counted ``dns_ok`` for NOERROR/NXDOMAIN and ``dns_timeout``
   on an exception. Any other rcode was counted as nothing. ``DnsTally`` counts
   every answer under its rcode; ``dns_ok`` keeps its meaning (NOERROR +
   NXDOMAIN), ``dns_answered`` is every response received, and timeouts are
   separated from other exceptions.

Pure and stdlib-only so it is unit-tested without a socket, dnspython, or an
appliance; the orchestrator owns the FSM and the counters, this module owns
the correlation and the verdict.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any

KIND_DISCOVER = "discover"
KIND_SELECT = "select"      # the REQUEST that answers an OFFER (the DORA 'R')
KIND_RENEW = "renew"
KIND_REBIND = "rebind"
DORA_KINDS = frozenset({KIND_DISCOVER, KIND_SELECT})

#: How long a closed / expired / abandoned exchange stays known, so a reply
#: that arrives that much later is still attributed (as duplicate or late)
#: rather than counted as unmatched. Bounds the ledger: at ~20 exchanges/s a
#: 120 s grace keeps a few thousand entries.
DEFAULT_GRACE_S = 120.0


@dataclass
class Exchange:
    xid: int
    index: int
    kind: str
    tx_at: float
    episode: int = 0                 # the device's DORA / renewal round this belongs to
    expired_at: float | None = None  # its own timer fired while it was still live
    gave_up_at: float | None = None  # the device abandoned the round it belonged to
    closed_at: float | None = None   # answered, superseded, or abandoned
    closed_by: str = ""              # "ack" | "nak" | "offer" | "superseded" | "abandoned"
    purge_at: float | None = None

    @property
    def open(self) -> bool:
        return self.closed_at is None


@dataclass
class Closed:
    """What an ACK/NAK/OFFER turned out to be, for the counters."""
    exchange: Exchange
    duplicate: bool = False    # the exchange had already been closed
    over_budget: bool = False  # its own timer had fired (the device retried past it)
    late: bool = False         # the device had given up on the round (timeout/lapse counted)


@dataclass
class ExchangeLedger:
    grace_s: float = DEFAULT_GRACE_S
    _by_xid: dict[int, Exchange] = field(default_factory=dict)
    _by_index: dict[int, set[int]] = field(default_factory=dict)
    _purge_heap: list[tuple[float, int]] = field(default_factory=list)

    # ---- bookkeeping ----
    def __len__(self) -> int:
        return len(self._by_xid)

    def get(self, xid: int | None) -> Exchange | None:
        if xid is None:
            return None
        return self._by_xid.get(int(xid))

    def open_for(self, index: int, kinds: frozenset[str] | None = None) -> list[Exchange]:
        out = []
        for xid in self._by_index.get(index, ()):
            ex = self._by_xid.get(xid)
            if ex is not None and ex.open and (kinds is None or ex.kind in kinds):
                out.append(ex)
        return out

    def _schedule_purge(self, ex: Exchange, now: float) -> None:
        ex.purge_at = now + self.grace_s
        heapq.heappush(self._purge_heap, (ex.purge_at, ex.xid))

    def purge(self, now: float) -> int:
        """Forget exchanges whose grace has passed; returns how many."""
        n = 0
        while self._purge_heap and self._purge_heap[0][0] <= now:
            _when, xid = heapq.heappop(self._purge_heap)
            ex = self._by_xid.get(xid)
            if ex is None or ex.purge_at is None or ex.purge_at > now:
                continue  # re-scheduled later, or already gone
            del self._by_xid[xid]
            idx_set = self._by_index.get(ex.index)
            if idx_set is not None:
                idx_set.discard(xid)
                if not idx_set:
                    del self._by_index[ex.index]
            n += 1
        return n

    # ---- lifecycle ----
    def open(self, xid: int, index: int, kind: str, tx_at: float, episode: int = 0) -> Exchange:
        """Register a sent request. A reused xid (the 8-bit nonce collided with
        a still-known exchange of the same device) closes the old one as
        superseded first, so the reply is attributed to the newer send."""
        old = self._by_xid.get(xid)
        if old is not None and old.open:
            self._close(old, tx_at, "superseded")
        ex = Exchange(xid=xid, index=index, kind=kind, tx_at=tx_at, episode=episode)
        self._by_xid[xid] = ex
        self._by_index.setdefault(index, set()).add(xid)
        return ex

    def expire(self, xid: int, now: float) -> Exchange | None:
        """The exchange's own timer fired. Marks it; the caller decides whether
        the device retries, escalates or gives up (see ``give_up``)."""
        ex = self._by_xid.get(xid)
        if ex is None:
            return None
        if ex.expired_at is None:
            ex.expired_at = now
        if ex.open:
            self._schedule_purge(ex, now)
        return ex

    def give_up(self, index: int, now: float,
                kinds: frozenset[str] | None = None) -> list[Exchange]:
        """The device abandoned its current round (DORA timeout, lapse): every
        open exchange of that round is marked so a later reply reads as late."""
        marked = []
        for ex in self.open_for(index, kinds):
            if ex.gave_up_at is None:
                ex.gave_up_at = now
            if ex.expired_at is None:
                ex.expired_at = now
            self._schedule_purge(ex, now)
            marked.append(ex)
        return marked

    def settle(self, index: int, now: float, keep: int | None = None,
               kinds: frozenset[str] | None = None,
               reason: str = "superseded") -> list[Exchange]:
        """The device's round is over (an ACK took a lease, a NAK restarted it):
        every other open exchange of that round is closed as ``reason``."""
        out = []
        for ex in self.open_for(index, kinds):
            if keep is not None and ex.xid == keep:
                continue
            self._close(ex, now, reason)
            out.append(ex)
        return out

    def _close(self, ex: Exchange, now: float, by: str) -> None:
        ex.closed_at = now
        ex.closed_by = by
        self._schedule_purge(ex, now)

    def close(self, xid: int, now: float, by: str = "ack") -> Closed | None:
        """A reply for ``xid`` arrived. None when the xid is unknown (purged, or
        never ours). Otherwise the verdict for the counters; the exchange is
        closed so a second reply reads as ``duplicate``."""
        ex = self._by_xid.get(xid)
        if ex is None:
            return None
        if not ex.open:
            return Closed(ex, duplicate=True)
        self._close(ex, now, by)
        return Closed(ex, over_budget=ex.expired_at is not None, late=ex.gave_up_at is not None)


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
