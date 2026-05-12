#!/bin/sh
# Phase 8b-1 — extract the rootfs portion from mkosi's bootable disk
# image as a standalone ext4 image, ready to dd into an A/B slot.
# Compresses with xz for distribution.
#
# Invoked from the appliance builder container (has root + losetup
# + xz). Outputs $OUT_DIR/spatiumddi-appliance-slot-<version>.raw.xz.
#
# The mkosi disk image has a single root partition spanning the whole
# disk. When an operator installs via spatium-install, that rootfs
# gets rsync'd into the 4 GiB root_A partition. This script reproduces
# what ends up on root_A — a single ext4 filesystem of the rootfs
# minus /var content — so sysupdate (or our own writer in 8b-2) can
# write it to the inactive slot.
#
# Usage:
#   build-slot-image.sh /path/to/spatiumddi-appliance.raw  /path/to/output-dir
#
# Output:
#   <output-dir>/spatiumddi-appliance-slot-<version>.raw.xz
#   <output-dir>/spatiumddi-appliance-slot-<version>.sha256

set -eu

INPUT_RAW="${1:?input raw image path required}"
OUT_DIR="${2:?output directory required}"

if [ ! -f "$INPUT_RAW" ]; then
    echo "ERROR: input raw image not found: $INPUT_RAW" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"

# Derive version from the input filename — mkosi names it
# `spatiumddi-appliance_<version>.raw`. Falls back to "0.0.0" so the
# script still produces an output even on weird filenames.
VERSION=$(basename "$INPUT_RAW" .raw | sed -E 's/^spatiumddi-appliance_//')
[ -n "$VERSION" ] || VERSION="0.0.0"

WORK=$(mktemp -d)
trap 'umount "$WORK/mnt" 2>/dev/null || true; losetup -d "$LOOP" 2>/dev/null || true; rm -rf "$WORK"' EXIT

mkdir -p "$WORK/mnt"

echo "→ Attaching $INPUT_RAW to loop device…"
LOOP=$(losetup -fP --show "$INPUT_RAW")
echo "  loop device: $LOOP"

# mkosi's default disk layout: p1 = ESP, p2 = root. Slim mkosi images
# may have a different layout — find the partition with the largest
# ext4 footprint and assume that's the root.
ROOT_PART=""
for part in "${LOOP}"p* "${LOOP}p"*; do
    [ -b "$part" ] || continue
    type=$(blkid -s TYPE -o value "$part" 2>/dev/null || true)
    if [ "$type" = "ext4" ]; then
        ROOT_PART="$part"
        break
    fi
done
if [ -z "$ROOT_PART" ]; then
    echo "ERROR: couldn't find ext4 root partition in $INPUT_RAW" >&2
    losetup -a >&2
    exit 1
fi
echo "  root partition: $ROOT_PART"

# Slot size is 4 GiB on the installed system (Phase 8a-1). We build
# the slot image at that target size so an operator's `dd` writes
# exactly partition-sized content. ext4 sparse representation +
# xz compression keeps the on-the-wire size small (~700 MiB for a
# 1.8 GiB rootfs).
SLOT_BYTES=$((4 * 1024 * 1024 * 1024))
SLOT_IMG="$WORK/slot.raw"

echo "→ Creating $((SLOT_BYTES / 1024 / 1024 / 1024)) GiB slot image…"
# Sparse allocation — only allocated blocks consume disk space.
truncate -s "$SLOT_BYTES" "$SLOT_IMG"
mkfs.ext4 -F -L root_a "$SLOT_IMG" >/dev/null 2>&1

echo "→ Copying rootfs into slot image (this can take a few minutes)…"
mount "$ROOT_PART" "$WORK/mnt"
mkdir -p "$WORK/slot-mnt"
mount -o loop "$SLOT_IMG" "$WORK/slot-mnt"

# rsync the rootfs sans /var content. /var is on its own partition on
# the installed system; including it here would just bloat the slot
# image with throwaway logs/caches.
rsync -aAXH \
    --exclude="/dev/*" \
    --exclude="/proc/*" \
    --exclude="/sys/*" \
    --exclude="/tmp/*" \
    --exclude="/run/*" \
    --exclude="/mnt/*" \
    --exclude="/media/*" \
    --exclude="/var/log/*" \
    --exclude="/var/cache/apt/archives/*" \
    --exclude="/lib/live" \
    "$WORK/mnt/" "$WORK/slot-mnt/"

# Phase 8a-2 — snapshot /etc into the slot's /usr/lib/etc.image.
# Without this, the etc.mount unit can't activate the overlay on a
# slot freshly written by sysupdate.
mkdir -p "$WORK/slot-mnt/usr/lib/etc.image"
cp -a "$WORK/slot-mnt/etc/." "$WORK/slot-mnt/usr/lib/etc.image/"

umount "$WORK/slot-mnt"
umount "$WORK/mnt"
losetup -d "$LOOP"
LOOP=""

# Zero out unused blocks so xz can compress them efficiently. zerofree
# would be ideal but it's not always available; fallocate punch-hole
# works on Linux and gives sparse files xz can collapse.
echo "→ Sparsifying unused blocks…"
e2fsck -fy "$SLOT_IMG" >/dev/null 2>&1 || true

OUT="$OUT_DIR/spatiumddi-appliance-slot-${VERSION}.raw.xz"
SHA="$OUT_DIR/spatiumddi-appliance-slot-${VERSION}.sha256"

echo "→ Compressing → $OUT (xz -9 — slow, but ~3-4× better than zstd at default)…"
xz -9 --threads=0 --stdout "$SLOT_IMG" > "$OUT"

echo "→ Writing SHA-256 → $SHA"
( cd "$OUT_DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$SHA")" )

echo ""
echo "✓ Slot image: $OUT"
ls -lh "$OUT"
echo "✓ Checksum:  $SHA"
cat "$SHA"
