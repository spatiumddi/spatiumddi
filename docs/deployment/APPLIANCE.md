# OS Appliance Deployment Specification

## Overview

SpatiumDDI can be shipped as a **self-contained OS appliance image** — a bootable image where the OS, all services, and the SpatiumDDI application are pre-installed and pre-configured. This allows deployment without any prior OS or container runtime setup: download, boot, configure via web UI, done.

---

## 1. Base OS Selection

### Decision (2026-05): Debian for the appliance, Alpine for containers

| Use Case | Base OS | Rationale |
|---|---|---|
| **Container images** (Docker/K8s) | Alpine Linux 3.x | Minimal footprint (~5MB base), musl libc, APK packages, Docker-native |
| **OS appliance** (qcow2 / ISO / cloud) | **Debian 13 "Trixie" (Stable)** | mkosi-supported (Alpine support was dropped from mkosi ≥ 23), broad hardware support, mature installer, glibc, systemd-native |

The earlier "dual-track Alpine + Debian" plan got narrowed once the
build tool was chosen. mkosi 25 (current Debian-trixie package)
dropped Alpine as a supported `Distribution=`, and the alternatives
(`alpine-make-vm-image`, raw `mkimage.sh`) would have meant carrying
two divergent build pipelines for the same artifact set. Debian gives
us one toolchain across qcow2 / ISO / cloud images and aligns with
APPLIANCE.md's pre-existing Option B. The **bundled service
containers stay Alpine-based** — only the appliance host OS shifts.

---

### Option A: Alpine Linux

**Pros:**
- Extremely small base image (~5MB Docker, ~130MB full install)
- `musl libc` — no GNU libc licensing concerns beyond the kernel itself
- `OpenRC` init system (lightweight, no systemd complexity)
- `APK` package manager — fast, reproducible
- Native Docker base image — our container images already use it
- BusyBox userland — familiar to embedded/appliance developers
- All packages and Alpine itself are MIT licensed (tools) + GPL2 (kernel)

**Cons:**
- `musl libc` can cause compatibility issues with some Python C extensions (rare but real)
- Smaller community than Debian/Ubuntu
- `OpenRC` differs from systemd — most guides assume systemd
- Hardware support can lag (kernel version behind Debian)
- ISC Kea and BIND9 packages exist but may be older versions

**Alpine License Note:**
- Alpine Linux itself: MIT license for Alpine-specific tooling
- The Linux kernel: GPL v2 (copyleft — source must be available, but does NOT affect your application code)
- APK packages: each package has its own license (Python: PSF, BIND: MPL 2.0, Kea: MPL 2.0)
- **Your application code is not affected by GPL2** — GPL2 does not extend to user-space applications that merely run on the kernel. It only requires kernel source availability.
- **No legal barrier** to shipping a closed or open-source appliance on Alpine.

---

### Option B: Debian 12 "Bookworm" Stable

**Pros:**
- Widest hardware driver support (NIC drivers, storage controllers, etc.)
- `glibc` — full compatibility with all Python C extensions
- `systemd` — industry standard, best documentation
- `apt` with `stable` channel — predictable, LTS lifecycle
- ISC Kea and BIND9 both have well-maintained `.deb` packages
- Debian itself is 100% free software (DFSG-compliant)

**Cons:**
- Larger footprint (~300MB minimal install vs ~130MB Alpine)
- Slower package updates than Ubuntu
- Docker images are larger than Alpine-based equivalents

**Debian License Note:**
- Debian itself: Debian Free Software Guidelines (DFSG) — all core packages are open source
- Same kernel GPL2 note as above applies
- `glibc`: LGPL 2.1 — applications linking against it are **not** required to be GPL-licensed (LGPL is designed for this)
- **No legal barriers** to shipping a commercial or open-source appliance on Debian.

---

### Option C: FreeBSD (Considered, Not Recommended for Phase 1)

**Pros:**
- BSD license (2-clause or 3-clause) — maximally permissive
- Excellent networking stack (pf firewall, CARP for HA IPs)
- ZFS built-in
- Ports tree is comprehensive

**Cons:**
- No Linux kernel → Docker images don't run natively (need Linux compat layer or bhyve VMs)
- Python ecosystem has some friction on FreeBSD
- Kea DHCP and some DNS drivers have less testing on FreeBSD
- Smaller pool of operators familiar with FreeBSD vs Linux
- Cannot use existing Linux container images directly
- Significantly more complex appliance build process

