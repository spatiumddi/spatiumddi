#!/bin/bash
# Bake every container image needed for any install role into a per-
# slot ext4 image that the appliance overlay-mounts on top of
# /var/lib/docker at boot (#170 Wave E1).
#
# The overlay-file approach replaces the prior "docker load tarballs
# at firstboot" path. Slot rollback automatically brings the matching
# container set back because the overlay's lowerdir lives on the slot
# rootfs. ``/var/lib/docker`` accumulation across slot swaps is no
# longer possible — the upperdir is per-slot too (cleared on swap).
#
# Output:
#   appliance/mkosi.extra/usr/lib/spatiumddi/docker-overlay.img
#       ext4 image carrying a populated /var/lib/docker. Mounted as
#       the lowerdir on /var/lib/docker at firstboot; the matching
#       container set is immediately available to the appliance's
#       docker daemon.
#   appliance/mkosi.extra/usr/lib/spatiumddi/docker-overlay.manifest
#       Human-readable list of images + their digests. Surfaces in
#       the appliance's diagnostic bundle so an operator can confirm
#       what's baked.
#   appliance/mkosi.extra/usr/lib/spatiumddi/docker-overlay.version
#       The SPATIUMDDI_VERSION the image was baked against. Used by
#       firstboot's "did the slot change?" check.
#
# Implementation:
#
# 1. Pull / verify every image is present in the host docker.
# 2. ``docker save`` everything into one combined tarball.
# 3. Spin up a sandboxed dockerd inside a dind container with
#    ``--data-root`` pointing at a host-bind-mounted directory we
#    control. Load the combined tarball into that daemon.
# 4. Stop the sandbox daemon. Its data-root is now populated with
#    the baked overlay2 layers + image metadata.
# 5. ``mkfs.ext4`` an image file sized to fit the data-root with
#    headroom, loop-mount it, rsync the data-root in. Unmount.
#
# Inputs (env):
#   SPATIUMDDI_VERSION  CalVer release tag, e.g. ``2026.05.14-1``.
#                        Defaults to "dev" for laptop builds.
#   BAKE_SOURCE         ``local`` (dev — use spatiumddi-*:dev tags)
#                        or ``ghcr`` (release — pull tags from ghcr).
#   OVERLAY_SIZE_GB     Capacity of the produced ext4 image. Default
#                        4 — large enough for the current container
#                        set (~1.5 GiB compressed → ~3 GiB on disk
#                        with overlay2 metadata) with headroom.
#
# Requires root or sudo for mkfs.ext4 + loop-mount. CI runners have
# this; laptop builds may need ``sudo make appliance-bake-images``.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$REPO_ROOT/appliance/mkosi.extra/usr/lib/spatiumddi"
mkdir -p "$DEST"

VERSION="${SPATIUMDDI_VERSION:-dev}"
if [ -z "${BAKE_SOURCE:-}" ]; then
    if [ "$VERSION" = "dev" ]; then BAKE_SOURCE=local; else BAKE_SOURCE=ghcr; fi
fi
OVERLAY_SIZE_GB="${OVERLAY_SIZE_GB:-4}"

echo "→ Baking docker overlay for SPATIUMDDI_VERSION=$VERSION (source=$BAKE_SOURCE, size=${OVERLAY_SIZE_GB} GiB)"

# Image set the appliance can ever need, regardless of install role.
SPATIUMDDI_IMAGES=(
    "ghcr.io/spatiumddi/spatiumddi-api"
    "ghcr.io/spatiumddi/spatiumddi-frontend"
    "ghcr.io/spatiumddi/spatium-supervisor"
    "ghcr.io/spatiumddi/dns-bind9"
    "ghcr.io/spatiumddi/dns-powerdns"
    "ghcr.io/spatiumddi/dhcp-kea"
)
# Third-party images pinned by the appliance compose.
THIRD_PARTY_IMAGES=(
    "docker.io/library/postgres:16-alpine"
    "docker.io/library/redis:7-alpine"
    "docker.io/library/nginx:1.27-alpine"
)

