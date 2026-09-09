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
#
# ``dpkg --add-architecture amd64`` is what lets this one Dockerfile
# build natively on BOTH architectures (#991). ``grub-pc-bin`` and
# ``grub-efi-amd64-bin`` are amd64-only packages — they ship no code that
# runs on the build host, only the x86 GRUB modules under
# ``/usr/lib/grub/{i386-pc,x86_64-efi}`` that grub-mkrescue embeds in the
# ISO — so on arm64 they install as a foreign architecture alongside the
# native ``grub2-common`` / ``grub-mkrescue``. On amd64 the ``:amd64``
# qualifiers name the native architecture and nothing changes.
#
# **Why an arm64 builder has to exist at all.** mkosi cross-builds x86-64
# quite happily, but the builder itself cannot be *emulated*: under
# qemu-user / Rosetta mkosi dies immediately on ``mount_setattr(2)``
# ("Function not implemented"), part of the new mount API that the syscall
# translation layers do not implement. The kernel in the Docker Desktop VM
# is fine; the translation layer is not, and no amount of ``--privileged``
# helps. So the builder runs NATIVE and mkosi cross-builds — which is also
# why ``build-appliance-builder.yml`` publishes both platforms.
# #1026 — arm64 is added the same way, and for the same reason: an
# appliance ISO for EITHER architecture can now be built on EITHER host,
# so grub-mkrescue needs both target platforms' modules present whichever
# way round the build is running. ``grub-efi-arm64-bin`` ships only
# ``/usr/lib/grub/arm64-efi/*`` — no host-executable code — so it installs
# as a foreign architecture exactly like the x86 pair does.
RUN dpkg --add-architecture amd64 \
    && dpkg --add-architecture arm64 \
    && apt-get update && apt-get install -y --no-install-recommends \
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
        grub-pc-bin:amd64 \
        grub-efi-amd64-bin:amd64 \
        grub-efi-arm64-bin:arm64 \
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
        # #1026 — ``efi.img`` for an arm64 ISO is assembled the same way,
        # so mtools covers both; what arm64 additionally needs is the
        # arm64-efi module directory named above.
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
ENTRYPOINT ["mkosi"]
CMD ["--help"]
