"""Keyboard layout is applied by a mechanism that exists (#1003 item 1).

#995 item 17 added the screen and got the mechanism wrong twice, in ways
that a `us` install cannot show:

**(a) `loadkeys <name>` could never succeed.** The image has no console
keymaps at all — Debian ships them in ``console-data``, which
``mkosi.conf`` does not install, so ``/usr/share/keymaps`` and
``/usr/lib/kbd/keymaps`` are both absent. Every name failed, including the
``us`` default, and because the "Something else" loop refuses any name
``_apply_keymap`` cannot load, an operator with a non-listed layout was
stuck at that prompt permanently. ``ckbcomp`` (console-setup + xkb-data,
both installed) compiles an XKB layout into a keymap instead.

**(b) The two persisted files are the same file.** ``/etc/vconsole.conf``
is a symlink to ``default/keyboard`` on this image, so writing the XKB
block and then ``KEYMAP=`` clobbered the first write — leaving
console-setup with no XKBMODEL, four "keyboard model is unknown" warnings
per boot, and no layout applied.

The layout-name mapping itself is verified against a real ``ckbcomp`` in
a Debian container during development, not here — these are the
structural guards that stop the mechanism regressing.

HOW TO RUN (from the repo root):
    python3 -m pytest appliance/tests/test_install_keyboard.py -v
"""

from __future__ import annotations

import re

from _installer_source import CODE, extract_fn


def _code_only(src: str) -> str:
    """Strip comment lines.

    These functions carry long comments that quote the very bug being
    guarded against, so a naive substring test matches the explanation
    and reports the defect as still present. That is not hypothetical —
    it is what the first run of this file did.
    """
    return "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )


def test_apply_keymap_does_not_call_loadkeys_with_a_name() -> None:
    """`loadkeys <name>` needs /usr/share/keymaps, which does not exist."""
    fn = _code_only(extract_fn("_apply_keymap"))
    assert "ckbcomp" in fn, "the layout must be compiled, not looked up by name"
    # `loadkeys -` (stdin) is right; `loadkeys -- "$1"` is the bug.
    assert 'loadkeys -- "$1"' not in fn
    assert re.search(r"loadkeys\s+-\s", fn), "the compiled map is fed on stdin"


def test_apply_keymap_maps_through_the_xkb_table() -> None:
    """`uk`, `br-abnt2` and `jp106` are console names ckbcomp rejects."""
    fn = _code_only(extract_fn("_apply_keymap"))
    assert "_keymap_to_xkb" in fn, (
        "passing the menu's console name straight to ckbcomp fails for "
        "exactly the layouts item 17 was filed for"
    )


def test_apply_keymap_omits_an_empty_variant() -> None:
    """`-variant ""` is not the same as no -variant flag."""
    fn = _code_only(extract_fn("_apply_keymap"))
    assert 'args+=(-variant "$xkb_variant")' in fn
    assert '[ -n "$xkb_variant" ]' in fn, "an empty variant must add no argument"


def test_apply_keymap_builds_args_as_an_array() -> None:
    """Unquoted `${v:+-variant $v}` word-splits a multi-word value."""
    fn = _code_only(extract_fn("_apply_keymap"))
    assert "local -a args=(" in fn
    assert '${xkb_variant:+' not in fn


def test_vconsole_is_not_written_when_it_is_the_same_file() -> None:
    """The clobber: vconsole.conf is a symlink to default/keyboard."""
    assert "readlink -f" in CODE
    # The bare KEYMAP= write that destroyed the XKB block must be gone.
    assert "printf 'KEYMAP=%s\\n' \"$KEYMAP\" > \"$MOUNT/etc/vconsole.conf\"" not in CODE


def test_persisted_block_carries_xkbmodel() -> None:
    """No XKBMODEL is what produced the four setupcon warnings per boot."""
    assert 'XKBMODEL="$xkb_model"' in CODE
    assert 'XKBLAYOUT="$xkb_layout"' in CODE
    assert 'XKBVARIANT="$xkb_variant"' in CODE


def test_other_prompt_names_xkb_layouts_not_console_keymaps() -> None:
    """The old prompt gave `be2` as an example and pointed at a command
    that returns nothing on this image. `be2` is a console keymap name;
    the XKB layout is `be`, and ckbcomp rejects `be2`."""
    assert "localectl list-keymaps" not in CODE, (
        "that command lists console keymaps, of which this image has none"
    )
    assert "localectl list-x11-keymap-layouts" in CODE
    assert "'be2'" not in CODE
