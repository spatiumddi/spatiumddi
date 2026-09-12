"""ExchangeLedger + DnsTally: a reply is attributed to the exchange it answers.

Hermetic — the ledger is pure; the orchestrator's FSM wiring is exercised in
test_device_fleet_accounting.py (skipped where the harness deps are absent).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from accounting import (  # noqa: E402
    DORA_KINDS,
    KIND_DISCOVER,
    KIND_REBIND,
    KIND_RENEW,
    KIND_SELECT,
    DnsTally,
    ExchangeLedger,
    rcode_name,
)

DEV = 7


def xid(nonce: int, index: int = DEV) -> int:
    return ((nonce & 0xFF) << 24) | (index & 0xFFFFFF)


def test_ack_to_the_live_exchange_is_strict() -> None:
    led = ExchangeLedger()
    led.open(xid(1), DEV, KIND_DISCOVER, 0.0, episode=1)
    led.close(xid(1), 0.05, "offer")                       # OFFER answered the DISCOVER
    led.open(xid(2), DEV, KIND_SELECT, 0.05, episode=1)
    c = led.close(xid(2), 0.09, "ack")
    assert c is not None and not c.duplicate and not c.over_budget and not c.late
    assert c.exchange.kind == KIND_SELECT and c.exchange.tx_at == 0.05


def test_ack_during_a_retry_is_over_budget_not_late() -> None:
    """REQUEST#1 timed out (retransmit fired), the device is still DISCOVERING
    on DISCOVER#2 when the ACK for REQUEST#1 lands: counted as an ack, flagged
    over budget, never late (the device had not given up)."""
    led = ExchangeLedger()
    led.open(xid(1), DEV, KIND_DISCOVER, 0.0, episode=1)
    led.close(xid(1), 0.1, "offer")
    led.open(xid(2), DEV, KIND_SELECT, 0.1, episode=1)
    led.expire(xid(2), 4.1)                                # its own timer fired → retry
    led.open(xid(3), DEV, KIND_DISCOVER, 4.1, episode=1)
    c = led.close(xid(2), 4.6, "ack")
    assert c is not None and c.over_budget and not c.late and not c.duplicate
    # the retried DISCOVER is settled with the round, so its OFFER later is not "open"
    settled = led.settle(DEV, 4.6, kinds=DORA_KINDS)
    assert [e.xid for e in settled] == [xid(3)]
    assert not led.get(xid(3)).open and led.get(xid(3)).closed_by == "superseded"


def test_ack_after_give_up_is_late_and_counted_once() -> None:
    led = ExchangeLedger()
    led.open(xid(1), DEV, KIND_DISCOVER, 0.0, episode=1)
    led.close(xid(1), 0.1, "offer")
    led.open(xid(2), DEV, KIND_SELECT, 0.1, episode=1)
    led.expire(xid(2), 4.1)
    led.open(xid(3), DEV, KIND_DISCOVER, 4.1, episode=1)
    led.expire(xid(3), 8.1)
    gave = led.give_up(DEV, 8.1, DORA_KINDS)              # MAX_DORA_RETRIES exceeded
    assert {e.xid for e in gave} == {xid(2), xid(3)}
    c = led.close(xid(2), 9.0, "ack")                      # kea's ACK, 5 s after the REQUEST
    assert c is not None and c.late and c.over_budget and not c.duplicate
    assert c.exchange.gave_up_at == 8.1
    again = led.close(xid(2), 9.5, "ack")                  # a retransmitted ACK
    assert again is not None and again.duplicate


def test_unknown_xid_is_none_and_purge_forgets_after_grace() -> None:
    led = ExchangeLedger(grace_s=10.0)
    assert led.close(xid(9), 1.0) is None
    led.open(xid(1), DEV, KIND_DISCOVER, 0.0)
    led.close(xid(1), 1.0, "nak")
    assert led.purge(5.0) == 0 and len(led) == 1           # still known within the grace
    assert led.purge(11.5) == 1 and len(led) == 0
    assert led.close(xid(1), 12.0) is None                 # now unmatched
    assert led.open_for(DEV) == []


def test_reused_xid_supersedes_the_older_send() -> None:
    led = ExchangeLedger()
    a = led.open(xid(1), DEV, KIND_DISCOVER, 0.0)
    b = led.open(xid(1), DEV, KIND_DISCOVER, 4.0)          # nonce collided on the retry
    assert not a.open and a.closed_by == "superseded" and b.open
    assert led.get(xid(1)) is b


def test_renew_ack_after_escalation_is_late_and_rebind_ack_is_strict() -> None:
    """The renew timer escalated to REBIND; both ACKs then arrive. Each is
    attributed to its own exchange: one late renew, one strict rebind — two
    ACKs from kea, two counted here."""
    led = ExchangeLedger()
    led.open(xid(1), DEV, KIND_RENEW, 100.0, episode=5)
    led.expire(xid(1), 104.0)
    led.give_up(DEV, 104.0, frozenset({KIND_RENEW}))       # the escalation abandons the renew leg
    led.open(xid(2), DEV, KIND_REBIND, 104.0, episode=5)
    renew = led.close(xid(1), 104.5, "ack")
    assert renew is not None and renew.over_budget and renew.late
    rebind = led.close(xid(2), 104.6, "ack")
    assert rebind is not None and not rebind.over_budget and not rebind.late
    assert renew.exchange.kind == KIND_RENEW and rebind.exchange.kind == KIND_REBIND


def test_rebind_ack_after_lapse_is_late() -> None:
    led = ExchangeLedger()
    led.open(xid(2), DEV, KIND_REBIND, 104.0, episode=5)
    led.expire(xid(2), 108.0)
    led.give_up(DEV, 108.0, frozenset({KIND_RENEW, KIND_REBIND}))   # lapse
    c = led.close(xid(2), 109.0, "ack")
    assert c is not None and c.late


def test_give_up_without_kinds_marks_every_open_exchange() -> None:
    led = ExchangeLedger()
    led.open(xid(1), DEV, KIND_RENEW, 0.0)
    led.open(xid(2), 8, KIND_RENEW, 0.0)                   # another device
    marked = led.give_up(DEV, 1.0)
    assert [e.xid for e in marked] == [xid(1)]
    assert led.get(xid(2)).gave_up_at is None


def test_dns_tally_counts_every_rcode_and_names_errors_once() -> None:
    t = DnsTally()
    for name in ("REFUSED", "REFUSED", "NOERROR", "SERVFAIL"):
        t.answered(name)
    assert t.counter_fields() == {
        "dns_rcode_NOERROR": 1, "dns_rcode_REFUSED": 2, "dns_rcode_SERVFAIL": 1}
    first = t.error("OSError")
    second = t.error("OSError")
    other = t.error("BadResponse")
    assert (first, second, other) == (True, False, True)
    assert t.errors == {"OSError": 2, "BadResponse": 1}


def test_rcode_name_uses_the_mnemonic_and_never_raises() -> None:
    assert rcode_name(5, lambda rc: {5: "REFUSED"}[rc]) == "REFUSED"
    assert rcode_name(17, lambda rc: {5: "REFUSED"}[rc]) == "RCODE17"   # to_text raised
    assert rcode_name(3) == "RCODE3"
    assert rcode_name("x") == "RCODE?"
