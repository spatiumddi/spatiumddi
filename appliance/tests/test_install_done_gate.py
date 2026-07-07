"""Host-portable regression test for the headless-install Done-screen gate.

#549 headless follow-up. A fully-preseeded (unattended) install has no
operator on tty1 to dismiss the final "Press OK to reboot" msgbox, so if
that dialog is shown unconditionally the install completes on disk but the
box hangs forever and never reboots — the appliance never comes up on its
own. `welcome` and the final `confirm` are already gated on
``FULLY_UNATTENDED``; the Done msgbox must be too.

do_install itself is NOT unit-testable host-side (it partitions a real disk
as root and rsyncs the live rootfs — the rest of this suite deliberately
exercises only ``spatium-install --check-preseed``, which touches nothing).
So this pins the exact regression as a STRUCTURAL invariant on the script:

  1. the ``--title "Done"`` msgbox sits INSIDE an
     ``if [ ... FULLY_UNATTENDED ... != "1" ]`` gate, and
  2. ``systemctl reboot`` runs AFTER that gate closes (i.e. unconditionally,
     on both the attended and the unattended path).

Fails on the pre-fix script (no gate around the msgbox). Tolerant of
whitespace / comment / wording changes around it.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_install_done_gate.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

INSTALLER = (
    Path(__file__).parent.parent / "mkosi.extra" / "usr" / "local" / "bin" /
    "spatium-install"
)


def _lines() -> list[str]:
    return INSTALLER.read_text(encoding="utf-8").splitlines()


def _index(pred, lines, start=0):
    for i in range(start, len(lines)):
        if pred(lines[i]):
            return i
    return -1


def test_done_msgbox_is_gated_on_fully_unattended():
    lines = _lines()

    done = _index(lambda ln: '--title "Done"' in ln, lines)
    assert done != -1, 'could not find the `--title "Done"` msgbox in spatium-install'

    # A FULLY_UNATTENDED gate must open BEFORE the Done msgbox, and no `fi`
    # may intervene between the gate and the msgbox (else the msgbox is not
    # inside it). Match the guard loosely so re-wording the test/comparison
    # doesn't break the test: an `if` line mentioning FULLY_UNATTENDED.
    gate_re = re.compile(r'^\s*if\b.*FULLY_UNATTENDED')
    gate = -1
    for i in range(done - 1, -1, -1):
        if re.match(r'^\s*fi\b', lines[i]):
            break  # a closed block sits between us and any earlier gate
        if gate_re.search(lines[i]):
            gate = i
            break
    assert gate != -1, (
        "the Done msgbox is not wrapped in a FULLY_UNATTENDED gate — a "
        "fully-preseeded install would hang on it forever (see #549 headless)"
    )

    # The gate must close (fi) after the msgbox, and `systemctl reboot` must
    # come AFTER that fi — i.e. the reboot is unconditional, only the dialog
    # is skipped on the unattended path.
    fi = _index(lambda ln: re.match(r'^\s*fi\b', ln), lines, start=done)
    assert fi != -1, "no `fi` closing the Done-msgbox gate after the msgbox"

    reboot = _index(lambda ln: "systemctl reboot" in ln, lines, start=done)
    assert reboot != -1, "no `systemctl reboot` after the Done msgbox"
    assert reboot > fi, (
        "`systemctl reboot` must run AFTER the FULLY_UNATTENDED gate closes "
        "(unconditionally) — otherwise the unattended path skips the reboot too"
    )


def test_welcome_and_confirm_are_also_unattended_gated():
    """Guard the sibling invariant the fix mirrors: welcome + the final
    confirm are skipped on a fully-unattended run. If these ever regress,
    the 'gate the Done screen the same way' rationale no longer holds."""
    text = INSTALLER.read_text(encoding="utf-8")
    # main()'s state machine skips welcome and confirm when FULLY_UNATTENDED.
    assert re.search(r'FULLY_UNATTENDED"?\s*!=\s*"1".*\n\s*welcome', text) or \
        re.search(r'if \[ "\$FULLY_UNATTENDED" != "1" \]; then\s*\n\s*welcome', text), \
        "welcome is no longer gated on FULLY_UNATTENDED"
    assert 'if [ "$FULLY_UNATTENDED" = "1" ]; then' in text, \
        "the confirm/do_install unattended gate in main() is gone"
