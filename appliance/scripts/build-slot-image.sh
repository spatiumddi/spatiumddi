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

# Phase 8c — write the image-baseline /etc/fstab to slot's underlying
# /etc/fstab. WITHOUT this, the slot boots with whatever fstab mkosi
# shipped (the live-config "overlay / overlay rw 0 0" form) — /var
# doesn't mount, etc.mount can't fire, and operator state in
# /var/persist/etc/ stays invisible. Same content spatium-install
# writes for slot_a at install time, so both slots agree.
echo "→ Writing image-baseline /etc/fstab into slot…"
cat > "$WORK/slot-mnt/etc/fstab" <<'FSTAB'
LABEL=var  /var       ext4   defaults                             0 2
LABEL=ESP  /boot/efi  vfat   umask=0077,shortname=winnt           0 2
tmpfs      /tmp       tmpfs  nosuid,nodev,noexec,mode=1777        0 0
FSTAB

# /etc/hostname is image-baseline only — empty file, so before the
# overlay mounts the kernel hostname is "(none)". Once etc.mount
# fires, /etc/hostname (from /var/persist/etc/hostname upper) is
# the operator's value. Slight cosmetic mid-boot blip; the
# operator never sees an "empty" hostname after boot completes.
: > "$WORK/slot-mnt/etc/hostname"

# Phase 8a-2 — snapshot /etc into the slot's /usr/lib/etc.image.
# Without this, etc.mount can't activate the overlay on a slot
# freshly written by spatium-upgrade-slot.
echo "→ Snapshotting /etc → /usr/lib/etc.image (overlay lower)…"
mkdir -p "$WORK/slot-mnt/usr/lib/etc.image"
cp -a "$WORK/slot-mnt/etc/." "$WORK/slot-mnt/usr/lib/etc.image/"

# Phase 8c — slot image needs a working /boot or grub can't boot
# from it. mkosi strips /boot from the rootfs during image build
# (kernel + initrd live as separate output files), so the tar pipe
# above brought across an empty /boot. Without this step, after
# `spatium-upgrade-slot apply` dd's the image and the operator
# `set-next-boot`s to slot_b, grub fails with "file /boot/vmlinuz
# not found" — which is exactly what build033 testing hit.
#
# Mirror spatium-install's kernel installation: copy the mkosi-
# output vmlinuz alongside the rootfs, regenerate the initrd in
# chroot so live-tools' update-initramfs diversion is undone (live-
# tools rewrites update-initramfs to a no-op for live-mode boots),
# then drop /boot/vmlinuz + /boot/initrd.img symlinks for grub.cfg
# to follow without knowing the kernel version.
KVER=$(ls "$WORK/slot-mnt/lib/modules" 2>/dev/null | head -1)
if [ -z "$KVER" ]; then
    echo "ERROR: no /lib/modules in slot image — tar pipe must have skipped modules" >&2
    exit 1
fi
INPUT_DIR=$(dirname "$INPUT_RAW")
INPUT_BASENAME=$(basename "$INPUT_RAW" .raw)
SOURCE_VMLINUZ="$INPUT_DIR/${INPUT_BASENAME}.vmlinuz"
if [ ! -f "$SOURCE_VMLINUZ" ]; then
    echo "ERROR: mkosi vmlinuz not at $SOURCE_VMLINUZ" >&2
    ls "$INPUT_DIR" >&2
    exit 1
fi
echo "→ Installing kernel ${KVER} into slot's /boot/…"
install -m 0644 "$SOURCE_VMLINUZ" "$WORK/slot-mnt/boot/vmlinuz-$KVER"
ln -sf "vmlinuz-$KVER" "$WORK/slot-mnt/boot/vmlinuz"
ln -sf "initrd.img-$KVER" "$WORK/slot-mnt/boot/initrd.img"

# Regenerate initrd in chroot. The slot's rootfs carries live-boot
# (added to the package set so a single rootfs can power both the
# live ISO and the installed disk). Inside that rootfs, live-tools
# dpkg-diverts update-initramfs into a no-op. Undivert it first so
# the actual binary runs, then regenerate.
echo "→ Regenerating initrd inside slot…"
mount --bind /proc "$WORK/slot-mnt/proc"
mount --bind /sys  "$WORK/slot-mnt/sys"
mount --bind /dev  "$WORK/slot-mnt/dev"

# Force virtio + ext4 modules into the initrd so the slot can find
# its root partition on common VM hypervisors (Proxmox/QEMU/libvirt).
sed -i 's/^MODULES=.*/MODULES=most/' \
    "$WORK/slot-mnt/etc/initramfs-tools/initramfs.conf" 2>/dev/null || true
cat > "$WORK/slot-mnt/etc/initramfs-tools/modules" <<'MODEOF'
# Forced into the initrd by build-slot-image.sh for VM compatibility.
virtio
virtio_pci
virtio_blk
virtio_scsi
virtio_net
virtio_ring
ext4
MODEOF

chroot "$WORK/slot-mnt" rm -f /usr/sbin/update-initramfs 2>/dev/null || true
chroot "$WORK/slot-mnt" /usr/bin/dpkg-divert --rename --remove \
    /usr/sbin/update-initramfs >/dev/null 2>&1 || true
chroot "$WORK/slot-mnt" env \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    /usr/sbin/update-initramfs -c -k "$KVER" >/dev/null 2>&1

if [ ! -f "$WORK/slot-mnt/boot/initrd.img-$KVER" ]; then
    echo "ERROR: update-initramfs did NOT produce /boot/initrd.img-$KVER" >&2
    ls -la "$WORK/slot-mnt/boot/" >&2
    exit 1
fi

umount "$WORK/slot-mnt/proc"
umount "$WORK/slot-mnt/sys"
umount "$WORK/slot-mnt/dev"

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
