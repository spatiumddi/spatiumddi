"""The bake's architecture guard must fire on wrong-arch images and NOT
on correct ones (#991 §2, fixed in #1028).

``appliance-verify-arch`` refuses to bake a source image that is not the
appliance's architecture, because the failure it catches is silent: a
wrong-arch image ``exec format error``s on first boot with nothing in the
build log. It probed with a bare ``docker image inspect -f
'{{.Architecture}}'``, which on Docker Desktop's **containerd image
store** answers for the HOST platform — so on the arm64 cross-build host
it reported ``arm64`` for one image and the EMPTY STRING for ten others,
all of which were correct. It blocked the cross-build path on precisely
the machine that path exists for.

Two properties are under test, and they pull in opposite directions:

* the false positive is gone — a multi-platform index whose host-platform
  content is absent, or present alongside the wanted one, verifies clean;
* **the guard still fails closed** — a single-platform image of the wrong
  arch is still caught, an unreadable image list is still an error, and a
  run that verified nothing is still an error.

The second is the one that matters, because the obvious fix breaks it:
with ``--platform`` bolted onto the old loop, a wrong-arch image makes
``docker image inspect`` exit non-zero, the loop's ``|| continue`` skips
to the next candidate tag, and the image is reported as "not present
locally (the bake will pull it)" — a silent pass on the exact thing the
guard exists to catch. ``test_wrong_arch_is_not_reported_as_absent``
pins that.

The shipped script is executed against a stubbed ``docker`` — the bytes
that ship, not a transcription — with the stub replaying the three
answers observed by hand against Docker 29.7.2.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_verify_image_arch.py -v

No Docker, no root, no appliance ISO required.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from _installer_source import SCRIPTS  # noqa: E402 - sibling module

SCRIPT = SCRIPTS / "verify-image-arch.sh"

#: The stub's answer table. ``plain`` is what a bare
#: ``docker image inspect -f '{{.Architecture}}'`` returns; ``platform``
#: is what ``--platform linux/amd64`` returns, with ``!`` meaning "exits
#: non-zero: this tag cannot provide that platform".
#:
#: Every row is a shape observed for real on the M4 Mac mini:
#:   correct-index        amd64 content present, host arch also present
#:   host-arch-index      amd64 present, inspect answers arm64  (redis)
#:   empty-index          amd64 present, inspect answers ""     (speaker)
#:   unpulled-index       amd64 listed but not pulled
#:   wrong-single         a local single-platform arm64 build
DEFAULT_TAGS = {
    "correct-index": ("amd64", "amd64"),
    "host-arch-index": ("arm64", "amd64"),
    "empty-index": ("", "amd64"),
    "unpulled-index": ("arm64", ""),
    "wrong-single": ("arm64", "!"),
}

_DOCKER_STUB = r"""#!/usr/bin/env python3
import os, sys
table = {}
for line in open(os.environ["ARCH_STUB_TABLE"]):
    line = line.rstrip("\n")
    if not line:
        continue
    tag, plain, plat = line.split("\t")
    table[tag] = (plain, plat)

argv = sys.argv[1:]
if argv[:2] == ["image", "inspect"]:
    argv = argv[2:]
else:
    sys.exit(99)
if "--help" in argv:
    if os.environ.get("ARCH_STUB_NO_PLATFORM_FLAG") == "1":
        print("      -f, --format string   Format output")
    else:
        print("      --platform string   Inspect a specific platform")
    sys.exit(0)

platform = None
fmt = None
tag = None
i = 0
while i < len(argv):
    a = argv[i]
    if a == "--platform":
        platform = argv[i + 1]; i += 2
    elif a in ("-f", "--format"):
        fmt = argv[i + 1]; i += 2
    else:
        tag = a; i += 1

if tag not in table:
    print(f"Error: No such image: {tag}", file=sys.stderr)
    sys.exit(1)
plain, plat = table[tag]
if platform is None:
    # Existence probe, or the legacy host-platform read.
    if fmt:
        print(plain)
    sys.exit(0)
if plat == "!":
    print(
        f"Error response from daemon: image with reference {tag} was found "
        f"but does not provide the specified platform ({platform})",
        file=sys.stderr,
    )
    sys.exit(1)
print(plat)
sys.exit(0)
"""


@pytest.fixture
def rig(tmp_path: Path):
    """A copy of the shipped script with a stubbed docker + image list.

    Returns a callable: ``run(images, tags=..., **env) -> CompletedProcess``.
    ``images`` is the list ``bake-images.sh --list-images`` will print;
    ``tags`` overrides the stub's answer table.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(_DOCKER_STUB)
    docker.chmod(0o755)

    scriptdir = tmp_path / "scripts"
    scriptdir.mkdir()
    shutil.copy(SCRIPT, scriptdir / SCRIPT.name)
    (scriptdir / SCRIPT.name).chmod(0o755)

    def run(images, tags=None, list_exit=0, **env):
        (scriptdir / "bake-images.sh").write_text(
            "#!/usr/bin/env bash\n"
            + "".join(f'echo "{i}"\n' for i in images)
            + f"exit {list_exit}\n"
        )
        (scriptdir / "bake-images.sh").chmod(0o755)

        table = dict(DEFAULT_TAGS)
        table.update(tags or {})
        table_file = tmp_path / "tags.tsv"
        table_file.write_text(
            "".join(f"{t}\t{p}\t{q}\n" for t, (p, q) in table.items())
        )

        environ = dict(os.environ)
        environ["PATH"] = f"{bindir}:{environ['PATH']}"
        environ["ARCH_STUB_TABLE"] = str(table_file)
        environ.update({k: str(v) for k, v in env.items()})
        return subprocess.run(
            ["bash", str(scriptdir / SCRIPT.name), "linux/amd64"],
            capture_output=True,
            text=True,
            check=False,
            env=environ,
        )

    return run