# Clean prior bake artefacts so a smaller image set this round doesn't
# leave stale bytes behind.
rm -f "$DEST/docker-overlay.img" "$DEST/docker-overlay.manifest" "$DEST/docker-overlay.version"
# Sweep any old per-tarball directory (pre-E1 layout) so a slot that
# previously baked tarballs cleanly migrates to the overlay model.
rm -rf "$REPO_ROOT/appliance/mkosi.extra/usr/local/share/spatiumddi/images"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; (sudo umount "$WORK/mnt" 2>/dev/null || true)' EXIT

ensure_image() {
    local published="$1"
    local src

    if [[ "$published" == *:* ]]; then
        # Third-party — fixed tag.
        src="$published"
        if ! docker image inspect "$src" >/dev/null 2>&1; then
            echo "  pulling $src …"
            docker pull "$src" >/dev/null
        fi
        printf '%s\n' "$src"
        return
    fi
    # SpatiumDDI image — append the release tag.
    if [ "$BAKE_SOURCE" = "local" ]; then
        local base candidates dev_tag
        base=$(basename "$published")
        # Local image naming in this repo is inconsistent:
        #   * ``ghcr.io/spatiumddi/spatiumddi-api`` → ``spatiumddi-api:dev``
        #     (basename already includes the ``spatiumddi-`` prefix)
        #   * ``ghcr.io/spatiumddi/spatium-supervisor`` → ``spatium-supervisor:dev``
        #     (basename matches verbatim)
        #   * ``ghcr.io/spatiumddi/dns-bind9`` → ``spatiumddi-dns-bind9:dev``
        #     (docker-compose.dev.yml prepends ``spatiumddi-`` to the
        #      bare service name to namespace the dev tag)
        # So we try both forms — basename verbatim, and basename with a
        # ``spatiumddi-`` prefix when not already present — and accept
        # whichever exists.
        candidates=("${base}:dev")
        case "$base" in
            spatiumddi-*|spatium-*) ;;
            *) candidates+=("spatiumddi-${base}:dev") ;;
        esac
        dev_tag=""
        for candidate in "${candidates[@]}"; do
            if docker image inspect "$candidate" >/dev/null 2>&1; then
                dev_tag="$candidate"
                break
            fi
        done
        if [ -n "$dev_tag" ]; then
            # Make sure the canonical release-tag exists too for the
            # appliance compose's image: pin to substitute against.
            docker tag "$dev_tag" "${published}:${VERSION}" >/dev/null 2>&1 || true
        elif ! docker image inspect "${published}:${VERSION}" >/dev/null 2>&1; then
            echo "✗ no local dev image found for ${published}" >&2
            echo "  tried: ${candidates[*]}" >&2
            echo "  hint: run 'make build' first." >&2
            return 1
        fi
        printf '%s\n' "${published}:${VERSION}"
    else
        local pub="${published}:${VERSION}"
        echo "  pulling $pub …"
        docker pull "$pub" >/dev/null
        printf '%s\n' "$pub"
    fi
}

# ── 1-2. Pull / save into one combined tarball ─────────────────────
declare -a TAGS=()
> "$DEST/docker-overlay.manifest.tmp"
for img in "${SPATIUMDDI_IMAGES[@]}"; do
    tag=$(ensure_image "$img")
    TAGS+=("$tag")
    # Digest for the manifest; falls back to the tag if no digest yet.
    digest=$(docker image inspect "$tag" --format '{{join .RepoDigests ","}}' 2>/dev/null || echo "")
    printf '%s\t%s\n' "$tag" "${digest:-<not-pushed>}" >> "$DEST/docker-overlay.manifest.tmp"
done
for img in "${THIRD_PARTY_IMAGES[@]}"; do
    tag=$(ensure_image "$img")
    TAGS+=("$tag")
    digest=$(docker image inspect "$tag" --format '{{join .RepoDigests ","}}' 2>/dev/null || echo "")
    printf '%s\t%s\n' "$tag" "${digest:-<not-pushed>}" >> "$DEST/docker-overlay.manifest.tmp"
done

COMBINED="$WORK/combined.tar"
echo "  combining $((${#TAGS[@]})) image(s) into one tarball …"
docker save "${TAGS[@]}" -o "$COMBINED"

