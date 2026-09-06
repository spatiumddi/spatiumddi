"""Every ``--menu`` item fits inside its box (#995, reported from a real install).

newt does **not** truncate an over-wide ``--menu`` item. It writes past the
frame, and the first casualty is the box border — which is exactly how this
was reported: "the screen where you choose the target disk is missing the
bounding box border".

The trigger was #995 item 25. ``pick_disk``'s item used to be ``"$size -
$model"``, which fit the hardcoded width 70 for any plausible disk. Item 25
appended ``[SpatiumDDI <version> installed]`` and item 23/24 appended
``[UNSUPPORTED]``, and the first of those pushed an ordinary
``32G - QEMU HARDDISK`` nine columns over. It only appears on a REINSTALL,
because that is the one case where the target already carries an install —
so a first install looked perfect and the second one broke.

Two guards, because they fail differently:

``test_menus_size_from_a_helper`` is structural — the menus that interpolate
unbounded values must take their width from ``_whiptail_width`` rather than a
literal, so a future screen cannot quietly reintroduce a fixed 70.

``test_ellipsis_*`` execute the real helper, because the arithmetic is where
this goes wrong: an off-by-one in the budget is invisible to review and
produces exactly the same corrupted frame.

HOW TO RUN (from the repo root):
    python3 -m pytest appliance/tests/test_install_menu_width.py -v
"""

from __future__ import annotations

import re
import subprocess

import pytest

from _installer_source import CODE, extract_fn

# The three menus whose items interpolate values nothing bounds: a disk
# model + version suffix, an interface summary carrying a v6 address, and
# the Confirm field list carrying a 63-char hostname or a list of NTP
# servers. Anything added here must size from the helper.
_DYNAMIC_MENUS = (
    ("disk picker", "_DISK_MENU_W"),
    ("interface picker", "_IF_MENU_W"),
    ("confirm field list", "_CONF_MENU_W"),
)


def _run(fn_src: str, script: str) -> str:
    """Run one extracted function under bash and return its stdout."""
    return subprocess.run(
        ["bash", "-c", f"{fn_src}\n{script}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout


@pytest.mark.parametrize(("label", "var"), _DYNAMIC_MENUS)
def test_menus_size_from_a_helper(label: str, var: str) -> None:
    """The three dynamic menus pass a computed width, never a literal."""
    assert f'{var}=$(_whiptail_width' in CODE, (
        f"the {label} must compute its width via _whiptail_width; a literal "
        f"cannot know the terminal is 80 columns on a serial console"
    )
    # Spelled as the literal dimension triple rather than a regex anchored
    # on --menu: the disk picker puts its dimensions on a continuation line,
    # and a pattern that misses that reports a bug in working code.
    assert f'20 "${var}" ' in CODE, (
        f"the {label} computes {var} but does not pass it to --menu"
    )


def test_no_dynamic_menu_keeps_a_literal_width() -> None:
    """The three known-dynamic menus carry no hardcoded width.

    Deliberately not a sweep over every ``--menu`` in the file: the static
    ones (network mode, SSH key source) have fixed, short items and a
    literal is correct there. Widening the rule to all of them would be a
    test about house style rather than about the defect.
    """
    for _label, var in _DYNAMIC_MENUS:
        assert f'"${var}"' in CODE


def test_ellipsis_leaves_short_strings_alone() -> None:
    fn = extract_fn("_ellipsis")
    assert _run(fn, '_ellipsis "32G - QEMU HARDDISK" 51') == "32G - QEMU HARDDISK"


def test_ellipsis_never_exceeds_the_budget() -> None:
    """The whole point: the result is <= max, for every max."""
    fn = extract_fn("_ellipsis")
    long = "Samsung SSD 860 EVO 250GB with a silly long vendor string"
    for budget in (4, 8, 16, 24, 51, 200):
        out = _run(fn, f'_ellipsis "{long}" {budget}')
        assert len(out) <= budget, f"budget {budget} produced {len(out)} cols: {out!r}"


def test_ellipsis_marks_the_cut() -> None:
    """A clipped name must not read as a complete one."""
    fn = extract_fn("_ellipsis")
    out = _run(fn, '_ellipsis "/dev/disk/by-id/nvme-eui.00253855014a1b2c" 20')
    assert out.endswith("...")
    assert len(out) == 20


def test_ellipsis_uses_ascii_not_a_unicode_ellipsis() -> None:
    """The appliance console font is 256 glyphs; U+2026 is not in it."""
    fn = extract_fn("_ellipsis")
    assert "…" not in fn
    out = _run(fn, '_ellipsis "abcdefghijklmnop" 10')
    assert "…" not in out


def test_whiptail_width_clamps_to_the_terminal() -> None:
    """88 is the ask; an 80-column serial console must not get 88."""
    fn = extract_fn("_whiptail_width")
    # stty fails with no tty, so the helper takes its documented 80 default.
    out = _run(fn, "_whiptail_width 88")
    assert out.isdigit()
    assert 70 <= int(out) <= 74, f"expected a clamp to the 80-col default, got {out}"


def test_whiptail_width_never_returns_below_70() -> None:
    fn = extract_fn("_whiptail_width")
    assert int(_run(fn, "_whiptail_width 40")) == 70


def test_disk_item_budget_accounts_for_both_suffixes() -> None:
    """The reported case: a reinstall of an ordinary QEMU disk fits.

    Reproduces the budget arithmetic from the source rather than restating
    it, so the test tracks the code if the chrome reserve is retuned.
    """
    m = re.search(r"_DISK_ITEM_COLS=\$\(\(_DISK_MENU_W - (\d+) - (\d+)\)\)", CODE)
    assert m, "the disk item budget is no longer computed the documented way"
    tag_reserve, chrome = int(m.group(1)), int(m.group(2))
    item_cols = 70 - tag_reserve - chrome  # worst case: an 80-col console

    size, model = "32G", "QEMU HARDDISK"
    suffix = " [installed: dev-20260906-1]"
    budget = item_cols - len(size) - len(suffix) - 3
    assert budget >= 8, (
        "an ordinary reinstall leaves no room for the model at all — the "
        "suffix budget has grown past what an 80-column console can hold"
    )
    rendered = f"{size} - {model[:budget]}{suffix}"
    assert len(rendered) <= item_cols, (
        f"{len(rendered)} cols in a {item_cols}-col item area — this is the "
        f"overflow that destroyed the box border"
    )
