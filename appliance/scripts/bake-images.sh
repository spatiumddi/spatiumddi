#!/bin/bash
# Bake locally-built dev images into the appliance rootfs so the ISO
# boots with our WIP code instead of pulling stale `:latest` from
# ghcr.io. Called by ``make appliance-bake-images`` (or directly).
#
# The mkosi.extra/ overlay tree gets copied into the rootfs verbatim
# during ``make appliance``, so any tarballs we drop under
# /usr/local/share/spatiumddi/images/ end up at the same path on the
# appliance. spatiumddi-firstboot iterates that directory at first
# boot, ``docker load``s each tarball, then skips ``docker-compose
# pull`` since the locally-baked images take precedence over ghcr.io
# (the compose ``image:`` line matches the loaded name+tag).
#
# Repeatable / idempotent — re-running clobbers prior tarballs.
# Tarball name encodes the registry-style tag so future targets
# (DNS/DHCP, role-split images for Phase 6, etc.) drop in without
# stepping on these two.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$REPO_ROOT/appliance/mkosi.extra/usr/local/share/spatiumddi/images"
mkdir -p "$DEST"

# Local dev tag → published image name (what the appliance compose
# references via ``image:``). docker tag attaches the published name
# to the dev image without copying bytes; docker save then writes the
# image content under that name so ``docker load`` on the appliance
# installs it under the published name (matching the compose ref).
declare -A IMAGES=(
    [spatiumddi-api:dev]=ghcr.io/spatiumddi/spatiumddi-api:latest
    [spatiumddi-frontend:dev]=ghcr.io/spatiumddi/spatiumddi-frontend:latest
)

for src in "${!IMAGES[@]}"; do
    dst="${IMAGES[$src]}"
    # File name: published image with / and : flattened to - so it
    # plays nice with shells, tar listings, and our load loop.
    file_name="$(printf '%s' "$dst" | tr '/:' '--').tar.zst"

    if ! docker image inspect "$src" >/dev/null 2>&1; then
        echo "✗ $src not found locally — run 'make build' first" >&2
        exit 1
    fi

    echo "→ Tag  $src  ⇒  $dst"
    docker tag "$src" "$dst"

    out="$DEST/$file_name"
    echo "→ Save+compress  ⇒  $out"
    # zstd -3 is the sweet spot for docker layers: ratio comparable to
    # -19 but ~10× faster. -T0 = use every available core.
    docker save "$dst" | zstd -3 -T0 -q -o "$out" -f
    ls -lh "$out"
done

# Drop a marker file so firstboot can short-circuit the
# "is this a dev-baked image bundle?" check without ls-globbing.
date -Iseconds > "$DEST/BAKED_AT"

echo
echo "✓ Baked $(ls "$DEST"/*.tar.zst 2>/dev/null | wc -l) image tarball(s) into $DEST"
echo "  Next: 'make appliance' rebuilds the raw image with these tarballs embedded;"
echo "        'make appliance-iso' wraps it as a hybrid USB/CD ISO."
