#!/bin/sh
# Phase 8b-1 — extract the rootfs portion from mkosi's bootable disk
# image as a standalone ext4 image, ready to dd into an A/B slot.
# Compresses with xz for distribution.
#
# Invoked from the appliance builder container (has root, kpartx,
# mount, xz). Outputs $OUT_DIR/spatiumddi-appliance-slot-<v>.raw.xz.
#
# The mkosi disk image has a GPT layout with ESP / BIOS-boot / root.
# We find the root partition by GPT PartType GUID (Linux root x86-64
# = 4f68bce3-e8cd-4db1-96e7-fbcaf984b709), mount it read-only via
# `mount -o loop,offset=…`, rsync its contents (minus /var content)
# into a freshly-mkfs'd 4 GiB ext4 image, add the /usr/lib/etc.image
# snapshot needed by Phase 8a's overlay, then xz-compress.
#
# Using mount -o loop,offset= rather than losetup -P + partprobe
# because the latter requires udev to populate /dev/loopXpN nodes —
# udev doesn't run inside the build container.
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
# `spatiumddi-appliance_<version>.raw`. Falls back to "0.0.0".
VERSION=$(basename "$INPUT_RAW" .raw | sed -E 's/^spatiumddi-appliance_//')
[ -n "$VERSION" ] || VERSION="0.0.0"

WORK=$(mktemp -d)
trap 'for mp in "$WORK/src-mnt" "$WORK/slot-mnt"; do umount "$mp" 2>/dev/null || true; done; rm -rf "$WORK"' EXIT

mkdir -p "$WORK/src-mnt" "$WORK/slot-mnt"

# Defensive cleanup — a prior failed run of this script can leave a
# stale loopback attached to the input raw, and mount -o loop,offset=
# refuses to attach a second loop ("overlapping loop device exists").
# Detach anything currently pointing at $INPUT_RAW before we start.
for stale in $(losetup -j "$INPUT_RAW" 2>/dev/null | cut -d: -f1); do
    echo "  detaching stale loop $stale"
    losetup -d "$stale" 2>/dev/null || true
done

# Find the Linux root x86-64 partition by GPT PartType GUID. sfdisk's
# dump format is machine-parseable and present in every Debian build
# image, so we don't have to install anything extra.
echo "→ Locating Linux root x86-64 partition in $INPUT_RAW…"
PARTTABLE=$(sfdisk -d "$INPUT_RAW" 2>/dev/null)
ROOT_LINE=$(printf '%s\n' "$PARTTABLE" | grep -iE 'type=4f68bce3-e8cd-4db1-96e7-fbcaf984b709' | head -1)
if [ -z "$ROOT_LINE" ]; then
    echo "ERROR: no Linux root x86-64 partition found in $INPUT_RAW" >&2
    printf '%s\n' "$PARTTABLE" >&2
    exit 1
fi

# Parse `start=`, `size=` from the sfdisk dump line. Values are in
# 512-byte sectors. Layout typically:
#   /work/build/...raw3 : start=2101248, size=3017832, type=4F68BCE3-...
START_SECTORS=$(printf '%s' "$ROOT_LINE" | sed -nE 's/.*start=\s*([0-9]+).*/\1/p')
SIZE_SECTORS=$(printf '%s' "$ROOT_LINE" | sed -nE 's/.*size=\s*([0-9]+).*/\1/p')
START_BYTES=$((START_SECTORS * 512))
SIZE_BYTES=$((SIZE_SECTORS * 512))
echo "  start=${START_SECTORS}s (${START_BYTES}b) size=${SIZE_SECTORS}s (${SIZE_BYTES}b)"

# Slot size matches the installed system's root_A/root_B size from
# Phase 8a-1 (4 GiB). spatium-upgrade-slot dd's this image directly
# into the inactive 4 GiB partition.
SLOT_BYTES=$((4 * 1024 * 1024 * 1024))
SLOT_IMG="$WORK/slot.raw"

echo "→ Creating ${SLOT_BYTES} B (4 GiB) slot image…"
truncate -s "$SLOT_BYTES" "$SLOT_IMG"
mkfs.ext4 -F -L root_a "$SLOT_IMG" >/dev/null 2>&1

echo "→ Mounting source rootfs (RO) + slot image (RW)…"
mount -o "ro,loop,offset=$START_BYTES,sizelimit=$SIZE_BYTES" \
    "$INPUT_RAW" "$WORK/src-mnt"
mount -o loop "$SLOT_IMG" "$WORK/slot-mnt"

echo "→ Copying rootfs into slot image…"
# Use a tar | tar pipe rather than rsync because rsync isn't shipped
# in the appliance-builder container (kept lean). tar preserves perms /
# xattrs / acls / hardlinks the same way rsync -aAXH does, and supports
# --exclude. The source has /var on the same partition (mkosi single-
# root layout), so we strip /var content here that's normally on the
# installer's separate var partition.
( cd "$WORK/src-mnt" && tar \
    --xattrs --acls --selinux --numeric-owner \
    --exclude="./dev/*" \
    --exclude="./proc/*" \
    --exclude="./sys/*" \
    --exclude="./tmp/*" \
    --exclude="./run/*" \
    --exclude="./mnt/*" \
    --exclude="./media/*" \
    --exclude="./var/log/*" \
    --exclude="./var/cache/apt/archives/*" \
    --exclude="./lib/live" \
    -cf - . ) | \
( cd "$WORK/slot-mnt" && tar --xattrs --acls --selinux --numeric-owner -xpf - )

# Phase 8a-2 — snapshot /etc into the slot's /usr/lib/etc.image.
# Without this, etc.mount can't activate the overlay on a slot
# freshly written by spatium-upgrade-slot.
echo "→ Snapshotting /etc → /usr/lib/etc.image (overlay lower)…"
mkdir -p "$WORK/slot-mnt/usr/lib/etc.image"
cp -a "$WORK/slot-mnt/etc/." "$WORK/slot-mnt/usr/lib/etc.image/"

umount "$WORK/slot-mnt"
umount "$WORK/src-mnt"

# Run an fsck pass so the image lands in a clean state. e2fsck -fy
# trims unused-but-allocated blocks too, which helps xz compress.
echo "→ Fsck + trim unused blocks…"
e2fsck -fy "$SLOT_IMG" >/dev/null 2>&1 || true

OUT="$OUT_DIR/spatiumddi-appliance-slot-${VERSION}.raw.xz"
SHA="$OUT_DIR/spatiumddi-appliance-slot-${VERSION}.sha256"

echo "→ Compressing → $OUT (xz -6, threads=all)…"
xz -6 --threads=0 --stdout "$SLOT_IMG" > "$OUT"

echo "→ Writing SHA-256 → $SHA"
( cd "$OUT_DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$SHA")" )

echo ""
echo "✓ Slot image: $OUT"
ls -lh "$OUT"
echo "✓ Checksum:   $SHA"
cat "$SHA"
