"""#783 — agentless drivers must apply a change as a whole-RRset write.

#773 fixed this class for the three agent drivers by shipping the complete
desired RRset on the op. The agentless drivers were left out on the
reasoning that they never read the field — but they were performing
whole-RRset writes the entire time:

* **Windows** rendered ``Get-DnsServerResourceRecord … | Remove-…``, which
  removes EVERY RR at the ``(name, type)``, and then added back only the one
  value the op named. So a name with two A records, a backup MX, or SPF
  beside a verification TXT served one of them after any write to the name.
* **Route 53 / Azure DNS / Google Cloud DNS** replaced the whole RRset with
  the op's single value on ``update``. Each carried a comment calling that
  an inherent per-row→RRset limitation; it was only inherent while the op
  carried one value.

These tests pin the rendered wire form rather than mocking it out, because
the bug lived entirely in what got rendered. They also pin the fallback:
a change with ``rrset=None`` (a caller that opted out via ``rrset_action``)
must behave exactly as it did before — in particular a legacy delete must
not become a remove-then-re-add.
"""

from __future__ import annotations

import base64
import json

from app.drivers.dns.base import RecordChange, RecordData, RRsetData, RRsetMember
from app.drivers.dns.windows import _ps_apply_record, _ps_apply_record_batch


def _change(
    op: str,
    *,
    value: str,
    rtype: str = "A",
    members: list[str] | None = None,
    name: str = "www",
    ttl: int | None = 3600,
) -> RecordChange:
    return RecordChange(
        op=op,  # type: ignore[arg-type]
        zone_name="example.com.",
        record=RecordData(name=name, record_type=rtype, value=value, ttl=ttl),
        target_serial=1,
        rrset=(
            None
            if members is None
            else RRsetData(ttl=ttl, members=tuple(RRsetMember(value=v) for v in members))
        ),
    )


def _batch_payload(script: str) -> list[dict]:
    """Pull the JSON op payload back out of the rendered batch script."""
    marker = "FromBase64String('"
    start = script.index(marker) + len(marker)
    end = script.index("'", start)
    return json.loads(base64.b64decode(script[start:end]).decode("utf-8"))


# ── Windows: the singular PowerShell path ────────────────────────────


def test_adding_a_second_a_record_re_adds_both() -> None:
    """THE #783 regression.

    The remove takes out the whole RRset, so the adds after it are the
    complete desired state or the sibling is gone.
    """
    script = _ps_apply_record(_change("create", value="10.0.0.2", members=["10.0.0.1", "10.0.0.2"]))

    assert script.count("Add-DnsServerResourceRecordA") == 2
    assert "-IPv4Address '10.0.0.1'" in script
    assert "-IPv4Address '10.0.0.2'" in script
    assert "Remove-DnsServerResourceRecord" in script


def test_deleting_one_of_two_values_re_adds_the_survivor() -> None:
    script = _ps_apply_record(_change("delete", value="10.0.0.2", members=["10.0.0.1"]))

    assert "-IPv4Address '10.0.0.1'" in script
    assert "10.0.0.2" not in script


def test_deleting_the_last_value_adds_nothing_back() -> None:
    script = _ps_apply_record(_change("delete", value="10.0.0.1", members=[]))

    assert "Remove-DnsServerResourceRecord" in script
    assert "Add-DnsServerResourceRecord" not in script


def test_mx_members_keep_their_own_preferences() -> None:
    change = RecordChange(
        op="update",
        zone_name="example.com.",
        record=RecordData(name="@", record_type="MX", value="mail1", ttl=3600, priority=10),
        target_serial=1,
        rrset=RRsetData(
            ttl=3600,
            members=(
                RRsetMember(value="mail1", priority=10),
                RRsetMember(value="mail2", priority=20),
            ),
        ),
    )

    script = _ps_apply_record(change)

    # A shared priority would collapse primary and backup into one policy.
    assert "-MailExchange 'mail1' -Preference 10" in script
    assert "-MailExchange 'mail2' -Preference 20" in script