**Recommendation:** Defer FreeBSD to a community contribution. It is architecturally possible but adds too much complexity for Phase 1.

---

## 2. Appliance Image Types

| Format | Tool | Target |
|---|---|---|
| `.iso` (bootable) | `live-build` (Debian) or `mkimage.sh` (Alpine) | Physical servers, VMs with ISO mount |
| `.qcow2` (QEMU/KVM) | `virt-builder` or `mkosi` | KVM, Proxmox, OpenStack |
| `.vmdk` (VMware) | Convert from qcow2 via `qemu-img` | VMware ESXi/vSphere |
| `.ova` (VMware) | `ovftool` wrapping vmdk | VMware vSphere deployment |
| `.vhd` (Hyper-V) | `qemu-img convert` | Microsoft Hyper-V |
| Docker image | Multi-stage `Dockerfile` | Docker / Kubernetes |

---

## 3. Appliance Build Process

### Build tool: `mkosi` (systemd project)

`mkosi` produces reproducible OS images from a declarative config. It handles:
- Base OS package installation
- Service configuration
- First-boot setup scripts
- Image format conversion

### Build runs inside a published builder container

The build's host dependencies (mkosi, qemu-utils, debian-archive-keyring,
grub-pc-bin + grub-efi-amd64-bin, python3-cryptography, …) live inside
`ghcr.io/spatiumddi/appliance-builder:latest`. The only host requirement
for `make appliance` is **Docker with privileged-container support**.
mkosi needs loop devices + namespaces + bind-mounts to bootstrap the
rootfs — same constraint as `packer`, `live-build`, `diskimage-builder`.

The builder image's `Dockerfile` lives at `appliance/builder/Dockerfile`
and republishes via `.github/workflows/build-appliance-builder.yml` on
changes to `appliance/builder/**`.

### Phase 1 (current — landed 2026-05)

```
make appliance
  ↓
docker pull ghcr.io/spatiumddi/appliance-builder:latest
  ↓
docker run --privileged appliance-builder
  → mkosi build → spatiumddi-appliance_0.1.0.raw   (2.1 GiB sparse)
  ↓
qemu-img convert -O qcow2
  → spatiumddi-appliance_0.1.0.qcow2  (~790 MiB)
```

Hybrid BIOS + UEFI boot via grub (`Bootable=yes`, `Bootloader=grub`,
`BiosBootloader=grub`). Same qcow2 boots on default-firmware QEMU/Proxmox
*and* UEFI Hyper-V/AWS/Azure.

### Future build pipeline (Phases 2–5)

```
trigger: tag push (CalVer)
  ↓
1. Reuse the existing image-build workflows
   - ghcr.io/spatiumddi/spatiumddi-api:<calver>
   - ghcr.io/spatiumddi/spatiumddi-frontend:<calver>
   - ghcr.io/spatiumddi/dns-{bind9,powerdns}:<calver>
   - ghcr.io/spatiumddi/dhcp-kea:<calver>
  ↓
2. Build appliance images via the builder container
   - Phase 1: amd64 qcow2 (all-in-one)
   - Phase 2: amd64 ISO installer
   - Phase 3: arm64 qcow2 + Raspberry Pi image
   - Phase 4: role-split (control / dns / dhcp)
   - Phase 5: cloud variants (AWS AMI / Azure VHD / GCP raw)
  ↓
3. Convert formats
   - qcow2 → vmdk, vhd, ova
  ↓
4. Sign images (cosign + GPG)
  ↓
5. Publish to GitHub Releases + object storage (Cloudflare R2)
```

---

## 4. Appliance First-Boot Setup

### Phase 1 (current): headless via cloud-init NoCloud

Phase 1 ships with `cloud-init` enabled and the NoCloud datasource
active. Operators drop a CIDATA ISO with `user-data` + `meta-data`,
attach it as a secondary drive, and the appliance configures itself
on first power-on.

The `spatiumddi-firstboot.service` systemd unit runs after
`cloud-final.service`:

1. Generates `/etc/spatiumddi/.env` (POSTGRES_PASSWORD, SECRET_KEY,
   CREDENTIAL_ENCRYPTION_KEY, DNS_AGENT_KEY, DHCP_AGENT_KEY,
   BOOTSTRAP_PAIRING_CODE) on first run only — preserved across
   reboots. ``BOOTSTRAP_PAIRING_CODE`` carries the operator-supplied
   8-digit code from the installer through to the agent containers
   on Phase 6 role-split agent appliances (see §10).
