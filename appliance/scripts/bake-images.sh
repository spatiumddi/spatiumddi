#!/bin/bash
# Bake every container image needed for any install role into the
# appliance rootfs overlay so the ISO is fully self-contained — no
# `docker pull` from ghcr.io ever runs on a post-A4 appliance.
#
# Called by ``make appliance-bake-images`` (or directly). The release
# workflow pulls the just-built ghcr.io images at the cut tag and
# invokes this script so the slot image carries the matching set;
# laptop iteration uses the local ``:dev`` tags built by
# ``make build``.
#
# The mkosi.extra/ overlay tree gets copied into the rootfs verbatim
# during ``make appliance``, so tarballs we drop under
# /usr/local/share/spatiumddi/images/ land at the same path on the
# appliance. ``spatiumddi-firstboot`` iterates that directory at
# first boot (and on every slot upgrade), ``docker load``s each
# tarball, stamps the two canonical tags (``<image>:<calver>`` +
# ``<image>:slot-baked``), and skips ``docker-compose pull`` because
# the loaded images take precedence over ghcr.io.
#
# Repeatable / idempotent — re-running clobbers prior tarballs.
#
# Inputs (env):
#   SPATIUMDDI_VERSION  — CalVer release tag, e.g. ``2026.05.14-1``.
#                          Defaults to "dev" for laptop builds.
#   BAKE_SOURCE         — ``local`` (dev — use spatiumddi-*:dev tags)
#                          or ``ghcr`` (release — pull tags from ghcr).
#                          Defaults to ``local`` when SPATIUMDDI_VERSION
#                          is "dev", else ``ghcr``.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$REPO_ROOT/appliance/mkosi.extra/usr/local/share/spatiumddi/images"
mkdir -p "$DEST"

VERSION="${SPATIUMDDI_VERSION:-dev}"
if [ -z "${BAKE_SOURCE:-}" ]; then
    if [ "$VERSION" = "dev" ]; then
        BAKE_SOURCE=local
    else
        BAKE_SOURCE=ghcr
    fi
fi

echo "→ Baking image set for SPATIUMDDI_VERSION=$VERSION (source=$BAKE_SOURCE)"

# Every image the appliance can ever run, regardless of install role.
# Listed as (published_name) pairs — the local source tag is derived
# below based on BAKE_SOURCE.
#
# Order matters only for the manifest; the load order on the appliance
# is whatever-the-glob-returns.
SPATIUMDDI_IMAGES=(
    "ghcr.io/spatiumddi/spatiumddi-api"
    "ghcr.io/spatiumddi/spatiumddi-frontend"
    "ghcr.io/spatiumddi/spatium-supervisor"
    "ghcr.io/spatiumddi/dns-bind9"
    "ghcr.io/spatiumddi/dns-powerdns"
    "ghcr.io/spatiumddi/dhcp-kea"
)

# Third-party images that ship via the appliance compose. Pinned to
# matching tags the appliance compose's ``image:`` lines reference.
THIRD_PARTY_IMAGES=(
    "docker.io/library/postgres:16-alpine"
    "docker.io/library/redis:7-alpine"
    "docker.io/library/nginx:1.27-alpine"
)

bake_one() {
    local published="$1"  # e.g. ghcr.io/spatiumddi/spatiumddi-api or docker.io/library/postgres:16-alpine
    local src tag_for_save

    if [[ "$published" == *:* ]]; then
        # Third-party — published already carries the tag.
        src="$published"
        tag_for_save="$published"
    else
        # SpatiumDDI image — append the release tag.
        if [ "$BAKE_SOURCE" = "local" ]; then
            # Map ghcr.io/spatiumddi/<name> → <name>:dev (matches what
            # ``make build`` produces).
            local local_name
            local_name=$(basename "$published")
            src="${local_name}:dev"
            tag_for_save="${published}:${VERSION}"
            if ! docker image inspect "$src" >/dev/null 2>&1; then
                # Supervisor doesn't have a :dev tag yet (Phase A1
                # ships only the release-tagged image). Try the
                # published name first as a fallback.
                if docker image inspect "${published}:${VERSION}" >/dev/null 2>&1; then
                    src="${published}:${VERSION}"
                else
                    echo "✗ neither $src nor ${published}:${VERSION} found locally" >&2
                    echo "  hint: run 'make build' (api/frontend) or" >&2
                    echo "        'docker build -t ${local_name}:dev -f agent/.../Dockerfile .'" >&2
                    return 1
                fi
            fi
        else
            # ghcr source — pull the cut tag first, then save it.
            src="${published}:${VERSION}"
            tag_for_save="$src"
            echo "  pulling $src …"
            docker pull "$src" >/dev/null
        fi
    fi

    if [ "$src" != "$tag_for_save" ]; then
        docker tag "$src" "$tag_for_save"
    fi

    # File name: flatten registry slashes + colons so the tarball
    # plays nice with shells + the firstboot load loop's glob.
    local file_name
    file_name="$(printf '%s' "$tag_for_save" | tr '/:' '--').tar.zst"
    local out="$DEST/$file_name"

    echo "  saving $tag_for_save → $(basename "$out")"
    # zstd -3 is the sweet spot for docker layers — ratio comparable
    # to -19 but ~10× faster. -T0 = use every core.
    docker save "$tag_for_save" | zstd -3 -T0 -q -o "$out" -f
}

> "$DEST/MANIFEST"  # truncate / create
for img in "${SPATIUMDDI_IMAGES[@]}"; do
    bake_one "$img"
    echo "${img}:${VERSION}" >> "$DEST/MANIFEST"
done
for img in "${THIRD_PARTY_IMAGES[@]}"; do
    bake_one "$img"
    echo "$img" >> "$DEST/MANIFEST"
done

# BAKED_AT — firstboot uses this to decide "is this a fresh slot I
# haven't loaded yet?". Slot-upgrade-aware logic in firstboot keys off
# changes here. Format: ISO-8601 with the release tag suffix.
date -u +"%Y-%m-%dT%H:%M:%SZ#${VERSION}" > "$DEST/BAKED_AT"

# SPATIUMDDI_VERSION — firstboot reads this to stamp the canonical
# ``<image>:<calver>`` tags after ``docker load``. Each image saved
# above already carries the calver tag, but a separate file lets
# firstboot also stamp the ``<image>:slot-baked`` alias without
# re-parsing the manifest.
printf '%s\n' "$VERSION" > "$DEST/VERSION"

echo ""
count=$(ls "$DEST"/*.tar.zst 2>/dev/null | wc -l)
total=$(du -sh "$DEST" 2>/dev/null | awk '{print $1}')
echo "✓ Baked $count image tarball(s), $total total, into $DEST"
echo "  Next: 'make appliance' rebuilds the raw image with these embedded;"
echo "        'make appliance-iso' wraps it as a hybrid USB/CD ISO;"
echo "        'make appliance-slot-image' produces the slot upgrade raw.xz."
