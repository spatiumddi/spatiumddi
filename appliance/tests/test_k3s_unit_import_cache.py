"""k3s.service must persist k3s's image-import cache.

k3s re-imports every tarball under ``agent/images/`` on EVERY start unless
``.cache.json`` exists in that directory: the importer opens the cache
``O_WRONLY|O_TRUNC`` and never creates it (k3s ``pkg/agent/containerd/
watcher.go``, ``syncCache``), and the docs say so outright — "Image archives
are imported every time k3s starts … To enable [the cache], create a
.cache.json file in the images directory". The appliance bakes ~20 tarballs
there and the import runs before the kubelet starts, so on a nested 3-node QA
cluster a member returning from a network partition took 168-284 s to report
Ready, 126 s of it "Importing images from …" (the HA drill's budget is
240 s). The unit creates the file before k3s starts; this test keeps it
there, in the main unit (a slot upgrade carries the unit file, a drop-in
would be left behind), and ahead of ``ExecStart``.

    python3 -m pytest appliance/tests/test_k3s_unit_import_cache.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UNIT = REPO / "appliance" / "mkosi.extra" / "etc" / "systemd" / "system" / "k3s.service"
IMAGES_DIR = "/var/lib/rancher/k3s/agent/images"

pytestmark = pytest.mark.skipif(not UNIT.is_file(), reason="appliance tree not present")


def _lines() -> list[str]:
    return [ln.strip() for ln in UNIT.read_text().splitlines()]


def test_unit_creates_the_import_cache_before_k3s_starts() -> None:
    lines = _lines()
    touch = f"ExecStartPre=/bin/touch {IMAGES_DIR}/.cache.json"
    assert touch in lines, (
        "k3s.service no longer creates agent/images/.cache.json — without it k3s "
        "re-imports every baked tarball on every start, before the kubelet"
    )
    mkdir = next(
        (
            i
            for i, ln in enumerate(lines)
            if ln.startswith("ExecStartPre=/bin/mkdir -p") and IMAGES_DIR in ln.split()
        ),
        None,
    )
    assert mkdir is not None, "the images dir must be created before the cache file is touched"
    assert mkdir < lines.index(touch) < lines.index("ExecStart=/usr/local/bin/k3s server")


def test_touch_is_not_ignored_on_failure() -> None:
    """``ExecStartPre=-…`` would let a failed touch pass silently and the
    slow path come back unnoticed; the mkdir line it follows is not ``-``
    either."""
    assert not any(
        ln.startswith("ExecStartPre=-") and ".cache.json" in ln for ln in _lines()
    )
