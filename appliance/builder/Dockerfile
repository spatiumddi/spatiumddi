# SpatiumDDI OS Appliance — build container.
#
# Bakes mkosi + the host tools mkosi shells out to, so `make appliance`
# only requires Docker. Same image is used by the developer-laptop
# `make appliance` flow and the GitHub-Actions release pipeline.
#
# Run pattern:
#   docker run --rm --privileged \
#       -v $PWD/appliance:/work \
#       ghcr.io/spatiumddi/appliance-builder:latest \
#       --output-directory=build --force build
#
# Privileged is required: mkosi creates loop devices, uses unshare for
# chroot isolation, and bind-mounts apt's package cache into the
# buildroot. Same constraint as packer, diskimage-builder, live-build.

FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive

# Versions are pinned by the trixie release; bumping mkosi is a
# debian-archive update inside this image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        mkosi \
        qemu-utils \
        debian-archive-keyring \
        ca-certificates \
        cryptsetup-bin \
        squashfs-tools \
        erofs-utils \
        dosfstools \
        e2fsprogs \
        btrfs-progs \
        kmod \
        cpio \
        zstd \
        xz-utils \
        gnupg \
        openssl \
        jq \
        python3-cryptography \
        python3-pefile \
        systemd-boot-tools \
        systemd-repart \
        systemd-ukify \
        grub2-common \
        grub-pc-bin \
        grub-efi-amd64-bin \
        # Phase 2 — true hybrid live ISO. grub-mkrescue (in
        # grub-common) builds the ISO with BIOS El Torito + UEFI El
        # Torito + USB-dd-bootable hybrid MBR. grub-pc-bin supplies
        # the i386-pc modules + BIOS eltorito.img. grub-efi-amd64-bin
        # supplies the x86_64-efi modules for the UEFI El Torito
        # image. squashfs-tools for the rootfs filesystem; xorriso is
        # the actual ISO writer.
        xorriso \
        squashfs-tools \
        # mtools provides mformat — grub-mkrescue uses it to build
        # the FAT12 ESP image embedded as the UEFI El Torito boot
        # image. Without it the ISO produces no UEFI boot path.
        mtools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
ENTRYPOINT ["mkosi"]
CMD ["--help"]