# ── 3. Populate a sandboxed dockerd data-root ─────────────────────
DATAROOT="$WORK/data-root"
mkdir -p "$DATAROOT"

echo "  starting sandbox dockerd to populate data-root …"
SANDBOX_NAME="spatium-bake-$$"
# Storage driver MUST match the appliance's runtime driver, because
# docker's per-driver graph lives at /var/lib/docker/<driver>/ and
# /var/lib/docker/image/<driver>/. The appliance runs with
# storage-driver=fuse-overlayfs (selected via /etc/docker/daemon.json
# because the appliance's /var/lib/docker is itself a kernel overlay,
# and overlay2 cannot nest on overlay). If we baked with overlay2,
# the appliance daemon would see /var/lib/docker/overlay2/ on disk
# but look at /var/lib/docker/fuse-overlayfs/ — finding zero images
# — and compose would silently fall through to pulling ``:dev`` from
# ghcr (where ``:dev`` doesn't exist as a published tag, hence
# "manifest unknown" → install wedged).
#
# fuse-overlayfs isn't in docker:dind's base image, so install it
# inside the sandbox before exec'ing dockerd-entrypoint.
docker run -d --rm --privileged --name "$SANDBOX_NAME" \
    -v "$DATAROOT":/var/lib/docker \
    -v "$COMBINED":/combined.tar:ro \
    docker:dind sh -c '
        apk add --no-cache fuse-overlayfs >/dev/null 2>&1 \
            || { echo "✗ apk add fuse-overlayfs failed" >&2; exit 1; }
        exec dockerd-entrypoint.sh \
            --host=unix:///var/run/docker.sock \
            --storage-driver=fuse-overlayfs
    ' >/dev/null

# Wait for the sandbox daemon to be ready.
for _ in $(seq 1 30); do
    if docker exec "$SANDBOX_NAME" docker info >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
docker exec "$SANDBOX_NAME" docker info >/dev/null 2>&1 || {
    echo "✗ sandbox dockerd never came up" >&2
    docker logs "$SANDBOX_NAME" | tail -30 >&2
    docker rm -f "$SANDBOX_NAME" >/dev/null
    exit 1
}

echo "  loading combined tarball into sandbox …"
docker exec "$SANDBOX_NAME" docker load -i /combined.tar >/dev/null

# Stop the sandbox cleanly so the daemon flushes its caches.
docker stop "$SANDBOX_NAME" >/dev/null
# --rm wipes the container; the data-root we bind-mounted survives.

# ── 4-5. Build the ext4 image + copy the data-root in ─────────────
IMG="$WORK/docker-overlay.img"
echo "  building ${OVERLAY_SIZE_GB} GiB ext4 image …"
truncate -s "${OVERLAY_SIZE_GB}G" "$IMG"
mkfs.ext4 -q -L spatium-docker -O ^has_journal "$IMG"
mkdir -p "$WORK/mnt"
sudo mount -o loop "$IMG" "$WORK/mnt"

echo "  rsync data-root → ext4 image …"
sudo rsync -aHAX "$DATAROOT/" "$WORK/mnt/"

sudo umount "$WORK/mnt"

# Move the finished image + sidecars into place. ``install`` sets
# perms; the image is read-only at runtime (the overlay's upperdir
# takes writes).
install -m 0644 "$IMG" "$DEST/docker-overlay.img"
mv "$DEST/docker-overlay.manifest.tmp" "$DEST/docker-overlay.manifest"
printf '%s\n' "$VERSION" > "$DEST/docker-overlay.version"

SIZE=$(du -h "$DEST/docker-overlay.img" | awk '{print $1}')
echo ""
echo "✓ Baked docker overlay (${SIZE}) → $DEST/docker-overlay.img"
echo "  manifest:  $DEST/docker-overlay.manifest"
echo "  version:   $DEST/docker-overlay.version ($VERSION)"
echo "  Next: 'make appliance' rebuilds the raw image with this embedded;"
echo "        firstboot overlay-mounts it on /var/lib/docker before"
echo "        starting docker.service — slot rollback ≡ container rollback."
