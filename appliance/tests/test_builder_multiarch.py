"""The builder image must build natively on both architectures (#991 §1).

The ISO is x86-64 and mkosi cross-builds it happily, but the builder
container itself **cannot be emulated**: under qemu-user or Rosetta mkosi
dies immediately on ``mount_setattr(2)`` ("Function not implemented"),
which the syscall translation layers do not implement and ``--privileged``
does not fix.  So an arm64 developer needs a *native* arm64 builder, and
the only blocker was that ``grub-pc-bin`` and ``grub-efi-amd64-bin`` are
amd64-only packages.

Debian multiarch solves it — they carry no host-executable code, only the
x86 GRUB modules under ``/usr/lib/grub/{i386-pc,x86_64-efi}`` that
grub-mkrescue embeds in the ISO — so one Dockerfile serves both hosts.

These are structural checks on the shipped Dockerfile and workflow rather
than a build (a real ``docker build`` of both platforms is minutes, not
milliseconds).  The end-to-end verification was done during development:
both platforms build, and both produce an ISO whose El Torito catalogue
carries a BIOS record at ``/boot/grub/i386-pc/eltorito.img`` and a UEFI
record at ``/efi.img``.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_builder_multiarch.py -v

No Docker, no root required.
"""

from __future__ import annotations

import re

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "appliance" / "builder" / "Dockerfile"
WORKFLOW = ROOT / ".github" / "workflows" / "build-appliance-builder.yml"


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text()


def test_amd64_is_added_as_a_foreign_architecture(dockerfile):
    """Without this, ``apt-get install grub-pc-bin`` simply fails on an
    arm64 host and there is no native builder at all.
    """
    assert "dpkg --add-architecture amd64" in dockerfile


def test_the_x86_grub_packages_are_architecture_qualified(dockerfile):
    """Unqualified, apt resolves them for the *host* arch — which does
    not exist for these packages on arm64. Qualified, they install as a
    foreign arch on arm64 and name the native arch on amd64, so one file
    serves both. (``dpkg --add-architecture amd64`` on an amd64 host is a
    no-op, verified: ``--print-foreign-architectures`` stays empty.)
    """
    assert "grub-pc-bin:amd64" in dockerfile
    assert "grub-efi-amd64-bin:amd64" in dockerfile
    # The unqualified spellings must be gone, or apt picks the host arch.
    for line in dockerfile.splitlines():
        stripped = line.strip().rstrip("\\").strip()
        assert stripped not in ("grub-pc-bin", "grub-efi-amd64-bin"), line


def test_the_add_architecture_runs_in_the_same_layer_as_the_install(dockerfile):
    """``dpkg --add-architecture`` before ``apt-get update`` in the SAME
    ``RUN``: a separate layer would work, but an ``apt-get update`` that
    ran before the architecture was added has no amd64 package lists, and
    the install then fails with a confusing "unable to locate package".
    """
    # Match the RUN line, not the comment above it that explains why it
    # is there — the first cut of this test picked the comment.
    #
    # Backslash continuations are JOINED first (#1026). The property is
    # about the logical command, not the physical line, and a second
    # ``--add-architecture`` on its own continued line is still the same
    # RUN — reading line-by-line reported a correct Dockerfile as a
    # regression.
    joined = re.sub(r"\\\n\s*", " ", dockerfile)
    run_line = next(
        line
        for line in joined.splitlines()
        if "dpkg --add-architecture" in line and line.lstrip().startswith("RUN ")
    )
    assert "apt-get update" in run_line
    assert run_line.index("add-architecture") < run_line.index("apt-get update")


def test_the_workflow_publishes_both_platforms():
    """An arm64 developer's ``make appliance`` pulls this image. If it is
    published amd64-only, docker either refuses ("no matching manifest")
    or runs it emulated, which is the mount_setattr dead end.
    """
    workflow = WORKFLOW.read_text()
    assert "platforms: linux/amd64,linux/arm64" in workflow


def test_the_dead_end_is_written_down_where_someone_would_look(dockerfile):
    """The emulated path fails with an error that reads like a missing
    privilege, so the natural next move is to add ``--privileged`` and
    lose an afternoon. Both the Dockerfile and the deployment doc say
    plainly that it cannot work.
    """
    assert "mount_setattr" in dockerfile
    appliance_doc = (ROOT / "docs" / "deployment" / "APPLIANCE.md").read_text()
    assert "mount_setattr" in appliance_doc


def test_the_cross_build_make_target_exists_and_verifies_arch():
    """``make appliance-baked-iso-cross`` is the supported entry point,
    and it must run the architecture assertion *between* building the
    images and baking them — after the bake, a wrong-arch image is
    already inside the ISO.
    """
    makefile = (ROOT / "Makefile").read_text()
    assert "appliance-baked-iso-cross:" in makefile
    assert "appliance-verify-arch:" in makefile
    body = makefile[makefile.index("appliance-baked-iso-cross:\n") :]
    body = body[: body.index("\n\n")]
    build = body.index("$(MAKE) build build-supervisor")
    verify = body.index("appliance-verify-arch")
    bake = body.index("appliance-bake-images")
    assert build < verify < bake, "the arch check must sit between build and bake"


def test_the_native_half_of_the_cross_build_has_no_platform_pin():
    """mkosi, the ISO wrap and the slot image must run in a NATIVE
    builder. If ``DOCKER_DEFAULT_PLATFORM`` leaked onto that line the
    builder would be emulated and mkosi would die on mount_setattr —
    which is the whole reason this target exists rather than one
    environment for the lot.
    """
    makefile = (ROOT / "Makefile").read_text()
    body = makefile[makefile.index("appliance-baked-iso-cross:\n") :]
    body = body[: body.index("\n\n")]
    native = next(
        line
        for line in body.splitlines()
        if "appliance appliance-iso appliance-slot-image" in line
    )
    assert "DOCKER_DEFAULT_PLATFORM" not in native
