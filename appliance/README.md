# SpatiumDDI OS Appliance — Phase 1 (Debian 13 amd64 qcow2 MVP)

Self-contained bootable image: Debian 13 (trixie) amd64 + Docker +
SpatiumDDI's `ghcr.io` container set, wired together so the operator
gets a working web UI on first boot.

> **Why Debian, not Alpine?** mkosi ≥ 23 dropped Alpine as a supported
> distribution. Phase 1's job is the proving ground for cloud-init,
> boot orchestration, and the build pipeline — keeping it on a
> mkosi-native distro avoids dragging in `alpine-make-vm-image` as a
> second build tool. The bundled `ghcr.io` container images stay
> Alpine-based; only the host OS shifts. APPLIANCE.md already lists
> Debian as Option B for the bare-metal / VM track, so the spec
> covers it.

> **Status:** Phase 1 of the appliance roadmap (issue
> [#134](https://github.com/spatiumddi/spatiumddi/issues/134)).
> Proving ground — feedback drives Phases 2–6 (ISO installer, arm64 +
> Pi, role-split images, cloud images, fleet management).
>
> Spec context: [`docs/deployment/APPLIANCE.md`](../docs/deployment/APPLIANCE.md).

## What it ships

- Debian 13 (trixie) amd64, `linux-image-cloud-amd64` kernel
- systemd + cloud-init (NoCloud datasource)
- `docker.io` + `docker-compose-v2`
- All-in-one stack at `/usr/local/share/spatiumddi/docker-compose.yml`:
  control plane (api + worker + beat + migrate + frontend) + Postgres
  + Redis + BIND9 + Kea
- First-boot orchestrator (`/usr/local/bin/spatiumddi-firstboot`)
  that generates secrets, pulls images, brings the stack up, and waits
  for `/health/live`

## Build prerequisites

- **Docker** with privileged-container support — that's the only host
  tool. mkosi + qemu-utils + apt keyring + grub variants live inside
  the published builder image
  (`ghcr.io/spatiumddi/appliance-builder:latest`)
- ~2 GiB free disk in the build directory

The build runs `docker run --privileged` because mkosi needs loop
devices, kernel namespaces, and bind-mounts to bootstrap the rootfs.
Same constraint as `packer`, `live-build`, `diskimage-builder`.

## Build

From the repo root:

```sh
make appliance
```

Pulls `ghcr.io/spatiumddi/appliance-builder:latest`, runs mkosi
inside it, and writes:

- `appliance/build/spatiumddi-appliance_0.1.0.raw`   (2.1 GiB sparse, 1.1 GiB consumed)
- `appliance/build/spatiumddi-appliance_0.1.0.qcow2` (~790 MiB compressed)

~5–10 min on a modern laptop with warm caches; first build is slower
because mkosi populates its apt cache.

### Iterating on the builder image

If you're modifying `appliance/builder/Dockerfile` (e.g. bumping mkosi)
and don't want to publish first:

```sh
make appliance-builder                                   # builds spatiumddi-appliance-builder:dev
make appliance APPLIANCE_BUILDER=spatiumddi-appliance-builder:dev
```

### Phase 2 — hybrid USB/CD ISO

After Phase 1 produces a raw image, wrap it as a hybrid ISO:

```sh
make appliance-iso
```

Output: `appliance/build/spatiumddi-appliance_0.1.0.iso` (~2.1 GiB —
ISOs don't compress, the Phase 1 qcow2's compression is lost on wrap).

The ISO has a hybrid GPT layout (verified with `fdisk -l`):
- Partition 1 (~70 KiB): ISO9660 metadata
- Partition 2 (~2.1 GiB): EFI System partition holding the entire Phase 1 raw image
- Partition 3 (~300 KiB): GPT backup tail

Boots in:
- **UEFI** mode (firmware reads GPT, finds the EFI System partition,
  chains into the appliance's grub) — Hyper-V, modern QEMU, AWS/Azure,
  most post-2012 hypervisors
- **USB** when `dd`'d (`sudo dd if=spatiumddi-appliance_0.1.0.iso of=/dev/sdX bs=4M conv=fsync`)
- **Hypervisors with UEFI CSM** (almost all of them) — Proxmox, VMware

Phase 2.x gap: classic El Torito BIOS-CD boot needs kernel + initrd +
grub.cfg copied into the ISO9660 tree as a separate boot image (`-b`
in xorriso). xorriso refuses `-isohybrid-mbr` + `-append_partition`
together, so we either wrap the raw (current — UEFI/USB only) or
build a true live ISO from scratch with squashfs (later).

### Cleaning

```sh
make appliance-clean
```
(may need sudo — mkosi's build artifacts are root-owned).

## Boot

The qcow2 is **hybrid BIOS + UEFI** — the same image boots on both
firmware modes. Pick whichever your hypervisor defaults to.

**BIOS (default for QEMU / Proxmox / older libvirt):**
```sh
qemu-system-x86_64 -enable-kvm -m 4G -smp 2 \
    -drive file=appliance/build/spatiumddi-appliance_0.1.0.qcow2,if=virtio \
    -drive file=appliance/cloud-init/cidata.iso,if=virtio,format=raw,readonly=on \
    -nic user,hostfwd=tcp::8080-:80,hostfwd=tcp::2222-:22 \
    -nographic
```

**UEFI (Hyper-V, modern QEMU, AWS/Azure cloud — Phase 5):**
```sh
qemu-system-x86_64 -enable-kvm -m 4G -smp 2 \
    -bios /usr/share/ovmf/OVMF.fd \
    -drive file=appliance/build/spatiumddi-appliance_0.1.0.qcow2,if=virtio \
    ...
```

(Build the `cidata.iso` first — see [cloud-init/README.md](cloud-init/README.md).)

After ~30 s for boot + ~60–120 s for the stack to come up, the web UI
is at <http://localhost:8080/>. Default login `admin / admin` (forces
password change on first login).

## Layout

```
appliance/
├── README.md                # this file
├── mkosi.conf               # top-level mkosi config (Debian trixie / amd64 / hybrid grub)
├── mkosi.postinst           # post-install hook: enable services, hardening (uses $BUILDROOT)
├── mkosi.extra/             # files copied into the rootfs after package install
│   ├── etc/
│   │   ├── systemd/system/spatiumddi-firstboot.service  # systemd unit
│   │   ├── motd                                         # console branding
│   │   └── spatiumddi/README                            # /etc/spatiumddi/ contract
│   └── usr/local/
│       ├── bin/
│       │   ├── spatiumddi-firstboot        # boot-time orchestrator
│       │   └── spatiumddi-stack-status     # operator status command
│       └── share/spatiumddi/
│           └── docker-compose.yml          # all-in-one stack
├── builder/
│   ├── Dockerfile           # the appliance-builder image (mkosi + qemu-img + xorriso + ...)
│   └── .dockerignore
├── scripts/
│   └── wrap-iso.sh          # Phase 2 — wrap raw image as hybrid USB/CD ISO via xorriso
└── cloud-init/
    ├── README.md
    ├── user-data.example
    └── meta-data.example
```

## Customising the stack

Compose file: `mkosi.extra/usr/local/share/spatiumddi/docker-compose.yml`.
Track tag-for-tag with the top-level `docker-compose.yml`; appliance
deltas should stay surgical (no profile clutter, no docker-volume
opt-ins, no host-socket mounts).

To pin a release tag instead of `:latest`, drop a
`/etc/spatiumddi/release` file via cloud-init `write_files` — see
[cloud-init/README.md](cloud-init/README.md).

## What this MVP does NOT do (yet)

Tracked in [#134](https://github.com/spatiumddi/spatiumddi/issues/134):

- **Classic El Torito BIOS-CD boot** (Phase 2.x) — current ISO is
  UEFI/USB hybrid, doesn't boot on pure-BIOS-CD hypervisors
- **arm64 / Raspberry Pi** (Phase 3) — builder image is amd64-only
- **Role-split images** (Phase 4) — single all-in-one only
- **Cloud images** (Phase 5) — no AWS/Azure/GCP datasource testing
- **Fleet management + A/B updates** (Phase 6)

Other gaps Phase 1 surfaces but doesn't close:
- No SBOM / GPG signing on the produced qcow2
- No `make appliance` CI workflow yet (the builder-image publish
  workflow ships, but there's no nightly/per-tag artifact build)
- Stack health waits on `/health/live` only — does not verify DNS /
  DHCP agents finished registration
- No host-level Prometheus exporters baked in
- Debian's `docker-compose` (Python v1) — switch to upstream Docker
  CE + `docker compose` plugin in Phase 1.x