2. `docker-compose pull` (first run) + `docker-compose up -d`.
3. Polls `http://127.0.0.1:8000/health/live` for up to 5 min.

Default web-UI login is `admin / admin` with `force_password_change=True`.

Recipe + examples: `appliance/cloud-init/README.md` and
`appliance/cloud-init/user-data.example`.

### Future: interactive first-boot wizard (Phase 1.x)

For operators with console access (no cloud-init datasource), an
interactive wizard served on port 80 before TLS is configured:

**Step 1: Network Configuration**
- Interface selection
- DHCP or static IP
- Hostname, DNS, gateway

**Step 2: Admin Account**
- Set superadmin username and password
- Optionally configure TOTP MFA

**Step 3: Database**
- Use built-in PostgreSQL (single-node)
- Or connect to external PostgreSQL (for HA setups)

**Step 4: Optional Services**
- Enable DHCP server on this appliance?
- Enable DNS server on this appliance?

**Step 5: TLS**
- Generate self-signed certificate
- Upload existing certificate + key
- Configure Let's Encrypt (requires public hostname)

**Step 6: Summary + Apply**

After completion, the appliance reboots into normal operation.

---

## 5. Appliance Update Mechanism

Two update paths land in 2026.05.12-1, addressing different
operator workflows:

### 5a. Container-stack release recycle (Phase 4c)

For incremental SpatiumDDI releases that don't change the host
OS. The `/appliance` Releases card lists recent GitHub releases;
operator clicks Apply, the api container writes a trigger file
the host-side `spatiumddi-release-update.path` unit watches, the
runner runs `docker-compose pull && docker-compose up -d` and
records progress in `/var/log/spatiumddi/release-update.log`.
The api container can recreate itself cleanly because the host
process owns the docker-compose command. No host reboot needed.

### 5b. Phase 8 atomic A/B image upgrades (slot upgrade)

