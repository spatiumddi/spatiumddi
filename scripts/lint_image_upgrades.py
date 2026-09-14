#!/usr/bin/env python3
"""Refuse a shipped image whose package layer can never be patched (#1088).

Every image the nightly and release workflows publish is built from a base
image somebody else maintains. Two things have to be true for a distro
security fix to actually reach it, and BOTH failed silently on the api image
for as long as it has existed:

  1. The Dockerfile must UPGRADE the base image's own packages. Installing a
     package list patches only what is on that list — ``perl-base``, ``gzip``,
     ``libpcre2``, ``libsqlite3`` came with ``python:3.12-slim`` and could
     only ever be fixed by upstream rebuilding it, which is not a schedule we
     control. The nightly of 2026-09-14 refused to publish the api image on
     34 HIGH/CRITICAL findings whose fixes had been on deb.debian.org for
     days.

  2. The upgrade must be able to RUN. BuildKit keys a layer on its RUN text,
     so a ``type=gha`` cache serves the whole layer — package set included —
     from whenever it was first built, until that text changes. An upgrade
     line that never executes is indistinguishable, in the built image, from
     no upgrade line at all. The Alpine images solve this with an
     ``ARG APK_SNAPSHOT`` the nightly passes its date tag to; the Debian ones
     use ``ARG APT_SNAPSHOT``. An image declaring neither ignores both build
     args without a word — which is how the same nightly reported ``perl`` at
     5.40.1-6 while the index had offered 5.40.1-6+deb13u1 for days.

Neither failure announces itself: the build is green, the image is published,
and the only symptom is a Trivy report weeks later blaming packages nobody
touched. So this asserts both, over the SAME image list the nightly matrix
builds from — a new image is covered the moment it is added there, rather
than when somebody remembers to add it here.

stdlib-only, no network, no docker. Exit 1 on any finding.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
NIGHTLY = REPO_ROOT / ".github" / "workflows" / "nightly.yml"

# The nightly's images list is a heredoc of JSON inside the gate step — one
# source of truth for which images exist (see the comment above it there).
# Parsed rather than duplicated, so this linter cannot fall behind the matrix.
_IMAGES_BLOCK = re.compile(
    r"cat > /tmp/images\.json <<'IMAGES_EOF'\n(.*?)\n\s*IMAGES_EOF", re.DOTALL
)

# A package-manager upgrade of everything already installed. `apt-get upgrade`
# and `apt-get dist-upgrade` both qualify; `apt-get install` deliberately does
# not — that is the half that was already there and was not enough.
_UPGRADES = (
    re.compile(r"\bapk\s+upgrade\b"),
    re.compile(r"\bapt-get\s+(?:-\S+\s+)*(?:dist-upgrade|upgrade)\b"),
)

# The cache-busting ARG, declared in the Dockerfile so the build arg the
# workflow passes is not silently discarded. An ARG declared but never
# referenced is equally inert, so the reference is checked too.
_SNAPSHOT_ARG = re.compile(r"^\s*ARG\s+(AP[KT]_SNAPSHOT)\b", re.MULTILINE)


def _images() -> list[dict[str, str]]:
    match = _IMAGES_BLOCK.search(NIGHTLY.read_text())
    if not match:
        raise SystemExit(
            f"{NIGHTLY}: could not find the images.json heredoc — if the gate step was "
            "restructured, update this linter rather than letting it silently check nothing"
        )
    # The heredoc body is indented inside the YAML block scalar.
    body = "\n".join(line.strip() for line in match.group(1).splitlines())
    return json.loads(body)


def _strip_comments(text: str) -> str:
    """Drop full-line ``#`` comments.

    These Dockerfiles carry long rationale comments that name the very
    commands being looked for (``apk upgrade``, ``APK_SNAPSHOT``), so matching
    against the raw text would pass an image whose comments merely DESCRIBE an
    upgrade it does not perform. Trailing comments inside a RUN continuation
    are left alone — they cannot introduce a false positive, since a match
    there still sits in a real RUN block.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def main() -> int:
    findings: list[str] = []

    for image in _images():
        rel = image["file"].lstrip("./")
        path = REPO_ROOT / rel
        if not path.is_file():
            findings.append(f"{rel}: listed in the nightly matrix but not on disk")
            continue

        body = _strip_comments(path.read_text())

        if not any(pattern.search(body) for pattern in _UPGRADES):
            findings.append(
                f"{rel} ({image['image']}): no `apk upgrade` / `apt-get upgrade` — the base "
                "image's own packages can never be patched by this build"
            )

        declared = {m.group(1) for m in _SNAPSHOT_ARG.finditer(body)}
        if not declared:
            findings.append(
                f"{rel} ({image['image']}): declares no ARG APK_SNAPSHOT / APT_SNAPSHOT — "
                "the nightly's cache will serve a frozen package layer and the upgrade "
                "above will not run"
            )
        else:
            for name in sorted(declared):
                # `ARG X` with no `${X}` in the RUN text does not change the
                # layer's cache key, so it busts nothing.
                if f"${{{name}}}" not in body and f"${name}" not in body:
                    findings.append(
                        f"{rel} ({image['image']}): ARG {name} is declared but never "
                        "referenced, so it does not change the RUN text and busts no cache"
                    )

    if findings:
        print("Shipped-image package-upgrade linter — findings:\n", file=sys.stderr)
        for finding in findings:
            print(f"  ✗ {finding}", file=sys.stderr)
        print(
            "\nSee scripts/lint_image_upgrades.py for why each of these is silent.",
            file=sys.stderr,
        )
        return 1

    print("✓ every shipped image upgrades its packages behind a cache-busting snapshot ARG")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