# ── The false positive #1028 was filed for ─────────────────────────


def test_multi_platform_index_reporting_the_host_arch_passes(rig):
    """``redis:8.8-alpine``: amd64 content present, plain inspect says
    ``arm64``. The old guard reported ``✗ ... is arm64, expected amd64``."""
    r = rig(["correct-index", "host-arch-index"])
    assert r.returncode == 0, r.stderr
    assert "host-arch-index" in r.stdout
    assert "✗" not in r.stderr


def test_multi_platform_index_reporting_an_empty_arch_passes(rig):
    """The ten third-party images: only amd64 pulled, so the host platform
    has no content and plain inspect answers "". The old guard reported
    ``✗ quay.io/metallb/speaker:v0.15.3 is , expected amd64``."""
    r = rig(["correct-index", "empty-index"])
    assert r.returncode == 0, r.stderr
    assert "✗" not in r.stderr


# ── Fail-closed: the half that must survive the fix ────────────────


def test_wrong_arch_single_platform_image_is_caught(rig):
    r = rig(["correct-index", "wrong-single"])
    assert r.returncode == 1
    assert "wrong-single" in r.stderr
    assert "wrong architecture" in r.stderr


def test_wrong_arch_is_not_reported_as_absent(rig):
    """The trap in the obvious fix.

    ``--platform`` exits non-zero for a wrong-arch image. If existence is
    not established FIRST, the candidate-tag loop treats that as "no such
    tag", falls off the end, and prints the benign "not present locally
    (the bake will pull it)" — turning the guard into a silent pass on
    the one case it exists for.
    """
    r = rig(["correct-index", "wrong-single"])
    assert "not present locally" not in r.stdout
    assert r.returncode == 1


def test_unreadable_image_list_is_an_error(rig):
    r = rig([], list_exit=1)
    assert r.returncode == 1
    assert "could not read the image list" in r.stderr
    assert "evaluates nothing" in r.stderr


def test_verifying_nothing_is_an_error(rig):
    """``checked == 0``. An index that merely LISTS the platform without
    having pulled it is not a verification, so a run of nothing but those
    must not report a clean pass."""
    r = rig(["unpulled-index"])
    assert r.returncode == 1
    assert "no source image was verified" in r.stderr


def test_absent_images_alone_are_an_error(rig):
    r = rig(["never-pulled-anywhere"])
    assert r.returncode == 1
    assert "not present locally" in r.stdout
    assert "no source image was verified" in r.stderr


def test_unpulled_platform_content_is_reported_not_silently_passed(rig):
    r = rig(["correct-index", "unpulled-index"])
    assert r.returncode == 0, r.stderr
    assert "has not pulled it" in r.stdout


# ── Degradation on an older docker CLI ─────────────────────────────


def test_missing_platform_flag_degrades_loudly(rig):
    """A CLI with no ``image inspect --platform`` falls back to the
    host-platform read — which is the buggy behaviour — so it has to say
    so rather than report a pass that means nothing."""
    r = rig(["correct-index"], ARCH_STUB_NO_PLATFORM_FLAG=1)
    assert r.returncode == 0, r.stderr
    assert "no 'image inspect --platform'" in r.stderr
    assert "unverified" in r.stderr


def test_missing_platform_flag_still_catches_a_wrong_arch_image(rig):
    """Degraded is not disabled: the fallback is exactly the pre-#1028
    check, which did catch a genuinely wrong single-platform image."""
    r = rig(["correct-index", "wrong-single"], ARCH_STUB_NO_PLATFORM_FLAG=1)
    assert r.returncode == 1
    assert "wrong architecture" in r.stderr


# ── Candidate-tag fallbacks ────────────────────────────────────────


def test_candidate_tag_forms_are_tried_in_order(rig):
    """The bake resolves ``ghcr.io/spatiumddi/x`` to one of four local tag
    spellings; the guard must check whichever one the bake would use."""
    r = rig(
        ["ghcr.io/spatiumddi/looking-glass"],
        tags={"ghcr.io/spatiumddi/looking-glass:dev": ("amd64", "amd64")},
    )
    assert r.returncode == 0, r.stderr
    assert "ghcr.io/spatiumddi/looking-glass:dev" in r.stdout