For upgrades that change the host OS (kernel, systemd units,
host packages, partition layout). Phase 8 (issue #138) ships a
dual-slot architecture: every install carves two equal-sized
root partitions (`root_A` + `root_B`) plus a shared `/var`;
the appliance always boots one slot while the other sits idle.
Apply a new slot image, reboot, `/health/live` confirms, grub
auto-commits the swap — or auto-reverts on next reboot if the
new slot didn't come up.

**Partition layout (2026.05.12-1):**

```
p1 BIOS Boot    1 MiB    ef02
p2 ESP        512 MiB    ef00   /boot/efi (FAT32, fmask=0133,dmask=0022)
p3 root_A       4 GiB    8304   active slot (this install)
p4 root_B       4 GiB    8304   inactive slot (staged by slot-upgrade)
p5 var         balance   8300   shared across slots (/var/lib/docker,
                                /var/persist/etc, /var/home, /var/root)
```

Hard floor: 16 GiB target disk.

**/etc overlayfs:** each slot ships an image-baseline `/etc`
at `/usr/lib/etc.image/`. At boot, a systemd `etc.mount` unit
mounts an overlay over `/etc` (lower=image-baseline,
upper=`/var/persist/etc`). All operator edits — fstab, network
config, ssh host keys, user accounts — land in the upper on the
persistent `/var` partition, so they survive a slot swap
verbatim. A `spatium-etc-reconcile` boot step merges system uid
/gid/shadow entries from lower → upper so new system users
introduced by an upgrade don't clobber operator-created ones.

**Slot upgrade flow:**

1. Operator opens the **OS Image** card in `/appliance` →
   Releases. The image-URL field is pre-filled with
   `https://github.com/spatiumddi/spatiumddi/releases/latest/
   download/spatiumddi-appliance-slot-amd64.raw.xz` so a
   first-time operator just clicks Apply.
2. The api container writes a trigger file the host-side
   `spatiumddi-slot-upgrade.path` unit watches.
3. The runner (`/usr/local/bin/spatiumddi-slot-upgrade`)
   invokes `spatium-upgrade-slot apply <url>`:
   - Streams + decompresses the `.raw.xz` to the inactive
     partition via dd.
   - Verifies SHA-256 against the sidecar.
   - Re-stamps the slot filesystem UUID into `/boot/efi/grub/
     grub.cfg` (since the slot raw.xz carries its own UUID
     baked at build time, the menuentry has to be patched).
   - The active slot is never touched.
4. `spatium-upgrade-slot set-next-boot` writes
   `next_entry=slot_b` (one-shot) via grub-reboot.
5. Operator reboots. Grub honours `next_entry`, clears it,
   and falls back to `saved_entry` (the durable default) if
   anything in steps 6-8 fails before they finish.
6. New slot boots. `spatiumddi-firstboot.service` waits for
   `/health/live` to return 200.
7. On health-OK: `grub-set-default <new_slot>` commits the
   swap durably. The next reboot stays on the new slot.
8. On health-fail (kernel panic, initramfs failure, api stack
   broken): no commit happens. Next reboot reverts to the
   previous `saved_entry` automatically. Worst case is one
   wasted reboot.

**CLI access (for emergency / scripted upgrades):**

```bash
# Inspect both slots
spatium-upgrade-slot status

# Apply (URL or local file path)
sudo spatium-upgrade-slot apply \
    https://github.com/.../spatiumddi-appliance-slot-amd64.raw.xz \
    --checksum https://.../spatiumddi-appliance-slot-amd64.sha256

# Arm one-shot next-boot
sudo spatium-upgrade-slot set-next-boot

# Reboot — the swap is automatic
sudo reboot

# Emergency: durably commit without waiting for firstboot
sudo spatium-upgrade-slot commit slot_b

# Refresh /var/lib/spatiumddi/release-state/slot-versions.json
# (called automatically by spatiumddi-firstboot at every boot + at
# the end of every apply; only invoke directly when debugging the
# OS Image card's per-slot version display).
sudo spatium-upgrade-slot sync-versions
```

**Per-slot version visibility (since 2026.05.12-3).** The OS Image
card shows the installed `APPLIANCE_VERSION` under each slot label
and the GRUB boot menu labels carry the version too. Source of
truth is `/var/lib/spatiumddi/release-state/slot-versions.json`,
a `{"slot_a": "<ver>", "slot_b": "<ver>"}` map that
`spatium-upgrade-slot sync-versions` maintains. Active slot reads
its own `/etc/spatiumddi/appliance-release` directly; inactive
slot is probed via a quick read-only mount + read of the same
file. The sidecar refreshes at every boot (`spatiumddi-firstboot`
calls `sync-versions`) and at the end of every successful apply
(`spatium-upgrade-slot apply` also calls it). The grub.cfg
menuentry label is rewritten by `spatium-upgrade-slot apply` via
the `_patch_grub_cfg_slot_label` helper — idempotent across both
the original `(slot A)` form and the already-stamped
`<ver> (slot A)` form. `spatium-install` writes the initial
labels with the install-time `APPLIANCE_VERSION` so both slots
get a consistent stamp at first boot.

**Build-time slot image:** `make appliance-slot-image`
extracts the root partition from the freshly-built appliance
raw, repacks it as a 4 GiB ext4 `spatiumddi-appliance-slot-
amd64.raw.xz` with the kernel + initrd baked in + the image-
baseline fstab + a snapshotted `/usr/lib/etc.image/`. Every
GitHub release attaches the slot image + its SHA-256 sidecar
at versioned + `/latest/` URLs.

### 5c. Phase 8f fleet upgrade orchestration

The Phase 8b/8c machinery covers one appliance at a time —
operator opens that appliance's `/appliance` UI and applies a
slot upgrade. For deployments with multiple agent appliances
(role-split DNS + DHCP boxes registered against a remote control
plane), the **Fleet** tab in the control plane's `/appliance` UI
drives upgrades for all of them from a single screen.

**How it works:**

* Each registered agent (DNS-BIND / DNS-PowerDNS / DHCP) reports
  its slot state on every heartbeat — `deployment_kind`
  (appliance / docker / k8s / unknown), `installed_appliance_version`,
  `current_slot`, `durable_default`, `is_trial_boot`,
  `last_upgrade_state`. The agent introspects via bind-mounted host
  paths the appliance docker-compose drops in (`/etc/spatiumddi-host`,
  `/boot/efi-host/grub/grubenv`, `/var/lib/spatiumddi-host/
  release-state`). On docker / k8s deploys these mounts don't
  exist; slot fields stay NULL and only `deployment_kind` populates.
* Control plane persists everything to `dns_server.*` /
  `dhcp_server.*` columns added in migration `f8b1c20d3e72`.
* Operator opens the **Fleet** tab — one row per agent showing
  kind, deployment, installed version, slot (with `(trial)` suffix
  when current ≠ durable), upgrade-state pill, last-seen, and any
  pending operator-set desired version.
* Clicking **Upgrade** on an appliance row opens a release picker
  (same `applianceReleasesApi.list` source as the per-box UI).
  The picked CalVer tag is written to that agent's
  `desired_appliance_version` + `desired_slot_image_url` columns.
* The agent's next ConfigBundle long-poll picks it up via the new
  `fleet_upgrade` block on the bundle. The agent's
  `slot_state.maybe_fire_fleet_upgrade()` compares `desired` to its
  own installed version; on mismatch it writes the slot-upgrade
  trigger file — the SAME `/var/lib/spatiumddi-host/release-state/
  slot-upgrade-pending` file the per-box `/appliance` UI uses. The
  host-side `spatiumddi-slot-upgrade.path` unit then drives the
  same dd → grub-reboot → /health/live → grub-set-default flow
  documented above.
* Once the agent's next heartbeat reports `installed_appliance_version`
  matching the operator's `desired_appliance_version` (and
  `last_upgrade_state ∈ {done, NULL}`), the server-side handler
  auto-clears both `desired_*` columns. The Fleet view's pending
  chip drops on the next refresh.

