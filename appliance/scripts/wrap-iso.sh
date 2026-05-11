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

# ── Mount the root partition rw (we need to regenerate the initrd) ────────────
# Release any stale loop devices left over from a previous failed run
# against the same file. Without this, mount errors with
# "overlapping loop device exists" on retry.
losetup -j "$RAW" 2>/dev/null | cut -d: -f1 | xargs -r -n1 losetup -d 2>/dev/null || true

MOUNT_DIR="$WORKDIR/mnt"
mkdir -p "$MOUNT_DIR"
# Mount rw — we chroot in below and run update-initramfs so the
# initrd we ship in the ISO has live-boot hooks. mkosi's own initrd
# (next to the raw on disk) is a systemd-style minimal rootfs purpose-
# built for disk boot and ignores Debian initramfs-tools hooks
# entirely. It stays as the qcow2's disk-boot initrd; the ISO needs
# a different one.
mount -o rw,loop,offset=$ROOT_OFFSET,sizelimit=$ROOT_SIZE "$RAW" "$MOUNT_DIR"

# ── Regenerate the initrd with live-boot hooks inside the chroot ──────────────
echo "→ Regenerating initrd with live-boot hooks (chroot into rootfs)…"
for d in proc sys dev; do
    mount --bind "/$d" "$MOUNT_DIR/$d"
done
cleanup_chroot() {
    for d in dev sys proc; do
        umount "$MOUNT_DIR/$d" 2>/dev/null || true
    done
}
trap 'cleanup_chroot; cleanup' EXIT

# Pick the kernel version from /lib/modules/<kver>/. update-initramfs
# needs an explicit version when /boot/vmlinuz isn't there — mkosi
# strips the kernel from /boot during build (it stages vmlinuz
# separately for disk boot).
KVER=$(ls "$MOUNT_DIR/lib/modules" 2>/dev/null | head -1)
if [ -z "$KVER" ]; then
    echo "no kernel modules found in /lib/modules — can't regenerate initrd" >&2
    exit 1
fi
echo "→ Kernel version: $KVER"

# update-initramfs -c -k <ver> creates /boot/initrd.img-<ver> using
# every initramfs-tools hook now present in the rootfs (including
# /usr/share/initramfs-tools/scripts/live from the live-boot package).
chroot "$MOUNT_DIR" update-initramfs -c -k "$KVER" 2>&1 | grep -vE "^(I:|W: Possible missing firmware)" | tail -10

if [ ! -f "$MOUNT_DIR/boot/initrd.img-$KVER" ]; then
    echo "update-initramfs did not produce /boot/initrd.img-$KVER" >&2
    ls -la "$MOUNT_DIR/boot/" >&2
    exit 1
fi

# ── Kernel + initrd ───────────────────────────────────────────────────────────
# mkosi's staged vmlinuz (next to the raw) + the chroot-regenerated
# Debian initrd. Different artefacts, intentionally — mkosi's own
# .initrd is a systemd-style minimal-rootfs purpose-built for disk
# boot and ignores Debian initramfs-tools hooks. Using mkosi's
# vmlinuz is fine since both initrds target the same kernel ABI.
cp "$KERNEL" "$ISO_ROOT/live/vmlinuz"
cp "$MOUNT_DIR/boot/initrd.img-$KVER" "$ISO_ROOT/live/initrd.img"
echo "→ Live initrd: $(ls -lh "$ISO_ROOT/live/initrd.img" | awk '{print $5}')"

# ── Squashfs of the rootfs ────────────────────────────────────────────────────
# Unmount the bind mounts before snapshotting — squashfs would
# otherwise descend into /proc and /sys and try to pack their
# contents, which fails on synthetic kernel files.
for d in dev sys proc; do
    umount "$MOUNT_DIR/$d" 2>/dev/null || true
done
trap cleanup EXIT

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
set timeout=5
set default=0

# Serial console mirror for headless boots (IPMI SoL, Proxmox
# serial0, RPi UART, embedded boards). The same grub menu shows on
# both VGA and ttyS0 @115200,8n1, and the kernel cmdline routes its
# own messages + getty to both consoles.
serial --unit=0 --speed=115200
terminal_input  console serial
terminal_output console serial

# Shared kernel cmdline. spatium-mode= picks which boot-time
# service takes over tty1 (install wizard vs live banner).
# cloud-init=disabled skips cloud-init in both modes since neither
# has a NoCloud datasource attached.
set common="boot=live components cloud-init=disabled console=tty0 console=ttyS0,115200n8"

menuentry "Install SpatiumDDI to disk" {
    linux /live/vmlinuz $common spatium-mode=install
    initrd /live/initrd.img
}

menuentry "Try SpatiumDDI live (run from CD/USB without installing)" {
    linux /live/vmlinuz $common spatium-mode=live quiet
    initrd /live/initrd.img
}

menuentry "Try SpatiumDDI live (verbose)" {
    linux /live/vmlinuz $common spatium-mode=live
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
