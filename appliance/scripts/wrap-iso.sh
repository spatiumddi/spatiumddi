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

# #1026 — which architecture's image are we wrapping? Passed in by the
# Makefile rather than sniffed from ``uname -m``: this script runs in a
# builder container that may be a different architecture from the image
# it is wrapping (that is the whole point of #991's cross-build), so the
# build host's own architecture is not the answer.
APPLIANCE_ARCH="${APPLIANCE_ARCH:-amd64}"
case "$APPLIANCE_ARCH" in
    amd64|x86_64|x86-64) APPLIANCE_ARCH=amd64 ;;
    arm64|aarch64)       APPLIANCE_ARCH=arm64 ;;
    *)
        echo "ERROR: unsupported APPLIANCE_ARCH '$APPLIANCE_ARCH' (amd64|arm64)" >&2
        exit 1
        ;;
esac

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

# Find the root partition by GPT type GUID. mkosi's x86-64 layout puts:
#   p1: ESP (FAT32)             type C12A7328-F81F-11D2-BA4B-00A0C93EC93B
#   p2: BIOS Boot Partition     type 21686148-6449-6E6F-744E-656564454649
#   p3: root-x86-64             type 4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709
# Locating by GUID instead of position survives layout changes in
# future mkosi versions — and on arm64 the BIOS Boot partition is
# absent entirely, so position would have been wrong there anyway.
#
# #1026 — the root type GUID is per-architecture, from the Discoverable
# Partitions Specification. This is why the arch has to be passed in:
# the two images differ in exactly this field, and looking for the wrong
# one fails with "could not locate root partition" on a perfectly good
# image.
case "$APPLIANCE_ARCH" in
    amd64) ROOT_TYPE_GUID='4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709' ;;  # root-x86-64
    arm64) ROOT_TYPE_GUID='B921B045-1DF0-41C3-AF44-4C6F280D3FAE' ;;  # root-arm64
esac
echo "→ Wrapping an $APPLIANCE_ARCH image (root type $ROOT_TYPE_GUID)" 

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
# #553 — trailing ``|| true``: under ``set -o pipefail`` the ``grep -vE``
# returns 1 when it filters EVERY line (all output was I:/firmware noise),
# which would abort a step that actually succeeded. The real
# success/failure check is the initrd.img existence test just below.
chroot "$MOUNT_DIR" update-initramfs -c -k "$KVER" 2>&1 | grep -vE "^(I:|W: Possible missing firmware)" | tail -10 || true

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

# Quiet-console flags for the install + live entries: without these
# the kernel + systemd spam type=1130/1131 audit records and unit
# start/stop status onto tty1, which scrolls over the whiptail
# wizard the operator is trying to read. The combination:
#   quiet                       -- suppress most boot-time prints
#   loglevel=3                  -- only ERR + worse to console
#   audit=0                     -- disable kernel audit subsystem
#                                  (no auditd shipped; messages
#                                  default to /dev/console)
#   systemd.show_status=false   -- systemd doesn't narrate units
set quietkbd="quiet loglevel=3 audit=0 systemd.show_status=false"

menuentry "Install SpatiumDDI to disk" {
    linux /live/vmlinuz $common $quietkbd spatium-mode=install
    initrd /live/initrd.img
}

# spatium-mode=live boots the OS from CD/USB WITHOUT starting the
# SpatiumDDI stack (k3s + firstboot are gated ``!boot=live``) — it's a
# rescue / diagnostics environment, NOT a product demo. Used to recover
# an unbootable install (inspect/mount the installed disk, read logs,
# re-run the installer). The ``spatium-mode=live`` keyword is unchanged
# (it's wired into many unit ConditionKernelCommandLine= gates); only
# the operator-facing labels + banner describe it accurately now.
menuentry "Rescue / diagnostics shell (run from CD/USB, no install)" {
    linux /live/vmlinuz $common $quietkbd spatium-mode=live
    initrd /live/initrd.img
}

menuentry "Rescue / diagnostics shell (verbose, for debugging)" {
    linux /live/vmlinuz $common spatium-mode=live
    initrd /live/initrd.img
}
EOF

# ── Build the ISO ─────────────────────────────────────────────────────────────
# On x86-64 grub-mkrescue handles:
#   - BIOS El Torito catalog + i386-pc eltorito.img
#   - UEFI El Torito alt-boot + x86_64-efi FAT image
#   - Hybrid MBR/GPT for USB-dd boot
#   - ISO9660 + Joliet + Rock Ridge for cross-OS readability
#
# On arm64 it is UEFI El Torito only — no BIOS catalog, no hybrid MBR,
# because there is no i386-pc target to build one from.
#
# #1026 — the platform is FORCED with ``-d`` rather than left to
# grub-mkrescue's auto-detection, and that is the load-bearing part.
# The builder image now carries the modules for i386-pc, x86_64-efi AND
# arm64-efi so it can build either ISO; left to itself, grub-mkrescue
# embeds every platform it finds, so an "arm64" ISO would silently come
# out carrying x86 boot paths as well — bootable on the wrong machine
# and confusing on the right one.
case "$APPLIANCE_ARCH" in
    amd64) GRUB_PLATFORMS="i386-pc x86_64-efi" ;;
    arm64) GRUB_PLATFORMS="arm64-efi" ;;
esac
GRUB_DIR_ARGS=()
for plat in $GRUB_PLATFORMS; do
    if [ ! -d "/usr/lib/grub/$plat" ]; then
        echo "ERROR: /usr/lib/grub/$plat is missing from this builder image." >&2
        echo "       An $APPLIANCE_ARCH ISO cannot be built without it — refusing" >&2
        echo "       rather than producing an ISO with no boot path for the" >&2
        echo "       architecture it claims to be." >&2
        exit 1
    fi
    GRUB_DIR_ARGS+=(-d "/usr/lib/grub/$plat")
done

echo "→ Running grub-mkrescue ($APPLIANCE_ARCH: $GRUB_PLATFORMS)…"
# `-volid SPATIUMDDI` is xorriso-native (sets the ISO9660 volume
# label). No `-appid` — that's mkisofs-compat syntax and grub-mkrescue
# invokes xorriso in native mode.
grub-mkrescue "${GRUB_DIR_ARGS[@]}" -o "$ISO" "$ISO_ROOT" \
    -- \
    -volid 'SPATIUMDDI'

echo ""
echo "✓ ISO: $ISO"
ls -lh "$ISO"
