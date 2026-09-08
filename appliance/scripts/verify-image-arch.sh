#!/usr/bin/env bash
# verify-image-arch.sh — assert every source image really is the
# appliance's architecture before it is baked (#991 §2, fixed in #1028).
#
# WHY THIS IS A REAL CHECK rather than a documented manual step: the
# failure it catches is silent and only shows up on the appliance. A
# third-party image already present as the HOST's arch (a ``redis`` or
# ``nginx`` pulled by the dev compose stack on an arm64 laptop) satisfies
# a naive ``docker image inspect``, gets baked, and then ``exec format
# error``s on first boot with nothing in the build log to explain it.
#
# It reads the image set from ``bake-images.sh --list-images`` — ALL four
# arrays, not just SpatiumDDI's own. One source of truth means a reformat
# of that file cannot silently empty the list.
#
# ── The #1028 fix ──────────────────────────────────────────────────────
#
# This lived in the Makefile and probed with a bare
# ``docker image inspect -f '{{.Architecture}}'``. On Docker Desktop's
# **containerd image store** a tag that resolves to a multi-platform index
# has no top-level architecture, so that answers for the HOST platform:
#
#   * ``arm64``      when the amd64 content is present but so is arm64;
#   * the empty string when ONLY amd64 is pulled and the host platform
#     has no content locally.
#
# Both are false positives, and between them they blocked
# ``make appliance-baked-iso-cross`` — the arm64 cross-build path — on
# precisely the machine that path exists for. The fix is to ask for the
# platform explicitly.
#
# ── The three outcomes, and why each is treated as it is ───────────────
#
# ``docker image inspect --platform <want>`` on a tag that IS present
# locally answers in one of three ways, verified by hand against Docker
# 29.7.2 with the containerd store:
#
#   rc=0, arch == want  the requested platform's content is present and
#                       correct.                                     ✓
#   rc=0, arch == ""    the tag is a multi-platform index that LISTS the
#                       platform, but its content has not been pulled.
#                       Not a wrong-arch risk — ``bake-images.sh`` pulls
#                       with ``--platform`` before saving — but it is
#                       also not something this script verified, so it is
#                       reported as such and does NOT count towards the
#                       "did we check anything at all" tally.
#   rc != 0             the tag exists and genuinely cannot provide that
#                       platform — a single-platform local build of the
#                       WRONG arch. **This is the case the guard exists
#                       for**, and the trap that makes the naive fix
#                       wrong: the old loop did ``|| continue``, so with
#                       ``--platform`` bolted on, a wrong-arch image
#                       would have fallen through to "not present
#                       locally (the bake will pull it)" — inverting the
#                       guard into a silent pass. Existence is therefore
#                       established FIRST, with a plain inspect, and only
#                       then is the platform asked for.
#
# ── It fails closed ────────────────────────────────────────────────────
#
# An unreadable list, or no verified image at all, is an error rather
# than a friendly note and exit 0 — the same "a guard that evaluates
# nothing looks exactly like one that passed" rule ``bake-images.sh``'s
# staleness check follows, and the one #1030 had to add to
# ``lint_untyped_routes.py``.
#
# Usage: verify-image-arch.sh [linux/amd64]

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APPLIANCE_ARCH="${1:-${APPLIANCE_ARCH:-linux/amd64}}"
WANT="${APPLIANCE_ARCH##*/}"

# Older Docker CLIs have no ``--platform`` on ``image inspect``. Probing
# once and degrading loudly beats erroring on every tag with a message
# that reads like a hundred wrong-arch images.
platform_inspect_supported() {
    docker image inspect --help 2>/dev/null | grep -q -- '--platform'
}

# Architecture of ``$1`` at platform ``$WANT``.
#
# Echoes the architecture and returns:
#   0  answered (the value may be "" — see the header)
#   1  the tag is not present locally at all
#   2  the tag is present but cannot provide $WANT  ← the guard's quarry
image_arch_at_platform() {
    local tag="$1" got
    docker image inspect "$tag" >/dev/null 2>&1 || return 1
    if [ "${PLATFORM_FLAG_OK:-1}" != 1 ]; then
        docker image inspect -f '{{.Architecture}}' "$tag" 2>/dev/null
        return 0
    fi
    if got="$(docker image inspect --platform "$APPLIANCE_ARCH" \
                -f '{{.Architecture}}' "$tag" 2>/dev/null)"; then
        echo "$got"
        return 0
    fi
    return 2
}

main() {
    local imgs bad=0 checked=0 unpulled=0

    if platform_inspect_supported; then
        PLATFORM_FLAG_OK=1
    else
        PLATFORM_FLAG_OK=0
        echo "WARN: this docker CLI has no 'image inspect --platform'; falling back" >&2
        echo "      to the host-platform answer, which is wrong for a multi-platform" >&2
        echo "      index (#1028). Treat a pass here as unverified." >&2
    fi
    export PLATFORM_FLAG_OK

    imgs="$("$SCRIPT_DIR/bake-images.sh" --list-images 2>/dev/null)"
    if [ -z "$imgs" ]; then
        echo "ERROR: could not read the image list from bake-images.sh --list-images." >&2
        echo "       Refusing to bake rather than reporting a clean check over" >&2
        echo "       nothing — a guard that evaluates nothing looks exactly like" >&2
        echo "       one that passed." >&2
        return 1
    fi

    local image short tag got rc found host_arch
    for image in $imgs; do
        short="$(basename "${image%%:*}")"
        found=0
        for tag in "$image" "${image%%:*}:dev" "spatiumddi-$short:dev" "$short:dev"; do
            got="$(image_arch_at_platform "$tag")"; rc=$?
            [ "$rc" = 1 ] && continue
            found=1
            if [ "$rc" = 2 ]; then
                host_arch="$(docker image inspect -f '{{.Architecture}}' "$tag" \
                             2>/dev/null || true)"
                echo "  ✗ $tag cannot provide $WANT (it is ${host_arch:-another arch})" >&2
                bad=1
            elif [ -z "$got" ]; then
                # The index lists $WANT but its content is not pulled.
                echo "  ? $tag lists $WANT but has not pulled it (the bake will)"
                unpulled=$((unpulled + 1))
            elif [ "$got" != "$WANT" ]; then
                echo "  ✗ $tag is $got, expected $WANT" >&2
                bad=1
            else
                printf '  ✓ %-52s %s\n' "$tag" "$got"
                checked=$((checked + 1))
            fi
            break
        done
        if [ "$found" = 0 ]; then
            echo "  ? $image — not present locally (the bake will pull it)"
        fi
    done

    if [ "$checked" = 0 ]; then
        echo "ERROR: no source image was verified as $WANT — run 'make build'" >&2
        echo "       (and 'make build-supervisor') before verifying." >&2
        if [ "$unpulled" -gt 0 ]; then
            echo "       ($unpulled image(s) list $WANT but have not pulled its content," >&2
            echo "        which says nothing about the images the bake will build.)" >&2
        fi
        return 1
    fi

    if [ "$bad" != 0 ]; then
        echo "" >&2
        echo "ERROR: source images are the wrong architecture for this appliance." >&2
        echo "       Rebuild them with DOCKER_DEFAULT_PLATFORM=$APPLIANCE_ARCH, or use" >&2
        echo "       'make appliance-baked-iso-cross' which sets it for you." >&2
        return 1
    fi
    return 0
}

main "$@"