def test_without_a_resolved_rrset_a_create_still_adds_only_its_own_value() -> None:
    # ``rrset_action`` callers (DNS pools, the Windows-cutover TTL pre-flight)
    # need precise per-RR semantics and must not be turned into whole-RRset
    # writes behind their back.
    script = _ps_apply_record(_change("create", value="10.0.0.9"))

    assert script.count("Add-DnsServerResourceRecordA") == 1
    assert "-IPv4Address '10.0.0.9'" in script


def test_without_a_resolved_rrset_a_delete_adds_nothing_back() -> None:
    # The regression this guards: routing every op through one
    # remove-then-add body without distinguishing the legacy case turns a
    # delete into a remove-then-re-add, i.e. a silent no-op.
    script = _ps_apply_record(_change("delete", value="10.0.0.9"))

    assert "Add-DnsServerResourceRecord" not in script


# ── Windows: the batched PowerShell path ─────────────────────────────


def test_batch_payload_carries_every_member() -> None:
    script = _ps_apply_record_batch(
        [_change("create", value="10.0.0.2", members=["10.0.0.1", "10.0.0.2"])]
    )

    ops = _batch_payload(script)

    assert [m["v"] for m in ops[0]["m"]] == ["10.0.0.1", "10.0.0.2"]
    assert ops[0]["rm"] == 1


def test_batch_delete_of_the_last_value_carries_no_members() -> None:
    script = _ps_apply_record_batch([_change("delete", value="10.0.0.1", members=[])])

    ops = _batch_payload(script)

    assert ops[0]["m"] == []
    assert ops[0]["rm"] == 1


def test_batch_legacy_delete_carries_no_members_and_still_clears() -> None:
    script = _ps_apply_record_batch([_change("delete", value="10.0.0.9")])

    ops = _batch_payload(script)

    assert ops[0]["m"] == [], "a legacy delete must not re-add its own value"
    assert ops[0]["rm"] == 1


def test_batch_legacy_create_adds_without_clearing_first() -> None:
    # Pre-#783 semantics for an op that opted out: create ADDS to whatever is
    # already at the name. Clearing first would make it a replace.
    script = _ps_apply_record_batch([_change("create", value="10.0.0.9")])

    ops = _batch_payload(script)

    assert [m["v"] for m in ops[0]["m"]] == ["10.0.0.9"]
    assert ops[0]["rm"] == 0


def test_batch_legacy_update_clears_then_adds_its_own_value() -> None:
    script = _ps_apply_record_batch([_change("update", value="10.0.0.9")])

    ops = _batch_payload(script)

    assert [m["v"] for m in ops[0]["m"]] == ["10.0.0.9"]
    assert ops[0]["rm"] == 1


def test_batch_txt_members_are_unquoted_like_the_singular_path() -> None:
    change = _change(
        "update",
        rtype="TXT",
        value='"v=spf1 -all"',
        members=['"v=spf1 -all"', '"google-site-verification=abc"'],
    )

    ops = _batch_payload(_ps_apply_record_batch([change]))

    assert [m["v"] for m in ops[0]["m"]] == [
        "v=spf1 -all",
        "google-site-verification=abc",
    ]


# ── The neutral type ─────────────────────────────────────────────────


def test_as_records_wears_the_ops_name_and_type() -> None:
    base = RecordData(name="www", record_type="A", value="10.0.0.1", ttl=60)
    rrset = RRsetData(ttl=300, members=(RRsetMember(value="10.0.0.7"),))

    (record,) = rrset.as_records(base)

    assert (record.name, record.record_type, record.value) == ("www", "A", "10.0.0.7")
    # The RRset's TTL wins — per RFC 2181 an RRset has one TTL, and the
    # resolver already reconciled disagreeing members down to the lowest.
    assert record.ttl == 300


def test_as_records_falls_back_to_the_base_ttl() -> None:
    base = RecordData(name="www", record_type="A", value="10.0.0.1", ttl=60)
    rrset = RRsetData(ttl=None, members=(RRsetMember(value="10.0.0.7"),))

    (record,) = rrset.as_records(base)

    assert record.ttl == 60
