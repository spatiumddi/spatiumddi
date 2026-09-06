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

import os
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
    # stdin=DEVNULL and TERM=dumb are load-bearing: `_whiptail_width` calls
    # `stty size`, which reads the CALLER's terminal if stdin is inherited.
    # Under `pytest -s` (or any wide terminal) the clamp assertion then sees
    # the real width and fails — the harness depending on pytest's default
    # fd-capture rather than enforcing the condition it documents.
    return subprocess.run(
        ["bash", "-c", f"{fn_src}\n{script}"],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "TERM": "dumb", "COLUMNS": "", "LINES": ""},
    ).stdout


@pytest.mark.parametrize(("label", "var"), _DYNAMIC_MENUS)
def test_menus_size_from_a_helper(label: str, var: str) -> None:
    """The three dynamic menus pass a computed width, never a literal."""
    assert f'{var}=$(_whiptail_width' in CODE, (
        f"the {label} must compute its width via _whiptail_width; a literal "
        f"cannot know the terminal is 80 columns on a serial console"
    )
    # Just "the variable reaches a --menu dimension slot". Not anchored on a
    # literal `20 "$VAR"`: the disk picker puts its dimensions on a
    # continuation line, and the interface picker's HEIGHT is now computed
    # too — a pattern tied to either shape reports a bug in working code.
    fn = {"disk picker": "pick_disk", "interface picker": "ask_network",
          "confirm field list": "confirm"}[label]
    body = extract_fn(fn)
    assert f'"${var}"' in body, (
        f"the {label} computes {var} but does not pass it to --menu"
    )


def test_every_dynamic_menu_truncates_its_items() -> None:
    """The property that actually caused the bug.

    A computed WIDTH is only half of it — the overflow came from an ITEM
    longer than the box, and newt draws that straight through the frame. So
    each of the three menus whose items interpolate an unbounded value must
    pass those items through ``_ellipsis``.

    This replaces a check that asserted the three menus "carry no hardcoded
    width" while only testing ``f'"${var}"' in CODE`` — satisfied by the very
    line the test above already pins, so it verified nothing. Scoping a
    literal-triple check by function does not work either: ``ask_network``
    holds two menus, and its static mode picker is CORRECTLY a literal.
    """
    for fn, item_source in (
        ("pick_disk", "options+=("),
        ("ask_network", "ifs+=("),
        ("confirm", '"Target disk'),
    ):
        body = extract_fn(fn)
        lines = [ln for ln in body.splitlines() if item_source in ln]
        assert lines, f"could not find where {fn} builds its menu items"
        assert any("_ellipsis" in ln for ln in lines), (
            f"{fn} builds menu items without _ellipsis — an over-wide item is "
            f"drawn through the box frame, not clipped"
        )


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
    """The reported case AND the worst case, asserted on the EMITTED length.

    The first version built only the `[installed: …]` suffix and asserted
    `budget >= 8` — so it was green while the clamp branch was still handing
    back up to 7 already-spent columns. The reachable worst case is a disk
    that is BOTH a hazard and carries an install whose version file will not
    mount (`_existing_install_version` yields the 15-char "unknown version"
    on a dirty ext4 — i.e. the reinstall this feature exists for).
    """
    m = re.search(r"_DISK_ITEM_COLS=\$\(\(_DISK_MENU_W - (\d+) - (\d+)\)\)", CODE)
    assert m, "the disk item budget is no longer computed the documented way"
    tag_reserve, chrome = int(m.group(1)), int(m.group(2))
    item_cols = 70 - tag_reserve - chrome  # worst case: an 80-col console

    cases = [
        ("32G", "QEMU HARDDISK", " [installed: dev-20260906-1]"),
        ("1.8T", "Samsung SSD 860 EVO 250GB",
         " [installed: unknown version] [UNSUPPORTED]"),
        ("931.5G", "WDC WD10EZEX-08WN4A0", " [UNSUPPORTED]"),
    ]
    for size, model, suffix in cases:
        budget = item_cols - len(size) - len(suffix) - 3
        if budget < 8:
            # Mirror the clamp branch: the SUFFIX is clipped, not the budget
            # floor-restored, or the total exceeds the area it was budgeted for.
            suffix = suffix[: item_cols - len(size) - 3 - 8]
            budget = 8
        rendered = f"{size} - {model[:budget]}{suffix}"
        assert len(rendered) <= item_cols, (
            f"{len(rendered)} cols in a {item_cols}-col item area for "
            f"{size}/{suffix!r} — this is the overflow that destroys the box "
            f"border"
        )


def test_the_clamp_clips_the_suffix_rather_than_restoring_the_budget() -> None:
    """Structural companion: `budget=8` alone re-spends columns already used."""
    body = "\n".join(
        ln for ln in extract_fn("pick_disk").splitlines()
        if not ln.lstrip().startswith("#")
    )
    i = body.index('[ "$budget" -lt 8 ]')
    block = body[i : i + 300]
    assert "_ellipsis" in block and "suffix=" in block, (
        "the under-budget branch must clip the suffix; floor-restoring the "
        "budget emits an item wider than _DISK_ITEM_COLS"
    )
