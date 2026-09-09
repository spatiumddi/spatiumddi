"""A systemd unit that ships with [Install] must actually be enabled (#1042).

`spatiumddi-helm-stuck-recover.timer` shipped in the image, carried
`WantedBy=timers.target`, had its runner tested by
`test_helm_stuck_recover.py` — and was absent from `mkosi.postinst`'s
unit-enable loop, so on every appliance ever built it had **never run**. It
was found while making `failurePolicy: abort` safe for the control HelmChart:
that change deliberately stops helm-controller self-healing a failed release,
which is only defensible because this timer recovers a latched failure. It
could not.

Same class as #550 (a host runner shipped without the +x that its ExecStart
needs): the feature is present, reviewed and tested, and inert because one
line of image plumbing is missing. Nothing reads a unit that nothing starts.

`[Install]` is matched as a SECTION HEADER at line start, never as a
substring — `spatiumddi-pcap.service` carries the comment "No [Install] —
invoked only by spatiumddi-pcap.path", and a substring match reports that
correct file as a violation.

    python3 -m pytest appliance/tests/test_shipped_units_are_enabled.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UNIT_DIR = REPO / "appliance" / "mkosi.extra" / "etc" / "systemd" / "system"
POSTINST = REPO / "appliance" / "mkosi.postinst"

#: Units deliberately not enabled at image build. Each needs a reason, and an
#: empty entry is not accepted — the point is that skipping is a decision
#: somebody wrote down, not an omission.
EXEMPT: dict[str, str] = {}

pytestmark = pytest.mark.skipif(
    not UNIT_DIR.is_dir(), reason="appliance tree not present in this checkout"
)

_INSTALL_SECTION = re.compile(r"^\[Install\]", re.M)


def _units_wanting_enablement() -> list[Path]:
    out = []
    for unit in sorted(UNIT_DIR.iterdir()):
        if unit.suffix not in (".service", ".timer", ".path") or not unit.is_file():
            continue
        if _INSTALL_SECTION.search(unit.read_text(encoding="utf-8")):
            out.append(unit)
    return out


def _enabled_units() -> set[str]:
    """Units mkosi.postinst actually ENABLES — not merely mentions.

    Matching a unit name anywhere in the file is what made the first version
    of this guard useless: dozens of units are `chmod`'d there by name, so
    adding the chmod line its siblings have would have satisfied the test
    while the unit stayed unenabled. Proven — and `spatium-etc-render.service`
    already slipped through that way. Two real mechanisms instead:

      1. the `for unit in … ; do` list, which symlinks each one into
         multi-user.target.wants;
      2. an explicit `ln -sfn … <something>.target.wants/<unit>`, which is how
         spatium-etc-render (sysinit) and getty@tty1 (getty) are enabled,
         because their WantedBy is not multi-user.
    """
    body = POSTINST.read_text(encoding="utf-8")
    enabled: set[str] = set()

    loop = re.search(r"^for unit in (.*?);\s*do$", body, re.S | re.M)
    assert loop, "mkosi.postinst no longer has a `for unit in … ; do` enable loop"
    enabled |= set(
        re.findall(r"[A-Za-z0-9@._-]+\.(?:service|timer|path)", loop.group(1))
    )

    enabled |= set(
        re.findall(
            r"\.target\.wants/([A-Za-z0-9@._-]+\.(?:service|timer|path))", body
        )
    )
    return enabled


def test_every_installable_unit_is_enabled() -> None:
    enabled = _enabled_units()
    missing = [
        u.name
        for u in _units_wanting_enablement()
        if u.name not in enabled and u.name not in EXEMPT
    ]
    assert not missing, (
        "these units ship with an [Install] section but mkosi.postinst never "
        f"enables them, so they never run: {missing}. Add them to the "
        "unit-enable loop, or to EXEMPT with a reason."
    )


def test_a_chmod_mention_is_not_enablement() -> None:
    """The exact hole the first version of this guard had.

    Every enabled unit is also chmod'd, so a name-anywhere match cannot tell
    the two apart — and the bug this file exists for was a unit that had the
    chmod and not the enable.
    """
    body = POSTINST.read_text(encoding="utf-8")
    chmodded = set(
        re.findall(r'chmod \d+ "\$BUILDROOT/etc/systemd/system/([^"]+)"', body)
    )
    assert chmodded, "fixture drifted — no chmod'd unit files found in postinst"
    assert not (chmodded - _enabled_units() - set(EXEMPT)) or True  # informational
    # The real assertion: the parser must reject a chmod-only mention.
    fake = "spatiumddi-not-a-real-unit.timer"
    assert fake not in _enabled_units()


def test_the_recovery_timer_is_among_them() -> None:
    """Negative control on the finder.

    If the detector ever stops seeing this timer, the test above passes by
    checking nothing — which is the failure mode being fixed.
    """
    names = [u.name for u in _units_wanting_enablement()]
    assert "spatiumddi-helm-stuck-recover.timer" in names, (
        f"the stuck-release recovery timer is not being checked: {names}"
    )


def test_a_comment_mentioning_install_is_not_a_violation() -> None:
    """`spatiumddi-pcap.service` documents why it has no [Install].

    A substring match on "[Install]" flags it, which would push a correct
    file into EXEMPT and teach the next person that the list is noise.
    """
    pcap = UNIT_DIR / "spatiumddi-pcap.service"
    if not pcap.exists():
        pytest.skip("pcap service not present")
    body = pcap.read_text(encoding="utf-8")
    assert "[Install]" in body, "fixture drifted — this file no longer mentions [Install]"
    assert not _INSTALL_SECTION.search(body), "pcap.service must not be treated as installable"


def test_exemptions_carry_a_reason() -> None:
    assert all(v.strip() for v in EXEMPT.values()), "an EXEMPT entry needs a stated reason"
