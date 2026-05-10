#!/bin/bash
# Wrap the Phase 1 raw appliance image as a hybrid USB/CD ISO.
#
# Strategy: produce an ISO9660 wrapper whose appended GPT partition
# is the raw image itself. The same ISO file:
#
#   - mounts as CD-ROM on a host system (ISO9660 filesystem visible)
#   - boots in UEFI mode via the appended GPT partition
#   - boots from USB when `dd`'d (the appended partition's grub takes over)
#   - boots in BIOS mode on hypervisors with UEFI CSM (most post-2012)
#
# Phase 2 MVP limitation: traditional El Torito BIOS-CD boot is NOT
# wired up — `-isohybrid-mbr` and `-append_partition` are mutually
# exclusive in xorriso (the MBR boot loader can only refer to data
# inside the ISO9660 filesystem, not the appended partition). Real
# El Torito + GPT hybrid is Phase 2.x territory and needs kernel +
# initrd + grub.cfg copied into the ISO tree separately.
#
# Usage (inside the appliance-builder container):
#   wrap-iso.sh <raw_image> <output_iso>

set -euo pipefail

RAW="${1:?usage: $0 <raw_image> <output_iso>}"
ISO="${2:?usage: $0 <raw_image> <output_iso>}"

if [ ! -f "$RAW" ]; then
    echo "raw image not found: $RAW" >&2
    exit 1
fi

# Empty directory that becomes the visible ISO9660 tree. The actual
# boot-relevant bytes live in the appended partition; the tree just
# carries the volume label + a NOTICE file for hosts that mount the
# ISO and look around.
ISO_ROOT=$(mktemp -d)
trap 'rm -rf "$ISO_ROOT"' EXIT

cat > "$ISO_ROOT/README.txt" <<EOF
SpatiumDDI live appliance — hybrid USB/CD ISO.

To boot:
  - Burn to a USB stick:  sudo dd if=spatiumddi-appliance.iso of=/dev/sdX bs=4M conv=fsync
  - Or: attach as CD-ROM in your hypervisor (Proxmox / VMware / Hyper-V / QEMU)
  - Or: PXE-chainload the appended raw partition

The appliance auto-boots, generates secrets, and brings up the all-in-one
DDI stack on first power-on. Default web-UI login: admin / admin
(forces password change on first login).

Docs: https://spatiumddi.github.io/spatiumddi/
EOF

xorriso -as mkisofs \
    -iso-level 3 \
    -full-iso9660-filenames \
    -joliet \
    -joliet-long \
    -rational-rock \
    -volid 'SPATIUMDDI' \
    -appid 'SpatiumDDI Appliance' \
    -publisher 'SpatiumDDI Contributors' \
    -partition_offset 16 \
    -appended_part_as_gpt \
    -append_partition 2 0xef "$RAW" \
    -o "$ISO" \
    "$ISO_ROOT"

echo ""
echo "✓ ISO: $ISO"
ls -lh "$ISO"