**Docker / k8s rows** don't have an A/B partition to dd into, so
the Fleet table renders a **Manual upgrade…** button instead of
Upgrade. That button opens a wide modal with the same release
picker plus a pre-filled copy-paste command:

  ```
  # Docker:
  SPATIUMDDI_VERSION=2026.05.12-2 docker compose pull && \
  SPATIUMDDI_VERSION=2026.05.12-2 docker compose up -d

  # Kubernetes:
  helm upgrade spatiumddi-dns-bind9 \
    oci://ghcr.io/spatiumddi/charts/spatiumddi \
    --set image.tag=2026.05.12-2 \
    --reuse-values
  ```

One-click Copy button. The agent reports the new
`installed_appliance_version` via heartbeat once the container
restarts; the Fleet table updates within ~30 s without further
operator input.

**No SSH from control plane to agent.** Everything flows through
the existing agent → control-plane HTTP poll loop with the agent's
trusted JWT; the operator never gives the control plane SSH
credentials. Same trust model as DNS / DHCP config sync.

**Audit log.** Every Fleet write is audit-logged
(`fleet_schedule_upgrade` / `fleet_clear_upgrade` action) with the
target version + agent ID; failed upgrades surface via the
heartbeat's `last_upgrade_state = "failed"` so the Fleet UI can
render a red state pill without polling per-agent endpoints.

### Future: update channels (Phase 8d, pending)

```
UpdateConfig
  channel: enum(stable, beta, nightly)
  check_interval_hours: int
  auto_apply: bool
  notify_on_update: bool
  update_window: cron expression   -- e.g., "0 2 * * 0" = Sundays at 2am
```

---

## 6. License Summary for Appliance Shipping

| Component | License | Implications for Shipping |
|---|---|---|
| Linux Kernel | GPL v2 | Must provide kernel source (link to upstream is sufficient) |
| Alpine / Debian OS tools | MIT, GPL v2, LGPL | Source links in docs; no impact on app code |
| glibc (Debian) | LGPL v2.1 | Applications linking it need not be LGPL |
| musl libc (Alpine) | MIT | No copyleft restrictions whatsoever |
| Python | PSF License | Permissive; include copyright notice |
| FastAPI, SQLAlchemy, etc. | MIT / BSD | Include license notices in NOTICE file |
| BIND9 | MPL 2.0 | File-level copyleft; modifications to BIND source must be MPL |
| ISC Kea | MPL 2.0 | Same as BIND9 |
| React, shadcn/ui | MIT | No copyleft restrictions |
| **SpatiumDDI itself** | Apache 2.0 | Permissive; compatible with all above |

### Key Conclusions:
1. You are **not required** to open-source the SpatiumDDI application code due to GPL components — GPL applies to the GPL'd components themselves, not to user-space applications running on top.
2. You **must** include a `NOTICE` file listing all bundled open-source components and their licenses.
3. If you just ship the binary unmodified (which is the plan), you must make the source available — linking to the upstream 4. ISC Kea and BIND9 are MPL 2.0 — same situation: modifications to those files must be MPL, but unmodified shipping just requires source availability (upstream link is fine).

### Required Files in Appliance
- `NOTICE` — lists all bundled components + licenses
- `LICENSES/` directory — full text of each license (GPL2, MPL2, MIT, Apache2, PSF, LGPL2.1)
- `SOURCE_LINKS.txt` — URLs to source for all GPL/LGPL/MPL components

