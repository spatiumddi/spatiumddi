"""Shared accessors for the installer script under test.

Every file here that reasons about ``spatium-install`` needs the same
three things: the path, the text, and a way to slice one shell function
out of it (the script ends in ``main "$@"``, so it cannot be sourced —
doing so would run a real install).

Before this there were five spellings of the extractor and seven copies
of the path constant across the suite, so renaming an installer function
meant editing five files. New files should import from here.

Deliberately NOT retrofitted into the four pre-existing files
(``test_preseed_security.py``, ``test_k3s_cidr_canonical.py``,
``test_firstboot_pod_posture.py``, ``test_grub_render.py``): they work,
and rewriting them is a cleanup of its own rather than part of #995.

Imported as a sibling module — pytest's default import mode prepends the
test file's own directory to ``sys.path``. Leading underscore so pytest
does not try to collect it.
"""

from __future__ import annotations

import re
from pathlib import Path

BIN = Path(__file__).parent.parent / "mkosi.extra" / "usr" / "local" / "bin"
INSTALLER = BIN / "spatium-install"
PARSER = BIN / "spatium-preseed-parse"

SRC = INSTALLER.read_text(encoding="utf-8")

#: ``SRC`` with comments stripped. Guards that assert on the ABSENCE of a
#: token need this, because each fix's comment explains the bug by
#: quoting the code it replaced — matching raw text reports the
#: explanation as the regression.
CODE = "\n".join(ln.split("#", 1)[0] for ln in SRC.splitlines())


def extract_fn(name: str, src: str | None = None) -> str:
    """Return one shell function definition, verbatim.

    Every function in the installer opens with ``name() {`` at column 0
    and closes with ``}`` at column 0, so a line-anchored slice is exact.
    """
    m = re.search(
        rf"^{re.escape(name)}\(\) \{{$.*?^\}}$",
        SRC if src is None else src,
        re.MULTILINE | re.DOTALL,
    )
    assert m, f"function {name}() not found in {INSTALLER}"
    return m.group(0)
