# OS Appliance Deployment Specification

## Overview

SpatiumDDI can be shipped as a **self-contained OS appliance image** — a bootable image where the OS, all services, and the SpatiumDDI application are pre-installed and pre-configured. This allows deployment without any prior OS or container runtime setup: download, boot, configure via web UI, done.

---

## 1. Base OS Selection

### Decision: Dual-track approach

| Use Case | Base OS | Rationale |
|---|---|---|
| **Container images** (Docker/K8s) | Alpine Linux 3.x | Minimal footprint (~5MB base), musl libc, APK packages, Docker-native |
| **Bare-metal / VM appliance ISO** | Debian 12 "Bookworm" (Stable) | Broad hardware support, glibc, mature installer, LTS support, wide driver coverage |
| **VM appliance (lightweight)** | Alpine Linux 3.x | When minimal footprint matters more than hardware compatibility |

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

### Build Tool: `mkosi` (systemd project, works for both Debian and Alpine)

`mkosi` produces reproducible OS images from a declarative config. It handles:
- Base OS package installation
- Service configuration
- First-boot setup scripts
- Image format conversion

### Build Pipeline (GitHub Actions)

```
trigger: tag push (v*.*.*)
  ↓
1. Build Docker images (Alpine-based)
   - ghcr.io/spatiumddi/api:v*
   - ghcr.io/spatiumddi/worker:v*
   - ghcr.io/spatiumddi/agent:v*
  ↓
2. Build Alpine appliance (VM + ISO)
   - Run mkosi with Alpine config
   - Produces: spatiumddi-alpine-*.iso, spatiumddi-alpine-*.qcow2
  ↓
3. Build Debian appliance (VM + ISO)
   - Run mkosi with Debian config
   - Produces: spatiumddi-debian-*.iso, spatiumddi-debian-*.qcow2
  ↓
4. Convert formats
   - qcow2 → vmdk, vhd, ova
  ↓
5. Sign images (GPG)
  ↓
6. Publish to GitHub Releases + object storage
```

---

## 4. Appliance First-Boot Setup

On first boot, the appliance runs an interactive **first-boot wizard** (served via the web UI on port 80 before TLS is configured):

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

Updates are delivered as:
- **Full image**: download new ISO/qcow2, redeploy (for VMs — simplest, stateless OS)
- **In-place update**: `spatiumddi-update` CLI tool that:
  1. Downloads new application packages/containers
  2. Runs database migrations
  3. Restarts services with zero downtime (rolling restart)
  4. Rolls back automatically on health check failure

Update channel configuration:
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