---

## 7. Appliance Security Hardening

Applied to both Alpine and Debian appliance images:

- Root login disabled (SSH key only, or password with MFA)
- Unnecessary packages removed (`apt autoremove` / `apk del`)
- Unused systemd services / OpenRC services disabled
- `nftables` firewall enabled with minimal ruleset (see System Admin spec)
- ASLR enabled (`/proc/sys/kernel/randomize_va_space = 2`)
- Core dumps disabled
- `/tmp` mounted as `tmpfs` (no-exec, no-suid)
- SSH: `PermitRootLogin no`, `PasswordAuthentication no` (key-only), `Protocol 2`
- All services run as non-root system users (`spatiumddi`, `kea`, `named`)
- AppArmor profiles (Debian) or seccomp profiles (Docker) for service isolation
- CIS Benchmark hardening script applied at image build time
- Image signed with GPG; checksum published

---

## 8. Environment Variables for Appliance

```bash
OPENIPAM_FIRSTBOOT=true          # Set to false after first-boot wizard completes
OPENIPAM_APPLIANCE_MODE=true     # Enables appliance-specific UI flows
OPENIPAM_UPDATE_CHANNEL=stable
OPENIPAM_LICENSE_ACCEPTED=false  # Must be true to complete first-boot
```

---

## 10. Joining an agent appliance to a control plane

Phase 6 role-split appliances (``dns-agent-bind9`` / ``dns-agent-powerdns``
/ ``dhcp-agent``) need a control-plane URL + a bootstrap secret on
first boot. The installer wizard offers two methods at the
**Bootstrap method** prompt:

### Pairing code (recommended) — issue #169

The control-plane operator generates a short-lived 8-digit code on
the web UI; the agent's installer prompts for that code instead of
the long ``DNS_AGENT_KEY`` / ``DHCP_AGENT_KEY`` hex string.

1. On the control plane, open **Appliance → Pairing**.
2. Click **New pairing code**, pick the agent kind (DNS / DHCP /
   DNS+DHCP for combined boxes), optionally pre-assign a server
   group, set the expiry (default 15 min, max 1 h), click
   **Generate code**.
3. The 8 digits appear in a large monospace box with a live
   countdown + copy button. Write them down or copy them to a
   second device.
4. On the agent appliance's installer console, pick **Pairing code**
   at the **Bootstrap method** radio, paste/type the 8 digits.
5. The installer validates ``^[0-9]{8}$`` locally (won't accept a
   typo) and writes ``BOOTSTRAP_PAIRING_CODE=<digits>`` to
   ``/etc/spatiumddi/role-config``. ``spatiumddi-firstboot`` copies
   it to ``/etc/spatiumddi/.env`` so docker-compose surfaces it in
   the agent container's environment.
6. On first contact, the agent POSTs
   ``/api/v1/appliance/pair {code, hostname}``; the control plane
   atomically marks the code claimed + returns the real bootstrap
   key. The agent caches the resolved key to
   ``/var/lib/spatium-<dns|dhcp>-agent/bootstrap.key`` (mode 0600)
   so subsequent re-registrations don't need a fresh code.
7. The console dashboard's **Pairing** row (on agent-role
   appliances) shows ``Paired ✓`` (green), ``Pairing in progress…``
   /  ``Registering…`` (yellow), or ``Pair failed — regenerate
   code on control plane`` (red).

Codes are single-use + time-bound. ``deployment_kind="both"`` returns
both DNS + DHCP bootstrap keys in one consume call — useful for a
combined BIND9 + Kea agent box (future ``agent`` install role,
issue #170).

### Bootstrap key (advanced)

For re-installs, air-gapped sites, or cases where a pairing code
expired before the installer reached its prompt. Operator pastes
the long 64-char hex key. Reveal it on the control plane via
**Settings → Security → Agent bootstrap keys** (password
re-confirm + audit row).

### Which to use

| Scenario | Recommended |
|---|---|
| First install of a new agent | Pairing code |
| Re-install / replacement hardware | Bootstrap key |
| Air-gapped site with the key saved out-of-band | Bootstrap key |
| Cloud-init / unattended installs | ``BOOTSTRAP_PAIRING_CODE`` env (cloud-init) or the key |
| Pairing code expired between generation and install | Bootstrap key, or generate a new code |
