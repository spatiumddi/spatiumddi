"""Structural guards for the #995 Phase 1 installer fixes.

``do_install`` partitions a real disk as root and rsyncs the live rootfs,
so it is not unit-testable host-side — the rest of this suite deliberately
exercises only ``--check-preseed`` and pure helpers. What CAN be pinned is
the shape of the code, and for these five items the shape IS the bug:
each one was a swallowed failure, a stale string or a hardcoded constant,
and each regresses by someone re-adding exactly the token removed here.

The executable halves live next door — ``test_install_dm_scope.py``
(item 10) and ``test_install_field_validators.py`` (items 3 + 4).

HOW TO RUN (from the repo root):
    python3 -m pytest appliance/tests/test_install_phase1_fixes.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

INSTALLER = (
    Path(__file__).parent.parent / "mkosi.extra" / "usr" / "local" / "bin" /
    "spatium-install"
)
SRC = INSTALLER.read_text(encoding="utf-8")
# Comment-stripped view. Every one of these guards asserts on the ABSENCE
# of a token, and each item's comment explains the bug by quoting the code
# it replaced — so matching raw text would report the explanation as the
# regression.
CODE = "\n".join(ln.split("#", 1)[0] for ln in SRC.splitlines())


def _fn(name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{$.*?^\}}$", SRC, re.MULTILINE | re.DOTALL)
    assert m, f"{name}() not found in {INSTALLER}"
    return m.group(0)


# ── Item 1 — the install logs survive the reboot ──────────────────────


def test_installer_logs_are_copied_to_the_target():
    fn = _fn("_save_install_logs")
    assert "/var/log/spatiumddi/install" in fn
    # All three of the diagnostics on_failure prints, or the set that
    # survives is not the set an operator is told to read.
    assert "$INSTALL_LOG" in fn
    assert "$TRACE_LOG" in fn
    assert "spatium-install-launch.log" in fn


def test_installer_logs_are_root_only():
    """The trace log is a full bash xtrace of the install: no secrets
    after the pairing-code fix, but every hostname, address and disk name
    the wizard touched."""
    fn = _fn("_save_install_logs")
    assert "chmod 0700" in fn
    assert "chmod 0600" in fn


def test_saving_logs_cannot_abort_a_successful_install():
    """Runs under ``set -e`` after everything else has succeeded. A
    failure to save a log must not be the thing that fails the install."""
    fn = _fn("_save_install_logs")
    assert fn.rstrip().endswith("return 0\n}"), fn[-200:]


def test_logs_are_saved_before_the_target_is_unmounted():
    """Ordering is the property: the copy writes to $MOUNT/var, so it has
    to happen while /var is still mounted."""
    save = CODE.index("        _save_install_logs")
    unmount = CODE.index('echo "95"; echo "Unmounting')
    assert save < unmount, "the log copy must precede the unmount block"


def test_devnull_fallback_logs_are_not_copied():
    """INSTALL_LOG / TRACE_LOG fall back to /dev/null when /var/log is not
    writable (the #581 host-portable path). Copying that makes an empty
    file that reads as "the installer logged nothing"."""
    assert '[ "$src" = "/dev/null" ] && continue' in _fn("_save_install_logs")


# ── Item 2 — the UEFI grub-install failure is no longer swallowed ─────


def _grub_block() -> str:
    start = CODE.index("grub-install --target=i386-pc")
    end = CODE.index("Cloning root_A", start)
    return CODE[start:end]


def test_neither_grub_install_is_unconditionally_ignored():
    blk = _grub_block()
    assert "|| true" not in blk, (
        "a grub-install whose failure is swallowed shows the Done screen "
        "on a box that will not boot"
    )


def test_both_firmware_modes_have_a_fatal_branch():
    blk = _grub_block()
    assert 'FIRMWARE_MODE" = "bios"' in blk
    assert 'FIRMWARE_MODE" = "uefi"' in blk
    # One abort per branch — the mode that did NOT boot this ISO stays
    # best-effort, because --removable and the ef02 partition mean either
    # install can legitimately fail on the other kind of machine.
    assert blk.count("exit 1") == 2, blk


def test_firmware_mode_comes_from_the_kernel_not_a_guess():
    assert "[ -d /sys/firmware/efi ]" in CODE


def test_firmware_mode_is_shown_before_the_wipe():
    assert "Booted:" in _fn("confirm")


# ── Item 4 — the useradd failure is no longer swallowed ───────────────


def test_useradd_is_fatal():
    i = CODE.index("useradd -m -G sudo")
    window = CODE[i - 200:i + 800]
    assert "|| true" not in window, (
        "PermitRootLogin is off, so a swallowed useradd leaves a box with "
        "no way in at all"
    )
    assert "exit 1" in window


# ── Items 5 / 6 / 7 — the screens say what is true ────────────────────


def _done_block() -> str:
    start = SRC.index("    local done_addr=")
    return SRC[start:SRC.index('Press OK to reboot."', start)]


def test_done_screen_advertises_https():
    blk = _done_block()
    assert "https://" in blk
    assert "http://<appliance IP>" not in blk, "the frontend 301s to https"


def test_done_screen_does_not_claim_images_are_pulled():
    """Baked into the rootfs since #170 Wave A4. An operator told they are
    being pulled goes hunting for a network fault that does not exist."""
    blk = _done_block()
    assert "pulls" not in blk
    assert "Nothing is downloaded." in blk


def test_done_screen_is_role_aware():
    blk = _done_block()
    assert 'if [ "$ROLE" = "appliance" ]' in blk
    assert "no web UI" in blk
    assert "CONTROL_PLANE_URL" in blk


def test_done_screen_offers_the_live_address_in_dhcp_mode():
    assert "ip -4 -br addr" in _done_block()


def test_done_screen_height_is_computed_and_clamped_to_the_terminal():
    """An 80x24 serial console is a first-class install path here
    (spatium-console@ttyS0), and newt does not draw a window taller than
    the screen. The role block makes the body length vary, so a fixed
    height cannot be right for both."""
    blk = SRC[SRC.index("    local done_rows"):SRC.index('--msgbox "$done_body"') + 60]
    assert "stty size" in blk
    assert "term_rows - 1" in blk
    assert '"$done_h" 76' in blk


def test_confirm_no_longer_promises_dns_and_dhcp_at_install():
    """Contradicted ask_role two screens earlier: since #272 both are off
    at install and enabled per node from the Fleet UI."""
    fn = _fn("confirm")
    assert "api + db + DNS + DHCP" not in fn
    assert "DNS + DHCP stay OFF" in fn


def test_the_retired_application_role_name_is_gone():
    """#170 Wave B retired it; ask_role says "Additional node"."""
    assert "Application install" not in SRC
    assert "Application appliance" not in SRC


def test_welcome_lists_every_question_the_wizard_asks():
    fn = _fn("welcome")
    assert "First node / Additional node" in fn
    assert "CIDR" in fn
    assert "pairing code" in fn


# ── Item 8 — the backtitle is the version you booted ──────────────────


def test_backtitle_is_not_hardcoded():
    assert "SpatiumDDI Appliance Installer 0.1.0" not in SRC
    assert 'BACKTITLE="SpatiumDDI Appliance Installer ${_INSTALLER_VERSION' in SRC


def test_the_version_is_parsed_in_exactly_one_place():
    """do_install reads the same value back out of the rsynced copy to
    label the grub menuentries; two awk blocks could disagree about which
    build this is."""
    assert CODE.count("APPLIANCE_VERSION=/{") == 1
    assert CODE.count("_appliance_version_from") == 3  # def + 2 call sites
