#!/usr/bin/env bash
# bake-images.sh — issue #183 Phase 7.
#
# Replaces the pre-Phase-7 docker-overlay.img approach. The k3s
# appliance preloads its container images by dropping ``*.tar.zst``
# archives into /var/lib/rancher/k3s/agent/images/. k3s scans that
# directory at startup and imports anything new into containerd —
# native, no firstboot shell-out needed.
#
# We tag each image as ``ghcr.io/spatiumddi/<name>:${SPATIUMDDI_VERSION}``
# before save so the imported image matches the reference the chart's
# values.yaml uses. SPATIUMDDI_VERSION resolves from the Makefile (CI
# release sets it to the CalVer tag; local dev gets ``dev-<short-sha>-
# <rand>``).
#
# Also writes /usr/lib/spatiumddi/spatiumddi-version so firstboot can
# sync .env's SPATIUMDDI_VERSION line to the baked tag — without that
# sidecar the chart's image reference would resolve to ``:latest``
# which doesn't exist in containerd, and pods would CrashLoopBackOff
# with ``ErrImagePull`` (air-gap: fatal).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPLIANCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$APPLIANCE_DIR/.." && pwd)"
IMAGES_DIR="$APPLIANCE_DIR/mkosi.extra/var/lib/rancher/k3s/agent/images"
VERSION_FILE="$APPLIANCE_DIR/mkosi.extra/usr/lib/spatiumddi/spatiumddi-version"

SPATIUMDDI_VERSION="${SPATIUMDDI_VERSION:-dev}"
BAKE_SOURCE="${BAKE_SOURCE:-local}"  # local (docker :dev) | ghcr (pull from ghcr.io)

# Image set the slot needs to carry. Names match the chart's
# values.yaml ``<role>.image.repository`` + the supervisor.
IMAGES=(
    "ghcr.io/spatiumddi/spatium-supervisor"
    "ghcr.io/spatiumddi/dns-bind9"
    "ghcr.io/spatiumddi/dns-powerdns"
    "ghcr.io/spatiumddi/dhcp-kea"
)

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker CLI required on the build host (used to save + retag images)." >&2
    echo "       The APPLIANCE itself ships zero docker; this is build-host tooling only." >&2
    exit 2
fi
if ! command -v zstd >/dev/null 2>&1; then
    echo "ERROR: zstd required on the build host (compresses image archives)." >&2
    exit 2
fi

mkdir -p "$IMAGES_DIR" "$(dirname "$VERSION_FILE")"

# Stamp the version file BEFORE the loop so a partial run still
# carries something the firstboot reconcile can find (chart bytes
# may already match an earlier successful bake).
echo "$SPATIUMDDI_VERSION" > "$VERSION_FILE"
echo "→ SPATIUMDDI_VERSION = $SPATIUMDDI_VERSION (stamped at $VERSION_FILE)"

resolve_source_tag() {
    # ``BAKE_SOURCE=local``: use the operator's ``make build`` :dev
    # images. ``BAKE_SOURCE=ghcr``: pull from ghcr at the requested
    # version (CI release path).
    local repo="$1"
    local short
    short="$(basename "$repo")"
    case "$BAKE_SOURCE" in
        local)
            # ``make build`` tags as ``<short>:dev`` (no ghcr.io prefix).
            echo "${short}:dev"
            ;;
        ghcr)
            echo "${repo}:${SPATIUMDDI_VERSION}"
            ;;
        *)
            echo "ERROR: unknown BAKE_SOURCE=${BAKE_SOURCE}" >&2
            exit 2
            ;;
    esac
}

for repo in "${IMAGES[@]}"; do
    short="$(basename "$repo")"
    source_tag="$(resolve_source_tag "$repo")"
    target_tag="${repo}:${SPATIUMDDI_VERSION}"
    out_tar="$IMAGES_DIR/${short}.tar.zst"

    if [ "$BAKE_SOURCE" = "ghcr" ]; then
        echo "→ Pulling $source_tag …"
        docker pull "$source_tag" >/dev/null
    elif ! docker image inspect "$source_tag" >/dev/null 2>&1; then
        echo "ERROR: $source_tag not in local docker. Run 'make build' first." >&2
        exit 3
    fi

    # Retag so containerd registers the image under the chart's
    # expected ghcr name. ``docker save`` writes whatever RepoTags
    # the image has at save time; without this step, the archive
    # would carry ``<short>:dev`` (local) — chart references
    # ``ghcr.io/spatiumddi/<short>:${SPATIUMDDI_VERSION}`` and the
    # kubelet would say ``ErrImagePull``.
    docker tag "$source_tag" "$target_tag"

    echo "→ Baking $target_tag → $out_tar"
    # docker save | zstd → containerd-readable archive. Atomic via
    # .new sibling so a crash mid-bake doesn't ship a torn tarball.
    tmp="${out_tar}.new"
    docker save "$target_tag" | zstd -T0 -19 -o "$tmp"
    mv "$tmp" "$out_tar"

    size="$(du -h "$out_tar" | awk '{print $1}')"
    echo "  ✓ $size"
done

TOTAL="$(du -hc "$IMAGES_DIR"/*.tar.zst 2>/dev/null | tail -1 | awk '{print $1}')"
echo "✓ ${#IMAGES[@]} images baked into $IMAGES_DIR ($TOTAL)"
echo "  k3s auto-imports these at startup (no firstboot shell-out required)"
