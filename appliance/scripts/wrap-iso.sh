#!/bin/bash
# Wrap the Phase 1 raw appliance image as a hybrid USB/CD live ISO.
#
# Approach: extract the kernel + initrd that mkosi staged alongside
# the raw, mount the raw's root partition, squashfs it, then drive
# grub-mkrescue to produce a tri-mode hybrid ISO:
#
#   - BIOS-CD boot: grub i386-pc eltorito.img + BIOS El Torito catalog
#   - UEFI-CD boot: grub x86_64-efi binary in a FAT ESP image,
#     referenced by UEFI El Torito alt-boot
#   - USB-dd boot:  the same image written to a USB stick boots via
#     either MBR (BIOS) or GPT (UEFI)
#
# At runtime the live-boot package (baked into the appliance's
# initrd by mkosi.conf's Packages= block) detects the boot medium,
# loop-mounts /live/filesystem.squashfs from the ISO, and overlays it
# with a tmpfs so writes work in RAM. The all-in-one stack starts
# normally via the spatiumddi-firstboot systemd unit.
#
# Usage (inside the appliance-builder container):
#   wrap-iso.sh <raw_image> <output_iso>

set -euo pipefail

RAW="${1:?usage: $0 <raw_image> <output_iso>}"
ISO="${2:?usage: $0 <raw_image> <output_iso>}"

[ -f "$RAW" ] || { echo "raw image not found: $RAW" >&2; exit 1; }

# mkosi stages the kernel + initrd next to the raw with matching
# basenames. Prefer those — extracting from the raw would require
# loop+mount and adds 30 s of work for the same bytes.
KERNEL="${RAW%.raw}.vmlinuz"
INITRD="${RAW%.raw}.initrd"

WORKDIR=$(mktemp -d)
MOUNT_DIR=
cleanup() {
    if [ -n "$MOUNT_DIR" ] && mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
        umount "$MOUNT_DIR" || true
    fi
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

# Find the root partition by GPT type GUID. mkosi's layout puts:
#   p1: ESP (FAT32)             type C12A7328-F81F-11D2-BA4B-00A0C93EC93B
#   p2: BIOS Boot Partition     type 21686148-6449-6E6F-744E-656564454649
#   p3: root-x86-64             type 4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709
# Locating by GUID instead of position survives layout changes in
# future mkosi versions.
ROOT_TYPE_GUID='4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709'

ROOT_INFO=$(sfdisk --json "$RAW" | jq -r --arg t "$ROOT_TYPE_GUID" '
    .partitiontable.partitions[] | select(.type == $t) | "\(.start) \(.size)"
')
ROOT_OFFSET_SECTORS=$(echo "$ROOT_INFO" | awk '{print $1}')
ROOT_SIZE_SECTORS=$(echo "$ROOT_INFO" | awk '{print $2}')

if [ -z "$ROOT_OFFSET_SECTORS" ] || [ -z "$ROOT_SIZE_SECTORS" ]; then
    echo "could not locate root partition (type $ROOT_TYPE_GUID) in $RAW:" >&2
    sfdisk -d "$RAW" >&2
    exit 1
fi

ROOT_OFFSET=$((ROOT_OFFSET_SECTORS * 512))
ROOT_SIZE=$((ROOT_SIZE_SECTORS * 512))
echo "→ Root partition: offset=$ROOT_OFFSET ($((ROOT_OFFSET / 1024 / 1024)) MiB), size=$((ROOT_SIZE / 1024 / 1024)) MiB"

ISO_ROOT="$WORKDIR/iso"
mkdir -p "$ISO_ROOT/live" "$ISO_ROOT/boot/grub"

# ── Mount the root partition (offset+sizelimit) ───────────────────────────────
# Release any stale loop devices left over from a previous failed run
# against the same file. Without this, mount errors with
# "overlapping loop device exists" on retry.
losetup -j "$RAW" 2>/dev/null | cut -d: -f1 | xargs -r -n1 losetup -d 2>/dev/null || true

MOUNT_DIR="$WORKDIR/mnt"
mkdir -p "$MOUNT_DIR"
mount -o ro,loop,offset=$ROOT_OFFSET,sizelimit=$ROOT_SIZE "$RAW" "$MOUNT_DIR"

# ── Kernel + initrd ───────────────────────────────────────────────────────────
if [ -f "$KERNEL" ] && [ -f "$INITRD" ]; then
    cp "$KERNEL" "$ISO_ROOT/live/vmlinuz"
    cp "$INITRD" "$ISO_ROOT/live/initrd.img"
else
    echo "kernel/initrd not staged beside raw — extracting from /boot" >&2
    cp "$MOUNT_DIR"/boot/vmlinuz-* "$ISO_ROOT/live/vmlinuz"
    cp "$MOUNT_DIR"/boot/initrd.img-* "$ISO_ROOT/live/initrd.img"
fi

# ── Squashfs of the rootfs ────────────────────────────────────────────────────

echo "→ Building squashfs (this is the slow step — ~2 min)…"
# -comp xz: tightest compression, optimal for read-mostly live boot
# -no-progress: silence per-percent updates in CI logs
# -e boot: exclude /boot since kernel + initrd are already in /live/
mksquashfs "$MOUNT_DIR" "$ISO_ROOT/live/filesystem.squashfs" \
    -comp xz \
    -no-progress \
    -e boot \
    -e tmp \
    -e var/log \
    -e var/cache/apt

# ── grub.cfg ──────────────────────────────────────────────────────────────────
cat > "$ISO_ROOT/boot/grub/grub.cfg" <<'EOF'
set timeout=3
set default=0

menuentry "SpatiumDDI Appliance — Live" {
    linux /live/vmlinuz boot=live components quiet
    initrd /live/initrd.img
}

menuentry "SpatiumDDI Appliance — Live (verbose)" {
    linux /live/vmlinuz boot=live components
    initrd /live/initrd.img
}
EOF

# ── Build the ISO ─────────────────────────────────────────────────────────────
# grub-mkrescue handles:
#   - BIOS El Torito catalog + i386-pc eltorito.img
#   - UEFI El Torito alt-boot + x86_64-efi FAT image
#   - Hybrid MBR/GPT for USB-dd boot
#   - ISO9660 + Joliet + Rock Ridge for cross-OS readability
echo "→ Running grub-mkrescue…"
# `-volid SPATIUMDDI` is xorriso-native (sets the ISO9660 volume
# label). No `-appid` — that's mkisofs-compat syntax and grub-mkrescue
# invokes xorriso in native mode.
grub-mkrescue -o "$ISO" "$ISO_ROOT" \
    -- \
    -volid 'SPATIUMDDI'

echo ""
echo "✓ ISO: $ISO"
ls -lh "$ISO"
