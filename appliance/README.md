# SpatiumDDI OS Appliance

Self-contained bootable image: Debian 13 (trixie) amd64 + k3s +
SpatiumDDI's container set baked in as containerd-format `.tar.zst`
archives, wired together so the operator gets a working web UI on
first boot.

**Issue #183 Phase 7 (2026-05-16):** the appliance is **k3s-only**.
The pre-Phase-7 docker + docker-compose stack is gone; pods are
managed by k3s's bundled helm-controller via the `spatiumddi-
appliance` Helm chart. The supervisor runs as a privileged DaemonSet
pod; service roles (DNS / DHCP) come up via per-role chart values
the supervisor PATCHes onto the chart on heartbeat.

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
- **k3s** (pinned via `K3S_VERSION` in the top-level Makefile; baked
  as a single static binary at `/usr/local/bin/k3s` with `kubectl`,
  `crictl`, `ctr` as symlinks)
- `kubectl` pre-`KUBECONFIG`'d for admin + root login shells
- **Helm chart** at `/usr/lib/spatiumddi/charts/spatiumddi-appliance
  .tgz` — applied by k3s's helm-controller on first boot via the
  bootstrap manifest the firstboot orchestrator writes into
  `/var/lib/rancher/k3s/server/manifests/`
- **Preloaded containerd image set** in
  `/var/lib/rancher/k3s/agent/images/*.tar.zst` (k3s auto-imports
  on first start, no docker-load shell-out needed)
- First-boot orchestrator (`/usr/local/bin/spatiumddi-firstboot`)
  that generates secrets, renders the bootstrap HelmChart manifest,
  and waits for kubeapi `/readyz`

## Build prerequisites

- **Docker** with privileged-container support on the **build host
  only** — the appliance itself ships zero docker. mkosi + qemu-
  utils + apt keyring + grub variants live inside the published
  builder image (`ghcr.io/spatiumddi/appliance-builder:latest`).
  The bake step uses host docker to `docker save` SpatiumDDI's
  service images into containerd-readable `.tar.zst` archives.
- **helm** + **zstd** on the build host — `make appliance-bake-
  chart` packages the Helm chart; `appliance/scripts/bake-images
  .sh` compresses image archives.
- ~2 GiB free disk in the build directory.

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

### Phase 2 — tri-mode hybrid live ISO

After Phase 1 produces a raw image, wrap it as a hybrid live ISO:

```sh
make appliance-iso
```

Output: `appliance/build/spatiumddi-appliance_0.1.0.iso` (~260 MiB —
the rootfs is squashfs-compressed with xz, much smaller than the
underlying raw).

The script extracts the kernel + initrd that mkosi staged alongside
the raw, mounts the raw's root partition by GPT type GUID
(`4F68BCE3-…` = root-x86-64), builds a squashfs of it, and drives
`grub-mkrescue` to produce a tri-mode hybrid ISO:

| Boot path     | Mechanism                                           |
|---            |---                                                  |
| BIOS-CD       | El Torito → `boot/grub/i386-pc/eltorito.img`        |
| UEFI-CD       | El Torito alt-boot → `/efi.img` (FAT ESP with grub) |
| USB-`dd`'d    | Hybrid MBR + GPT, boots via either MBR (BIOS) or GPT (UEFI) |

At runtime, **live-boot** (baked into the appliance's initrd by
`mkosi.conf` Packages=) detects the boot medium, loop-mounts
`/live/filesystem.squashfs` from the ISO, and overlays it with a
tmpfs so writes work in RAM. The `spatiumddi-firstboot` service
then runs the same way as on the qcow2 (docker compose up, wait for
`/health/live`).

Verify the boot records with `xorriso -indev <iso> -report_el_torito plain`.

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
